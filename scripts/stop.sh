#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

for label in node chrome; do
  PID_FILE="$ROOT/.${label}.pid"
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      echo "Sent TERM to $label (pid $PID)"
      sleep 1
      if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null || true
        echo "Sent KILL to $label (pid $PID)"
      fi
    fi
    rm -f "$PID_FILE"
  fi
done
echo "Stopped."
