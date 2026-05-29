#!/usr/bin/env bash
# Launch Chrome stable + our extension in the background (nohup-style),
# so it persists across this shell session ending.
#
# Chrome is tethered to the Node supervisor via the CDP pipe — if the
# supervisor dies, Chrome dies. Use scripts/stop.sh to cleanly stop both.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PID_FILE="$ROOT/.node.pid"
LOG_FILE="$ROOT/.supervisor.log"

# If already running, stop first
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Supervisor already running (pid $(cat $PID_FILE)). Stopping..."
  "$HERE/stop.sh"
  sleep 1
fi

# Build config from current env (gitignored output)
"$HERE/build-config.sh" > /dev/null

# Spawn supervisor detached
nohup node "$HERE/install-extension.js" "$@" > "$LOG_FILE" 2>&1 &
SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"

echo "Supervisor (node) pid=$SUP_PID  log=$LOG_FILE"
echo "Waiting for Chrome to be ready..."
READY=0
for i in $(seq 1 40); do
  if grep -q "Extension loaded" "$LOG_FILE" 2>/dev/null; then
    READY=1; break
  fi
  if ! kill -0 "$SUP_PID" 2>/dev/null; then
    echo
    echo "Supervisor died early. Log:"
    tail -30 "$LOG_FILE" | sed 's/^/   /'
    rm -f "$PID_FILE"
    exit 2
  fi
  sleep 0.5
done

if [[ "$READY" != "1" ]]; then
  echo
  echo "Timed out waiting for ready signal. Log so far:"
  tail -30 "$LOG_FILE" | sed 's/^/   /'
  exit 3
fi

echo
echo "✓ Extension loaded into real Chrome stable."
grep -E "Connected|Extension loaded|profile|pid|ext id" "$LOG_FILE" | sed 's/^/   /'

# ─────────────── Resolve service URL + print paste-to-Claude prompt ─────────────── #
echo
echo "Waiting up to 30s for service worker to register with Hypha..."
SERVICE_ID=""
for i in $(seq 1 30); do
  SVC=$(hypha services --json 2>/dev/null | python3 -c "
import json, sys, os, urllib.parse, urllib.request, urllib.error
data = json.load(sys.stdin) if sys.stdin else []
H = {'Authorization':'Bearer '+os.environ.get('HYPHA_TOKEN',''), 'Content-Type':'application/json'}
cands = [s['id'] for s in data if 'browser-ext-' in s.get('id','') and s.get('id','').endswith(':browser-controller')]
live = ''
for sid in reversed(cands):
    ws, rest = sid.split('/', 1)
    url = f\"https://hypha.aicell.io/{urllib.parse.quote(ws,safe='')}/services/{urllib.parse.quote(rest,safe='')}/ping?_mode=last\"
    try:
        r = urllib.request.Request(url, data=b'{}', method='POST', headers=H)
        with urllib.request.urlopen(r, timeout=3) as resp:
            if json.loads(resp.read()).get('ok'):
                live = sid; break
    except Exception: continue
print(live)
" 2>/dev/null)
  if [[ -n "$SVC" ]]; then SERVICE_ID="$SVC"; break; fi
  sleep 1
done

if [[ -z "$SERVICE_ID" ]]; then
  echo
  echo "⚠ Hypha service didn't register within 30s. Chrome is running but the extension"
  echo "  hasn't connected to Hypha yet. Check .chrome.log for errors."
  exit 0
fi

WS=$(echo "$SERVICE_ID" | cut -d/ -f1)
REST=$(echo "$SERVICE_ID" | cut -d/ -f2-)
WS_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$WS',safe=''))")
REST_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$REST',safe=''))")
SERVICE_URL="${HYPHA_SERVER_URL:-https://hypha.aicell.io}/$WS_ENC/services/$REST_ENC"
DOCS_URL="${HYPHA_BROWSER_DOCS_URL:-https://hypha.aicell.io/$WS_ENC/artifacts/hypha-browser-demo/files/index.html}"

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                  ✓ hypha-browser-use is ready

  Browser:    real Chrome stable, profile ~/.hypha-browser-use/profile
  Service:    $SERVICE_URL
  Docs page:  $DOCS_URL

  Copy the prompt below and paste into Claude / Claude Code:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

I have a remote-controllable real Chrome browser via Hypha RPC.

  Service URL:  $SERVICE_URL
  Docs URL:     $DOCS_URL

To use a tool, POST JSON to \$SERVICE_URL/<tool>?_mode=last with body
{"kwargs": {...}} and header Authorization: Bearer \$HYPHA_TOKEN.

First, fetch the docs URL above to learn the tool surface, then drive the
browser to do what I ask. For 2FA push prompts that need biometric, call
notify_user and wait for me to tap "Approve" on my real phone.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

To verify: ./scripts/test-rpc.sh   (or run the full test+report:
                                    python3 scripts/test-and-report.py)
To stop:   ./scripts/stop.sh
EOF
