#!/usr/bin/env bash
# Start the browser-share login interface in the background and expose it via svamp.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

: "${HYPHA_TOKEN:?HYPHA_TOKEN must be set}"
: "${HYPHA_WORKSPACE:?HYPHA_WORKSPACE must be set}"

# Find a live browser-controller service
if [[ -z "${BROWSER_SERVICE_ID:-}" ]]; then
  echo "Discovering live browser-controller service..."
  BROWSER_SERVICE_ID="$(hypha services --json 2>/dev/null | python3 -c '
import json, sys, os, urllib.parse, urllib.request, urllib.error
data = json.load(sys.stdin) if sys.stdin else []
H = {"Authorization":"Bearer "+os.environ.get("HYPHA_TOKEN",""),"Content-Type":"application/json"}
for s in reversed(data):
    sid = s.get("id","")
    if "browser-ext-" in sid and sid.endswith(":browser-controller"):
        ws, rest = sid.split("/", 1)
        url = f"https://hypha.aicell.io/{urllib.parse.quote(ws,safe=chr(0))}/services/{urllib.parse.quote(rest,safe=chr(0))}/ping?_mode=last"
        try:
            r = urllib.request.Request(url, data=b"{\"kwargs\":{}}", method="POST", headers=H)
            with urllib.request.urlopen(r, timeout=3) as resp:
                if json.loads(resp.read()).get("ok"):
                    print(sid); break
        except Exception: continue
')"
fi
if [[ -z "$BROWSER_SERVICE_ID" ]]; then
  echo "No live browser-controller service found. Start it first: ./scripts/run.sh --headless"
  exit 2
fi
export BROWSER_SERVICE_ID
echo "Using service: $BROWSER_SERVICE_ID"

PORT="${PORT:-8765}"
export PORT
LOG="$ROOT/.browser-share.log"
PID_FILE="$ROOT/.browser-share.pid"

# Already running?
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "browser-share already running (pid $(cat $PID_FILE)). Stopping..."
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  sleep 1
fi

# Run via uv (auto-installs deps in an ephemeral env)
cd "$HERE"
nohup uv run --with fastapi --with uvicorn --with httpx \
  python3 server.py > "$LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "Server pid=$SERVER_PID  log=$LOG"

# Wait for server up
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/state" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server died. Log:"; tail -30 "$LOG"; exit 3
  fi
  sleep 0.5
done

# Expose publicly via svamp (idempotent)
svamp service expose browser-share --port "$PORT" 2>/dev/null || \
  svamp service expose browser-share --port "$PORT"

# Print the public URL
EXPOSED="$(svamp service info browser-share 2>/dev/null | grep -E "https?://" | head -1 | awk '{print $1}' || true)"
if [[ -z "$EXPOSED" ]]; then
  EXPOSED="(check 'svamp service info browser-share')"
fi
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  browser-share is live at:"
echo "    local:    http://127.0.0.1:$PORT"
echo "    public:   $EXPOSED"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "To stop:  ./browser-share/stop.sh"
