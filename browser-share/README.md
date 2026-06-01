# browser-share — embed-anywhere remote browser

A tiny FastAPI app that exposes the hypha-browser-use Chrome as an **embeddable login interface + full remote browser**:

- Live screenshot (auto-refresh, no-flash)
- **Tap to click + soft-keyboard typing** on mobile (iOS/iPad), works through CSP-strict pages (KTH ADFS, banking, etc.)
- **Real browser toolbar** — Back / Forward / Reload + URL bar + tab strip
- **Touch swipe to scroll**, mouse wheel scroll, scroll-to-position API
- **Safe credential form** — type email/password into a real form, fields are POSTed via HTTPS to the local server (never typed in chat, never logged)
- **Quick-nav chips** for KTH intranet, Outlook, kth.se, SharePoint, GitHub, Google, etc.

The user logs in through the embedded panel; agents continue with the same Chrome session via the hypha-browser-use RPC tools.

## How the architecture splits responsibilities

```
┌───────────────────────────────────────────────────────────────────────┐
│                        Embedded login panel                           │
│              (iframe in chat / standalone in a new tab)               │
│                                                                       │
│   /              login.html  (HTML/CSS/JS UI)                         │
│   /screen.jpg    current screenshot (cache-busted)                    │
│   /state         {url, title, has_email/password/submit, ...}         │
│   /api/click_at  {x, y}        → CSP-safe click_at extension tool     │
│   /api/paste     {text}        → CSP-safe paste_text                  │
│   /api/key       {key, mods}   → press_key_v2 (Backspace etc)         │
│   /api/scroll_by {dx, dy}      → scroll_by                            │
│   /api/back  /api/forward  /api/reload                                │
│   /api/navigate  {url}                                                │
│   /api/fill      {email, password, submit}  ← safe-paste credentials  │
│   /api/tabs  /api/new_tab  /api/close_tab  /api/activate_tab          │
└───────────────────────────┬───────────────────────────────────────────┘
                            │ kwargs-wrapped JSON over HTTPS
                            ▼
              ┌────────────────────────────────┐
              │   Hypha cloud (RPC relay)      │
              └────────────────┬───────────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │  Real Chrome stable + our      │
              │  MV3 extension (40+ tools)     │
              └────────────────────────────────┘
```

## Why the embedded panel is needed at all

The agent (Claude / Claude Code) has full access to the same 40+ tools and can drive the browser autonomously. The panel exists for **the parts an agent cannot do** without compromising security:

1. **Typing your password.** An agent reading credentials in its reasoning trace is a leak by default. The panel keeps secrets in a textarea on a server-side endpoint that never logs them.
2. **Approving 2FA via biometric.** Microsoft Authenticator push, banking apps — these require Face ID/Touch ID on your physical phone, by design.
3. **Solving captchas / Cloudflare challenges.** Same as above: requires you, the human, momentarily.
4. **First-login per site.** Cookies don't exist yet, the agent has nothing to work with. Once you've signed in once via the panel, cookies persist; subsequent agent runs skip the login.

Once you finish step 1-4, **close the panel and let the agent take over**. It uses exactly the same Chrome session and cookies.

## The login → handoff flow

```
                    YOU (in the panel)                    THE AGENT (later, autonomous)
                    ──────────────────                    ──────────────────────────────

1. ./scripts/run.sh --headless        ┐
   ./browser-share/run.sh             ├─ once at session start
   open the panel URL                  ┘

2. Navigate to https://intra.kth.se  (panel: type URL or tap chip)

3. Type email → "Fill email only"
   (or tap email field on screen → soft kbd → type)

4. MS Authenticator push → approve on phone

5. Inbox loaded — cookie ready                    ──► 6. Agent calls navigate, click, fill, etc.
                                                      using the same chrome session.

7. Close the panel (optional). Agent continues.
```

The agent's tools and the panel's tools are the **same tools**. The panel is a thin wrapper that adds (a) a UI and (b) a credential-paste endpoint that doesn't bleed into the agent's reasoning context.

## Quick reference

| Action | Panel UI | Agent equivalent (via `$SERVICE_URL/<tool>?_mode=last`) |
|---|---|---|
| Navigate | URL bar + `Go` / chip / `navigate` btn | `POST /navigate {"kwargs":{"url":"..."}}` |
| Back / Forward / Reload | toolbar buttons | `go_back`, `go_forward`, `reload` |
| Click on a UI element | tap on the screenshot | `click_at {x, y}` (CSS px in remote viewport) |
| Type a character | tap field → soft keyboard | `paste_text {text}` (focused element) |
| Press Backspace / Enter | mobile delete / Return | `press_key_v2 {key, modifiers}` |
| Scroll | swipe / mouse wheel | `scroll_by {dx, dy}` |
| Manage tabs | tabstrip below toolbar | `list_tabs`, `create_tab`, `close_tab`, `activate_tab` |
| Fill login form safely | `Email` + `Password` fields → `Sign in →` | `fill {selector, value}` + `click {selector}` (avoid; use panel for secrets) |

## Run

```bash
# Make sure the browser is up
./scripts/run.sh --headless

# Start the share panel (separate process; daemonized)
./browser-share/run.sh

# The panel is exposed at https://browser-share-<id>.svc.hypha.aicell.io
# (printed in stdout; also via `svamp service list`)

# Stop
./browser-share/stop.sh
```

The panel auto-discovers the live `browser-controller` service. If multiple Chromes have registered, it picks the most recent.

## Environment variables

| Var | Purpose |
|---|---|
| `HYPHA_TOKEN`     | Hypha access token (required) |
| `HYPHA_WORKSPACE` | Hypha workspace (required) |
| `HYPHA_SERVER_URL` | Default `https://hypha.aicell.io` |
| `BROWSER_SERVICE_ID` | Override the auto-discovered service id (optional) |
| `PORT`            | Local port for FastAPI (default 8765) |

## Security notes

- **CORS is `*`** because the iframe (with `null` origin in Svamp's artifact sandbox) needs to fetch. The data is already gated by:
  - The unguessable `svc.hypha.aicell.io` subdomain hostname
  - Server-side `HYPHA_TOKEN` (the panel runs in the user's local network)
  - Workspace-isolated Hypha auth
- **Passwords are NEVER logged** by the FastAPI server. The `/api/fill` and `/api/paste` endpoints accept secrets in the body and immediately forward to the chrome extension; nothing is written to disk.
- The hidden `<input>` that captures mobile soft-keyboard input is 1×1 px and off-screen so iOS doesn't show paste/magnifier menus over your screenshot.
- Killing `./browser-share/stop.sh` removes both the local process and the public tunnel.

## Mobile-specific design notes

| Concern | Mitigation |
|---|---|
| iOS shows "Paste / Look up" menu when tapping inputs | The kbd input is `position:fixed; 1px×1px; opacity:0; pointer-events:none` — taps go to the `<img>`, the input only receives `.focus()` programmatically. |
| Soft keyboard won't appear if `.focus()` is async | `focus()` is called *synchronously* inside the touch handler — must be in the user gesture stack. |
| Coordinate misalignment | Uses `shot.naturalWidth` / `naturalHeight` (real screenshot pixels), not hardcoded. Shotbox `aspect-ratio` is synced to image dimensions on every load. |
| Broken-image flash on refresh | New screenshots are preloaded into memory; `<img>.src` is only swapped when the new image is fully decoded. |
| iOS auto-zoom on input focus | `font-size: 16px` on the kbd input prevents Safari's "smart" zoom. |
| Touch swipe vs click | A swipe with > 18px movement triggers `scroll_by`; below that threshold it's a click. The synthesized click after touchend is suppressed in swipe case. |
