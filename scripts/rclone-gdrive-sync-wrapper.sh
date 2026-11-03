#!/bin/bash
# Wrapper qui lit l'intervalle depuis settings.json et déclenche la synchro
# Utilisé par le timer systemd pour supporter les intervalles configurables
set -euo pipefail

SETTINGS_FILE="$HOME/.config/rclone-gdrive-sync/settings.json"
SYNC_SCRIPT="$HOME/.local/bin/rclone-gdrive-sync.sh"
WATCH_SERVICE="rclone-gdrive-watch.service"

# Si le mode temps réel est actif, ne pas lancer la synchro par timer
REALTIME=$(python3 -c "
import json
try:
    with open('$SETTINGS_FILE') as f:
        s = json.load(f)
    print('true' if s.get('realtime_watch', False) else 'false')
except:
    print('false')
" 2>/dev/null || echo "false")

if [ "$REALTIME" = "true" ]; then
    # Mode temps réel actif : le watch gère déjà les synchros
    exit 0
fi

# Lancer la synchronisation
exec bash "$SYNC_SCRIPT"