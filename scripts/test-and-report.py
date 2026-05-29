#!/usr/bin/env python3
"""
Exercise every category of tool against a real page and write a self-contained
HTML report (screenshots inlined as base64) to outputs/report.html.

Picks the latest live :browser-controller service from Hypha (or use --service).
"""
import argparse, base64, html, json, os, sys, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime

HYPHA_TOKEN     = os.environ["HYPHA_TOKEN"]
HYPHA_SERVER    = os.environ.get("HYPHA_SERVER_URL", "https://hypha.aicell.io")
HYPHA_WORKSPACE = os.environ["HYPHA_WORKSPACE"]
HEADERS = {"Authorization": f"Bearer {HYPHA_TOKEN}", "Content-Type": "application/json"}


def list_services():
    """List services via hypha CLI (returns full IDs with workspace prefix)."""
    import subprocess
    out = subprocess.check_output(["hypha", "services", "--json"], text=True)
    return json.loads(out)


def find_live_service():
    """Find the latest live :browser-controller service."""
    full = list_services()
    cands = [s for s in full
             if "browser-ext-" in s.get("id","") and s.get("id","").endswith(":browser-controller")]
    for s in reversed(cands):
        sid = s["id"]
        try:
            r = call(sid, "ping", {}, timeout=3)
            if isinstance(r, dict) and r.get("ok"): return sid
        except Exception:
            continue
    return None


def call(service_id, tool, args, timeout=30):
    """Hypha HTTP transport binds body field 'kwargs' into the tool function's
    first positional argument. We always wrap args under 'kwargs'."""
    ws, rest = service_id.split("/", 1)
    url = (f"{HYPHA_SERVER}/{urllib.parse.quote(ws,safe='')}"
           f"/services/{urllib.parse.quote(rest,safe='')}/{tool}?_mode=last")
    body = {"kwargs": args or {}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                  method="POST", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try: return json.loads(raw)
            except Exception: return raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try: return {"_error_http": e.code, "_error_body": json.loads(body)}
        except Exception: return {"_error_http": e.code, "_error_body": body}


# ─────────────────────────── Test plan ─────────────────────────── #

class Run:
    def __init__(self, service_id):
        self.service_id = service_id
        self.steps = []
        self.tab_id = None

    def step(self, category, name, tool, args=None, *, capture_screenshot=False, expect_keys=None):
        args = args or {}
        t0 = time.time()
        try:
            result = call(self.service_id, tool, args)
            elapsed = (time.time() - t0) * 1000
            ok = "_error_http" not in (result if isinstance(result, dict) else {})
            if expect_keys and ok and isinstance(result, dict):
                if not all(k in result for k in expect_keys):
                    ok = False
            entry = {
                "category": category, "name": name, "tool": tool,
                "args": args, "result": result, "elapsed_ms": round(elapsed, 1),
                "ok": ok,
            }
            if capture_screenshot:
                try:
                    shot = call(self.service_id, "screenshot", {"tab_id": self.tab_id} if self.tab_id else {})
                    if isinstance(shot, dict) and shot.get("base64"):
                        entry["screenshot_b64"] = shot["base64"]
                except Exception as e:
                    entry["screenshot_error"] = str(e)
            self.steps.append(entry)
            return result
        except Exception as e:
            self.steps.append({
                "category": category, "name": name, "tool": tool,
                "args": args, "result": {"_exception": str(e)},
                "elapsed_ms": round((time.time() - t0)*1000, 1), "ok": False,
            })
            return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", help="explicit service id; otherwise auto-detect latest live")
    ap.add_argument("--url", default="https://example.com",
                    help="URL to exercise DOM tools against (default: example.com)")
    ap.add_argument("--out", default="outputs/report.html")
    args = ap.parse_args()

    svc = args.service or find_live_service()
    if not svc:
        print("No live :browser-controller service found. Run ./scripts/run.sh first.", file=sys.stderr)
        sys.exit(2)
    print(f"Using service: {svc}", flush=True)
    print(f"Target URL:    {args.url}", flush=True)

    r = Run(svc)

    # ── Meta / health
    r.step("Meta", "Ping", "ping")
    r.step("Meta", "Extension info", "get_extension_info")

    # ── Tab management
    initial = r.step("Tabs", "List tabs (initial)", "list_tabs")
    new_tab = r.step("Tabs", f"Create tab → {args.url}", "create_tab",
                     {"url": args.url, "active": True})
    if isinstance(new_tab, dict) and "id" in new_tab:
        r.tab_id = new_tab["id"]
        r.step("Tabs", "Get active tab", "get_active_tab")

    # ── Navigation
    r.step("Navigation", "Wait for load",     "wait_for_load",
           {"tab_id": r.tab_id, "timeout_ms": 20000})

    # Give the page a moment for any redirects / dynamic content
    time.sleep(1.5)
    r.step("Navigation", "Get page info",     "get_page_info",
           {"tab_id": r.tab_id}, capture_screenshot=True)

    # ── DOM (selector-based)
    r.step("DOM", "Query 'h1'",       "query",        {"selector": "h1", "tab_id": r.tab_id})
    r.step("DOM", "Read text of h1",  "read_text",    {"selector": "h1", "tab_id": r.tab_id})
    r.step("DOM", "Read attr of <html lang>", "read_attribute",
           {"selector": "html", "attr": "lang", "tab_id": r.tab_id})
    r.step("DOM", "Get outer HTML (first 1000 chars)", "get_html",
           {"selector": None, "tab_id": r.tab_id})

    # ── JS execution
    r.step("JS", "eval: document.title",   "eval_js",
           {"code": "return document.title", "tab_id": r.tab_id})
    r.step("JS", "eval: link count",       "eval_js",
           {"code": "return document.querySelectorAll('a').length", "tab_id": r.tab_id})
    r.step("JS", "eval: viewport (object)", "eval_js",
           {"code": "return {w: innerWidth, h: innerHeight, dpr: devicePixelRatio}",
            "tab_id": r.tab_id})

    # ── Smart DOM (indexed)
    r.step("Smart DOM", "get_browser_state (viewport)",  "get_browser_state",
           {"tab_id": r.tab_id, "viewport_only": True})
    r.step("Smart DOM", "get_browser_state (entire pg)", "get_browser_state",
           {"tab_id": r.tab_id, "viewport_only": False})

    # ── Cookies
    r.step("Cookies", f"List cookies on {args.url}", "get_cookies", {"url": args.url})

    # ── Interaction: open google.com in a separate tab and interact
    g_tab = r.step("Interaction", "Create tab → google.com",
                   "create_tab", {"url": "https://www.google.com", "active": True})
    g_tab_id = g_tab["id"] if isinstance(g_tab, dict) and "id" in g_tab else None
    if g_tab_id:
        r.step("Interaction", "Wait for google to load", "wait_for_load",
               {"tab_id": g_tab_id, "timeout_ms": 20000})
        time.sleep(2)
        r.step("Interaction", "Screenshot of google.com",
               "screenshot", {"tab_id": g_tab_id}, capture_screenshot=False)  # the call IS the screenshot
        # Try to fill the search box
        r.step("Interaction", "Type into search box (textarea[name=q])", "fill",
               {"selector": "textarea[name=q]", "value": "hypha rpc browser automation",
                "tab_id": g_tab_id})
        time.sleep(0.5)
        r.step("Interaction", "Read back search value", "eval_js",
               {"code": "return document.querySelector('textarea[name=q]')?.value",
                "tab_id": g_tab_id})
        # Press enter to submit
        r.step("Interaction", "Press Enter", "press_key",
               {"key": "Enter", "selector": "textarea[name=q]", "tab_id": g_tab_id})
        r.step("Interaction", "Wait for results page", "wait_for_load",
               {"tab_id": g_tab_id, "timeout_ms": 15000})
        time.sleep(1.5)
        r.step("Interaction", "Page info after search", "get_page_info",
               {"tab_id": g_tab_id}, capture_screenshot=True)
        r.step("Interaction", "Read first result", "read_text",
               {"selector": "h3", "tab_id": g_tab_id})

    # ── Cleanup
    if r.tab_id:
        r.step("Cleanup", f"Close tab {r.tab_id}", "close_tab", {"tab_id": r.tab_id})
    if g_tab_id:
        r.step("Cleanup", f"Close google tab {g_tab_id}", "close_tab", {"tab_id": g_tab_id})

    # ─────────────── Render report ─────────────── #
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    write_html(r, args.url, out_path)
    print(f"\n✓ Report written to: {out_path}")
    print(f"  (open in a browser to view)")


def write_html(run: Run, target_url: str, out_path: str):
    total = len(run.steps)
    passed = sum(1 for s in run.steps if s["ok"])
    failed = total - passed
    by_cat = {}
    for s in run.steps:
        by_cat.setdefault(s["category"], []).append(s)

    def render_value(v, depth=0):
        if v is None: return "<em>null</em>"
        if isinstance(v, bool): return f"<code>{str(v).lower()}</code>"
        if isinstance(v, (int, float)): return f"<code>{v}</code>"
        if isinstance(v, str):
            shown = v if len(v) < 600 else v[:600] + f"…<span class=truncnote>({len(v)} chars)</span>"
            return f"<code>{html.escape(shown)}</code>"
        if isinstance(v, (list, dict)):
            return f"<pre>{html.escape(json.dumps(v, indent=2)[:1500])}</pre>"
        return html.escape(str(v))

    rows = []
    for cat, items in by_cat.items():
        ok_in_cat = sum(1 for s in items if s["ok"])
        rows.append(f"""
<section>
  <h2>{html.escape(cat)} <small>({ok_in_cat}/{len(items)} ok)</small></h2>
  <table>
    <thead><tr><th>Step</th><th>Tool</th><th>Args</th><th>Result</th><th>Time</th><th>OK</th></tr></thead>
    <tbody>""")
        for s in items:
            row_class = "ok" if s["ok"] else "fail"
            shot = ""
            if s.get("screenshot_b64"):
                shot = f"""<details><summary>📸 screenshot</summary>
                <img class="shot" src="data:image/png;base64,{s["screenshot_b64"]}" /></details>"""
            rows.append(f"""
      <tr class="{row_class}">
        <td><strong>{html.escape(s["name"])}</strong>{shot}</td>
        <td><code>{html.escape(s["tool"])}</code></td>
        <td>{render_value(s["args"])}</td>
        <td>{render_value(s["result"])}</td>
        <td class="t">{s["elapsed_ms"]:.0f} ms</td>
        <td class="s">{"✓" if s["ok"] else "✗"}</td>
      </tr>""")
        rows.append("</tbody></table></section>")
    sections = "\n".join(rows)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pct = (passed/total*100) if total else 0

    html_out = f"""<!doctype html>
<html><head><meta charset=utf-8><title>hypha-browser-use — test report</title>
<style>
  :root {{ color-scheme: light dark; --ok:#16a34a; --fail:#dc2626; --muted:#666; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.45 -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 30px auto; padding: 0 16px; }}
  header {{ border-bottom: 1px solid #00000022; padding-bottom: 14px; margin-bottom: 18px; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: var(--muted); font-size: 13px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-weight: 600; font-size: 12px; margin-right: 6px; }}
  .badge.ok   {{ background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }}
  .badge.fail {{ background: color-mix(in srgb, var(--fail) 18%, transparent); color: var(--fail); }}
  section {{ margin: 28px 0; }}
  h2 {{ font-size: 17px; margin: 0 0 8px; }}
  h2 small {{ color: var(--muted); font-weight: 400; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; vertical-align: top; border-bottom: 1px solid #00000010; }}
  th {{ background: #00000008; font-weight: 600; }}
  tr.ok td.s   {{ color: var(--ok); font-weight: 700; }}
  tr.fail td.s {{ color: var(--fail); font-weight: 700; }}
  tr.fail {{ background: color-mix(in srgb, var(--fail) 5%, transparent); }}
  code {{ background: #00000008; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  pre  {{ background: #00000008; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 12px; margin: 4px 0; }}
  details {{ margin-top: 4px; }}
  details summary {{ cursor: pointer; color: var(--muted); }}
  .shot {{ max-width: 100%; height: auto; border: 1px solid #00000022; border-radius: 4px; margin-top: 6px; }}
  .truncnote {{ color: var(--muted); font-style: italic; }}
  .t {{ white-space: nowrap; color: var(--muted); }}
  .s {{ text-align: center; }}
  footer {{ margin-top: 40px; padding-top: 14px; border-top: 1px solid #00000022; color: var(--muted); font-size: 12px; }}
</style></head><body>
<header>
  <h1>hypha-browser-use — test report</h1>
  <div class="meta">
    <span class="badge ok">✓ {passed} passed</span>
    <span class="badge fail">✗ {failed} failed</span>
    <span>{passed}/{total} ({pct:.0f}%)</span> · generated {ts}
  </div>
  <div class="meta" style="margin-top:6px">
    Service: <code>{html.escape(run.service_id)}</code><br>
    Target page: <code>{html.escape(target_url)}</code>
  </div>
</header>
{sections}
<footer>
  <p>This report was generated by <code>scripts/test-and-report.py</code> and exercises the
  hypha-browser-use Hypha service over its HTTPS RPC endpoint. Every cell above represents one
  HTTPS call to a Chrome extension running in real Chrome stable. Each screenshot is captured by
  the <code>screenshot</code> tool (which uses <code>chrome.tabs.captureVisibleTab</code>) and
  inlined as base64 — the report is fully self-contained, no external assets.</p>
</footer>
</body></html>"""
    with open(out_path, "w") as f:
        f.write(html_out)


if __name__ == "__main__":
    main()
