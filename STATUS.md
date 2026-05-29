# Status — 2026-05-28 21:30

## Working end-to-end ✅

The architectural breakthrough landed. Full chain:

```
Claude → HTTPS → Hypha → SSE → MV3 SW in REAL Chrome stable 148 → chrome.* API → response
```

Verified by curl-driving the registered Hypha endpoint:

```
$ curl $BASE/ping -d '{}'
{"ok":true,"version":"0.1.0","time":"2026-05-28T20:27:35.937Z"}

$ curl $BASE/get_extension_info -d '{}'
{"name":"Hypha Browser Use","version":"0.1.0",
 "extension_id":"anighocfgfinjlhemgjgfmhnfcfnggll",
 "service_id":"browser-controller",
 "workspace":"ws-user-github|478667",
 "server_url":"https://hypha.aicell.io"}

$ curl $BASE/list_tabs -d '{}'
[{"id":..., "url":"about:blank", "title":"about:blank", "active":true, ...}]

$ curl $BASE/create_tab -d '{"url":"https://github.com"}'
{"id":..., "active":true, "status":"loading", ...}
```

## What unlocked it

1. **Extensions.loadUnpacked over CDP `--remote-debugging-pipe`.** The official replacement for the now-removed `--load-extension`. No Web Store, no sudo, no signing dance.
2. **Node.js supervisor (not Python).** Python's `subprocess.Popen` can't reliably pass FDs 3/4 to Chrome on macOS (Chrome's launcher strips them). Node's `child_process.spawn({stdio: ['ignore', logFh, logFh, 'pipe', 'pipe']})` works first try.
3. **Hypha HTTP-streaming transport (not WebSocket).** The JS WS client hangs the auth handshake in MV3 SW context. HTTP transport (POST + SSE) is well-behaved.
4. **Patched `eval()` out of hypha-rpc.mjs.** MV3 CSP forbids `'unsafe-eval'`. The bundle uses `eval(typedArrayName)` at module init; replaced with `globalThis[name]`.

## Known remaining issues

### 1. DOM tools fail on `about:blank` tabs (expected, easy fix)

`chrome.scripting.executeScript` requires explicit host permission. `<all_urls>` doesn't cover `about:blank`/`chrome://` URLs.

```
$ curl $BASE/get_page_info -d '{"tab_id":117}'
{"success":false,"detail":"Error: Cannot access contents of url \"about:blank\".
 Extension manifest must request permission to access this host."}
```

**Fix:** the agent should always navigate to a real http(s) URL before calling DOM tools. Document this in tool descriptions / agent guidelines.

### 2. URL navigation appears to fail without an attached display (needs verification)

When the supervisor + Chrome are launched from a no-display session (e.g., the user is away from the desktop), `chrome.tabs.create({url: "https://github.com"})` returns a tab with `status: "loading"` initially, then settles to `url: "about:blank"` instead of fetching the requested URL. `chrome://newtab` does load (no network needed).

This may be because the Mac mini's Chrome can't render external content without a connected display / GUI session. **Should be tested when the user is back at the desktop.** If it still fails, investigate:
- `--start-maximized` / `--window-size` flags
- The `WindowServer` connection state from a non-GUI shell session
- `--headless=new` mode (but verify it doesn't break Extensions.loadUnpacked)

### 3. Service registers twice

The SW connects in both `chrome.runtime.onInstalled` and the top-level `ensureConnected()` call. Two `browser-ext-XXXX:browser-controller` entries appear in `hypha services`. Harmless (both work) but ugly. Fix: a "one-flight" guard or move all init to a single trigger.

### 4. Extension doesn't persist across Chrome restarts without the supervisor

`Extensions.loadUnpacked` is ephemeral — when Chrome exits, the extension is gone. The Node supervisor must stay alive for the duration. This is by design (pipe security) and is the right model: `run.sh` launches the supervisor, supervisor launches Chrome, both live together.

For autostart on Mac mini boot, set up a LaunchAgent (TODO Phase 6).

## Files

```
hypha-browser-use/
├── README.md                  ✓
├── CLAUDE.md                  ✓
├── ARCHITECTURE.md            ✓
├── STATUS.md                  ✓ this file
├── .gitignore                 ✓
├── extension/
│   ├── manifest.json          ✓
│   ├── background.js          ✓ (40+ tools)
│   ├── popup.html / .js       ✓
│   ├── config.template.js     ✓
│   ├── config.js              ✓ (gitignored)
│   └── lib/
│       ├── hypha-rpc.mjs      ✓ (patched)
│       └── hypha-rpc.mjs.orig ✓
├── scripts/
│   ├── run.sh                 ✓ background launcher
│   ├── stop.sh                ✓ shutdown both
│   ├── install-extension.js   ✓ Node supervisor (the magic)
│   ├── install-extension.py   - earlier attempt; left for ref
│   ├── build-config.sh        ✓
│   └── test-rpc.sh            ✓
├── docs/                      empty (TODO)
└── experiments/               empty
```

## What the next session should do

In priority order:
1. **Verify the navigation issue resolves with a display attached.** Once the user is back at desktop, restart the supervisor and try `create_tab({url: "https://example.com"})`. If still broken → investigate Chrome+display interaction; otherwise → move to step 2.
2. **First manual login per target site** in the agent's profile (`~/.hypha-browser-use/profile/`). Visit github.com, login, MFA, etc. This builds the cookie/history age that any future agent automation depends on.
3. **End-to-end demo**: a real script that uses the tools to log into one TOTP-protected site, with the OTP fetched from the user's phone via existing 2FA app (out-of-band) — wired into the `notify_user` + wait-loop pattern.
4. **Audit log** + **per-tool risk tiers** before exposing to a real Claude session.
5. **LaunchAgent plist** so the supervisor auto-restarts on Mac mini reboot.
