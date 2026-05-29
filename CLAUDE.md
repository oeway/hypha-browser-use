# hypha-browser-use

A Hypha-RPC-exposed MV3 Chrome extension that lets a Claude agent drive the user's **real Google Chrome stable** on macOS. The agent calls `https://hypha.aicell.io/.../browser-controller/<tool>` over HTTPS; the extension's service worker handles those calls and drives chrome.* APIs.

**New to this project? Read [`README.md`](./README.md) first, then [`ARCHITECTURE.md`](./ARCHITECTURE.md).**

## Status (snapshot)

- ✅ Real Chrome stable 148 loads our unpacked extension via CDP `Extensions.loadUnpacked`. No Web Store, no sudo, no `--load-extension`.
- ✅ The extension SW registers a Hypha service over HTTP-streaming transport.
- ✅ Basic chrome.* tools work end-to-end via curl (`ping`, `list_tabs`, `create_tab`, `create_window`).
- ⚠️ DOM tools (`get_page_info`, `read_text`, `screenshot`, `get_browser_state`, `eval_js`) currently fail on `about:blank` tabs because `chrome.scripting.executeScript` requires explicit host permission and `<all_urls>` doesn't cover `about:blank`. Real http(s) pages are needed; navigation to real URLs from a no-display Mac mini session may also need attention.
- 🔜 Need to test on a session with a real attached display to confirm network navigation works as expected.

## Load-bearing constraints — read before touching code

1. **Chrome stable 148+ refuses `--load-extension`.** Hardcoded `#if BUILDFLAG(GOOGLE_CHROME_BRANDING)` in `chrome/browser/extensions/extension_service.cc`. There is **no policy escape** (the "enterprise-managed Chrome re-enables it" idea is widely repeated but wrong — verified by reading the Chromium source). The only programmatic install path that works in 2026 is the CDP `Extensions.loadUnpacked` method over `--remote-debugging-pipe`.
2. **The CDP pipe is a "tether".** When the parent process closes the pipe, Chrome immediately exits with `Connection terminated while reading from pipe`. Therefore the Node supervisor in `scripts/install-extension.js` must stay alive for the life of the Chrome session. Use `scripts/run.sh` (which backgrounds it with nohup).
4. **`Extensions.loadUnpacked` is pipe-only.** Trying it over `--remote-debugging-port` returns `Method not available.` (verified). The pipe restriction is a Google security measure.
5. **Python's subprocess can't reliably pass FDs to Chrome on macOS.** Use Node. Node's `child_process.spawn` with `stdio: ['ignore', logFh, logFh, 'pipe', 'pipe']` is the only thing we've found that gives Chrome's macOS launcher its expected FDs 3 and 4.
6. **hypha-rpc.mjs uses `eval()` at module init.** Line 7627: `typedArrayToDtypeKeys.push(eval(arrType))`. MV3's CSP forbids `'unsafe-eval'`. We patched the bundle in place to use `globalThis[arrType]`. Original is at `extension/lib/hypha-rpc.mjs.orig`. **If you re-download the bundle, re-apply the patch** or run the patch step in `scripts/patch-hypha-rpc.sh` (TODO: extract this).
7. **hypha-rpc WebSocket transport hangs in MV3 SW.** Use the HTTP-streaming transport (`connectToServerHTTP`). Switch is in `extension/background.js` near the top (`TRANSPORT = "http"`). WebSocket is left as an option but is known broken in this environment.
8. **`chrome.scripting.executeScript` will not run on `about:blank`, `chrome://` URLs, or PDF viewer.** Tools that depend on it should only be called against tabs at http(s) URLs the user has consented to via `<all_urls>` host permission.
9. **The extension assigns each interactive element a `data-__hyphaIdx` attribute** during `get_browser_state`. Don't reuse that attribute name elsewhere.

## How to run

```bash
./scripts/run.sh                  # background-spawn supervisor + Chrome + ext
./scripts/test-rpc.sh             # smoke-test the registered tools
./scripts/stop.sh                 # graceful shutdown of both Node + Chrome
```

Environment required for `build-config.sh` (run by `run.sh`):
- `HYPHA_SERVER_URL` (default `https://hypha.aicell.io`)
- `HYPHA_WORKSPACE`
- `HYPHA_TOKEN`

## Coding conventions

- **Never commit `extension/config.js`** — it's gitignored and contains the Hypha token.
- **Never commit the agent profile** at `~/.hypha-browser-use/profile/` — it contains user cookies after first manual login.
- **Tool functions in `background.js` take a single object argument** (destructured `{tab_id, ...}`). Hypha's HTTP transport passes the JSON body as one positional argument.
- **Default to the active tab** when `tab_id` is omitted via `resolveTabId(tab_id)`.
- **Wrap chrome.* errors into structured results** when reasonable; pageEval helpers should let errors bubble so Hypha returns a 500 with the message.
- **Page eval (`chrome.scripting.executeScript`) must use `world: "MAIN"`** when the script needs access to the page's JS context (e.g., React); use `ISOLATED` when we just need DOM access.
- **Do not bundle external scripts via CDN at runtime.** MV3 extensions cannot load remote code; everything must ship in `extension/lib/`.

## Where to look for stuff

- Service worker entry point: [`extension/background.js`](./extension/background.js)
- Hypha SDK: [`extension/lib/hypha-rpc.mjs`](./extension/lib/hypha-rpc.mjs) (patched ESM bundle)
- Supervisor (the "tether" Node process): [`scripts/install-extension.js`](./scripts/install-extension.js)
- Tool registration: bottom of `extension/background.js` (`TOOL_TABLE` + `connect()`)
- Config: `extension/config.js` (generated by `scripts/build-config.sh`)

## Open work (next session)

1. **Diagnose page navigation on no-display sessions.** Tabs created with URLs end up at `about:blank` when Chrome runs without a display attached. Verify this resolves when user is at desktop; if not, investigate Chrome flags / display-context options.
2. **Allowlist for sensitive tools.** Add a per-tool risk tier (low/medium/high) and require explicit confirmation in the agent for "high" tools (e.g., `eval_js`, `download`).
3. **Audit log** to `~/auth-agent-audit.log` for every tool call (timestamp, tool, args minus secrets).
4. **Smart DOM hardening.** `get_browser_state` writes `data-__hyphaIdx` attributes; these are mutated DOM, which some sites' MutationObservers may notice. Consider a parallel WeakMap instead.
5. **LaunchAgent / launchd plist** so the supervisor auto-starts on Mac mini boot.
6. **Dedupe service registration.** Currently registers twice (once from `onInstalled`, once from top-level `ensureConnected()`). Harmless but ugly in `hypha services` listings.
