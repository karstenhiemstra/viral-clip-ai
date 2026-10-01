#!/usr/bin/env bash
# Stop ViralClip AI (Mac/Linux). Je clips en instellingen blijven bewaard in de map data/.
cd "$(dirname "$0")" && docker compose stop && echo "ViralClip AI is gestopt. Opnieuw starten: ./start-linux.sh of start-mac.command"
