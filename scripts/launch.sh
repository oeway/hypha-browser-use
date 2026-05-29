#!/usr/bin/env bash
# Launch headless Chrome with our extension loaded, in a dedicated profile dir.
# Does NOT touch the user's main Chrome.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
EXT="$ROOT/extension"
PROFILE_DIR="${HYPHA_BROWSER_PROFILE:-$HOME/.hypha-browser-use/profile}"
PID_FILE="$ROOT/.chrome.pid"
LOG_FILE="$ROOT/.chrome.log"
# Default to Chrome for Testing if available (regular Chrome stable refuses --load-extension).
CFT_DEFAULT="$HOME/.hypha-browser-use/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
if [[ -n "${CHROME_BINARY:-}" ]]; then
  CHROME="$CHROME_BINARY"
elif [[ -x "$CFT_DEFAULT" ]]; then
  CHROME="$CFT_DEFAULT"
else
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi

if [[ ! -x "$CHROME" ]]; then
  echo "Chrome not found at: $CHROME" >&2; exit 1
fi
if [[ ! -f "$EXT/config.js" ]]; then
  echo "extension/config.js missing — run scripts/build-config.sh first" >&2; exit 1
fi

mkdir -p "$PROFILE_DIR"

# If already running, stop it.
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Chrome already running (pid $(cat $PID_FILE)). Stopping first..."
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  sleep 1
fi

HEADLESS_FLAG="${HEADLESS:-1}"
HEADLESS_ARG=""
if [[ "$HEADLESS_FLAG" = "1" ]]; then
  HEADLESS_ARG="--headless=new"
fi

DEBUG_PORT="${HYPHA_BROWSER_DEBUG_PORT:-9222}"
nohup "$CHROME" \
  ${HEADLESS_ARG} \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=DialMediaRouteProvider \
  --user-data-dir="$PROFILE_DIR" \
  --load-extension="$EXT" \
  --remote-debugging-port="$DEBUG_PORT" \
  --remote-allow-origins='*' \
  --disable-features=DisableLoadExtensionCommandLineSwitch \
  --enable-logging=stderr --v=0 \
  "about:blank" \
  > "$LOG_FILE" 2>&1 &

PID=$!
echo "$PID" > "$PID_FILE"
echo "Chrome launched: pid=$PID profile=$PROFILE_DIR log=$LOG_FILE"
echo "Tail the log:    tail -f $LOG_FILE"
