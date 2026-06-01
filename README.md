# hypha-browser-use

Remote-controllable browser automation for **real Google Chrome stable** on macOS, exposed as a Hypha RPC service. A Claude agent (or any HTTP client) drives any tab in the user's real Chrome — same profile, same Google login, same cookies, same fingerprint — via the user's existing chrome.* API surface inside an MV3 extension.

**Status (2026-05-28):** end-to-end working. Real Chrome 148 stable, extension installed via the official `Extensions.loadUnpacked` CDP API (no Web Store, no sudo, no `--load-extension`), service registered with Hypha over HTTP transport, tool calls succeed.

## What this gives you

- **40+ MCP/RPC tools** for tabs, windows, navigation, DOM (CSS selector + browser-use-style indexed elements), screenshots, cookies, downloads, JS eval, push notifications.
- **Stable HTTPS endpoint** via Hypha — `https://hypha.aicell.io/<ws>/services/<id>:browser-controller/<tool>` — callable from anywhere with a token.
- **The user's real browser environment** — no detection battle. Cloudflare, banking sites, Google login, etc. see a regular user.
- **2FA handoff via the user's real phone** — when a site requires push approval, the agent calls `notify_user` and waits while the user taps approve on their physical phone. Solves the KTH-biometric problem cleanly.

## Quickstart

```bash
# 1. Make sure HYPHA_TOKEN, HYPHA_WORKSPACE, HYPHA_SERVER_URL are exported
# 2. Bring up Chrome + extension (Node supervisor holds the CDP pipe open)
./scripts/run.sh --headless

# 3. (Optional but recommended) Bring up the embeddable login + remote-browser panel.
#    Used for credential-entry, 2FA pause, and as a quick remote view.
./browser-share/run.sh
#    Prints a public https://browser-share-xxxx.svc.hypha.aicell.io URL — embed it
#    anywhere (artifact, iframe, new tab). See ./browser-share/README.md for details.

# 4. Confirm Chrome + tools
./scripts/test-rpc.sh                  # curl smoke tests

# 5. From an agent / curl
TOKEN=$HYPHA_TOKEN
SVC="ws-user-github%7C478667/services/browser-ext-XXXX%3Abrowser-controller"
BASE="https://hypha.aicell.io/$SVC"
curl -fsS "$BASE/list_tabs?_mode=last" -H "Authorization: Bearer $TOKEN" \
     -d '{"kwargs":{}}' -H 'Content-Type: application/json'

# 6. Stop
./browser-share/stop.sh        # remote panel
./scripts/stop.sh              # Chrome + supervisor
```

`scripts/run.sh` is a backgrounding wrapper around `scripts/install-extension.js`. The Node script holds Chrome alive via the CDP pipe — `scripts/stop.sh` shuts both down cleanly.

## The login → handoff workflow

The intended user experience: **you log in once via the embedded panel; the agent takes over from there.**

```
                YOU (in the panel)                 THE AGENT (later, autonomous)
                ──────────────────                 ──────────────────────────────
 ./scripts/run.sh --headless
 ./browser-share/run.sh
 Open the printed panel URL

 1. Navigate (URL bar / tap a chip)
 2. Type email → tap "Fill email only"
 3. Tap password field → soft kbd → type
 4. Tap Sign in →
 5. MS Authenticator push → tap Approve on phone (biometric)
 6. Inbox visible — cookie stored                ──► 7. Agent calls navigate, click,
                                                       fill, read_text, etc. — same
                                                       Chrome session, no re-auth.
```

The panel and the agent use **the same 40+ MCP/RPC tools**. The panel exists for the parts that have to happen with a human in the loop: typing secrets, approving biometric 2FA, solving captchas, first-login per site.

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                       Claude agent                            │
│                                                               │
└───────────────────────┬───────────────────────────────────────┘
                        │ HTTPS POST + bearer token
                        ▼
            ┌───────────────────────┐
            │   Hypha cloud (RPC)   │
            └──────────┬────────────┘
                       │ HTTP-streaming transport
                       │ (SSE down, POST up)
                       ▼
   ┌────────────────────────────────────────────┐
   │   MV3 service worker  (background.js)      │
   │   - hypha-rpc client (HTTP transport)      │
   │   - 40+ tools wrapping chrome.* APIs       │
   └─────────────────┬──────────────────────────┘
                     │ chrome.tabs / chrome.scripting /
                     │ chrome.windows / chrome.cookies / ...
                     ▼
   ┌────────────────────────────────────────────┐
   │   Real Google Chrome stable (148+)         │
   │   loaded via Extensions.loadUnpacked       │
   │   over CDP --remote-debugging-pipe         │
   └─────────────────┬──────────────────────────┘
                     │ supervised by
                     ▼
   ┌────────────────────────────────────────────┐
   │   Node.js supervisor (install-extension.js)│
   │   - spawned by run.sh                      │
   │   - holds the pipe open → Chrome stays up  │
   │   - exits if Chrome exits and vice versa   │
   └────────────────────────────────────────────┘
```

## Why this architecture (load-bearing decisions)

| Decision | Why |
|---|---|
| Chrome MV3 extension (not Puppeteer/Playwright/CDP-direct) | All cookies/profile/login are the user's real Chrome session, by definition. No "second browser" with separate state. |
| `Extensions.loadUnpacked` over CDP `--remote-debugging-pipe` | The ONLY way to load an unpacked extension into branded Chrome stable in 2026. `--load-extension` was hardcoded off in chrome 148. The official replacement requires the pipe transport (security: pipe is parent-only, can't be hit by remote attackers). |
| Node.js supervisor (not Python) | Python's `subprocess.Popen` can pass arbitrary FDs to children, but Chrome's macOS launcher strips inherited non-stdio FDs early in init. Node's `child_process.spawn` with `stdio: ['ignore', logFh, logFh, 'pipe', 'pipe']` is the cleanest cross-platform way to give Chrome its expected FDs 3/4. |
| Hypha **HTTP transport** (not WebSocket) | The hypha-rpc WebSocket client hangs the auth handshake when called from an MV3 service worker (SW lifecycle vs. WS auth round-trip). The HTTP-streaming transport (SSE down + POST up) is well-behaved in SW context. |
| Patched `eval()` in hypha-rpc.mjs | MV3's `extension_pages` CSP forbids `'unsafe-eval'`. The hypha-rpc bundle uses `eval(typedArrayName)` at module init; we replaced it with `globalThis[name]` in `extension/lib/hypha-rpc.mjs`. Original kept as `.orig` for reference. |
| Separate profile, not user's main profile | User's main Chrome can stay running undisturbed. The agent uses its own profile. User logs into the agent's profile once when at the desktop. |

## What you do once when at the desktop

The Chrome instance we launch uses a separate `--user-data-dir`. For sites that need authentication, **log in once manually in that profile**. The cookies/history persist; subsequent agent visits are already authenticated. (See `docs/first-login.md`.)

## Files

```
hypha-browser-use/
├── README.md                       # this file
├── CLAUDE.md                       # project conventions for future Claude sessions
├── ARCHITECTURE.md                 # detailed design + call flows
├── STATUS.md                       # implementation status / known issues
├── extension/                      # the MV3 extension
│   ├── manifest.json
│   ├── background.js               # SW: 40+ tools + Hypha service registration
│   ├── popup.html / popup.js       # status UI
│   ├── config.js                   # generated, gitignored
│   ├── config.template.js          # template (committed)
│   └── lib/
│       ├── hypha-rpc.mjs           # patched MV3-CSP-safe ESM bundle
│       └── hypha-rpc.mjs.orig      # original (for diffing)
├── scripts/
│   ├── run.sh                      # background-spawn the supervisor + extension
│   ├── stop.sh                     # terminate supervisor + Chrome
│   ├── install-extension.js        # Node supervisor: loads ext + holds Chrome
│   ├── install-extension.py        # earlier Python attempt (FD-passing fails on macOS)
│   ├── build-config.sh             # writes extension/config.js from env
│   └── test-rpc.sh                 # curl smoke tests for tools
├── docs/
│   └── (TODO: tool-reference, agent-examples, troubleshooting)
└── experiments/
```

## Tool reference

Quick list — full schemas via the SKILL.md in `.claude/skills/hypha-browser/`:

- **Meta**: `ping`, `get_extension_info`, `notify_user`
- **Tabs**: `list_tabs`, `get_active_tab`, `create_tab`, `close_tab`, `activate_tab`, `duplicate_tab`
- **Windows**: `list_windows`, `create_window`, `close_window`, `focus_window`
- **Navigation**: `navigate`, `go_back`, `go_forward`, `reload`, `wait_for_load`
- **Page state**: `get_page_info`, `get_html`, `get_text`, `screenshot`
- **DOM (selectors)**: `query`, `click`, `fill`, `select_option`, `scroll`, `scroll_to`, `focus_element`, `press_key`, `read_text`, `read_attribute`, `wait_for_selector`
- **Smart DOM (indexed)**: `get_browser_state`, `click_by_index`, `input_by_index`
- **CSP-safe primitives (for strict-CSP sites like banks / KTH ADFS):**
  - `click_at {x, y}` — click at viewport coords + drop a visual marker
  - `paste_text {text}` — insert at the focused element
  - `press_key_v2 {key, modifiers}` — KeyboardEvent + value mutation for Backspace/Delete/Enter
  - `scroll_by {dx, dy}`, `scroll_to_position {x, y}`
- **Cookies / downloads / files**: `get_cookies`, `set_cookie`, `delete_cookie`, `download`, `list_downloads`, `wait_for_download`, `upload_file`
- **Other**: `eval_js` (NOT CSP-safe — see above), `set_dialog_handler`, `get_outer_html`

## Embedded login panel

See [`./browser-share/README.md`](./browser-share/README.md) for the full panel — a FastAPI app that exposes the same Chrome as an embeddable iframe with:
- Live screenshot (no-flash refresh, 1440×813 view aligned)
- Touch swipe + mouse wheel **scroll**
- **Browser toolbar** — back / forward / reload + URL bar + tab strip
- **Click + type on the screenshot** (CSP-safe; works on KTH/banks)
- **Safe credential form** below the screenshot (typed values posted via HTTPS, never logged)
- Quick chips for KTH intranet, Outlook, kth.se, SharePoint, GitHub, Google, ChatGPT
