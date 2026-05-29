#!/usr/bin/env python3
"""
Install an unpacked extension into Chrome stable via CDP `Extensions.loadUnpacked`.

Google removed `--load-extension` from branded Chrome stable in 2025; the official
replacement is the CDP `Extensions` domain, which requires:

  1. `--enable-unsafe-extension-debugging`
  2. `--remote-debugging-pipe`   (NOT `--remote-debugging-port`; pipe is parent-
     process-only, so the API can't be hit by a remote attacker)

After loadUnpacked succeeds, the extension is registered in the profile and
persists across Chrome restarts until removed from chrome://extensions.

Usage:
  python3 scripts/install-extension.py [--profile DIR] [--ext-path DIR]
                                       [--keep-running] [--no-restore]

Defaults:
  --profile     ~/.hypha-browser-use/profile   (separate from user's main Chrome)
  --ext-path    <repo>/extension
  --keep-running    keep Chrome alive after install (default: yes — needed for
                    the extension to run); pass --no-keep-running to quit Chrome
                    after install (extension still persists in profile)
  --no-restore  skip --restore-last-session
"""
import argparse, json, os, signal, subprocess, sys, threading, time, pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_EXT = ROOT / "extension"
DEFAULT_PROFILE = pathlib.Path.home() / ".hypha-browser-use" / "profile"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PID_FILE = ROOT / ".chrome.pid"
LOG_FILE = ROOT / ".chrome.log"


class CdpPipe:
    """JSON-RPC over Chrome's --remote-debugging-pipe (FD 3 read, FD 4 write).

    The two FDs are NUL-delimited JSON message streams (each message terminated
    by a single 0x00 byte). We dup them into the child as 3 and 4 via preexec_fn,
    then talk to Chrome on our parent ends.
    """

    def __init__(self):
        # parent reads from r_parent, child reads from r_child (becomes FD 3)
        self.r_child, self.w_parent = os.pipe()
        self.r_parent, self.w_child = os.pipe()
        for fd in (self.r_child, self.w_parent, self.r_parent, self.w_child):
            os.set_inheritable(fd, True)
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._events = []
        self._reader = None
        self._closed = False

    def preexec(self):
        # Remap our child FDs to 3 and 4 (Chrome's expected pipe FDs).
        # IMPORTANT: subprocess.close_fds runs AFTER preexec and would close
        # any FD not in pass_fds — so we must call this with close_fds=False
        # and clean up extras ourselves here.
        os.dup2(self.r_child, 3)
        os.dup2(self.w_child, 4)
        # Close our originals + the parent ends we don't want leaked into child.
        for fd in (self.r_child, self.w_child, self.r_parent, self.w_parent):
            try: os.close(fd)
            except OSError: pass
        # Close anything else above 4 (avoid leaking unrelated FDs).
        for fd in range(5, 256):
            try: os.close(fd)
            except OSError: pass

    def post_spawn(self):
        # Close child ends in parent — we don't need them.
        os.close(self.r_child); os.close(self.w_child)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        buf = b""
        while not self._closed:
            try:
                chunk = os.read(self.r_parent, 65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\x00" in buf:
                msg, buf = buf.split(b"\x00", 1)
                if not msg:
                    continue
                try:
                    obj = json.loads(msg)
                except Exception as e:
                    print(f"[cdp] bad msg: {msg!r} ({e})", file=sys.stderr)
                    continue
                mid = obj.get("id")
                if mid is not None and mid in self._pending:
                    self._pending.pop(mid).put(obj)
                else:
                    self._events.append(obj)

    def call(self, method, params=None, timeout=15):
        from queue import Queue
        with self._lock:
            self._next_id += 1
            mid = self._next_id
        q = Queue()
        self._pending[mid] = q
        msg = {"id": mid, "method": method}
        if params: msg["params"] = params
        data = json.dumps(msg).encode() + b"\x00"
        os.write(self.w_parent, data)
        return q.get(timeout=timeout)

    def close(self):
        self._closed = True
        for fd in (self.w_parent, self.r_parent):
            try: os.close(fd)
            except OSError: pass


def quit_running_chrome():
    """Best-effort graceful quit of any user-launched Chrome so we can take
    over with the pipe + unsafe-extension-debugging flag. Tabs are restored
    via --restore-last-session.
    """
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to quit'],
            check=False, timeout=10,
        )
    except Exception:
        pass
    # give Chrome time to flush session state
    for _ in range(20):
        out = subprocess.run(["pgrep", "-fl", "Google Chrome"], capture_output=True, text=True)
        # ignore our headless test instance and child renderers without main app
        if "Google Chrome.app" not in out.stdout:
            return True
        time.sleep(0.25)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--ext-path", default=str(DEFAULT_EXT))
    ap.add_argument("--chrome", default=os.environ.get("CHROME_BINARY", DEFAULT_CHROME))
    ap.add_argument("--quit-running", action="store_true",
                    help="Quit any user-launched Chrome first (use when targeting their main profile)")
    ap.add_argument("--no-restore", action="store_true")
    ap.add_argument("--keep-running", action="store_true", default=True)
    ap.add_argument("--no-keep-running", dest="keep_running", action="store_false")
    args = ap.parse_args()

    chrome = args.chrome
    profile = os.path.abspath(args.profile)
    ext_path = os.path.abspath(args.ext_path)

    if not os.path.exists(chrome):
        print(f"chrome binary not found: {chrome}", file=sys.stderr); sys.exit(2)
    if not os.path.isdir(ext_path):
        print(f"extension dir not found: {ext_path}", file=sys.stderr); sys.exit(2)
    os.makedirs(profile, exist_ok=True)

    if args.quit_running:
        print("Quitting any running Chrome...", flush=True)
        quit_running_chrome()

    pipe = CdpPipe()
    chrome_args = [
        chrome,
        "--enable-unsafe-extension-debugging",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
    ]
    if not args.no_restore:
        chrome_args.append("--restore-last-session")
    chrome_args.append("about:blank")

    print(f"Launching: {' '.join(chrome_args)}", flush=True)
    log_fh = open(LOG_FILE, "ab", buffering=0)
    proc = subprocess.Popen(
        chrome_args,
        preexec_fn=pipe.preexec,
        # We manage FDs ourselves in preexec; subprocess must NOT additionally
        # close FDs 3/4 which preexec just installed.
        close_fds=False,
        stdout=log_fh, stderr=log_fh,
    )
    pipe.post_spawn()
    PID_FILE.write_text(str(proc.pid))
    print(f"Chrome PID: {proc.pid}", flush=True)

    # Wait for the browser to be ready by calling Browser.getVersion.
    deadline = time.time() + 20
    version = None
    while time.time() < deadline:
        try:
            r = pipe.call("Browser.getVersion", timeout=2)
            if "result" in r:
                version = r["result"]; break
        except Exception:
            pass
        time.sleep(0.2)
    if not version:
        print("Browser did not respond to Browser.getVersion", file=sys.stderr); sys.exit(3)
    print(f"Connected to: {version.get('product')} (rev {version.get('revision', '?')[:7]})", flush=True)

    print(f"Calling Extensions.loadUnpacked({ext_path}) ...", flush=True)
    r = pipe.call("Extensions.loadUnpacked", {"path": ext_path}, timeout=20)
    if "error" in r:
        print(f"loadUnpacked FAILED: {r['error']}", file=sys.stderr)
        sys.exit(4)
    ext_id = r["result"]["id"]
    print(f"✓ Extension loaded: id={ext_id}", flush=True)

    # Save extension id for other scripts
    (ROOT / ".extension.id").write_text(ext_id + "\n")
    print(f"  saved to .extension.id", flush=True)

    if args.keep_running:
        print(f"\nChrome is running with the extension loaded.", flush=True)
        print(f"  profile: {profile}", flush=True)
        print(f"  ext id:  {ext_id}", flush=True)
        print(f"  log:     {LOG_FILE}", flush=True)
        print(f"  pid:     {proc.pid}  (use scripts/stop.sh to terminate)", flush=True)
        pipe.close()
    else:
        print("Quitting Chrome (extension stays installed in profile)...", flush=True)
        try:
            pipe.call("Browser.close", timeout=5)
        except Exception:
            pass
        pipe.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
