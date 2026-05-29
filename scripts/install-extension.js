#!/usr/bin/env node
/**
 * Install an unpacked extension into Chrome stable on macOS by calling
 * `Extensions.loadUnpacked` via CDP over `--remote-debugging-pipe`.
 *
 * `--load-extension` was removed from branded Chrome stable in 2025; this is
 * the official replacement that still works in real Chrome 137+.
 *
 * Node is used (instead of Python) because Node's `child_process.spawn`
 * supports `stdio: ['inherit','inherit','inherit','pipe','pipe']` natively —
 * letting Chrome see our pipes at FDs 3/4 without any preexec/dup2 hacks.
 *
 * Usage:
 *   node scripts/install-extension.js [--profile DIR] [--ext-path DIR]
 *                                     [--quit-running] [--no-restore]
 *                                     [--keep-running|--no-keep-running]
 */

const fs = require("fs");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const DEFAULTS = {
  profile: path.join(process.env.HOME, ".hypha-browser-use", "profile"),
  ext: path.join(ROOT, "extension"),
  chrome: process.env.CHROME_BINARY ||
          "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
};
const PID_FILE = path.join(ROOT, ".chrome.pid");
const LOG_FILE = path.join(ROOT, ".chrome.log");

const args = process.argv.slice(2);
const flag = (n) => args.includes(n);
const opt  = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i+1] : d; };

const profile     = path.resolve(opt("--profile",  DEFAULTS.profile));
const extPath     = path.resolve(opt("--ext-path", DEFAULTS.ext));
const chrome      = opt("--chrome", DEFAULTS.chrome);
const quitRunning = flag("--quit-running");
const noRestore   = flag("--no-restore");
const keepRunning = !flag("--no-keep-running");

for (const p of [chrome]) {
  if (!fs.existsSync(p)) { console.error(`Not found: ${p}`); process.exit(2); }
}
if (!fs.statSync(extPath).isDirectory()) {
  console.error(`Not a directory: ${extPath}`); process.exit(2);
}
fs.mkdirSync(profile, { recursive: true });

// ── Optionally quit user's running Chrome ────────────────────────────── //
if (quitRunning) {
  console.error("Quitting any running Google Chrome (saves session)...");
  try {
    spawnSync("osascript", ["-e", 'tell application "Google Chrome" to quit'],
              { timeout: 10000 });
  } catch (_) {}
  // Wait up to 10s for shutdown
  for (let i = 0; i < 40; i++) {
    const r = spawnSync("pgrep", ["-fl", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]);
    if (!r.stdout.toString().includes("Google Chrome.app/Contents/MacOS")) break;
    spawnSync("sleep", ["0.25"]);
  }
}

// ── Spawn Chrome with pipes at FDs 3/4 ───────────────────────────────── //
const headless = flag("--headless");
const chromeArgs = [
  "--enable-unsafe-extension-debugging",
  "--remote-debugging-pipe",
  "--no-first-run",
  "--no-default-browser-check",
  `--user-data-dir=${profile}`,
  ...(headless ? ["--headless=new", "--window-size=1440,900"] : []),
  ...(noRestore ? [] : ["--restore-last-session"]),
  "about:blank",
];

const logFh = fs.openSync(LOG_FILE, "a");
console.error(`Launching: ${chrome} ${chromeArgs.join(" ")}`);
const proc = spawn(chrome, chromeArgs, {
  // FD 0 = ignored, FD 1/2 = log, FD 3 = pipe IN, FD 4 = pipe OUT
  // Chrome writes its CDP messages to FD 4 (we read them), Chrome reads
  // commands from FD 3 (we write them).
  stdio: ["ignore", logFh, logFh, "pipe", "pipe"],
  detached: false,
});
proc.on("error", (e) => { console.error("spawn error:", e); process.exit(3); });
fs.writeFileSync(PID_FILE, String(proc.pid));
console.error(`Chrome PID: ${proc.pid}`);

const writePipe = proc.stdio[3];  // we write -> Chrome reads
const readPipe  = proc.stdio[4];  // Chrome writes -> we read
if (!writePipe || !readPipe) {
  console.error("stdio[3]/[4] not available — node version too old?"); process.exit(3);
}

// ── NUL-delimited JSON-RPC framing ───────────────────────────────────── //
let nextId = 0;
const pending = new Map();
let recvBuf = Buffer.alloc(0);
const onError = (label) => (e) => { console.error(`[pipe ${label}]`, e?.code || e); };
writePipe.on("error", onError("write"));
readPipe.on("error",  onError("read"));
readPipe.on("data", (chunk) => {
  recvBuf = Buffer.concat([recvBuf, chunk]);
  let idx;
  while ((idx = recvBuf.indexOf(0)) >= 0) {
    const raw = recvBuf.slice(0, idx);
    recvBuf = recvBuf.slice(idx + 1);
    if (!raw.length) continue;
    let obj;
    try { obj = JSON.parse(raw.toString("utf8")); }
    catch (e) { console.error("bad msg:", e); continue; }
    if (obj.id != null && pending.has(obj.id)) {
      const { resolve } = pending.get(obj.id);
      pending.delete(obj.id);
      resolve(obj);
    }
  }
});
readPipe.on("close", () => {
  for (const { reject } of pending.values()) reject(new Error("pipe closed"));
});

function call(method, params = {}, timeoutMs = 20000) {
  const id = ++nextId;
  const msg = JSON.stringify({ id, method, params });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, timeoutMs);
    pending.set(id, {
      resolve: (r) => { clearTimeout(timer); resolve(r); },
      reject:  (e) => { clearTimeout(timer); reject(e); },
    });
    writePipe.write(msg + "\0");
  });
}

// ── Drive the install ────────────────────────────────────────────────── //
(async () => {
  // Probe Browser.getVersion
  let v;
  for (let i = 0; i < 30; i++) {
    try { v = await call("Browser.getVersion", {}, 1500); break; }
    catch (_) { await new Promise(r => setTimeout(r, 200)); }
  }
  if (!v || !v.result) { console.error("Chrome did not respond"); process.exit(4); }
  console.error(`Connected: ${v.result.product} (rev ${v.result.revision?.slice(0,7) || "?"})`);

  console.error(`Calling Extensions.loadUnpacked(${extPath}) ...`);
  const r = await call("Extensions.loadUnpacked", { path: extPath });
  if (r.error) { console.error("loadUnpacked FAILED:", r.error); process.exit(5); }
  const extId = r.result.id;
  console.log(extId);  // stdout = just the extension id, for piping
  console.error(`✓ Extension loaded: id=${extId}`);
  fs.writeFileSync(path.join(ROOT, ".extension.id"), extId + "\n");

  if (keepRunning) {
    console.error(`\nChrome is running with the extension loaded.`);
    console.error(`  profile: ${profile}`);
    console.error(`  ext id:  ${extId}`);
    console.error(`  log:     ${LOG_FILE}`);
    console.error(`  pid:     ${proc.pid}  (Chrome)`);
    console.error(`  this node pid: ${process.pid}  (Chrome supervisor — DO NOT KILL)`);
    console.error(`\nChrome is tethered to this Node process via the CDP pipe.`);
    console.error(`If this process exits, Chrome will exit too.`);
    console.error(`Use scripts/stop.sh to terminate both.`);
    // Stay alive forever; exit cleanly if Chrome dies
    proc.on("exit", (code) => {
      console.error(`Chrome exited (code=${code}); supervisor terminating.`);
      try { fs.unlinkSync(PID_FILE); } catch (_) {}
      process.exit(code ?? 0);
    });
    process.on("SIGTERM", () => { try { proc.kill("SIGTERM"); } catch(_){} });
    process.on("SIGINT",  () => { try { proc.kill("SIGTERM"); } catch(_){} });
    // Periodic CDP ping so the pipe isn't seen as idle (and to detect hang)
    setInterval(async () => {
      try { await call("Browser.getVersion", {}, 5000); }
      catch (e) { console.error("ping failed:", e.message); }
    }, 30000);
    // Block — never resolves
    await new Promise(() => {});
  } else {
    console.error("Closing Chrome (extension persists in profile)...");
    try { await call("Browser.close", {}, 5000); } catch (_) {}
    writePipe.end();
    readPipe.destroy();
    proc.on("exit", () => process.exit(0));
    setTimeout(() => { proc.kill("SIGTERM"); process.exit(0); }, 5000);
  }
})().catch((e) => { console.error("FATAL:", e); process.exit(6); });
