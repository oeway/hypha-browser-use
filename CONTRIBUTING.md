# Contributing

Thanks for looking at hypha-browser-use! This project crystallized from a lot of dead ends — `docs/LEARNINGS.md` has the back-story. If you're considering a contribution, the most useful directions are:

## High-impact areas

- **Linux & Windows ports.** Currently macOS-only. The Node `stdio: ['ignore', logFh, logFh, 'pipe', 'pipe']` pattern *should* port directly, but Chrome's stable-builds may have different `--load-extension` policies. Verify and document.
- **Auto-restart / launchd plist** for the supervisor on Mac mini boot. Pull request template welcome.
- **Audit log + per-tool risk tiers** in `extension/background.js`. Right now every tool is "default allow"; we want a tier system (low / medium / high) with the high-tier tools requiring extra confirmation.
- **Smart DOM hardening.** `get_browser_state` writes `data-__hyphaIdx` attributes on the live DOM. Some sites' MutationObservers will notice. Move to a WeakMap-based shadow registry instead.
- **Per-site profiles.** Right now we have one Chrome profile. Some agents may want isolated profiles per site (banking vs. social). Add a `--profile-name` option.
- **Tests.** Currently zero. A Playwright-based end-to-end test that spins up the supervisor, calls a few tools, and tears down would catch regressions.

## How to develop

```bash
git clone <this-repo>
cd hypha-browser-use
export HYPHA_TOKEN=...    # from https://hypha.aicell.io
export HYPHA_WORKSPACE=ws-user-...
./scripts/run.sh
./scripts/test-rpc.sh
```

The extension source lives at `extension/`. Reload after editing by stopping the supervisor (`./scripts/stop.sh`) and re-running (`./scripts/run.sh`). The supervisor re-installs the unpacked extension from disk each time.

## Style

- Keep `extension/background.js` to plain modern JS — no TypeScript, no bundler. The whole point is "drop in unpacked", and adding a build step would be a regression.
- Tool functions take ONE object argument (destructured). All errors propagate to Hypha as the response — don't swallow them.
- Don't add features without a use case. The tool surface is already wide. Prefer to make existing tools more reliable over adding new ones.

## Bug reports

Please include:

- Chrome version (`chrome://version`)
- Output of `./scripts/test-rpc.sh`
- The relevant portion of `.chrome.log` and `.supervisor.log`
- macOS version and Apple Silicon vs Intel (we currently only test Apple Silicon)

## Code of conduct

Be kind. The project exists to make browser automation work for real personal-productivity use cases. Patches that make it more reliable, simpler, or work on more platforms are very welcome.
