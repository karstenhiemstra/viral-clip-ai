#!/usr/bin/env bash
# Start API, worker and dashboard for local development. Ctrl+C stops everything.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x backend/.venv/bin/python ]]; then
  echo "Backend niet geïnstalleerd. Draai eerst: make setup" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  echo "⚠  ffmpeg niet gevonden - installeer het (brew install ffmpeg / apt install ffmpeg) voor analyse en rendering." >&2
fi
mkdir -p data/inbox

pids=()
cleanup() { echo; echo "Stoppen..."; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd backend && .venv/bin/python -m app.migrate)
(cd backend && exec .venv/bin/uvicorn app.main:app --reload --port 8000) & pids+=($!)
(cd backend && exec .venv/bin/python -m app.worker.runner) & pids+=($!)
(cd frontend && BACKEND_URL="${BACKEND_URL:-http://localhost:8000}" exec npm run dev) & pids+=($!)

echo "▶ Dashboard: http://localhost:3000   API: http://localhost:8000/docs   Inbox: $ROOT/data/inbox"
wait -n
