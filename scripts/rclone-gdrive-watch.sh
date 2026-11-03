#!/bin/bash
# Surveillance temps réel des dossiers synchronisés via inotifywait
# Déclenche une synchro après debounce quand un fichier est modifié/créé/supprimé
set -euo pipefail

SETTINGS_FILE="$HOME/.config/rclone-gdrive-sync/settings.json"
DIRS_CONFIG="$HOME/.config/rclone-gdrive-sync/dirs.json"
SYNC_SCRIPT="$HOME/.local/bin/rclone-gdrive-sync.sh"
LOCK_FILE="/tmp/rclone-gdrive-sync.lock"
WATCH_LOCK="/tmp/rclone-gdrive-watch.lock"
LOG_FILE="$HOME/.local/share/rclone-gdrive-sync/watch.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# Vérifier si déjà en cours
if [ -f "$WATCH_LOCK" ]; then
    PID=$(cat "$WATCH_LOCK")
    if ps -p "$PID" > /dev/null 2>&1; then
        log "Watch déjà actif (PID $PID), sortie"
        exit 0
    fi
    rm -f "$WATCH_LOCK"
fi

echo $$ > "$WATCH_LOCK"
trap 'rm -f "$WATCH_LOCK"' EXIT

# Lire le debounce depuis settings.json
DEBOUNCE=$(python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE') as f:
        s = json.load(f)
    print(s.get('watch_debounce_seconds', 30))
except:
    print(30)
" 2>/dev/null || echo 30)

log "Démarrage watch (debounce=${DEBOUNCE}s)"

# Construire la liste des dossiers à surveiller depuis dirs.json
WATCH_DIRS=$(python3 -c "
import json, os
with open('$DIRS_CONFIG') as f:
    cfg = json.load(f)
for d in cfg.get('directories', []):
    if d.get('enabled', True):
        src = os.path.expanduser(d['src'])
        if os.path.isdir(src):
            print(src)
" 2>/dev/null)

if [ -z "$WATCH_DIRS" ]; then
    log "Aucun dossier à surveiller"
    exit 1
fi

# Exclusions inotifywait (regex)
EXCLUDE_PATTERNS="node_modules|__pycache__|\.cache|\.venv|\.git/objects|\.next|\.turbo|coverage|dist|build"

LAST_SYNC=0
PENDING=0

while true; do
    # inotifywait avec timeout pour vérifier périodiquement si on doit quitter
    EVENT=$(inotifywait -r -q \
        --exclude "$EXCLUDE_PATTERNS" \
        -e close_write -e delete -e moved_to -e moved_from \
        --format '%w%f %e' \
        --timeout 60 \
        $WATCH_DIRS 2>/dev/null || true)

    if [ -n "$EVENT" ]; then
        PENDING=1
        log "Changement détecté: $EVENT"
    fi

    # Si des changements sont en attente, attendre le debounce puis sync
    if [ "$PENDING" -eq 1 ]; then
        sleep "$DEBOUNCE"
        NOW=$(date +%s)
        ELAPSED=$((NOW - LAST_SYNC))

        # Éviter les synchros trop rapprochées (min 60s entre deux)
        if [ "$ELAPSED" -ge 60 ]; then
            log "Déclenchement synchro après debounce"
            if [ ! -f "$LOCK_FILE" ]; then
                bash "$SYNC_SCRIPT" >> "$LOG_FILE" 2>&1 &
                LAST_SYNC=$(date +%s)
            else
                log "Synchro déjà en cours, reportée"
            fi
        else
            log "Synchro récente (${ELAPSED}s), attente"
        fi
        PENDING=0
    fi
done