# First login (one-time, per site)

The agent's Chrome runs in a separate `--user-data-dir` (`~/.hypha-browser-use/profile/` by default), so it starts with NO cookies. Before the agent can do anything on a logged-in site, the user logs in **once manually** in that profile.

## How to do it

1. **Make sure the supervisor is up:**
   ```bash
   ./scripts/run.sh
   ```
   This launches Chrome in headed mode (with a visible window, on the user's actual display).
2. **Switch to the agent's Chrome window.** It'll have a fresh new-tab page.
3. **Sign into each target site** like a normal user: enter email, password, MFA as usual. Browse around for a minute so the cookies/history feel "real".
4. **Quit the Chrome window** with cmd-Q OR just leave it open. The supervisor keeps it alive.

Now the agent can do `navigate("https://github.com")` and land on a logged-in page without auth.

## Why a separate profile

If we used your main Chrome profile:
- Every agent action would affect your real history/downloads/extensions.
- Closing the agent Chrome would close your everyday Chrome.
- A buggy tool could trash your main session.

Separate profile = strict isolation. Same Chrome binary, same Google login flow, your bookmarks/extensions/passwords stay in YOUR profile.

## How long do logins last?

- **Google / Microsoft / GitHub**: weeks to months unless you change password or get bumped out by a policy.
- **Banking**: usually shorter (~7 days) and may require re-auth on every important action. Expect to redo MFA each session.
- **Other SaaS**: varies. If the agent reports "redirected to login", just do the first-login dance again.

## What gets saved in the agent profile

```
~/.hypha-browser-use/profile/
├── Default/
│   ├── Cookies            ← session cookies
│   ├── Login Data         ← saved passwords (Chrome's autofill)
│   ├── History            ← browsing history
│   ├── Preferences        ← Chrome settings
│   └── ...
└── Local State            ← profile-wide settings
```

All of this is **gitignored** and stays on the Mac mini.

## Troubleshooting first logins

- **"This browser isn't supported" warning on Google**: shouldn't appear with real Chrome stable; if it does, check that the supervisor used `/Applications/Google Chrome.app` (not Chrome for Testing or another channel).
- **Captcha / Turnstile challenge**: complete it once; the cookies persist; next agent visit should sail through.
- **Page asks for a passkey on a device without TouchID**: the Mac mini may not have a registered passkey for the site. Use password+2FA flow instead.
