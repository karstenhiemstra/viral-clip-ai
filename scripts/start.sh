#!/usr/bin/env bash
# ViralClip AI - start everything with one command (macOS / Linux).
#   ./start.sh            (or double-click "Start ViralClip.command" on a Mac)
# First run: creates .env with random secrets, builds the app (5-10 min), opens the dashboard.
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://localhost:${WEB_PORT:-3000}"
say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

# 1. Docker installed and running?
if ! command -v docker >/dev/null 2>&1; then
  fail "Docker is niet geïnstalleerd. Installeer Docker Desktop via https://www.docker.com/products/docker-desktop/ en start dit script opnieuw."
fi
if ! docker info >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ]; then
    say "Docker Desktop wordt gestart…"
    open -a Docker || true
  fi
  printf 'Wachten tot Docker draait'
  for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && break
    printf '.'; sleep 2
  done
  echo
  docker info >/dev/null 2>&1 || fail "Docker draait niet. Open Docker Desktop, wacht tot er 'Engine running' staat en start dit script opnieuw."
fi

# 2. Settings file with random secrets (only the first time)
rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 40; }
if [ ! -f .env ]; then
  say "Instellingenbestand .env aanmaken (met willekeurige geheime sleutels)"
  cp .env.example .env
  sed -i.bak \
    -e "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=$(rand)|" \
    -e "s|^API_AUTH_TOKEN=.*|API_AUTH_TOKEN=$(rand)|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand)|" \
    .env && rm -f .env.bak
fi
mkdir -p data/inbox

# 3. Build + start
say "ViralClip AI starten (de eerste keer duurt dit 5-10 minuten)…"
docker compose up -d --build || fail "Starten mislukt (zie de melding hierboven). Controleer je internetverbinding en start opnieuw; details: docker compose logs --tail 50"

# 4. Wait until the dashboard answers, then open it
printf 'Wachten tot het dashboard klaar is'
for _ in $(seq 1 150); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL" || true)
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    echo
    say "Klaar! Open $URL"
    if command -v open >/dev/null 2>&1; then open "$URL"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
    echo "Stoppen: ./stop.sh (of 'docker compose down'). Je clips en instellingen blijven bewaard in de map data/."
    exit 0
  fi
  printf '.'; sleep 2
done
echo
fail "Het dashboard reageert nog niet. Bekijk wat er gebeurt met: docker compose logs --tail 50"
