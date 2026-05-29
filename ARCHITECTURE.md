# Architecture

## The fundamental problem

In 2026, Google removed the `--load-extension` command-line switch from branded Chrome stable. The replacement is the **`Extensions` CDP domain**, which has three constraints:

1. Chrome must be launched with `--enable-unsafe-extension-debugging`.
2. The CDP transport must be `--remote-debugging-pipe` (not `--remote-debugging-port`). The pipe is a parent-process-only channel; the port is internet-reachable — Google made `Extensions.loadUnpacked` pipe-only as a security measure.
3. While Chrome is alive, the pipe must stay connected — closing the parent's pipe end terminates Chrome with `Connection terminated while reading from pipe`.

Constraint 3 means we need a long-lived "supervisor" process that holds the pipe open for as long as we want Chrome to run.

## The three processes

```
                   ┌─────────────────────────────────┐
                   │            User shell            │
                   │ ./scripts/run.sh (nohup + fork)  │
                   └────────────────┬─────────────────┘
                                    │ spawns
                                    ▼
        ┌────────────────────────────────────────────┐
        │  Node.js supervisor                        │
        │  scripts/install-extension.js              │
        │                                            │
        │  - child_process.spawn(Chrome, {           │
        │      stdio: ['ignore', log, log,           │
        │              'pipe',  'pipe']              │
        │    })                                      │
        │  - Chrome reads its FD 3, writes its FD 4  │
        │  - Node holds the parent ends in           │
        │    proc.stdio[3] (write) / [4] (read)      │
        │  - JSON-RPC NUL-delimited framing          │
        │  - Calls Extensions.loadUnpacked           │
        │  - Then blocks forever (keeps pipe open)   │
        └────────────────┬───────────────────────────┘
                         │ spawns via FD-3/4 pipe
                         ▼
        ┌────────────────────────────────────────────┐
        │  Google Chrome stable                      │
        │                                            │
        │  Flags:                                    │
        │    --enable-unsafe-extension-debugging     │
        │    --remote-debugging-pipe                 │
        │    --user-data-dir=~/.hypha-browser-use/   │
        │                     profile                │
        │    --restore-last-session                  │
        │                                            │
        │  Extension loaded via CDP:                 │
        │    Extensions.loadUnpacked({path: …})      │
        │    → returns deterministic ext id          │
        │       (from manifest contents)             │
        └────────────────┬───────────────────────────┘
                         │ runs MV3 service worker
                         ▼
        ┌────────────────────────────────────────────┐
        │  Service worker (background.js)            │
        │                                            │
        │  On startup:                               │
        │  - Import patched hypha-rpc.mjs            │
        │  - connectToServerHTTP({ token, ws })      │
        │  - Register one service with 40+ tools     │
        │                                            │
        │  Per tool call:                            │
        │  - Receive over HTTP-streaming transport   │
        │  - Dispatch to chrome.* API                │
        │  - Return result as JSON                   │
        └────────────────────────────────────────────┘
```

## Why each transport / library choice

### `connectToServerHTTP` (not `connectToServer` over WS)

The hypha-rpc WebSocket client logs `Creating a new websocket connection to wss://hypha.aicell.io/ws` and then hangs the authentication handshake when called from an MV3 service worker. The SW termination behavior (~30s idle) interacts badly with the auth round-trip; even with our `chrome.alarms` keepalive, the connection stays in the "connecting" state.

The HTTP-streaming transport uses POST for uplink and SSE for downlink, both of which are first-class in service worker context (fetch + ReadableStream). It registered cleanly on the first attempt.

### Node supervisor (not Python)

Python's `subprocess.Popen` with `preexec_fn` and manual `os.dup2` to FDs 3/4 works for ordinary child binaries (verified — `/usr/bin/python3` child saw the pipes as `mode=0o10660`). It does **not** work for Chrome on macOS — Chrome's launcher exits with `[ERROR:chrome/app/chrome_main_delegate.cc:1096] Remote debugging pipe file descriptors are not open`. Likely cause is Chrome's hardened-runtime sandbox stripping inherited non-stdio FDs during early initialization.

Node's `child_process.spawn({ stdio: ['ignore', logFh, logFh, 'pipe', 'pipe'] })` uses a different low-level path (libuv `uv_spawn`) that gives Chrome the FDs it expects. This is the same path Puppeteer / chrome-launcher use.

### Patched hypha-rpc.mjs

MV3 `extension_pages` CSP forbids `'unsafe-eval'`. The pristine hypha-rpc bundle uses `eval()` at module init time on line 7627:

```js
for (const arrType of Object.keys(typedArrayToDtypeMapping)) {
  typedArrayToDtypeKeys.push(eval(arrType));
}
```

Patched in place to:

```js
for (const arrType of Object.keys(typedArrayToDtypeMapping)) {
  typedArrayToDtypeKeys.push(globalThis[arrType]);
}
```

Plus two adjacent uses of `new Function(...)` and `eval(script.content)` that we don't exercise but patched defensively. Original kept at `extension/lib/hypha-rpc.mjs.orig`.

## Call flow: agent issues a tool call

```
Claude agent
   │
   │ POST https://hypha.aicell.io/<ws>/services/<id>/click
   │ Headers: Authorization: Bearer <hypha_token>
   │ Body:    {"selector": "#submit"}
   │
   ▼
Hypha cloud  ── routes by service ID ──►  HTTP-streaming connection
                                          (SSE down + POST up)
                                                  │
                                                  ▼
                                          Service worker
                                          background.js:
                                            click({selector}) →
                                              chrome.scripting.executeScript({
                                                target: {tabId},
                                                world: "MAIN",
                                                func: (sel) => {
                                                  document.querySelector(sel).click();
                                                  return {ok: true};
                                                },
                                                args: [selector]
                                              })
                                                  │
                                                  │ {ok: true}
                                                  ▲
                                          ◄───────┘
   │ HTTP 200
   ▼
   {ok: true}
```

## 2FA handoff design

The agent never has access to the user's phone. Instead:

```
Agent: navigate("https://kth.outlook.com/auth")
Agent: fill("#email", "wei@kth.se")
Agent: click("#next")
Agent: wait_for_selector(".push-prompt-number", timeout_ms=10000)
Agent: number := read_text(".push-prompt-number")  // e.g. "27"
Agent: notify_user("Approve KTH sign-in on your phone with code " + number)
Agent: wait_for_selector(".inbox-loaded", timeout_ms=60000)   // user taps approve
Agent: ...continue automation...
```

- `notify_user` triggers a macOS notification (`chrome.notifications.create`) AND can be paired on the agent side with `svamp session notify` for the user's Claude UI.
- For KTH (biometric required), the user taps "Approve" on their physical phone after Face/Touch ID. The agent just waits.
- No emulator, no biometric bypass, no security policy violation.

## Profile / state lifecycle

| Where | What | Lifetime |
|---|---|---|
| `~/.hypha-browser-use/profile/` | Chrome user-data-dir for the agent's Chrome instance | Persists across reboots until manually deleted |
| `<profile>/Default/Cookies` | Site cookies (set when user logs into sites in this Chrome) | Persists |
| `<profile>/Preferences` | Extension state — but the unpacked extension is **not** in here. Loaded ephemerally each time. | Per session |
| Extension itself | At `/Users/weio/workspace/hypha-browser-use/extension/` | Persists in source tree |
| `~/auth-agent-audit.log` (TODO) | One JSON line per tool call | Persists; rotate via launchd |
| `extension/config.js` | Hypha token + workspace + service-id | gitignored, regenerated by `build-config.sh` |

## Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Supervisor (Node) crashes | Chrome dies within seconds (pipe closes) | `./scripts/run.sh` again |
| Chrome crashes | Supervisor's `proc.on("exit")` fires → supervisor exits | `./scripts/run.sh` again |
| Hypha disconnects | SW reconnect logic (in `ensureConnected()`) re-registers within seconds | None needed |
| SW killed by Chrome lifecycle | `chrome.alarms` (`keepalive`, every 24s) wakes it; reconnects | None needed |
| Tool call against tab at `about:blank` | `chrome.scripting.executeScript` returns "Cannot access" error | Agent should navigate first |
| Network navigation fails (no display) | Tab status goes `complete` but URL stuck at `about:blank` | Open from a session with display; or investigate further |
