// background.js — service worker for Hypha Browser Use
// Registers a Hypha RPC service that exposes chrome.* APIs as tools.

import { hyphaWebsocketClient, connectToServerHTTP, schemaFunction } from "./lib/hypha-rpc.mjs";
import { HYPHA_CONFIG } from "./config.js";

// MV3 service workers terminate ~30s idle; WebSocket connect can hang on the
// auth handshake in this environment. We default to the HTTP-streaming
// transport (Server-Sent Events for downlink + POST for uplink) which is
// well-behaved in SW context. Override per build via HYPHA_CONFIG.transport.
const TRANSPORT = (HYPHA_CONFIG.transport || "http").toLowerCase();

const VERSION = chrome.runtime.getManifest().version;

let server = null;
let svcInfo = null;
let connectAttempts = 0;
let lastError = null;

// ─────────────────────────── helpers ─────────────────────────── //

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function setStatus(patch) {
  const cur = (await chrome.storage.local.get("status")).status || {};
  await chrome.storage.local.set({ status: { ...cur, ...patch, updatedAt: Date.now() } });
}

async function resolveTabId(tabId) {
  if (tabId != null) return tabId;
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab");
  return tab.id;
}

async function waitForTabComplete(tabId, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const t = await chrome.tabs.get(tabId);
    if (t.status === "complete") return t;
    await sleep(120);
  }
  throw new Error(`wait_for_load: timeout after ${timeoutMs}ms`);
}

// Run a function in the page main world via chrome.scripting.executeScript.
// Returns the function's return value from the top frame.
async function pageEval(tabId, func, args = []) {
  const [frame] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func,
    args,
  });
  if (!frame) throw new Error("executeScript: no frame result");
  return frame.result;
}

async function pageEvalIsolated(tabId, func, args = []) {
  const [frame] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func,
    args,
  });
  if (!frame) throw new Error("executeScript: no frame result");
  return frame.result;
}

function summarizeTab(t) {
  return {
    id: t.id,
    window_id: t.windowId,
    index: t.index,
    url: t.url,
    title: t.title,
    active: t.active,
    pinned: t.pinned,
    audible: t.audible,
    muted: t.mutedInfo?.muted ?? false,
    status: t.status,
    incognito: t.incognito,
  };
}

function summarizeWindow(w) {
  return {
    id: w.id,
    focused: w.focused,
    state: w.state,
    type: w.type,
    tab_count: w.tabs?.length ?? 0,
    tabs: (w.tabs || []).map(summarizeTab),
  };
}

// ────────────────────────── tool impls ───────────────────────── //
// Each tool is a plain async fn. They will be wrapped with schemas
// when we registerService below.

// ── meta / health
async function ping() {
  return { ok: true, version: VERSION, time: new Date().toISOString() };
}
async function get_extension_info() {
  const manifest = chrome.runtime.getManifest();
  return {
    name: manifest.name,
    version: manifest.version,
    extension_id: chrome.runtime.id,
    service_id: HYPHA_CONFIG.service_id,
    workspace: HYPHA_CONFIG.workspace,
    server_url: HYPHA_CONFIG.server_url,
  };
}
async function notify_user(message, level = "info") {
  console.log(`[hypha-browser-use:${level}] ${message}`);
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon-128.png"),
      title: `Hypha Agent (${level})`,
      message: String(message),
      priority: level === "error" ? 2 : 1,
    });
  } catch (e) {
    // notifications.create can throw if icon missing — non-fatal
  }
  return { ok: true };
}

// ── tabs
async function list_tabs({ window_id = null, current_window = false } = {}) {
  const query = {};
  if (window_id != null) query.windowId = window_id;
  if (current_window) query.lastFocusedWindow = true;
  const tabs = await chrome.tabs.query(query);
  return tabs.map(summarizeTab);
}
async function get_active_tab() {
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return t ? summarizeTab(t) : null;
}
async function create_tab({ url = "about:blank", window_id = null, active = true } = {}) {
  const props = { url, active };
  if (window_id != null) props.windowId = window_id;
  const t = await chrome.tabs.create(props);
  return summarizeTab(t);
}
async function close_tab({ tab_id }) {
  await chrome.tabs.remove(tab_id);
  return { ok: true };
}
async function activate_tab({ tab_id }) {
  const t = await chrome.tabs.update(tab_id, { active: true });
  await chrome.windows.update(t.windowId, { focused: true });
  return summarizeTab(t);
}
async function duplicate_tab({ tab_id }) {
  const t = await chrome.tabs.duplicate(tab_id);
  return summarizeTab(t);
}

// ── windows
async function list_windows() {
  const wins = await chrome.windows.getAll({ populate: true });
  return wins.map(summarizeWindow);
}
async function create_window({ url = null, type = "normal", incognito = false } = {}) {
  const props = { type, incognito };
  if (url) props.url = url;
  const w = await chrome.windows.create(props);
  return summarizeWindow(w);
}
async function close_window({ window_id }) {
  await chrome.windows.remove(window_id);
  return { ok: true };
}
async function focus_window({ window_id }) {
  const w = await chrome.windows.update(window_id, { focused: true });
  return summarizeWindow(w);
}

// ── navigation
async function navigate({ url, tab_id = null, wait = true, timeout_ms = 30000 }) {
  const id = await resolveTabId(tab_id);
  await chrome.tabs.update(id, { url });
  if (wait) await waitForTabComplete(id, timeout_ms);
  return summarizeTab(await chrome.tabs.get(id));
}
async function go_back({ tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  await chrome.tabs.goBack(id);
  return { ok: true };
}
async function go_forward({ tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  await chrome.tabs.goForward(id);
  return { ok: true };
}
async function reload({ tab_id = null, bypass_cache = false } = {}) {
  const id = await resolveTabId(tab_id);
  await chrome.tabs.reload(id, { bypassCache: bypass_cache });
  return { ok: true };
}
async function wait_for_load({ tab_id = null, timeout_ms = 30000 } = {}) {
  const id = await resolveTabId(tab_id);
  const t = await waitForTabComplete(id, timeout_ms);
  return summarizeTab(t);
}

// ── page state
async function get_page_info({ tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  const info = await pageEval(id, () => ({
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    viewport: { width: innerWidth, height: innerHeight },
    scroll: { x: scrollX, y: scrollY, max_y: document.documentElement.scrollHeight },
    user_agent: navigator.userAgent,
  }));
  return info;
}
async function get_html({ selector = null, tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const root = sel ? document.querySelector(sel) : document.documentElement;
    return root ? root.outerHTML : null;
  }, [selector]);
}
async function get_text({ selector = null, tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const root = sel ? document.querySelector(sel) : document.body;
    return root ? (root.innerText || root.textContent || "") : null;
  }, [selector]);
}
async function screenshot({ tab_id = null, format = "png", quality = 90 } = {}) {
  let id = tab_id;
  if (id != null) await activate_tab({ tab_id: id });
  else id = (await get_active_tab())?.id;
  const t = await chrome.tabs.get(id);
  const dataUrl = await chrome.tabs.captureVisibleTab(t.windowId, { format, quality });
  // Return as base64 (strip data: prefix) + meta
  const b64 = dataUrl.replace(/^data:image\/\w+;base64,/, "");
  return { format, base64: b64, bytes: Math.floor(b64.length * 0.75), tab_id: id };
}

// ── DOM interaction (selector-based)
async function query({ selector, tab_id = null, limit = 50 }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel, lim) => {
    const out = [];
    const els = document.querySelectorAll(sel);
    for (let i = 0; i < els.length && i < lim; i++) {
      const el = els[i];
      const rect = el.getBoundingClientRect();
      const attrs = {};
      for (const a of el.attributes) attrs[a.name] = a.value;
      out.push({
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.textContent || "").slice(0, 200),
        attrs,
        bbox: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
        visible: rect.width > 0 && rect.height > 0,
      });
    }
    return { count: els.length, returned: out.length, items: out };
  }, [selector, limit]);
}
async function click({ selector, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, error: "not found" };
    el.scrollIntoView({ block: "center", inline: "center" });
    el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    if (typeof el.focus === "function") el.focus();
    el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    el.click();
    return { ok: true };
  }, [selector]);
}
async function fill({ selector, value, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel, val) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, error: "not found" };
    el.scrollIntoView({ block: "center" });
    el.focus();
    if (el.isContentEditable) {
      el.textContent = val;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: val }));
      return { ok: true };
    }
    // React-friendly value set
    const setter =
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set ||
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(el, val);
    else el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }, [selector, value]);
}
async function select_option({ selector, value, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel, val) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, error: "not found" };
    if (el.tagName !== "SELECT") return { ok: false, error: "not a <select>" };
    let matched = false;
    for (const opt of el.options) {
      if (opt.value === val || opt.text.trim() === val) {
        el.value = opt.value;
        matched = true;
        break;
      }
    }
    if (!matched) return { ok: false, error: "option not found", value: val };
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, value: el.value };
  }, [selector, value]);
}
async function scroll({ direction = "down", amount = null, selector = null, tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (dir, amt, sel) => {
    const px = amt ?? (dir === "left" || dir === "right" ? innerWidth * 0.8 : innerHeight * 0.8);
    const dx = dir === "left" ? -px : dir === "right" ? px : 0;
    const dy = dir === "up" ? -px : dir === "down" ? px : 0;
    if (sel) {
      const el = document.querySelector(sel);
      if (!el) return { ok: false, error: "not found" };
      el.scrollBy({ left: dx, top: dy, behavior: "smooth" });
    } else {
      window.scrollBy({ left: dx, top: dy, behavior: "smooth" });
    }
    return { ok: true, dx, dy };
  }, [direction, amount, selector]);
}
async function scroll_to({ selector, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, error: "not found" };
    el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    return { ok: true };
  }, [selector]);
}
async function focus_element({ selector, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, error: "not found" };
    if (typeof el.focus === "function") el.focus();
    return { ok: true };
  }, [selector]);
}
async function press_key({ key, modifiers = [], selector = null, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (k, mods, sel) => {
    const target = sel ? document.querySelector(sel) : (document.activeElement || document.body);
    if (!target) return { ok: false, error: "no target" };
    const opts = {
      key: k,
      bubbles: true,
      cancelable: true,
      ctrlKey: mods.includes("ctrl"),
      shiftKey: mods.includes("shift"),
      altKey: mods.includes("alt"),
      metaKey: mods.includes("meta"),
    };
    target.dispatchEvent(new KeyboardEvent("keydown", opts));
    target.dispatchEvent(new KeyboardEvent("keypress", opts));
    target.dispatchEvent(new KeyboardEvent("keyup", opts));
    return { ok: true };
  }, [key, modifiers, selector]);
}
async function read_text({ selector, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return el.innerText || el.textContent || "";
  }, [selector]);
}
async function read_attribute({ selector, attr, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel, a) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return el.getAttribute(a);
  }, [selector, attr]);
}
async function wait_for_selector({ selector, timeout_ms = 10000, visible = true, tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  const t0 = Date.now();
  while (Date.now() - t0 < timeout_ms) {
    const found = await pageEval(id, (sel, vis) => {
      const el = document.querySelector(sel);
      if (!el) return false;
      if (!vis) return true;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }, [selector, visible]);
    if (found) return { ok: true, elapsed_ms: Date.now() - t0 };
    await sleep(200);
  }
  return { ok: false, error: "timeout", elapsed_ms: Date.now() - t0 };
}

// ── arbitrary JS
async function eval_js({ code, tab_id = null, world = "MAIN" }) {
  const id = await resolveTabId(tab_id);
  // We wrap user code in a function so it can `return` a value.
  const wrappedFn = world === "ISOLATED" ? pageEvalIsolated : pageEval;
  return await wrappedFn(id, (src) => {
    try {
      const fn = new Function(src);
      const result = fn();
      // best-effort serialization
      if (result === undefined) return { ok: true, value: null };
      try {
        JSON.stringify(result);
        return { ok: true, value: result };
      } catch {
        return { ok: true, value: String(result) };
      }
    } catch (e) {
      return { ok: false, error: String(e), stack: e.stack };
    }
  }, [code]);
}

// ── smart DOM (lightweight indexed interactive elements, browser-use style)
async function get_browser_state({ tab_id = null, viewport_only = true } = {}) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (vpOnly) => {
    const isInteractive = (el) => {
      const tag = el.tagName.toLowerCase();
      if (["a", "button", "input", "select", "textarea", "summary", "label"].includes(tag)) return true;
      if (el.isContentEditable) return true;
      if (el.hasAttribute("tabindex") && el.getAttribute("tabindex") !== "-1") return true;
      const role = el.getAttribute("role");
      if (role && ["button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "textbox", "combobox", "option"].includes(role)) return true;
      const cs = getComputedStyle(el);
      if (cs.cursor === "pointer") return true;
      return false;
    };
    const isVisible = (el) => {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) return false;
      return true;
    };
    const inViewport = (el) => {
      const r = el.getBoundingClientRect();
      return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    };
    const elements = [];
    const all = document.querySelectorAll("*");
    let idx = 0;
    for (const el of all) {
      if (!isInteractive(el)) continue;
      if (!isVisible(el)) continue;
      if (vpOnly && !inViewport(el)) continue;
      const r = el.getBoundingClientRect();
      const attrs = {};
      for (const a of el.attributes) {
        if (["id", "name", "class", "type", "value", "placeholder", "href", "role", "aria-label", "title", "alt"].includes(a.name)) {
          attrs[a.name] = a.value;
        }
      }
      elements.push({
        index: idx++,
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.textContent || "").trim().slice(0, 160),
        attrs,
        bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
        in_viewport: inViewport(el),
      });
      el.dataset.__hyphaIdx = String(idx - 1);
    }
    return {
      url: location.href,
      title: document.title,
      viewport: { width: innerWidth, height: innerHeight },
      scroll: { x: scrollX, y: scrollY, max_y: document.documentElement.scrollHeight },
      elements,
      count: elements.length,
    };
  }, [viewport_only]);
}
async function click_by_index({ index, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (i) => {
    const el = document.querySelector(`[data-__hypha-idx="${i}"]`) ||
               document.querySelector(`[data-__hyphaIdx="${i}"]`);
    // dataset key __hyphaIdx renders in DOM as data-__hypha-idx
    const finalEl = el || [...document.querySelectorAll("*")].find((e) => e.dataset?.__hyphaIdx === String(i));
    if (!finalEl) return { ok: false, error: "index not found — call get_browser_state first" };
    finalEl.scrollIntoView({ block: "center", inline: "center" });
    finalEl.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    finalEl.click();
    return { ok: true };
  }, [index]);
}
async function input_by_index({ index, text, tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (i, val) => {
    const finalEl = [...document.querySelectorAll("*")].find((e) => e.dataset?.__hyphaIdx === String(i));
    if (!finalEl) return { ok: false, error: "index not found — call get_browser_state first" };
    finalEl.scrollIntoView({ block: "center" });
    finalEl.focus?.();
    if (finalEl.isContentEditable) {
      finalEl.textContent = val;
      finalEl.dispatchEvent(new InputEvent("input", { bubbles: true, data: val }));
      return { ok: true };
    }
    const setter =
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set ||
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(finalEl, val);
    else finalEl.value = val;
    finalEl.dispatchEvent(new Event("input", { bubbles: true }));
    finalEl.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }, [index, text]);
}

// ── cookies
async function get_cookies({ url }) {
  return await chrome.cookies.getAll({ url });
}
async function delete_cookie({ url, name }) {
  return await chrome.cookies.remove({ url, name });
}

// ── downloads
async function download({ url, filename = null, save_as = false }) {
  const opts = { url, saveAs: save_as };
  if (filename) opts.filename = filename;
  const id = await chrome.downloads.download(opts);
  return { download_id: id };
}
async function list_downloads({ state = null, limit = 50 } = {}) {
  const q = { limit, orderBy: ["-startTime"] };
  if (state) q.state = state;
  return await chrome.downloads.search(q);
}
async function wait_for_download({ download_id, timeout_ms = 60000 }) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout_ms) {
    const [item] = await chrome.downloads.search({ id: download_id });
    if (!item) return { ok: false, error: "not_found" };
    if (item.state === "complete") return { ok: true, ...item };
    if (item.state === "interrupted") return { ok: false, ...item };
    await sleep(500);
  }
  return { ok: false, error: "timeout" };
}

// ── file upload (decode base64 → set input.files)
async function upload_file({ selector, file_b64, filename, mime_type = "application/octet-stream", tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (sel, b64, name, mime) => {
    const el = document.querySelector(sel);
    if (!el || el.tagName !== "INPUT" || el.type !== "file") {
      return { ok: false, error: "not a file input" };
    }
    const bin = atob(b64);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    const file = new File([u8], name, { type: mime });
    const dt = new DataTransfer();
    dt.items.add(file);
    el.files = dt.files;
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, file: { name, size: u8.length, type: mime } };
  }, [selector, file_b64, filename, mime_type]);
}

// ── cookies (set; companion to get/delete)
async function set_cookie({ url, name, value, domain = null, path = "/", secure = true, http_only = false, same_site = "lax", expires_at = null }) {
  const props = { url, name, value, path, secure, httpOnly: http_only, sameSite: same_site };
  if (domain) props.domain = domain;
  if (expires_at) props.expirationDate = expires_at;
  return await chrome.cookies.set(props);
}

// ── dialog handler (auto-accept/dismiss alert/confirm/prompt)
async function set_dialog_handler({ mode = "auto-accept", prompt_response = "", tab_id = null }) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, (m, pr) => {
    const accept = m === "auto-accept";
    window.alert = () => {};
    window.confirm = () => accept;
    window.prompt = () => accept ? pr : null;
    return { ok: true, mode: m };
  }, [mode, prompt_response]);
}

// ── PDF (via window.print → save shortcut not feasible; use eval to get full HTML)
async function get_outer_html({ tab_id = null } = {}) {
  const id = await resolveTabId(tab_id);
  return await pageEval(id, () => document.documentElement.outerHTML);
}

// ────────────────────────── Hypha glue ───────────────────────── //

const TOOL_TABLE = {
  ping, get_extension_info, notify_user,
  list_tabs, get_active_tab, create_tab, close_tab, activate_tab, duplicate_tab,
  list_windows, create_window, close_window, focus_window,
  navigate, go_back, go_forward, reload, wait_for_load,
  get_page_info, get_html, get_text, screenshot,
  query, click, fill, select_option, scroll, scroll_to,
  focus_element, press_key, read_text, read_attribute, wait_for_selector,
  eval_js,
  get_browser_state, click_by_index, input_by_index,
  get_cookies, set_cookie, delete_cookie,
  download, list_downloads, wait_for_download,
  upload_file, set_dialog_handler, get_outer_html,
};

// Normalize arguments coming over Hypha RPC. The HTTP transport may pass:
//   • fn({tab_id: 123, url: "..."})          ← what we want
//   • fn([{tab_id: 123, url: "..."}])        ← wrapped in a list (some clients)
//   • fn(val1, val2, ...)                    ← positional spread (rare)
// We coerce to a single object so per-tool destructuring just works.
function normalizeArgs(rawArgs) {
  if (!rawArgs || rawArgs.length === 0) return {};
  if (rawArgs.length === 1) {
    let a = rawArgs[0];
    if (Array.isArray(a) && a.length === 1 && a[0] && typeof a[0] === "object") a = a[0];
    if (a === null || a === undefined) return {};
    return (typeof a === "object" && !Array.isArray(a)) ? a : { _value: a };
  }
  // multiple positional args → leave first if it's an object, else wrap
  return (typeof rawArgs[0] === "object" && !Array.isArray(rawArgs[0])) ? rawArgs[0] : { _positional: rawArgs };
}

// Hypha HTTP transport binds JSON body fields to function positional args BY
// schema property order. Plain async functions get called with zero args (body
// is dropped). Our tools take a single destructured object — easiest is to
// declare one schema property `kwargs` and have the caller send `{"kwargs":{...}}`.
function wrap(name, fn) {
  const inner = async (kwargs) => {
    const args = (kwargs && typeof kwargs === "object" && !Array.isArray(kwargs)) ? kwargs : {};
    const t0 = performance.now();
    try {
      const out = await fn(args);
      const dt = (performance.now() - t0).toFixed(0);
      console.log(`[tool] ${name} ok (${dt}ms)`, args);
      return out;
    } catch (e) {
      const dt = (performance.now() - t0).toFixed(0);
      console.warn(`[tool] ${name} fail (${dt}ms)`, args, e);
      throw e;
    }
  };
  return schemaFunction(inner, {
    name,
    description: `Tool ${name} — pass arguments under "kwargs" key in the request body, e.g. {"kwargs":{"tab_id":42,"url":"https://example.com"}}`,
    parameters: {
      type: "object",
      properties: {
        kwargs: { type: "object", additionalProperties: true,
                  description: "Tool arguments (URL, selector, tab_id, etc.)" }
      },
    },
  });
}

async function connect() {
  connectAttempts++;
  await setStatus({ state: "connecting", attempt: connectAttempts });
  try {
    const cfg = {
      server_url: HYPHA_CONFIG.server_url,
      workspace: HYPHA_CONFIG.workspace,
      token: HYPHA_CONFIG.token,
      client_id: `browser-ext-${Math.random().toString(36).slice(2, 10)}`,
    };
    console.log(`[hypha-browser-use] connecting via ${TRANSPORT}`);
    server = TRANSPORT === "ws"
      ? await hyphaWebsocketClient.connectToServer(cfg)
      : await connectToServerHTTP(cfg);

    const svcDef = {
      id: HYPHA_CONFIG.service_id,
      name: HYPHA_CONFIG.service_name,
      type: "browser-controller",
      description: "Remote-controllable browser via Hypha RPC. Drives any tab/window in the host Chrome.",
      config: { visibility: HYPHA_CONFIG.visibility || "public" },
    };
    for (const [name, fn] of Object.entries(TOOL_TABLE)) {
      svcDef[name] = wrap(name, fn);
    }
    svcInfo = await server.registerService(svcDef);

    const httpBase = `${HYPHA_CONFIG.server_url}/${HYPHA_CONFIG.workspace}/services/${HYPHA_CONFIG.service_id}`;
    await setStatus({
      state: "connected",
      service_id: svcInfo?.id ?? HYPHA_CONFIG.service_id,
      workspace: HYPHA_CONFIG.workspace,
      http_base: httpBase,
      connected_at: Date.now(),
      error: null,
    });
    console.log("[hypha-browser-use] service registered", svcInfo);
    lastError = null;
  } catch (e) {
    lastError = e?.message || String(e);
    await setStatus({ state: "error", error: lastError });
    console.error("[hypha-browser-use] connect failed", e);
    throw e;
  }
}

async function ensureConnected() {
  if (server && svcInfo) return;
  try {
    await connect();
  } catch (e) {
    // retry later
    setTimeout(() => ensureConnected().catch(() => {}), Math.min(60000, 2000 * connectAttempts));
  }
}

// ─────────────── service worker lifecycle wiring ─────────────── //

chrome.runtime.onInstalled.addListener(() => {
  console.log("[hypha-browser-use] onInstalled");
  ensureConnected();
});
chrome.runtime.onStartup.addListener(() => {
  console.log("[hypha-browser-use] onStartup");
  ensureConnected();
});

// Keep service worker alive while we expect to be reachable.
// (MV3 SW is killed after ~30s idle; an alarm wakes it up.)
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "keepalive") {
    // Touch storage so SW stays warm
    await chrome.storage.local.set({ keepalive_ts: Date.now() });
    if (!server || !svcInfo) ensureConnected();
  }
});

// Trigger connection on load too (module SWs run top-level on wake).
ensureConnected();

// Expose a small message API for the popup
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "get_status") {
    chrome.storage.local.get("status").then((r) => sendResponse(r.status || {}));
    return true;
  }
  if (msg?.type === "reconnect") {
    server = null; svcInfo = null;
    ensureConnected().then(() => sendResponse({ ok: true })).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  return false;
});
