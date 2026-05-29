#!/usr/bin/env bash
# Smoke-test the registered Hypha service via curl.
set -euo pipefail
: "${HYPHA_SERVER_URL:=https://hypha.aicell.io}"
: "${HYPHA_WORKSPACE:?HYPHA_WORKSPACE not set}"
: "${HYPHA_TOKEN:?HYPHA_TOKEN not set}"
SERVICE_ID="${1:-browser-controller}"
WS_ENC="$(printf '%s' "$HYPHA_WORKSPACE" | python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))')"
BASE="$HYPHA_SERVER_URL/$WS_ENC/services/$SERVICE_ID"

echo "== ping"
curl -fsS "$BASE/ping?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" | head -c 400; echo

echo "== get_extension_info"
curl -fsS "$BASE/get_extension_info?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" | head -c 600; echo

echo "== list_tabs"
curl -fsS "$BASE/list_tabs?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" \
     -H "Content-Type: application/json" -d '{}' -X POST | head -c 800; echo

echo "== create_tab https://example.com"
curl -fsS "$BASE/create_tab?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" \
     -H "Content-Type: application/json" -d '{"url":"https://example.com","active":true}' -X POST | head -c 600; echo

sleep 2

echo "== get_page_info"
curl -fsS "$BASE/get_page_info?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" \
     -H "Content-Type: application/json" -d '{}' -X POST | head -c 600; echo

echo "== get_browser_state (viewport only)"
curl -fsS "$BASE/get_browser_state?_mode=last" -H "Authorization: Bearer $HYPHA_TOKEN" \
     -H "Content-Type: application/json" -d '{"viewport_only": true}' -X POST | head -c 1000; echo
