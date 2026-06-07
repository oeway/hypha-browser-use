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

- [ ] Network navigation works when the Mac mini has an active GUI session (display attached). — **CONFIRMED OK with `--headless=new`**: extension loads, full page rendering works, screenshots come through at 1440×813.
- [x] `Extensions.loadUnpacked` + `--headless=new` together. **Verified working** — adds `--headless=new --window-size=1440,900` to the existing pipe + unsafe-extension-debugging combo. No display needed.
- [ ] LaunchAgent plist for auto-start on reboot.
- [ ] The duplicate SW connection on startup (harmless but cosmetic — two services register).
- [ ] Behavior across Chrome major-version upgrades (148 → 149 → ...).

---

# Part 2: Real-world login flows (KTH ADFS, MS 365, banks) — learnings

Added 2026-05-29 after building the embedded login panel + orchestrator.

## 15. Many enterprise login flows are multi-page

The flow is rarely "fill email + password + submit". You'll see:

```
Page 1 (login.microsoftonline.com)        — email only ("Enter email")
   → submit → routes to your tenant
Page 2 (login.ug.kth.se/adfs/...)         — password only,
                                            but with a HIDDEN username
                                            field that ALSO must be filled
   → submit → ADFS validates both
Page 3 (back on login.microsoftonline.com) — 2FA number-match prompt:
                                            "Approve sign-in with 27"
   → user taps "27" on their phone
   → page advances on its own
Page 4 (target — Outlook, SharePoint, etc.) — logged in
```

A single-shot "fill both fields and click submit" fails on step 2 because the email field doesn't exist on the password page — and the password page has a HIDDEN username field that must also be filled.

**The orchestrator pattern:**

```python
async def login_step():
    state = await get_browser_state()
    if has_2fa_digit_visible(state):  return "show number to user"
    if has_password_field(state):     fill_username_AND_password(); submit()
    if has_email_field(state):        fill_email(); submit()
    if past_login_host(state):        return "complete"
```

Client polls in a loop. Each call advances one step.

## 16. ADFS-style pages have a hidden username field on step 2

KTH (login.ug.kth.se) reuses ONE template for both username and password steps:

```html
<form id="loginForm">
  <input id="userNameInput" name="UserName" type="email" ...>    <!-- step 1: visible. step 2: display:none -->
  <input id="passwordInput" name="Password" type="password" ...> <!-- step 1: not in DOM. step 2: visible -->
  <span id="nextButton"   ...>Next</span>                        <!-- step 1 only -->
  <span id="submitButton" ...>Sign in</span>                     <!-- step 2 only -->
</form>
```

On step 2, the username container is `display:none` but the **input still exists in the DOM and is required by the submit handler**:

```js
function submitLoginRequest() {                          // wired to span#submitButton.onclick
    var userName = document.getElementById('userNameInput');
    if (!userName.value || !userName.value.match('[@\\\\]')) {
        u.setError(userName, e.userNameFormatError);    // ← "Enter your user ID in the format 'domain\user' or 'user@domain'"
        return false;
    }
    ...
    document.forms['loginForm'].submit();
}
```

If the hidden username is empty when this runs, the error displays — which from the user's POV looks like a "format error" but is actually "field is empty".

**Fix:** the orchestrator must fill the hidden username via CSS selector (`#userNameInput`, `input[name=UserName|loginfmt|Username]`) on the password step, BEFORE clicking submit.

## 17. `get_browser_state` was silently skipping hidden form fields

The original extension code:

```js
const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    ...
};
if (!isVisible(el)) continue;
```

This skipped any element with zero bounding rect — including KTH ADFS's hidden username input. The orchestrator never even saw the field existed.

**Fix:** keep form-fillable inputs (type=text/email/password/tel/number/search/url, plus `<textarea>` and `<select>`) even when CSS-hidden. Non-form invisible elements are still excluded.

```js
const isFormFillable = (el) => {
    const tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    if (tag !== "INPUT") return false;
    const t = (el.type || "").toLowerCase();
    return ["text","email","password","tel","number","search","url",""].includes(t);
};
const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return isFormFillable(el);
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0)
        return isFormFillable(el);
    return true;
};
```

## 18. `Element.getAttribute('value')` and `Element.value` are different

The DOM `.value` property is what the form actually posts; the HTML `value` attribute reflects only the initial markup or values set via `setAttribute()`.

So tools that "verify a field is filled" using `read_attribute`/`getAttribute('value')` will return empty even when the field was successfully filled via the React-friendly setter — because the setter sets the DOM property only.

**Always check via `.value`** when verifying field state. Either through `eval_js`:

```js
document.querySelector('#userNameInput').value
```

or via a tool that explicitly reads `.value`:

```js
return await pageEval(id, (sel) => document.querySelector(sel)?.value, [selector]);
```

## 19. Multiple separate chrome.scripting.executeScript calls can lose state

Each tool call (e.g., `fill`, `click_by_index`, `input_by_index`, `click_by_index`) is a separate `chrome.scripting.executeScript({func, args})` invocation. Each runs in its own micro-task and returns. The page can re-render between them.

Specifically observed on KTH ADFS combined-page:
1. POST `/fill #userNameInput → "user@domain"` → DOM `.value` set, dispatch input/change → ok
2. (Chrome scripting context disposes, page processes events, possibly re-renders) ← 100-200ms gap
3. POST `/fill #passwordInput → "..."` → DOM `.value` for password set, but **username has been reset by the page's own state-rehydration code**
4. POST `/click_by_index` on submit → page's `Login.submitLoginRequest()` runs → reads `userName.value` → empty → shows format error

**Fix:** when filling a form whose submit handler reads multiple field values, do username + password + submit in **one atomic `eval_js`** so the page can't dispose state between operations:

```js
// One eval_js call that does ALL of it:
(() => {
  const userField = document.querySelector('#userNameInput, input[name=UserName]');
  const pwField   = document.querySelector('input[type=password]');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(userField, EMAIL);
  userField.setAttribute('value', EMAIL);          // belt-and-suspenders
  userField.dispatchEvent(new Event('input', {bubbles:true}));
  userField.dispatchEvent(new Event('change', {bubbles:true}));
  setter.call(pwField, PASSWORD);
  pwField.dispatchEvent(new Event('input', {bubbles:true}));
  pwField.dispatchEvent(new Event('change', {bubbles:true}));
  if (window.Login && window.Login.submitLoginRequest)  // prefer page's own submit
    return window.Login.submitLoginRequest();
  document.querySelector('#submitButton').click();
})()
```

This costs one round-trip and the page sees a complete, consistent form state at submit time.

## 20. Prefer the page's OWN submit function over `button.click()`

ADFS's submit button is `<span id="submitButton" onclick="return Login.submitLoginRequest();">`. Simulating `.click()` on the span SHOULD work, but:

- The span has CSS that's not a real button — some pages set `pointer-events: none` based on validation state
- Calling `submitButton.click()` runs onclick, which calls `Login.submitLoginRequest()` — but if our synthetic click event doesn't propagate exactly like a user one, the onclick might not fire
- Direct `Login.submitLoginRequest()` invocation is faster and runs the exact same validation the user gets

When the page exposes a global submit function (`Login.submitLoginRequest`, `App.signIn`, etc.), call it directly:

```js
if (window.Login?.submitLoginRequest) return window.Login.submitLoginRequest();
```

Fall back to `button.click()` only if no such function exists.

## 21. 2FA number-match detection heuristic

The MS Authenticator number-match prompt renders the 2-digit number in a big element. The exact selector varies (`#idRichContext_DisplaySign`, `.display-sign-in-number`, no class at all). A robust detector:

```python
def detect_2fa_number(elements):
    candidates = []
    for e in elements:
        text = (e.get("text") or "").strip()
        if not (text.isdigit() and 1 <= len(text) <= 3): continue
        bbox = e.get("bbox") or {}
        w, h = bbox.get("w", 0), bbox.get("h", 0)
        if w < 20 or h < 20: continue
        # Prefer elements with hinting attributes
        attrs = e.get("attrs", {})
        cls_id = (attrs.get("id","") + " " + attrs.get("class","")).lower()
        score = w * h
        if "sign" in cls_id: score *= 10
        if attrs.get("aria-live"): score *= 5
        candidates.append((score, text, e))
    return candidates[0][1] if (candidates := sorted(candidates, reverse=True)) else None
```

Combined with URL check (`microsoftonline.com` in URL, or "approve" in title), this catches the prompt reliably without false positives from "2" appearing in dates or counters.

When detected, the orchestrator should:
1. Return the number to the client
2. Client shows a big overlay with the number
3. Fire a native notification (`chrome.notifications` or `osascript -e 'display notification'`)
4. Continue polling — the page advances on its own when the user taps approve on their phone

## 22. Mobile-specific browser-control gotchas (iPad / Safari iOS)

Discovered while building the embeddable login panel.

| Issue | Cause | Fix |
|---|---|---|
| Soft keyboard doesn't appear when tapping the screenshot | `focus()` only triggers keyboard if called **synchronously** inside the user-gesture event handler | Call `kbd.focus()` directly in the touch handler, BEFORE any `await` |
| iOS shows "Paste / Look up" menu on screenshot tap | `<img>` long-press triggers context menu | `-webkit-touch-callout: none; -webkit-user-select: none;` on the img |
| Tap gestures suppressed as "swipe" | Setting a swipe flag on any touchmove with >10px drift | Only set the flag in touchend when distance >25px; clear after 200ms |
| Coordinates misaligned | Hardcoded `VIEW = 1440 × 900` doesn't match actual viewport (1440 × 813 with Chrome chrome) | Use `shot.naturalWidth` / `naturalHeight` — adapts automatically |
| Screen flashes "broken image ?" between refreshes | Setting `<img>.src` unloads the current image during fetch | Preload the new image to memory (`new Image()`), swap `<img>.src` only after `onload` fires |
| iOS auto-zooms when focusing input | Default font-size < 16px triggers Safari's "smart" zoom | `font-size: 16px` on the focusable input |
| Keyboard returns from event but value is wrong | `keydown` on mobile doesn't fire for every char (composition / autocorrect) | Listen to `input` events on the hidden field, not just `keydown`. Backspace fires `inputType: deleteContentBackward` |

The hidden-input technique for mobile keyboards:

```html
<div class="shotbox">
  <img id="shot" src="/screen.jpg">
  <input class="kbd" id="kbd" type="text" inputmode="text"
         autocomplete="off" autocorrect="off" autocapitalize="off"
         spellcheck="false">  <!-- overlaid; transparent text + caret -->
</div>
```

```css
.kbd {
  position: absolute; inset: 0; width: 100%; height: 100%;
  background: transparent; color: transparent; caret-color: transparent;
  font-size: 16px;  /* prevent iOS zoom */
  -webkit-appearance: none;
}
```

```js
// Tapping the shot synchronously focuses the kbd — keyboard appears.
shot.addEventListener('touchend', (e) => {
    // ... compute coordinates, forward click ...
    kbd.value = '';
    kbd.focus({ preventScroll: true });   // ← MUST be synchronous, in this handler
});
// Keystrokes captured via input event, batched + sent to remote.
kbd.addEventListener('input', () => {
    const text = kbd.value; kbd.value = '';
    if (text) post('/api/paste', { text });
});
```

## 23. iframe sandbox + CORS

When embedded in an iframe sandbox (Svamp artifact, etc.), the iframe has `null` origin. `fetch('/state')` to the same hostname is treated as cross-origin → CORS rules apply.

Without CORS headers, `Failed to fetch` appears with no other clue (no preflight 403, just an inability to load).

```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # acceptable since the unguessable tunnel hostname
    allow_methods=["*"],   # already gates access
    allow_headers=["*"],
    allow_credentials=False,
)
```

## 24. Tunnel URLs are stable but the tunnel can briefly 404 during restart

`svamp service expose` keeps the same URL prefix across restarts (it hashes the service name into the subdomain), but the actual frpc forwarder briefly disconnects when the local process restarts. During that window (~2s), requests get an `frp` 404 page.

**Implication for embedded iframes:** the artifact may show "page not found" right after a server restart. The fix is to wait 2-3 seconds and refresh. Don't change the embed URL.

## 25. Architectural shape, take 2

After the panel work, the full stack is:

```
   YOU (browser in chat panel or new tab)
   ──────────────────────────────────────
              │ HTTPS + CORS
              ▼
   browser-share FastAPI  (login UI + click_at/paste_text passthrough +
                           multi-step login_step orchestrator)
              │ kwargs-wrapped JSON over Hypha RPC
              ▼
   Hypha cloud relay
              │ HTTP-streaming transport
              ▼
   MV3 service worker  (40+ CSP-safe tools)
              │ chrome.* API
              ▼
   Real Chrome stable on the Mac mini
              │
              └─ same Chrome session continues to be driven by the agent
                 once you close the panel (cookies persist in profile)
```

The panel handles the parts that NEED a human (passwords, biometric 2FA, CAPTCHAs, first-login per site). The agent handles everything else autonomously, using the same Chrome session.
