#!/usr/bin/env bash
# install-icons.sh — Installe les icônes rclone-gdrive-sync dans le thème hicolor
# Compatible : Debian/Ubuntu, Fedora/RHEL, Arch Linux, openSUSE, et toute distro XDG
# Usage : sudo ./install-icons.sh          (système)
#       ./install-icons.sh --user          (utilisateur courant uniquement)

set -euo pipefail

ICON_NAME="RcloneGDriveSync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ICONS_DIR="$(cd "$SCRIPT_DIR/../icons" && pwd)"
SIZES=(16 22 24 32 48 64 128 256)

# ── Détection du mode d'installation ───────────────────────────────
USER_MODE=false
if [[ "${1:-}" == "--user" ]]; then
    USER_MODE=true
fi

if $USER_MODE; then
    ICON_ROOT="$HOME/.local/share/icons/hicolor"
    CACHE_CMD="gtk-update-icon-cache \"$ICON_ROOT\" 2>/dev/null || true"
else
    if [[ $EUID -ne 0 ]]; then
        echo "❌ Ce script doit être exécuté en root ou avec --user" >&2
        echo "   sudo $0          # installation système" >&2
        echo "   $0 --user        # installation utilisateur" >&2
        exit 1
    fi
    ICON_ROOT="/usr/share/icons/hicolor"
    CACHE_CMD="gtk-update-icon-cache \"$ICON_ROOT\" 2>/dev/null || true"
fi

# ── Vérification des sources ──────────────────────────────────────
if [[ ! -d "$REPO_ICONS_DIR" ]]; then
    echo "❌ Répertoire d'icônes introuvable : $REPO_ICONS_DIR" >&2
    exit 1
fi

SOURCE_FOUND=false
for size in "${SIZES[@]}"; do
    if [[ -f "$REPO_ICONS_DIR/gdrive-sync-${size}.png" ]]; then
        SOURCE_FOUND=true
        break
    fi
done

if ! $SOURCE_FOUND; then
    echo "❌ Aucune icône source trouvée dans $REPO_ICONS_DIR" >&2
    exit 1
fi

# ── Installation ──────────────────────────────────────────────────
echo "📦 Installation des icônes $ICON_NAME dans $ICON_ROOT"

INSTALLED=0
for size in "${SIZES[@]}"; do
    SRC="$REPO_ICONS_DIR/gdrive-sync-${size}.png"
    DEST_DIR="$ICON_ROOT/${size}x${size}/apps"
    DEST="$DEST_DIR/$ICON_NAME.png"

    if [[ ! -f "$SRC" ]]; then
        echo "  ⚠️  Taille ${size}x${size} absente, ignorée"
        continue
    fi

    mkdir -p "$DEST_DIR"
    cp "$SRC" "$DEST"
    chmod 644 "$DEST"
    echo "  ✅ ${size}x${size} → $DEST"
    ((INSTALLED++))
done

if [[ $INSTALLED -eq 0 ]]; then
    echo "❌ Aucune icône installée" >&2
    exit 1
fi

# ── Mise à jour du cache GTK ──────────────────────────────────────
echo "🔄 Mise à jour du cache d'icônes GTK..."
eval "$CACHE_CMD"

# ── Résumé ────────────────────────────────────────────────────────
echo ""
echo "✅ $INSTALLED icônes installées avec succès"
echo ""
echo "Pour vérifier :"
echo "  python3 -c \"import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; \\"
echo "    info = Gtk.IconTheme.get_default().lookup_icon('$ICON_NAME', 48, 0); \\"
echo "    print(info.get_filename() if info else 'NOT FOUND')\""
echo ""
echo "Si l'icône n'apparaît pas immédiatement dans GNOME :"
echo "  • Déconnexion/reconnexion de la session"
echo "  • Ou Alt+F2 → r (GNOME Shell restart)"
echo "  • Ou redémarrage du service tray : systemctl --user restart rclone-gdrive-tray.service"