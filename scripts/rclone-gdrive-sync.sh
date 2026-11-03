#!/bin/bash
# Script de synchronisation GDrive avec gestion du token OAuth
# Auteur: rony
# Date: 2026-11-03

set -euo pipefail

CONFIG_DIR="$HOME/.config/rclone"
CONFIG_FILE="$CONFIG_DIR/rclone.conf"
LOG_DIR="$HOME/.local/share/rclone-gdrive-sync"
LOG_FILE="$LOG_DIR/sync.log"
LOCK_FILE="/tmp/rclone-gdrive-sync.lock"

# Couleurs pour les logs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Créer les répertoires si nécessaire
mkdir -p "$LOG_DIR"

# Fonction de log
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log_error() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] ${RED}ERROR: $*${NC}" | tee -a "$LOG_FILE"
}

log_success() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] ${GREEN}SUCCESS: $*${NC}" | tee -a "$LOG_FILE"
}

log_warning() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] ${YELLOW}WARNING: $*${NC}" | tee -a "$LOG_FILE"
}

# Vérifier si déjà en cours d'exécution
if [ -f "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        log_warning "Sync déjà en cours (PID $PID), sortie"
        exit 0
    else
        rm -f "$LOCK_FILE"
    fi
fi

# Créer le lock
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# Vérifier que rclone est installé
if ! command -v rclone &> /dev/null; then
    log_error "rclone n'est pas installé"
    exit 1
fi

# Vérifier que le fichier de config existe
if [ ! -f "$CONFIG_FILE" ]; then
    log_error "Fichier de config rclone manquant: $CONFIG_FILE"
    exit 1
fi

# Fonction pour vérifier la validité du token OAuth
check_oauth_token() {
    log "Vérification du token OAuth..."

    # Tester l'accès à GDrive via rclone about (rapide, ne liste pas les fichiers)
    if timeout 30 rclone about gdrive: --json > /dev/null 2>&1; then
        log_success "Token OAuth valide"
        return 0
    else
        log_error "Token OAuth expiré ou invalide"
        return 1
    fi
}

# Fonction pour rafraîchir le token OAuth
refresh_oauth_token() {
    log "Rafraîchissement du token OAuth nécessaire..."

    # Lancer l'application de reconnexion
    if [ -f "$HOME/.local/bin/rclone-gdrive-reconnect.sh" ]; then
        "$HOME/.local/bin/rclone-gdrive-reconnect.sh"
    else
        log_error "Script de reconnexion manquant"
        exit 1
    fi
}

# Fonction de notification
notify() {
    local title="$1"
    local message="$2"
    local urgency="${3:-normal}"

    # Essayer notify-send d'abord
    if command -v notify-send &> /dev/null; then
        notify-send -u "$urgency" "$title" "$message" 2>/dev/null || true
    fi

    # Loguer aussi
    log "NOTIFICATION: $title - $message"
}

# Fonction de synchronisation bidirectionnelle (bisync)
# En cas de conflit, le fichier le plus récent est conservé
sync_directory() {
    local src="$1"
    local dst="$2"
    local name="$3"
    shift 3
    local excludes=("$@")

    log "Synchronisation bidirectionnelle: $name ($src <-> $dst)"

    # Vérifier si c'est la première exécution (resync nécessaire)
    local listing_dir="$HOME/.cache/rclone/bisync/$name"
    local needs_resync=false
    if [ ! -d "$listing_dir" ] || [ -z "$(ls -A "$listing_dir" 2>/dev/null)" ]; then
        needs_resync=true
        log_warning "Première exécution bisync pour $name, resync initial nécessaire"
    fi

    # Inclure explicitement .git (exclu par défaut par rclone) avant les exclusions
    local cmd=(rclone bisync "$src" "$dst" --fast-list --transfers=16 --checkers=32 --stats=30s --conflict-resolve newer --verbose --include ".git/**")

    if [ "$needs_resync" = true ]; then
        cmd+=(--resync)
    fi

    for exclude in "${excludes[@]}"; do
        cmd+=(--exclude "$exclude")
    done

    if "${cmd[@]}" 2>&1 | tee -a "$LOG_FILE"; then
        log_success "Synchronisation terminée: $name"
        return 0
    else
        log_error "Échec synchronisation: $name"
        return 1
    fi
}

# Programme principal
main() {
    log "=== Démarrage de la synchronisation GDrive ==="

    # Vérifier le token
    if ! check_oauth_token; then
        notify "⚠️ Synchronisation GDrive" "Token OAuth expiré - Reconnexion nécessaire" "critical"
        refresh_oauth_token
        exit 1
    fi

    # Définir les exclusions communes
    local common_excludes=(
        "node_modules/**"
        "__pycache__/**"
        ".cache/**"
        "*.tmp"
    )

    local project_excludes=(
        "node_modules/**"
        "__pycache__/**"
        ".venv/**"
        "*.pyc"
        ".cache/**"
        "dist/**"
        "build/**"
        ".next/**"
        ".turbo/**"
        "coverage/**"
        "**/.env"
        "**/.env.*"
    )

    # Synchroniser les petits dossiers config
    sync_directory "$HOME/.config/git" "gdrive:SyncPC/config-git" "config-git" "${common_excludes[@]}" || true

    # Synchroniser .claude (sélectif, bisync)
    log "Synchronisation bidirectionnelle: .claude (sélectif)"
    local claude_listing="$HOME/.cache/rclone/bisync/dot-claude"
    local claude_resync=""
    if [ ! -d "$claude_listing" ] || [ -z "$(ls -A "$claude_listing" 2>/dev/null)" ]; then
        claude_resync="--resync"
        log_warning "Première exécution bisync pour .claude, resync initial"
    fi
    rclone bisync "$HOME/.claude" "gdrive:SyncPC/dot-claude" \
        --include "skills/**" \
        --include "plugins/**" \
        --include "scripts/**" \
        --include "CLAUDE.md" \
        --include "agents/**" \
        --include "commands/**" \
        --include "docs/**" \
        --include "hooks/**" \
        --exclude "**" \
        --conflict-resolve newer \
        --fast-list --transfers=16 --checkers=32 --stats=30s \
        $claude_resync 2>&1 | tee -a "$LOG_FILE" || true

    # Synchroniser .codex (sélectif, bisync)
    log "Synchronisation bidirectionnelle: .codex (sélectif)"
    local codex_listing="$HOME/.cache/rclone/bisync/dot-codex"
    local codex_resync=""
    if [ ! -d "$codex_listing" ] || [ -z "$(ls -A "$codex_listing" 2>/dev/null)" ]; then
        codex_resync="--resync"
        log_warning "Première exécution bisync pour .codex, resync initial"
    fi
    rclone bisync "$HOME/.codex" "gdrive:SyncPC/dot-codex" \
        --include "memories/**" \
        --include "skills/**" \
        --include "AGENTS.md" \
        --include "config.toml" \
        --exclude "**" \
        --conflict-resolve newer \
        --fast-list --transfers=16 --checkers=32 --stats=30s \
        $codex_resync 2>&1 | tee -a "$LOG_FILE" || true

    # Synchroniser les gros dossiers projets
    sync_directory "$HOME/Assets Projets" "gdrive:SyncPC/Assets-Projets" "Assets-Projets" "${common_excludes[@]}" || true
    sync_directory "$HOME/Projets" "gdrive:SyncPC/Projets" "Projets" "${project_excludes[@]}" || true
    sync_directory "$HOME/projects" "gdrive:SyncPC/projects" "projects" "${project_excludes[@]}" || true

    log_success "=== Synchronisation GDrive terminée ==="
    notify "✅ Synchronisation GDrive" "Toutes les synchronisations terminées avec succès" "normal"
}

main "$@"