#!/bin/bash
# Application de reconnexion OAuth pour rclone GDrive
# Écrit le token directement dans rclone.conf pour éviter que
# rclone config update ne relance un flow OAuth interactif
# Affiche une interface graphique simple avec Zenity ou fallback terminal

set -euo pipefail

CONFIG_FILE="$HOME/.config/rclone/rclone.conf"

notify() {
    local title="$1"
    local message="$2"
    local urgency="${3:-normal}"
    if command -v notify-send &> /dev/null; then
        notify-send -u "$urgency" "$title" "$message" 2>/dev/null || true
    fi
}

do_reconnect() {
    # Extraire client_id et client_secret depuis rclone.conf pour utiliser
    # notre propre app OAuth (pas celle par défaut de rclone)
    local CLIENT_ID CLIENT_SECRET
    CLIENT_ID=$(grep '^client_id = ' "$CONFIG_FILE" | sed 's/client_id = //')
    CLIENT_SECRET=$(grep '^client_secret = ' "$CONFIG_FILE" | sed 's/client_secret = //')

    if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
        echo "❌ client_id ou client_secret manquant dans $CONFIG_FILE"
        notify "❌ Reconnexion GDrive" "Credentials OAuth manquants" "critical"
        return 1
    fi

    # Lancer rclone authorize avec NOTRE client_id/secret
    TOKEN_OUTPUT=$(printf 'y\n' | timeout 120 rclone authorize "drive" "$CLIENT_ID" "$CLIENT_SECRET" 2>&1) || {
        echo "❌ La reconnexion a échoué ou a été annulée."
        notify "❌ Reconnexion GDrive" "Échec de la reconnexion OAuth" "critical"
        return 1
    }

    # Extraire le JSON du token
    TOKEN_JSON=$(echo "$TOKEN_OUTPUT" | grep -o '{.*}' | head -1)

    if [ -z "$TOKEN_JSON" ]; then
        echo "❌ Impossible d'extraire le token OAuth."
        notify "❌ Reconnexion GDrive" "Token non extrait" "critical"
        return 1
    fi

    # Écrire le token directement dans le fichier de config
    # Cela préserve client_id, client_secret, scope, team_drive
    # et évite que rclone config update ne relance un flow OAuth
    if grep -q '^token = ' "$CONFIG_FILE"; then
        # Remplacer la ligne token existante
        sed -i "s|^token = .*|token = $TOKEN_JSON|" "$CONFIG_FILE"
    else
        # Ajouter la ligne token après client_secret
        sed -i "/^client_secret = /a token = $TOKEN_JSON" "$CONFIG_FILE"
    fi

    # Vérifier que ça marche via rclone about (rapide, ne liste pas les fichiers)
    if timeout 30 rclone about gdrive: --json > /dev/null 2>&1; then
        echo "✅ Reconnexion réussie !"
        notify "✅ Reconnexion GDrive" "Token rafraîchi avec succès" "normal"
        return 0
    else
        echo "⚠️ Token sauvegardé mais vérification échouée."
        notify "⚠️ Reconnexion GDrive" "Token sauvegardé mais vérification échouée" "critical"
        return 1
    fi
}

# Vérifier si zenity est disponible pour l'interface graphique
if command -v zenity &> /dev/null; then
    # Mode graphique
    zenity --info \
        --title="🔐 Reconnexion GDrive" \
        --text="Le token OAuth a expiré.\n\nUne fenêtre du navigateur va s'ouvrir pour vous reconnecter.\n\nCliquez OK pour continuer." \
        --width=400 2>/dev/null || true

    if ! do_reconnect; then
        zenity --error \
            --title="❌ Erreur de reconnexion" \
            --text="La reconnexion a échoué.\n\nVeuillez réessayer manuellement:\nrclone authorize \"drive\"" \
            --width=400 2>/dev/null || true
        exit 1
    fi

    zenity --info \
        --title="✅ Reconnexion réussie" \
        --text="La reconnexion OAuth a réussi !\n\nLa synchronisation va reprendre automatiquement." \
        --width=400 2>/dev/null || true
else
    # Mode terminal (fallback)
    echo "=== RECONNEXION OAUTH GDRIVE ==="
    echo "Le token OAuth a expiré."
    echo "Une fenêtre du navigateur va s'ouvrir pour vous reconnecter."
    echo ""
    read -p "Appuyez sur Entrée pour continuer..."

    if ! do_reconnect; then
        exit 1
    fi
fi