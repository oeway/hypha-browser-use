"""
browser-share: a tiny FastAPI app that mirrors the hypha-browser Chrome live and
exposes a "secure login" form so the user can type credentials into a real
webpage (NOT into chat) and have them filled into the remote browser via the
hypha RPC service.

Run:
  HYPHA_TOKEN=... HYPHA_WORKSPACE=... \\
  BROWSER_SERVICE_ID=ws-user-.../browser-ext-xxxx:browser-controller \\
  uv run --with fastapi --with uvicorn --with httpx python3 server.py
"""
from __future__ import annotations
import os, base64, urllib.parse, asyncio, time, json, subprocess
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse as _urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

HERE = Path(__file__).resolve().parent

HYPHA_TOKEN  = os.environ["HYPHA_TOKEN"]
HYPHA_SERVER = os.environ.get("HYPHA_SERVER_URL", "https://hypha.aicell.io")

def _service_base(service_id: str) -> str:
    ws, rest = service_id.split("/", 1)
    return (f"{HYPHA_SERVER}/{urllib.parse.quote(ws, safe='')}"
            f"/services/{urllib.parse.quote(rest, safe='')}")

# Allow runtime override; auto-rediscover if stale.
SERVICE_ID  = os.environ.get("BROWSER_SERVICE_ID", "")
SERVICE_URL = _service_base(SERVICE_ID) if SERVICE_ID else None

state = {"client": None, "service_url": SERVICE_URL, "service_id": SERVICE_ID}

async def hypha_get_live_service() -> str | None:
    """Re-discover a live browser-controller via Hypha registry (slow path)."""
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(f"{HYPHA_SERVER}/public/services/ws/list_services",
                        params={"workspace": os.environ.get("HYPHA_WORKSPACE","")},
                        headers={"Authorization": f"Bearer {HYPHA_TOKEN}"})
        # Fallback: use the registry HTTP route if available — otherwise let
        # the caller re-set BROWSER_SERVICE_ID manually.
        if r.status_code != 200: return None
        for svc in r.json():
            sid = svc.get("id","")
            if "browser-ext-" in sid and sid.endswith(":browser-controller"):
                base = _service_base(sid)
                pr = await c.post(f"{base}/ping?_mode=last",
                                  headers={"Authorization": f"Bearer {HYPHA_TOKEN}",
                                           "Content-Type": "application/json"},
                                  content='{"kwargs":{}}')
                if pr.status_code == 200 and pr.json().get("ok"):
                    return sid
    return None

async def call(tool: str, kwargs: dict | None = None, timeout: float = 15.0):
    if not state["service_url"]:
        sid = await hypha_get_live_service()
        if not sid: raise RuntimeError("no live browser-controller service")
        state["service_id"] = sid
        state["service_url"] = _service_base(sid)
    payload = {"kwargs": kwargs or {}}
    r = await state["client"].post(
        f"{state['service_url']}/{tool}?_mode=last",
        headers={"Authorization": f"Bearer {HYPHA_TOKEN}",
                 "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{tool} → HTTP {r.status_code}: {r.text[:200]}")
    return r.json()

@asynccontextmanager
async def lifespan(app):
    state["client"] = httpx.AsyncClient()
    try:
        yield
    finally:
        await state["client"].aclose()

app = FastAPI(lifespan=lifespan)
# The Svamp artifact iframe runs in a sandbox with `null` origin — without
# CORS, fetch() from the embedded HTML is blocked. The data exposed here is
# already gated by Hypha-side auth and the unguessable tunnel hostname, so
# wildcard CORS is acceptable for the iframe-embed use case.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    allow_credentials=False,
)

# ─────────────────────────── routes ─────────────────────────── #

@app.get("/", response_class=HTMLResponse)
async def index():
    return (HERE / "login.html").read_text()

@app.get("/screen.jpg")
async def screen():
    try:
        result = await call("screenshot", {"format": "jpeg", "quality": 60})
        b64 = result.get("base64") if isinstance(result, dict) else None
        if not b64:
            return Response(b"no screenshot", media_type="text/plain", status_code=503)
        return Response(base64.b64decode(b64), media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return Response(f"screenshot error: {e}".encode(),
                        media_type="text/plain", status_code=500)

def _classify_elements(elements):
    """Find the email-like, password-like, and submit-like elements in a get_browser_state result."""
    email = None; password = None; submit = None
    submit_keywords = ("sign in", "log in", "sign-in", "log-in", "logon", "log on",
                       "next", "submit", "continue", "logga in")
    for e in elements:
        tag = e.get("tag",""); attrs = e.get("attrs",{}) or {}
        typ  = (attrs.get("type") or "").lower()
        name = (attrs.get("name") or "").lower()
        ph   = (attrs.get("placeholder") or "").lower()
        aria = (attrs.get("aria-label") or "").lower()
        idattr = (attrs.get("id") or "").lower()
        role = (attrs.get("role") or "").lower()
        text = (e.get("text") or "").strip().lower()
        if tag == "input" and typ == "password" and password is None:
            password = e
        elif tag == "input" and typ in ("email","text","tel","") and email is None:
            blob = " ".join((name, ph, aria, idattr))
            if any(k in blob for k in ("email","username","user","login","loginfmt","upn","account","mail")):
                email = e
        if submit is None:
            text_match = any(k == text or (k in text and len(text) < 30) for k in submit_keywords)
            if (tag == "button" and (typ == "submit" or text_match)) or \
               (tag == "input"  and typ == "submit") or \
               (tag in ("a", "div", "span") and (role == "button" or text_match) and text_match):
                submit = e
    # fallback: any first input[type=text|tel|empty] as email if nothing matched
    if email is None:
        for e in elements:
            t = e.get("tag"); ty = (e.get("attrs",{}).get("type") or "").lower()
            if t == "input" and ty in ("text","email","tel",""):
                email = e; break
    return email, password, submit

@app.get("/state")
async def get_state():
    # Resilient: even if the active tab is at about:blank or chrome:// (where
    # content scripts can't run), still return basic info from list_tabs.
    out = {"service_id": state["service_id"], "url": None, "title": None,
           "has_email_field": False, "has_password_field": False,
           "has_submit": False, "element_count": 0, "warning": None}
    try:
        tab = await call("get_active_tab", {})
        if tab:
            out["url"]   = tab.get("url")
            out["title"] = tab.get("title")
    except Exception as e:
        out["warning"] = f"active_tab: {e}"
    # Only try DOM tools if URL is not about: or chrome:
    if out["url"] and not out["url"].startswith(("about:", "chrome:", "chrome-extension:", "edge:", "view-source:")):
        try:
            bs = await call("get_browser_state", {"viewport_only": True})
            elements = bs.get("elements", []) if isinstance(bs, dict) else []
            email, password, submit = _classify_elements(elements)
            out["has_email_field"]    = email is not None
            out["has_password_field"] = password is not None
            out["has_submit"]         = submit is not None
            out["element_count"]      = bs.get("count", 0) if isinstance(bs, dict) else 0
        except Exception as e:
            out["warning"] = (out["warning"] + "; " if out["warning"] else "") + f"browser_state: {e}"
    return out

# ───────── Multi-step login orchestrator ───────── #
def _detect_2fa_number(elements):
    """Look for the MS Authenticator number-match digit on the page.
    Returns the number as a string, or None.
    Heuristic: a small visible element whose text is 1-3 digits and which
    occupies a reasonably large bbox (it's typically rendered big)."""
    candidates = []
    for e in elements:
        text = (e.get("text") or "").strip()
        if not (text.isdigit() and 1 <= len(text) <= 3):
            continue
        bbox = e.get("bbox") or {}
        w = bbox.get("w", 0); h = bbox.get("h", 0)
        if w < 20 or h < 20: continue
        attrs = e.get("attrs", {}) or {}
        # Prioritize elements with hinting attributes
        score = w * h
        if "sign" in (attrs.get("id","") + attrs.get("class","") + attrs.get("data-bind","")).lower():
            score *= 10
        if attrs.get("aria-live"):
            score *= 5
        candidates.append((score, text, e))
    if not candidates: return None
    candidates.sort(reverse=True)
    return candidates[0][1]

def _detect_error_message(elements):
    """Find a visible error message on the page (e.g. 'Wrong password')."""
    for e in elements:
        text = (e.get("text") or "").strip().lower()
        attrs = e.get("attrs", {}) or {}
        cls = (attrs.get("class") or "").lower()
        idattr = (attrs.get("id") or "").lower()
        if any(k in cls + " " + idattr for k in ("error", "alert", "validation")):
            if text and len(text) < 200 and len(text) > 5:
                return e.get("text", "").strip()
        if any(k in text for k in ("incorrect password","wrong password","invalid","not match",
                                    "could not find","does not exist","try again")):
            if len(text) < 200: return e.get("text", "").strip()
    return None

@app.post("/api/login_step")
async def login_step(req: Request):
    """One step of an orchestrated multi-page login flow.

    Body: {email?, password?}

    Looks at the current page state and decides what to do:
    - email field present → fill (if email given) and click submit
    - password field present → fill (if password given) and click submit
    - 2FA number visible → return the number for the user to approve on phone
    - logged-in / unknown → return state info

    The client polls this until step == "complete" or "needs_human".
    """
    body = await req.json()
    email = body.get("email")
    password = body.get("password")

    info = await call("get_page_info", {})
    url = (info or {}).get("url", "")
    title = (info or {}).get("title", "")

    # If we're on a non-actionable URL (about:blank, chrome://), bail out.
    if url.startswith(("about:", "chrome:", "chrome-extension:", "edge:", "view-source:")):
        return {"step": "no_page", "url": url, "title": title,
                "message": "No real page loaded — navigate first."}

    # FIRST: are we already past all known login hosts? Treat as complete.
    # Otherwise the orchestrator can mistake post-login search boxes for
    # email inputs and start typing creds into them (real bug observed).
    on_login_host = any(h in url for h in ("login.microsoftonline.com", "login.kth.se",
                                            "login.live.com", "accounts.google.com",
                                            "/adfs/", "/oauth2/", "login.live.net",
                                            "okta.com", "auth0.com", "/saml", "/sso/"))
    if not on_login_host:
        return {"step": "complete", "url": url, "title": title,
                "message": f"Logged in (off any login host) — {title}"}

    bs = await call("get_browser_state", {"viewport_only": True})
    elements = bs.get("elements", []) if isinstance(bs, dict) else []
    e_el, p_el, s_el = _classify_elements(elements)
    err = _detect_error_message(elements)
    if err:
        return {"step": "error", "url": url, "title": title, "error": err,
                "message": f"Page reports: {err}"}

    # Check: are we on a 2FA number-match prompt?
    is_msft_url = "microsoftonline.com" in url or "login.live.com" in url
    twofa_num = _detect_2fa_number(elements)
    if twofa_num and (is_msft_url or "approve" in title.lower() or "sign in" in title.lower()):
        return {"step": "2fa_waiting", "url": url, "title": title,
                "number": twofa_num,
                "message": f"Approve sign-in on your phone — tap the tile labeled “{twofa_num}”."}

    # Password page (possibly with a HIDDEN username field, e.g. KTH ADFS)
    if p_el is not None:
        if not password:
            return {"step": "password_needed", "url": url, "title": title,
                    "message": "Password field is visible — provide password."}
        actions = []
        # Some Microsoft/ADFS pages have a hidden username field that posts
        # alongside password. Set value with BOTH the setter AND setAttribute
        # in one atomic call (some pages re-read getAttribute('value') during
        # submit). Do this BEFORE filling password so the page's submit
        # handler sees a complete form.
        if email:
            import json as _j
            email_js = _j.dumps(email)
            user_fill_code = (
                "(() => {"
                "  const selectors = ['#userNameInput','input[name=UserName]',"
                "    'input[name=loginfmt]','input[name=Username]','input[name=username]'];"
                "  let target = null;"
                "  for (const s of selectors) { const el = document.querySelector(s);"
                "    if (el && el.tagName === 'INPUT') { target = el; break; } }"
                "  if (!target) return {ok:false, error:'no username input on page'};"
                "  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                f" const v = {email_js};"
                "  setter.call(target, v);"
                "  target.setAttribute('value', v);"
                "  target.dispatchEvent(new Event('input',  {bubbles:true}));"
                "  target.dispatchEvent(new Event('change', {bubbles:true}));"
                "  return {ok:true, id: target.id, name: target.name, value: target.value};"
                "})()"
            )
            try:
                r = await call("eval_js", {"code": f"return {user_fill_code};"})
                if isinstance(r, dict) and isinstance(r.get("value"), dict):
                    inner = r["value"]
                    if inner.get("ok"):
                        actions.append(f"filled hidden username #{inner.get('id')}={inner.get('value')}")
                    else:
                        actions.append({"op": "username", "error": inner.get("error")})
                else:
                    actions.append({"op": "username", "error": "eval_js bad shape"})
            except Exception as e:
                actions.append({"op": "username", "error": str(e)})
        # Now the visible password field — use the SAME atomic pattern so
        # the value isn't cleared by an intervening render.
        import json as _j
        pw_js = _j.dumps(password)
        pw_fill_code = (
            "(() => {"
            "  const el = document.querySelector('input[type=password]');"
            "  if (!el) return {ok:false, error:'no password input'};"
            "  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            f" setter.call(el, {pw_js});"
            "  el.dispatchEvent(new Event('input',  {bubbles:true}));"
            "  el.dispatchEvent(new Event('change', {bubbles:true}));"
            "  return {ok:true, length: el.value.length};"
            "})()"
        )
        try:
            r = await call("eval_js", {"code": f"return {pw_fill_code};"})
            actions.append("filled password")
        except Exception as e:
            actions.append({"op": "password", "error": str(e)})
        # Click the actual submit button — preferring the page's own submit
        # function if exposed (handles validation + form.submit() correctly).
        submit_code = (
            "(() => {"
            "  if (window.Login && typeof window.Login.submitLoginRequest === 'function') {"
            "    return {via:'Login.submitLoginRequest', r: window.Login.submitLoginRequest()};"
            "  }"
            "  const btn = document.querySelector('#submitButton, button[type=submit], input[type=submit]');"
            "  if (btn) { btn.click(); return {via:'btn.click', tag:btn.tagName, id:btn.id}; }"
            "  const form = document.forms[0];"
            "  if (form) { form.submit(); return {via:'form.submit'}; }"
            "  return {ok:false, error:'no submit'};"
            "})()"
        )
        try:
            r = await call("eval_js", {"code": f"return {submit_code};"})
            actions.append("clicked submit")
        except Exception as e:
            actions.append({"op": "submit", "error": str(e)})
        return {"step": "password_submitted", "url": url, "title": title,
                "actions": actions,
                "message": "Password submitted — waiting for next step…"}

    # Email page
    if e_el is not None:
        if not email:
            return {"step": "email_needed", "url": url, "title": title,
                    "message": "Email field is visible — provide email."}
        await call("click_by_index", {"index": e_el["index"]})
        await call("input_by_index", {"index": e_el["index"], "text": email})
        actions = ["filled email"]
        if s_el is not None:
            await call("click_by_index", {"index": s_el["index"]})
            actions.append("clicked submit")
        return {"step": "email_submitted", "url": url, "title": title,
                "actions": actions,
                "message": "Email submitted — waiting for password page…"}

    # No login fields detected — either logged in OR still loading
    on_login_host = any(h in url for h in ("login.microsoftonline.com", "login.kth.se",
                                            "login.live.com", "accounts.google.com",
                                            "/adfs/", "/oauth2/"))
    if not on_login_host:
        return {"step": "complete", "url": url, "title": title,
                "message": f"Logged in (or no login page) — {title}"}

    return {"step": "waiting", "url": url, "title": title, "element_count": len(elements),
            "message": "Login page detected but no actionable fields — page may still be loading."}

@app.post("/api/fill")
async def fill_and_submit(req: Request):
    """Fill any provided fields, then click the most likely submit button.
    Body: {email?: str, password?: str, submit?: bool=true}"""
    body = await req.json()
    email = body.get("email")
    password = body.get("password")
    submit_after = body.get("submit", True)
    actions = []
    try:
        bs = await call("get_browser_state", {"viewport_only": True})
        elements = bs.get("elements", []) if isinstance(bs, dict) else []
        e_el, p_el, s_el = _classify_elements(elements)

        if email and e_el is not None:
            r = await call("input_by_index", {"index": e_el["index"], "text": email})
            actions.append({"op": "email", "index": e_el["index"], "ok": (r or {}).get("ok", False)})
        elif email and e_el is None:
            actions.append({"op": "email", "error": "no email-like field"})

        if password and p_el is not None:
            r = await call("input_by_index", {"index": p_el["index"], "text": password})
            actions.append({"op": "password", "index": p_el["index"], "ok": (r or {}).get("ok", False)})
        elif password and p_el is None:
            actions.append({"op": "password", "error": "no password field"})

        if submit_after and s_el is not None:
            r = await call("click_by_index", {"index": s_el["index"]})
            actions.append({"op": "submit", "index": s_el["index"], "ok": (r or {}).get("ok", False)})

        return {"actions": actions}
    except Exception as e:
        return JSONResponse({"actions": actions, "error": str(e)}, status_code=500)

# ───────── browser-toolbar endpoints ───────── #
@app.post("/api/back")
async def go_back():
    try: return await call("go_back", {})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/forward")
async def go_forward():
    try: return await call("go_forward", {})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/reload")
async def reload_tab(req: Request):
    try:
        body = {}
        try: body = await req.json()
        except Exception: pass
        return await call("reload", {"bypass_cache": bool(body.get("hard", False))})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/scroll_by")
async def scroll_by(req: Request):
    body = await req.json()
    try:
        return await call("scroll_by", {
            "dx": float(body.get("dx", 0)),
            "dy": float(body.get("dy", 0)),
        })
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/scroll_to")
async def scroll_to_pos(req: Request):
    body = await req.json()
    try:
        return await call("scroll_to_position", {
            "x": body.get("x"), "y": body.get("y"),
        })
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/new_tab")
async def new_tab(req: Request):
    body = {}
    try: body = await req.json()
    except Exception: pass
    try:
        return await call("create_tab", {"url": body.get("url", "about:blank"), "active": True})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/close_tab")
async def api_close_tab(req: Request):
    body = await req.json()
    try:
        return await call("close_tab", {"tab_id": int(body["tab_id"])})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/activate_tab")
async def api_activate_tab(req: Request):
    body = await req.json()
    try:
        return await call("activate_tab", {"tab_id": int(body["tab_id"])})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/tabs")
async def api_tabs():
    try:
        return await call("list_tabs", {})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/notify")
async def api_notify(req: Request):
    body = await req.json()
    try:
        return await call("notify_user", {
            "message": body.get("message", ""),
            "level":   body.get("level", "info"),
        })
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

# ───────── macOS Keychain credential storage ───────── #
# We use a DEDICATED app keychain at ~/Library/Keychains/hypha-browser.keychain-db
# with an empty password and auto-unlock. This avoids the GUI authorization
# prompt that the login keychain requires for writes from a headless/SSH session.
# Items use:
#   - Internet password: service="hypha-browser:<site>", account=<email>
#   - A generic-password JSON index for fast listing.
KC_NAME = "hypha-browser.keychain"
KC_PATH = str(Path.home() / "Library" / "Keychains" / "hypha-browser.keychain-db")
KC_SERVICE_PREFIX = "hypha-browser:"
KC_INDEX_SERVICE = "hypha-browser:_index"
KC_INDEX_ACCOUNT = "index"

def _kc_service_for(site: str) -> str:
    return f"{KC_SERVICE_PREFIX}{site}"

def _kc_ensure_exists() -> str:
    """Create the dedicated keychain if missing; unlock + persist."""
    # security looks up by name OR by full path. Prefer path to avoid
    # ambiguity with a same-named keychain elsewhere.
    target = KC_PATH if os.path.exists(KC_PATH) else KC_NAME
    if not os.path.exists(KC_PATH):
        subprocess.run(
            ["/usr/bin/security", "create-keychain", "-p", "", KC_PATH],
            check=True, timeout=5, capture_output=True, text=True)
        # Settings: no auto-lock, no timeout
        subprocess.run(
            ["/usr/bin/security", "set-keychain-settings", "-lut", "0", KC_PATH],
            check=False, timeout=5, capture_output=True)
        # Make sure the keychain is in the user's search list so it can be
        # discovered by `find-internet-password` without explicit -k path.
        try:
            cur = subprocess.run(
                ["/usr/bin/security", "list-keychains", "-d", "user"],
                capture_output=True, text=True, timeout=5)
            existing = [x.strip().strip('"') for x in cur.stdout.splitlines() if x.strip()]
            if KC_PATH not in existing:
                subprocess.run(
                    ["/usr/bin/security", "list-keychains", "-d", "user",
                     "-s", KC_PATH, *existing],
                    check=False, timeout=5, capture_output=True)
        except Exception: pass
    # Always unlock (empty password) before any operation
    subprocess.run(
        ["/usr/bin/security", "unlock-keychain", "-p", "", KC_PATH],
        check=False, timeout=5, capture_output=True)
    return KC_PATH

def _site_from_url(url: str) -> str:
    """Extract a stable 'site' key from a URL — eTLD+1 by default.
    e.g. 'https://login.ug.kth.se/adfs/...' → 'kth.se'
         'https://outlook.office.com/mail' → 'office.com'
    """
    try:
        host = (_urlparse(url).hostname or "").lower()
        if not host: return ""
        parts = host.split(".")
        # multi-part TLDs (co.uk, com.au, etc.) — keep last 3 if penultimate ≤ 3 chars
        if len(parts) >= 3 and len(parts[-2]) <= 3 and parts[-2] in ("co","com","ac","gov","org","net","edu"):
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""

def _kc_read_index() -> list:
    try:
        _kc_ensure_exists()
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", KC_INDEX_SERVICE, "-a", KC_INDEX_ACCOUNT, "-w",
             KC_PATH],
            capture_output=True, text=True, timeout=4)
        if r.returncode != 0: return []
        s = (r.stdout or "").rstrip("\n")
        if not s: return []
        return json.loads(s)
    except Exception:
        return []

def _kc_write_index(idx: list) -> None:
    _kc_ensure_exists()
    blob = json.dumps(idx, separators=(",", ":"))
    subprocess.run(
        ["/usr/bin/security", "add-generic-password",
         "-s", KC_INDEX_SERVICE, "-a", KC_INDEX_ACCOUNT, "-w", blob,
         "-U", "-T", "/usr/bin/security",
         KC_PATH],
        check=True, timeout=4, capture_output=True, text=True)

def _kc_get_password(site: str, account: str) -> str | None:
    _kc_ensure_exists()
    r = subprocess.run(
        ["/usr/bin/security", "find-internet-password",
         "-s", _kc_service_for(site), "-a", account, "-w",
         KC_PATH],
        capture_output=True, text=True, timeout=5)
    if r.returncode != 0: return None
    return (r.stdout or "").rstrip("\n")

def _kc_set_password(site: str, account: str, password: str) -> None:
    _kc_ensure_exists()
    subprocess.run(
        ["/usr/bin/security", "add-internet-password",
         "-s", _kc_service_for(site), "-a", account, "-w", password,
         "-U", "-T", "/usr/bin/security",
         KC_PATH],
        check=True, timeout=5, capture_output=True, text=True)

def _kc_delete(site: str, account: str) -> None:
    _kc_ensure_exists()
    subprocess.run(
        ["/usr/bin/security", "delete-internet-password",
         "-s", _kc_service_for(site), "-a", account,
         KC_PATH],
        capture_output=True, text=True, timeout=4)

@app.get("/api/credentials")
async def list_credentials():
    """List saved credentials (no passwords, just site + account)."""
    try:
        idx = _kc_read_index()
        # Also suggest the current page's site as the default "save under" target
        suggested_site = ""
        try:
            tab = await call("get_active_tab", {})
            if isinstance(tab, dict) and tab.get("url"):
                suggested_site = _site_from_url(tab["url"])
        except Exception: pass
        return {"items": idx, "suggested_site": suggested_site}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/credentials/save")
async def save_credential(req: Request):
    """Body: {site, account, password}. site may be omitted → derived from current URL."""
    body = await req.json()
    site    = (body.get("site") or "").strip()
    account = (body.get("account") or "").strip()
    password = body.get("password") or ""
    if not account or not password:
        return JSONResponse({"error": "account and password required"}, status_code=400)
    if not site:
        try:
            tab = await call("get_active_tab", {})
            if isinstance(tab, dict) and tab.get("url"):
                site = _site_from_url(tab["url"])
        except Exception: pass
    if not site:
        return JSONResponse({"error": "site required (couldn't infer from active tab)"}, status_code=400)

    try:
        _kc_set_password(site, account, password)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"keychain save failed: {e.stderr or e}"}, status_code=500)

    # Update the index (idempotent)
    idx = _kc_read_index()
    idx = [e for e in idx if not (e.get("site") == site and e.get("account") == account)]
    idx.append({"site": site, "account": account})
    idx.sort(key=lambda e: (e.get("site",""), e.get("account","")))
    try: _kc_write_index(idx)
    except Exception: pass
    return {"ok": True, "site": site, "account": account, "total": len(idx)}

@app.post("/api/credentials/delete")
async def delete_credential(req: Request):
    body = await req.json()
    site = (body.get("site") or "").strip()
    account = (body.get("account") or "").strip()
    if not site or not account:
        return JSONResponse({"error": "site and account required"}, status_code=400)
    _kc_delete(site, account)
    idx = _kc_read_index()
    idx = [e for e in idx if not (e.get("site") == site and e.get("account") == account)]
    try: _kc_write_index(idx)
    except Exception: pass
    return {"ok": True}

@app.post("/api/login_step_saved")
async def login_step_saved(req: Request):
    """Like /api/login_step but pulls password from macOS Keychain.
    Body: {site, account}. Never returns the password to the client."""
    body = await req.json()
    site = (body.get("site") or "").strip()
    account = (body.get("account") or "").strip()
    if not site or not account:
        return JSONResponse({"error": "site and account required"}, status_code=400)
    password = _kc_get_password(site, account)
    if password is None:
        return JSONResponse({"error": f"no saved credential for {account}@{site}"}, status_code=404)
    # Re-issue login_step with credentials populated server-side.
    class _FakeReq:
        def __init__(self, body): self._body = body
        async def json(self): return self._body
    return await login_step(_FakeReq({"email": account, "password": password}))

@app.post("/api/navigate")
async def navigate(req: Request):
    body = await req.json()
    url = body.get("url")
    if not url: return JSONResponse({"error":"url required"}, status_code=400)
    try:
        tabs = await call("list_tabs", {})
        active = next((t for t in (tabs or []) if t.get("active")), None)
        if not active:
            w = await call("create_window", {"url": url})
            return {"created_window": True, "tab": (w.get("tabs") or [None])[0]}
        await call("navigate", {"tab_id": active["id"], "url": url})
        return {"navigated": True, "tab_id": active["id"]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/click")
async def click(req: Request):
    """Body: {index: int} OR {selector: str}"""
    body = await req.json()
    if "index" in body:
        r = await call("click_by_index", {"index": body["index"]}); return r
    if "selector" in body:
        r = await call("click", {"selector": body["selector"]}); return r
    return JSONResponse({"error":"index or selector required"}, status_code=400)

# ───────── click-anywhere pass-through ───────── #
@app.post("/api/click_at")
async def click_at(req: Request):
    """Body: {x, y} in browser viewport CSS pixels (the page JS does the scaling)."""
    body = await req.json()
    x = float(body["x"]); y = float(body["y"])
    try:
        return await call("click_at", {"x": x, "y": y, "mark": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────── keyboard pass-through ───────── #
@app.post("/api/key")
async def key(req: Request):
    """Body:
      - For a printable character: {char: "a"}        → inserted at caret of focused element
      - For a special key:         {key: "Enter"|"Backspace"|"Tab"|"ArrowLeft"|..., modifiers?:[...]}
    """
    body = await req.json()
    if "char" in body:
        try:
            return await call("paste_text", {"text": body["char"]})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    key_name = body.get("key") or ""
    if not key_name:
        return JSONResponse({"error":"key or char required"}, status_code=400)
    mods = list(body.get("modifiers") or [])
    try:
        return await call("press_key_v2", {"key": key_name, "modifiers": mods})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    # (unreachable — old dynamic-code path below kept for diff context)
    if False:
        js_key = ""
        ctrl  = "false"; shift = "false"; alt = "false"; meta = "false"
        code = (
            "(() => {"
            "   const el = document.activeElement || document.body;"
            f"  const k = {js_key};"
            f"  const opts = {{key:k, bubbles:true, cancelable:true, ctrlKey:{ctrl}, shiftKey:{shift}, altKey:{alt}, metaKey:{meta}}};"
            "   el.dispatchEvent(new KeyboardEvent('keydown', opts));"
            # Backspace + Delete: also mutate value in INPUT/TEXTAREA so it visibly takes effect
            "   if (k === 'Backspace' && (el.tagName==='INPUT' || el.tagName==='TEXTAREA')) {"
            "     const start = el.selectionStart != null ? el.selectionStart : el.value.length;"
            "     const end   = el.selectionEnd   != null ? el.selectionEnd   : start;"
            "     const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')?.set"
            "                 || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value')?.set;"
            "     const cutStart = start === end ? Math.max(0, start - 1) : start;"
            "     const next = el.value.slice(0, cutStart) + el.value.slice(end);"
            "     if (setter) setter.call(el, next); else el.value = next;"
            "     try { el.setSelectionRange(cutStart, cutStart); } catch(e){}"
            "     el.dispatchEvent(new Event('input', {bubbles:true}));"
            "   } else if (k === 'Delete' && (el.tagName==='INPUT' || el.tagName==='TEXTAREA')) {"
            "     const start = el.selectionStart != null ? el.selectionStart : 0;"
            "     const end   = el.selectionEnd   != null ? el.selectionEnd   : start;"
            "     const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value')?.set"
            "                 || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value')?.set;"
            "     const cutEnd = start === end ? Math.min(el.value.length, end + 1) : end;"
            "     const next = el.value.slice(0, start) + el.value.slice(cutEnd);"
            "     if (setter) setter.call(el, next); else el.value = next;"
            "     try { el.setSelectionRange(start, start); } catch(e){}"
            "     el.dispatchEvent(new Event('input', {bubbles:true}));"
            "   } else if (k === 'Enter' && (el.tagName==='INPUT')) {"
            # form submit on Enter in <input>
            "     if (el.form && el.form.requestSubmit) el.form.requestSubmit();"
            "     else if (el.form && el.form.submit) el.form.submit();"
            "   }"
            "   el.dispatchEvent(new KeyboardEvent('keyup', opts));"
            "   return {ok:true, key:k, tag:el.tagName};"
            "})()"
        )
    try:
        r = await call("eval_js", {"code": f"return {code};"})
        return r
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────── paste a whole string (debounced/batched typing) ───────── #
@app.post("/api/paste")
async def paste(req: Request):
    """Body: {text: str} — inserts at the caret of the focused element.
    Uses the CSP-safe `paste_text` tool (not eval_js)."""
    body = await req.json()
    text = body.get("text") or ""
    if not text: return {"ok": True, "inserted": 0}
    try:
        return await call("paste_text", {"text": text})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
