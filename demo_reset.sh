#!/usr/bin/env bash
# Reset and start the live demo: clears posted requests and agent conversations, then starts
#   the agents backend (FastAPI + Gemini) on :8000 and the web UI (serve.py) on :8765.
# Logs: /tmp/salespatriot-backend.log, /tmp/salespatriot-web.log. Stop with: ./demo_reset.sh stop
set -euo pipefail
cd "$(dirname "$0")"

pkill -f "uvicorn app.backend.main:app" 2>/dev/null || true
pkill -f "serve.py" 2>/dev/null || true
[ "${1:-}" = "stop" ] && { echo "Stopped."; exit 0; }
sleep 1

# Demo state only (gitignored): requests posted by buyers and the agents' conversations.
rm -f data/demo/live_requests.json data/demo/agent_db.json

nohup uv run --project app/backend uvicorn app.backend.main:app --host 127.0.0.1 --port 8000 \
  > /tmp/salespatriot-backend.log 2>&1 &
nohup python3 serve.py --port 8765 > /tmp/salespatriot-web.log 2>&1 &

echo -n "Starting"
for _ in $(seq 1 60); do
  if grep -q "Deal checks ready" /tmp/salespatriot-web.log 2>/dev/null \
     && curl -fs -o /dev/null http://127.0.0.1:8000/health/live; then
    echo " ready."
    echo "Supplier: http://127.0.0.1:8765/workspace?as=supplier-pol-9482182612"
    echo "Buyer:    http://127.0.0.1:8765/workspace?as=buyer-deu-991-19518-88"
    exit 0
  fi
  echo -n "."; sleep 1
done
echo " not ready after 60 s — see /tmp/salespatriot-web.log and /tmp/salespatriot-backend.log"
exit 1
