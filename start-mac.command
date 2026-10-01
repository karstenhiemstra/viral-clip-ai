#!/bin/bash
# Dubbelklik om ViralClip AI te starten (Mac). Eerste keer: rechtsklik → Open.
cd "$(dirname "$0")" || exit 1
bash scripts/start.sh
echo
read -r -p "Druk op Enter om dit venster te sluiten…" _
