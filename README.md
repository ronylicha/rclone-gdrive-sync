# rclone-gdrive-sync

![Icon](docs/icon-preview.png)

Synchronisation bidirectionnelle automatique entre votre poste Linux et Google Drive, avec indicateur tray, mode temps réel (inotify) et gestion OAuth.

## Fonctionnalités

- **Synchronisation bidirectionnelle** (`rclone bisync`) avec résolution de conflits par fichier le plus récent
- **Indicateur tray** avec icône personnalisée dans la barre de notification GNOME/Zorin
- **Mode temps réel** : surveillance via `inotifywait` avec debounce configurable (déclenchement automatique dès qu'un fichier est modifié)
- **Intervalle configurable** : synchro périodique via systemd timer (1 min à 1 heure)
- **Gestion OAuth** : détection automatique de token expiré + reconnexion interactive
- **Configuration JSON** : dossiers, exclusions, intervalle et mode temps réel éditables via le menu tray ou directement
- **Notifications desktop** : succès, erreurs et changements de configuration via `notify-send`
- **Exclusions intelligentes** : `node_modules`, `.cache`, `.venv`, `dist`, `.env`, etc. exclues par défaut

## Prérequis

- Linux (testé sur Ubuntu 24.04 / Zorin OS)
- Python 3.10+
- [rclone](https://rclone.org/) configuré avec un remote Google Drive nommé `gdrive:`
- `inotify-tools` (pour le mode temps réel)
- `libayatana-appindicator3` + extension GNOME AppIndicator (pour l'icône tray)

```bash
sudo apt install rclone inotify-tools gir1.2-ayatanaappindicator3-0.1 \
  gnome-shell-extension-appindicator python3-pip python3-venv
```

## Installation rapide

```bash
# Cloner le dépôt
git clone https://github.com/ronylicha/rclone-gdrive-sync.git
cd rclone-gdrive-sync

# Installer les dépendances Python dans un venv
python3 -m venv ~/.local/share/rclone-gdrive-sync/venv
~/.local/share/rclone-gdrive-sync/venv/bin/pip install pystray Pillow

# Copier les scripts
mkdir -p ~/.local/bin ~/.config/rclone-gdrive-sync \
  ~/.local/share/rclone-gdrive-sync/icons \
  ~/.local/share/icons/hicolor/{48x48,128x128}/apps

cp scripts/*.sh scripts/*.py ~/.local/bin/
chmod +x ~/.local/bin/rclone-gdrive-*.sh

# Copier les icônes
cp icons/*.png ~/.local/share/rclone-gdrive-sync/icons/
cp icons/gdrive-sync-128.png ~/.local/share/icons/hicolor/128x128/apps/RcloneGDriveSync.png
convert icons/gdrive-sync-128.png -resize 48x48 \
  ~/.local/share/icons/hicolor/48x48/apps/RcloneGDriveSync.png 2>/dev/null || \
  cp icons/gdrive-sync-128.png ~/.local/share/icons/hicolor/48x48/apps/RcloneGDriveSync.png

# Créer le thème d'icônes local si absent
if [ ! -f ~/.local/share/icons/hicolor/index.theme ]; then
  cat > ~/.local/share/icons/hicolor/index.theme << 'EOF'
[Icon Theme]
Name=hicolor
Comment=Fallback icon theme
Directories=48x48/apps,128x128/apps

[48x48/apps]
Size=48
Type=Fixed
Context=Applications

[128x128/apps]
Size=128
Type=Fixed
Context=Applications
EOF
fi
gtk-update-icon-cache -f ~/.local/share/icons/hicolor/

# Copier la configuration exemple
cp config-examples/settings.json ~/.config/rclone-gdrive-sync/
cp config-examples/dirs.json ~/.config/rclone-gdrive-sync/

# Installer les services systemd
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/

# Activer et démarrer
systemctl --user daemon-reload
systemctl --user enable --now rclone-gdrive-tray.service
systemctl --user enable --now rclone-gdrive-sync.timer
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

## Configuration

### Dossiers synchronisés (`dirs.json`)

```json
{
  "directories": [
    {
      "name": "Projets",
      "src": "~/Projets",
      "dst": "gdrive:SyncPC/Projets",
      "enabled": true,
      "type": "full"
    },
    {
      "name": "dot-claude",
      "src": "~/.claude",
      "dst": "gdrive:SyncPC/dot-claude",
      "enabled": true,
      "type": "selective",
      "includes": ["skills/**", "plugins/**", "CLAUDE.md"]
    }
  ]
}
```

- `type: "full"` : tout le dossier est synchronisé (avec exclusions globales)
- `type: "selective"` : seuls les patterns `includes` sont synchronisés
- `enabled: false` : dossier ignoré sans suppression de la config

### Paramètres (`settings.json`)

```json
{
  "sync_interval_minutes": 5,
  "realtime_watch": false,
  "watch_debounce_seconds": 30,
  "excluded_patterns": ["node_modules/**", "__pycache__/**", ".cache/**"]
}
```

| Paramètre | Description | Défaut |
|-----------|-------------|--------|
| `sync_interval_minutes` | Intervalle du timer systemd (ignoré si `realtime_watch=true`) | `5` |
| `realtime_watch` | Active la surveillance inotify au lieu du timer | `false` |
| `watch_debounce_seconds` | Délai d'attente après le dernier changement avant synchro | `30` |
| `excluded_patterns` | Patterns globaux exclus de toutes les synchronisations | voir fichier |

Ces paramètres sont aussi modifiables via le **menu contextuel du tray** (clic droit sur l'icône).

## Utilisation

### Menu tray (clic droit)

- **Ouvrir la fenêtre** : état détaillé (auth, progression, fichiers transférés)
- **Lancer la synchro** : déclencher manuellement une synchronisation immédiate
- **Intervalle** → sous-menu : 1 min / 5 min / 15 min / 30 min / 1 heure
- **Temps réel** : bascule entre mode inotify et mode timer
- **Quitter** : arrêter l'indicateur tray

### Services systemd

```bash
# État des services
systemctl --user status rclone-gdrive-tray.service
systemctl --user status rclone-gdrive-sync.timer
systemctl --user status rclone-gdrive-watch.service

# Logs
journalctl --user -u rclone-gdrive-tray.service -f
tail -f ~/.local/share/rclone-gdrive-sync/sync.log
tail -f ~/.local/share/rclone-gdrive-sync/watch.log

# Arrêter / redémarrer
systemctl --user stop rclone-gdrive-tray.service
systemctl --user restart rclone-gdrive-sync.timer
```

### Première synchronisation

Au premier lancement, `rclone bisync` effectue un `--resync` automatique pour établir la baseline. Les exécutions suivantes sont incrémentales.

## Architecture

```
rclone-gdrive-tray.py          ← Indicateur tray (pystray + AppIndicator3)
├── StatusWindow               ← Fenêtre GTK d'état détaillé
├── Menu intervalle/temps réel ← Configuration dynamique
└── notify_send                ← Notifications desktop

rclone-gdrive-sync-wrapper.sh  ← Wrapper timer (respecte realtime_watch)
└── rclone-gdrive-sync.sh      ← Synchro bisync effective

rclone-gdrive-watch.sh         ← Surveillance inotifywait + debounce
└── Déclenche sync après délai ← Évite les synchros trop rapprochées

rclone-gdrive-reconnect.sh     ← Reconnexion OAuth interactive
```

### Flux de données

```
Modification fichier → inotifywait → debounce (30s) → rclone bisync → GDrive
Timer systemd ──────────────────────────────────────→ rclone bisync → GDrive
Menu tray "Lancer synchro" ─────────────────────────→ rclone bisync → GDrive
```

## Licence

MIT — voir [LICENSE](LICENSE)

## Contribuer

Les issues et pull requests sont bienvenues. Pour développer localement :

```bash
# Modifier les scripts dans le repo, puis recharger
systemctl --user restart rclone-gdrive-tray.service
```