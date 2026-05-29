# Key learnings

Hard-won knowledge from building hypha-browser-use. Written as a single document so others don't have to discover these themselves.

Last updated 2026-05-28 for Chrome 148.

---

## 1. `--load-extension` is dead in branded Chrome stable

Verified by reading the current Chromium source:

```cpp
// chrome/browser/extensions/extension_service.cc, ~L418
if (switch_name == switches::kLoadExtension) {
#if BUILDFLAG(GOOGLE_CHROME_BRANDING) && !BUILDFLAG(IS_CHROMEOS)
  LOG(WARNING) << "--load-extension is not allowed in Google Chrome, ignoring.";
  return;
```

It's a **compile-time `#if`** — no runtime escape hatch:

- **No `IsManaged()` check.** The folklore "set any enterprise policy and `--load-extension` comes back" is wrong. Verified by trying every reasonable per-user policy path on macOS; none re-enable it.
- **No feature flag.** No `FeatureList::IsEnabled` gate. There was once `DisableLoadExtensionCommandLineSwitch`; it's gone.
- **`--disable-features=DisableLoadExtensionCommandLineSwitch` does nothing** in Chrome 148.
- The kill is in branded builds only. Chromium, Chrome for Testing, and Chrome Beta/Dev/Canary still accept the flag. **But:** Chrome for Testing blocks Google login (different signing/origin); Beta/Dev/Canary do support it but are separate browsers.

**Practical implication:** If you want unpacked-extension loading into the user's real Chrome, you cannot do it through CLI flags. You need a different mechanism.

## 2. The replacement: `Extensions.loadUnpacked` via CDP

Google built an official replacement and quietly added it to the CDP `Extensions` domain:

```
chrome --enable-unsafe-extension-debugging \
       --remote-debugging-pipe \
       --user-data-dir=...
```

Then send over the CDP pipe:

```json
{"id":1,"method":"Extensions.loadUnpacked","params":{"path":"/abs/path/to/extension"}}
```

Returns the assigned extension id. The extension is loaded into that Chrome session and is fully functional.

**Three non-obvious restrictions:**

1. **`--enable-unsafe-extension-debugging` is required.** Without it, the method returns `{"error":{"message":"Method not available."}}`.
2. **The transport MUST be `--remote-debugging-pipe`**, NOT `--remote-debugging-port`. Pipe is a parent-process-only channel (FDs 3 read, 4 write); port is a TCP socket reachable from anywhere on localhost. Google made `Extensions.loadUnpacked` pipe-only as a security measure: a malicious remote attacker shouldn't be able to install extensions just by connecting to a debug port.
3. **`Extensions.loadUnpacked` is ephemeral.** When the pipe closes (i.e., parent process exits), Chrome exits within ~1s with `Connection terminated while reading from pipe`. The loaded extension does NOT persist in the profile across Chrome restarts. **You need a long-lived supervisor process to keep the pipe open.**

Sources:
- [CDP Extensions domain reference](https://chromedevtools.github.io/devtools-protocol/tot/Extensions/)
- [RFC: Removing --load-extension](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/aEHdhDZ-V0E/m/UWP4-k32AgAJ)

## 3. Python can't pass FDs 3/4 to Chrome on macOS (Node can)

**Symptom:**
```
[ERROR:chrome/app/chrome_main_delegate.cc:1096] Remote debugging pipe file descriptors are not open.
```

What we verified:

- `subprocess.Popen(..., preexec_fn=lambda: (os.dup2(r_child, 3), os.dup2(w_child, 4)), close_fds=False)` is the standard technique. It DOES work for ordinary children — we tested with `/usr/bin/python3` as the child and `os.fstat(3)` / `os.fstat(4)` showed pipe FDs (mode `0o10660`).
- It does NOT work for Chrome on macOS. Chrome's main process gets EBADF on FDs 3/4 even though our preexec ran.
- Most likely cause: Chrome's **macOS Hardened Runtime + Library Validation** sanitizes the FD table during early init. The relevant entitlements are baked into Chrome's signed binary; nothing on the parent side can override them.

**The fix:** use Node.js. `child_process.spawn` with explicit stdio gives Chrome the FDs cleanly:

```js
const proc = spawn(chrome, args, {
  stdio: ["ignore", logFh, logFh, "pipe", "pipe"],
});
const writeToChrome = proc.stdio[3];  // we write -> Chrome reads as FD 3
const readFromChrome = proc.stdio[4]; // Chrome writes as FD 4 -> we read
```

libuv's `uv_spawn` underneath does whatever Puppeteer / chrome-launcher / Playwright do, and Chrome is happy with it.

## 4. The "pipe tether" lifecycle

Once Chrome is running with `--remote-debugging-pipe`, Chrome treats the pipe as a lifeline. The instant the parent closes its end (`writePipe.end()`), Chrome exits.

**Consequence:** the install script CANNOT exit after `Extensions.loadUnpacked`. It must stay alive as a supervisor for the entire Chrome session.

Our `scripts/install-extension.js` blocks on `await new Promise(() => {})` after install and registers `proc.on("exit", ...)` so supervisor and Chrome share a lifecycle.

`scripts/run.sh` backgrounds the supervisor via `nohup` so the supervising shell can exit.

## 5. JSON-RPC NUL-delimited framing on the pipe

The pipe transport is one message per NUL-byte (`\x00`). Each message is JSON, no length prefix:

```
{"id":1,"method":"Browser.getVersion","params":{}}\x00
```

Both directions. Buffer until you see `\x00`, then `JSON.parse` the chunk:

```js
let buf = Buffer.alloc(0);
readPipe.on("data", chunk => {
  buf = Buffer.concat([buf, chunk]);
  let idx;
  while ((idx = buf.indexOf(0)) >= 0) {
    const msg = JSON.parse(buf.slice(0, idx).toString());
    buf = buf.slice(idx + 1);
    dispatch(msg);
  }
});
```

## 6. MV3 CSP forbids `'unsafe-eval'`. hypha-rpc uses `eval()` at module init.

The hypha-rpc webpack bundle has three uses of `eval()` / `new Function()`:

| Line  | What it does                                                  | Triggers when |
|-------|---------------------------------------------------------------|---------------|
| 7627  | `typedArrayToDtypeKeys.push(eval(arrType))`                   | Module init time (unconditionally) |
| 7524  | `await new Function("url", "return import(url)")(...)`        | Only when `loadRequirements` is called |
| 11795 | `eval(script.content)`                                        | Plugin script execution path |

Only line 7627 fires automatically. MV3's default `extension_pages` CSP is:

```
script-src 'self' 'wasm-unsafe-eval' 'inline-speculation-rules';
object-src 'self';
```

There is NO way to add `'unsafe-eval'` (Chrome strips it). So `eval()` at module init throws `EvalError` and the whole SW fails silently to load.

**Patch:** replace `eval(arrType)` with `globalThis[arrType]` — does the exact same thing (looks up the named TypedArray class) but doesn't go through eval. We also patched the other two defensively. See `extension/lib/hypha-rpc.mjs` vs `.mjs.orig` for the diff.

The patch is idempotent and survives bundle minification (the strings are unique). Re-applying when hypha-rpc is updated takes 3 sed commands.

## 7. hypha-rpc WebSocket transport hangs in MV3 SW

When you call `hyphaWebsocketClient.connectToServer({...})` from an MV3 service worker, the WS opens (`Creating a new websocket connection to wss://...` is logged), then the auth handshake hangs. The status stays at `state: connecting`, never advances.

Direct test: Python `hypha_rpc.connect_to_server({...})` from the same machine with the same token registers a service in ~500ms. So it's not server-side, not token, not workspace.

Best theory: MV3 SW lifecycle (30s idle termination) interacts badly with the WS auth round-trip. Even with `chrome.alarms` keepalive, the SW restart/wake pattern is hostile to a connection-oriented protocol that holds open a stateful auth handshake.

**Fix: use `connectToServerHTTP` instead.** Same API surface (`registerService`, `getService`, etc.), but uses POST for uplink and Server-Sent Events for downlink. Both fetch+SSE work cleanly in SW context. Registration succeeded on the first attempt and has been stable.

## 8. Per-tool host permissions and `about:blank`

`chrome.scripting.executeScript` checks the URL of the target tab against the extension's host permissions. `<all_urls>` covers `http://*`, `https://*`, `file://*`, `ftp://*` — but NOT `about:blank`, `chrome://*`, or the new-tab page. Calling it against any of those returns:

```
Cannot access contents of url "about:blank".
Extension manifest must request permission to access this host.
```

Agents driving the extension should always navigate to a real http(s) URL before calling DOM-level tools. The basic chrome.* tools (`list_tabs`, `create_tab`, `navigate`) work on any tab.

## 9. macOS-specific: launch quirks

- **LaunchServices forwarding**: when launching `.app` bundles on macOS, the system may forward the launch to an existing instance via Apple Events. With `--user-data-dir=<unique>` Chrome should spawn a fresh instance, but only if you call the binary directly (`Contents/MacOS/Google Chrome`), not via `open`. (`open -na` forces new instance but loses FD inheritance.)
- **Hardened Runtime + sandbox** strips arbitrary inherited FDs. Don't expect to pass anything but FDs 0/1/2 and the ones the binary explicitly accepts (3/4 for `--remote-debugging-pipe`).
- **`/Library/Managed Preferences/com.google.Chrome.plist`** requires root to write, even from an "admin group" user, because `/Library` is root-owned on modern macOS.
- **`defaults write com.google.Chrome <key> <value>`** writes to `~/Library/Preferences/com.google.Chrome.plist` which Chrome reads as "user preferences", NOT as "managed policy". So setting `ExtensionInstallSources` etc. via `defaults write` does not turn Chrome into a managed instance.
- **Tabs may not navigate to network URLs without an attached display**. Observed on a Mac mini accessed remotely with the GUI session inactive: `chrome.tabs.create({url: "https://example.com"})` settles at `about:blank`, while `chrome://newtab` (no network) works fine. Probably WindowServer/GPU context dependency. Untested whether `--headless=new` would behave differently with `Extensions.loadUnpacked`.

## 10. macOS app entitlements are not relevant for FD inheritance, but…

The Mac's Hardened Runtime IS relevant. You CAN pass FDs 3/4 if you spawn Chrome via Node/libuv. Don't waste time looking up Apple entitlements — there's no entitlement to add to Chrome.

## 11. Things that look like they should work but don't (in 2026)

| Idea | Why it doesn't |
|---|---|
| Set `ExtensionInstallSources` via `defaults write` to re-enable `--load-extension` | User defaults are not managed policy. |
| Install a `.mobileconfig` profile with Chrome policies to flip the managed bit | `.mobileconfig` install requires admin; macOS UI prompt; even where installable, payload goes to `/Library/Managed Preferences/` which needs admin to write. And Chrome `--load-extension` doesn't check `IsManaged()` at runtime anyway. |
| Pack as CRX + use `~/Library/Application Support/Google/Chrome/External Extensions/<id>.json` | macOS branded Chrome requires the external `update_url` to be `clients2.google.com/service/update2/crx` (i.e., the Web Store). Unsigned/local CRX is rejected with `CRX_REQUIRED_PROOF_MISSING`. |
| Modify `~/.../Chrome/Default/Preferences` `extensions.settings` directly with `location: 4` (unpacked) | The adjacent `Secure Preferences` file holds HMACs keyed off a machine-bound seed; Chrome detects modifications and wipes the entries at next launch. Last reliable reports of this working: ~2019. |
| Drive `chrome://extensions` "Load unpacked" via CDP automation | Native file picker, not scriptable through CDP. (`Extensions.loadUnpacked` is the supported automation route — use it.) |
| Use `chrome-extension://<id>/_generated_background_page.html` to invoke chrome.management APIs from outside | Restricted internal APIs; only accessible to extensions with `developerPrivate` permission, which is itself component-only. |
| Run Chrome for Testing instead — it's the same Chrome | Same code but different signing/origin; Google's account login detects it and refuses sign-in. Fine for headless test automation, not fine for personal-productivity browsing. |

## 12. The architectural shape that emerged

After working through everything above, the design that works is:

```
[script] ──spawn──► [Node supervisor (long-lived)] ──pipe──► [Chrome (real stable)]
                                                                    │
                                                                    └── runs MV3 SW
                                                                         │
                                                            HTTP-streaming up/down
                                                                         │
                                                                         ▼
                                                                  [Hypha cloud]
                                                                         │
                                                                         ▼
                                                                   [agent client]
```

Three processes; clean lifecycle; supervisor and Chrome live together via the pipe; the SW takes care of all chrome.* calls; the cloud is only a relay (no logic there); the agent is just another HTTP client.

The whole thing avoids:
- Browser fingerprint detection (it's the user's real Chrome)
- Web Store publishing (zero touch)
- sudo / admin (zero touch)
- Persistent extension hacks (extension is reinstalled from the source dir each session)

## 13. Reusable artifacts we built

If you want to lift any of this for your own project:

- **`scripts/install-extension.js`** — minimal Node CDP-pipe-over-stdio client + extension installer. ~150 lines. The interesting bits are stdio config + NUL-frame buffering. Can be adapted to drive any CDP command, not just Extensions.loadUnpacked.
- **`scripts/run.sh` + `scripts/stop.sh`** — nohup-style supervisor pattern that ties Chrome lifecycle to a Node process.
- **`extension/background.js`** — a complete chrome.* → Hypha bridge with 40+ tools. Cleanly separable into "tool functions" + "transport wiring."
- **The hypha-rpc.mjs patch** — `sed -i 's/typedArrayToDtypeKeys\.push(eval(arrType))/typedArrayToDtypeKeys.push(globalThis[arrType])/'` makes the bundle MV3-CSP-safe.

## 14. Things to verify next

- [ ] Network navigation works when the Mac mini has an active GUI session (display attached).
- [ ] `Extensions.loadUnpacked` + `--headless=new` together (untested combination).
- [ ] LaunchAgent plist for auto-start on reboot.
- [ ] The duplicate SW connection on startup (harmless but cosmetic — two services register).
- [ ] Behavior across Chrome major-version upgrades (148 → 149 → ...).
