# Troubleshooting

## Supervisor died right after launch

Check `.supervisor.log`:

```bash
tail -50 .supervisor.log
```

Common causes:

- **`Extensions.loadUnpacked` returned an error**: usually the extension folder path is wrong or `manifest.json` is invalid. Validate with `python3 -c "import json; json.load(open('extension/manifest.json'))"`.
- **Chrome exited immediately with "Remote debugging pipe file descriptors are not open"**: you're on Linux/Windows and the Node FD-passing convention differs. Open an issue.
- **Hypha config missing**: `extension/config.js` not found → run `./scripts/build-config.sh` (or just `./scripts/run.sh` which calls it).

## `hypha services` shows no `browser-controller`

The supervisor + Chrome are running but the SW didn't register.

Check Chrome's log for SW errors:

```bash
grep -a "anighoc\|hypha-browser-use" .chrome.log
```

Look for `EvalError` (CSP issue — the hypha-rpc patch reverted?) or network errors.

If Chrome says nothing useful, attach to the SW directly via CDP. The Mac mini's Chrome isn't running with `--remote-debugging-port` by default (only the pipe), so for live inspection restart with:

```bash
./scripts/stop.sh
# edit scripts/install-extension.js to add --remote-debugging-port=9222 to chromeArgs
./scripts/run.sh
# then: curl http://127.0.0.1:9222/json
```

Find the SW target with `type=service_worker` and `url=chrome-extension://<id>/background.js`, then attach via `Runtime.enable` and check the console.

## Tools return `Cannot access contents of url "about:blank"`

You're calling a content-script-injection tool against a tab at `about:blank` or `chrome://newtab`. **Navigate to a real URL first**:

```bash
curl "$BASE/navigate" -d '{"url":"https://example.com"}'
curl "$BASE/wait_for_load" -d '{}'
curl "$BASE/get_page_info" -d '{}'   # now works
```

## Two `browser-controller` services in `hypha services`

Known harmless issue: the SW connects in both `chrome.runtime.onInstalled` and top-level `ensureConnected()`. Pick either entry — both work. Will fix in a future revision.

## Page navigation never completes (tabs stuck at `about:blank`)

If you're on a Mac mini accessed remotely with no GUI session attached, Chrome may launch but be unable to render network content. Verify by checking whether `chrome://newtab` loads (it should — no network) vs. `https://example.com` (which won't if the issue is present).

Workarounds:
- Make sure someone is signed into the macOS account on the physical display.
- For autonomous setups, configure macOS to auto-login on boot (System Settings → Users → Login Options → Auto-login).

## Service worker times out

MV3 service workers terminate after ~30s idle. The extension has `chrome.alarms.create("keepalive", {periodInMinutes: 0.4})` to wake itself every 24s. If you see the SW dropping connections, increase the keepalive frequency (lower `periodInMinutes`, minimum 0.4 = 24s per MV3 spec).

## Chrome window keeps stealing focus

`scripts/install-extension.js` launches Chrome with `--restore-last-session` so a user who's at the desktop gets their tabs restored. If you don't want this — e.g., for headless-but-windowed setups — pass `--no-restore`:

```bash
./scripts/run.sh --no-restore
```

## Want a clean profile

```bash
./scripts/stop.sh
rm -rf ~/.hypha-browser-use/profile
./scripts/run.sh
```

You'll need to re-do first logins on all target sites.

## Cleanup test pollution

If you tried any of the `defaults write com.google.Chrome` workarounds during debugging:

```bash
defaults delete com.google.Chrome ExtensionInstallSources
defaults delete com.google.Chrome ExtensionInstallAllowlist
# etc.
```

## "It works on Chrome for Testing but not on regular Chrome"

Chrome for Testing accepts `--load-extension` (which `install-extension.js` doesn't use anyway). Both should work; if regular Chrome doesn't, your version is < 137 (when `Extensions.loadUnpacked` shipped). Update Chrome to current stable.
