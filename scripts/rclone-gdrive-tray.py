#!/usr/bin/env python3
"""
Indicateur tray pour la synchronisation rclone GDrive.
Affiche une icône dans la barre de notification avec :
- État d'authentification OAuth
- Progression de la synchro (fichiers, %, direction)
- Clic gauche = ouvrir la fenêtre d'état
- Clic droit = menu contextuel
"""

import os
# Forcer le backend AppIndicator3 pour l'association correcte avec le dock GNOME
os.environ['PYSTRAY_BACKEND'] = 'appindicator'

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, GdkPixbuf, Pango
import pystray
from PIL import Image, ImageDraw, ImageFont
import subprocess
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

CONFIG_FILE = os.path.expanduser("~/.config/rclone/rclone.conf")
LOG_FILE = os.path.expanduser("~/.local/share/rclone-gdrive-sync/sync.log")
LOCK_FILE = "/tmp/rclone-gdrive-sync.lock"
SYNC_SCRIPT = os.path.expanduser("~/.local/bin/rclone-gdrive-sync.sh")
RECONNECT_SCRIPT = os.path.expanduser("~/.local/bin/rclone-gdrive-reconnect.sh")
DIRS_CONFIG = os.path.expanduser("~/.config/rclone-gdrive-sync/dirs.json")
ICON_BASE = os.path.expanduser("~/.local/share/rclone-gdrive-sync/icons/gdrive-sync-64.png")


class SyncState:
    """État partagé de la synchronisation."""
    def __init__(self):
        self.authenticated = False
        self.syncing = False
        self.direction = ""  # "upload", "download", "bidirectional"
        self.files_transferred = 0
        self.files_total = 0
        self.bytes_transferred = 0
        self.bytes_total = 0
        self.percent = 0.0
        self.speed = ""
        self.last_update = ""
        self.error = ""
        self.token_expiry = ""
        self.file_list = []  # Liste des fichiers en cours: [(name, direction, percent), ...]
        self.lock = threading.Lock()

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return {k: getattr(self, k) for k in [
                'authenticated', 'syncing', 'direction',
                'files_transferred', 'files_total',
                'bytes_transferred', 'bytes_total',
                'percent', 'speed', 'last_update', 'error', 'token_expiry',
                'file_list'
            ]}


state = SyncState()


def check_auth():
    """Vérifie si le token OAuth est valide via rclone about (rapide)."""
    try:
        result = subprocess.run(
            ["rclone", "about", "gdrive:", "--json"],
            capture_output=True, text=True, timeout=30
        )
        authenticated = result.returncode == 0
    except Exception:
        authenticated = False

    expiry = ""
    try:
        with open(CONFIG_FILE, 'r') as f:
            content = f.read()
        match = re.search(r'"expiry":"([^"]+)"', content)
        if match:
            expiry = match.group(1)
    except Exception:
        pass

    state.update(authenticated=authenticated, token_expiry=expiry)
    return authenticated


def parse_log_tail():
    """Parse les dernières lignes du log pour extraire la progression et la liste des fichiers."""
    try:
        if not os.path.exists(LOG_FILE):
            return {}

        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()[-200:]

        info = {}
        file_list = []

        for line in reversed(lines):
            line = line.strip()

            # Pattern rclone stats: Transferred: X / Y, Z%, Speed
            m = re.search(
                r'Transferred:\s+(\S+)\s*/\s*(\S+),\s*(\d+)%.*?Speed:\s*(\S+)',
                line
            )
            if m:
                info['bytes_transferred'] = m.group(1)
                info['bytes_total'] = m.group(2)
                info['percent'] = float(m.group(3))
                info['speed'] = m.group(4)

            # Fichiers transférés
            m = re.search(r'Transferred:\s*(\d+)\s*/\s*(\d+)', line)
            if m and 'files_transferred' not in info:
                info['files_transferred'] = int(m.group(1))
                info['files_total'] = int(m.group(2))

            # Direction globale
            if 'bisync' in line.lower():
                info['direction'] = 'bidirectionnelle'
            elif 'Copying' in line or 'Uploading' in line:
                info['direction'] = 'upload'
            elif 'Downloading' in line:
                info['direction'] = 'download'

            # Fichiers individuels en cours de transfert
            # Pattern: " * filename:  XX% /size" ou "Copying (uploading/downloading): filename"
            m_file = re.search(
                r'(?:Copying \(?(uploading|downloading)\)?|Transferring):\s+"?([^"]+)"?\s*:\s*(\d+)%',
                line, re.IGNORECASE
            )
            if m_file:
                direction = m_file.group(1).lower() if m_file.group(1) else ''
                if 'upload' in direction:
                    arrow = '⬆️'
                elif 'download' in direction:
                    arrow = '⬇️'
                else:
                    arrow = '🔄'
                pct = int(m_file.group(3))
                fname = m_file.group(2).strip()
                # Limiter la longueur du nom de fichier
                if len(fname) > 60:
                    fname = '...' + fname[-57:]
                file_list.append((fname, arrow, pct))

            # Aussi capturer les lignes de type "* filename size" pendant le transfert
            m_transfer = re.search(r'^\*\s+(.+?)\s+\d+\.\d+\w$', line)
            if m_transfer and len(file_list) < 50:
                fname = m_transfer.group(1).strip()
                if len(fname) > 60:
                    fname = '...' + fname[-57:]
                file_list.append((fname, '🔄', -1))

        # Dédupliquer et limiter à 100 entrées
        seen = set()
        unique_files = []
        for entry in file_list:
            key = entry[0]
            if key not in seen:
                seen.add(key)
                unique_files.append(entry)
        info['file_list'] = unique_files[:100]

        return info
    except Exception:
        return {}


def is_sync_running():
    """Vérifie si une synchro est en cours via le lock file ET un process rclone actif."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            # Vérifier aussi qu'un process rclone est réellement actif
            result = subprocess.run(
                ["pgrep", "-f", "rclone bisync"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
    except (ProcessLookupError, ValueError, OSError, subprocess.TimeoutExpired):
        pass
    return False


def load_dirs_config():
    """Charge la configuration des dossiers synchronisés."""
    try:
        with open(DIRS_CONFIG, 'r') as f:
            return json.load(f)
    except Exception:
        return {"directories": []}


def save_dirs_config(config):
    """Sauvegarde la configuration des dossiers synchronisés."""
    os.makedirs(os.path.dirname(DIRS_CONFIG), exist_ok=True)
    with open(DIRS_CONFIG, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def monitor_loop():
    """Boucle de monitoring en arrière-plan."""
    while True:
        auth = check_auth()
        syncing = is_sync_running()
        log_info = parse_log_tail() if syncing else {}

        updates = {
            'authenticated': auth,
            'syncing': syncing,
            'last_update': datetime.now().strftime('%H:%M:%S'),
        }
        if syncing and log_info:
            updates.update(log_info)
        elif not syncing:
            updates.update({
                'percent': 0, 'files_transferred': 0, 'files_total': 0,
                'direction': '', 'speed': '', 'error': ''
            })

        state.update(**updates)
        time.sleep(3)


def create_icon_image(authenticated, syncing, percent):
    """Crée l'image de l'icône tray en superposant un indicateur de statut sur l'icône personnalisée."""
    size = 64
    try:
        img = Image.open(ICON_BASE).convert('RGBA').resize((size, size), Image.LANCZOS)
    except Exception:
        # Fallback si l'icône n'est pas disponible
        img = Image.new('RGBA', (size, size), (66, 133, 244, 255))

    draw = ImageDraw.Draw(img)

    # Indicateur de statut en bas à droite
    dot_r = 10
    dot_cx, dot_cy = size - dot_r - 4, size - dot_r - 4

    if not authenticated:
        # Rouge = non authentifié
        draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
                     fill=(220, 50, 50, 255), outline=(255, 255, 255, 255), width=2)
        # X blanc au centre de l'icône
        draw.line([20, 20, size-20, size-20], fill=(255, 255, 255, 200), width=3)
        draw.line([size-20, 20, 20, size-20], fill=(255, 255, 255, 200), width=3)
    elif syncing:
        # Bleu = synchro en cours
        draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
                     fill=(66, 133, 244, 255), outline=(255, 255, 255, 255), width=2)
        # Arc de progression autour de l'icône
        if percent > 0:
            end_angle = -90 + (percent / 100.0 * 360)
            draw.arc([1, 1, size-1, size-1], start=-90, end=end_angle,
                     fill=(52, 168, 83, 255), width=3)
    else:
        # Vert = prêt et authentifié → retourner None pour utiliser l'icône du thème GTK
        return None

    return img


class StatusWindow(Gtk.Window):
    """Fenêtre affichant l'état détaillé de la synchronisation."""

    def __init__(self):
        super().__init__(title="Synchro GDrive")
        self.set_default_size(420, 320)
        self.set_resizable(False)
        self.set_border_width(15)
        self.connect("delete-event", self.on_close)

        # WM_CLASS pour association avec le .desktop et l'icône du dock
        self.set_wmclass("rclone-gdrive-sync", "RcloneGDriveSync")

        # Icône personnalisée pour le dock et la barre de titre
        icon_png = os.path.expanduser("~/.local/share/rclone-gdrive-sync/icons/gdrive-sync-128.png")
        try:
            self.set_icon_from_file(icon_png)
        except Exception:
            pass  # Fallback sur l'icône par défaut si le fichier n'existe pas

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(vbox)

        # Titre
        title = Gtk.Label()
        title.set_markup("<b><big>🔄 Synchronisation Google Drive</big></b>")
        title.set_halign(Gtk.Align.START)
        vbox.pack_start(title, False, False, 0)

        # Séparateur
        vbox.pack_start(Gtk.Separator(), False, False, 5)

        # Labels d'état
        self.lbl_auth = Gtk.Label()
        self.lbl_auth.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_auth, False, False, 0)

        self.lbl_status = Gtk.Label()
        self.lbl_status.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_status, False, False, 0)

        self.lbl_progress = Gtk.Label()
        self.lbl_progress.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_progress, False, False, 0)

        self.lbl_files = Gtk.Label()
        self.lbl_files.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_files, False, False, 0)

        self.lbl_speed = Gtk.Label()
        self.lbl_speed.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_speed, False, False, 0)

        self.lbl_token = Gtk.Label()
        self.lbl_token.set_halign(Gtk.Align.START)
        self.lbl_token.set_line_wrap(True)
        vbox.pack_start(self.lbl_token, False, False, 0)

        self.lbl_update = Gtk.Label()
        self.lbl_update.set_halign(Gtk.Align.START)
        vbox.pack_start(self.lbl_update, False, False, 0)

        # Barre de progression
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        vbox.pack_start(self.progress_bar, False, False, 5)

        # Liste scrollable des fichiers en cours
        file_label = Gtk.Label()
        file_label.set_markup("<b>Fichiers en cours :</b>")
        file_label.set_halign(Gtk.Align.START)
        vbox.pack_start(file_label, False, False, 0)

        self.file_store = Gtk.ListStore(str, str, str)  # nom, direction, %
        self.file_view = Gtk.TreeView(model=self.file_store)
        self.file_view.set_headers_visible(True)
        self.file_view.set_enable_search(False)

        col_name = Gtk.TreeViewColumn("Fichier", Gtk.CellRendererText(), text=0)
        col_name.set_expand(True)
        col_name.set_resizable(True)
        self.file_view.append_column(col_name)

        col_dir = Gtk.TreeViewColumn("Sens", Gtk.CellRendererText(), text=1)
        col_dir.set_min_width(50)
        self.file_view.append_column(col_dir)

        col_pct = Gtk.TreeViewColumn("%", Gtk.CellRendererText(), text=2)
        col_pct.set_min_width(50)
        self.file_view.append_column(col_pct)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.add(self.file_view)
        vbox.pack_start(scrolled, True, True, 0)

        # Bouton auth
        self.btn_auth = Gtk.Button(label="🔐 Se reconnecter")
        self.btn_auth.connect("clicked", self.on_reconnect)
        vbox.pack_start(self.btn_auth, False, False, 0)

        # Bouton sync manuelle
        self.btn_sync = Gtk.Button(label="▶ Lancer la synchronisation")
        self.btn_sync.connect("clicked", self.on_manual_sync)
        vbox.pack_start(self.btn_sync, False, False, 0)

        # Bouton gestion des dossiers
        self.btn_dirs = Gtk.Button(label="📁 Gérer les dossiers synchronisés")
        self.btn_dirs.connect("clicked", self.on_open_dirs_manager)
        vbox.pack_start(self.btn_dirs, False, False, 0)

        self.update_display()
        GLib.timeout_add_seconds(2, self.update_display)

    def on_open_dirs_manager(self, button):
        """Ouvre la fenêtre de gestion des dossiers synchronisés."""
        win = DirsManagerWindow(self)
        win.show_all()

    def on_close(self, widget, event):
        self.hide()
        return True  # Empêche la destruction

    def on_reconnect(self, button):
        button.set_sensitive(False)
        button.set_label("⏳ Reconnexion en cours...")
        threading.Thread(target=self._do_reconnect, args=(button,), daemon=True).start()

    def _do_reconnect(self, button):
        try:
            subprocess.run([RECONNECT_SCRIPT], timeout=130)
        except Exception:
            pass
        GLib.idle_add(lambda: button.set_sensitive(True))
        GLib.idle_add(lambda: button.set_label("🔐 Se reconnecter"))

    def on_manual_sync(self, button):
        if is_sync_running():
            return
        button.set_sensitive(False)
        button.set_label("⏳ Synchronisation en cours...")
        threading.Thread(target=self._do_sync, args=(button,), daemon=True).start()

    def _do_sync(self, button):
        try:
            subprocess.run([SYNC_SCRIPT], timeout=600)
        except Exception:
            pass
        GLib.idle_add(lambda: button.set_sensitive(True))
        GLib.idle_add(lambda: button.set_label("▶ Lancer la synchronisation"))

    def update_display(self):
        s = state.snapshot()

        # Auth
        if s['authenticated']:
            self.lbl_auth.set_markup("<b>✅ Authentifié</b>")
            self.btn_auth.set_sensitive(False)
            self.btn_auth.set_label("✅ Connecté")
        else:
            self.lbl_auth.set_markup("<b>❌ Non authentifié</b>")
            self.btn_auth.set_sensitive(True)
            self.btn_auth.set_label("🔐 Se reconnecter")

        # Status
        if s['syncing']:
            direction = s['direction'] or 'synchro'
            self.lbl_status.set_markup(f"<b>🔄 En cours :</b> {direction}")
            self.btn_sync.set_sensitive(False)
            self.btn_sync.set_label("⏳ Synchronisation en cours...")
        else:
            self.lbl_status.set_markup("<b>💤 Inactif</b>")
            self.btn_sync.set_sensitive(True)
            self.btn_sync.set_label("▶ Lancer la synchronisation")

        # Progression
        pct = s['percent']
        self.progress_bar.set_fraction(pct / 100.0)
        self.progress_bar.set_text(f"{pct:.0f}%")
        self.lbl_progress.set_markup(
            f"<b>Progression :</b> {pct:.1f}% "
            f"({s['bytes_transferred']} / {s['bytes_total']})"
        )

        # Fichiers
        self.lbl_files.set_markup(
            f"<b>Fichiers :</b> {s['files_transferred']} / {s['files_total']}"
        )

        # Vitesse
        self.lbl_speed.set_markup(f"<b>Vitesse :</b> {s['speed'] or 'N/A'}")

        # Token expiry
        if s['token_expiry']:
            try:
                exp = datetime.fromisoformat(s['token_expiry'].replace('Z', '+00:00'))
                now = datetime.now(exp.tzinfo) if exp.tzinfo else datetime.now()
                delta = exp - now
                if delta.total_seconds() > 0:
                    hours = int(delta.total_seconds() // 3600)
                    mins = int((delta.total_seconds() % 3600) // 60)
                    self.lbl_token.set_markup(
                        f"<b>Token :</b> expire dans {hours}h{mins:02d}min"
                    )
                else:
                    self.lbl_token.set_markup("<b>Token :</b> ⚠️ EXPIRÉ")
            except Exception:
                self.lbl_token.set_markup(f"<b>Token :</b> {s['token_expiry']}")
        else:
            self.lbl_token.set_markup("<b>Token :</b> inconnu")

        # Liste des fichiers en cours
        self.file_store.clear()
        for fname, direction, pct in s.get('file_list', []):
            pct_str = f"{pct}%" if pct >= 0 else "..."
            self.file_store.append([fname, direction, pct_str])

        # Dernière mise à jour
        self.lbl_update.set_markup(f"<small>Dernière MAJ : {s['last_update']}</small>")

        return True  # Continue le timer


class DirsManagerWindow(Gtk.Window):
    """Fenêtre de gestion des dossiers synchronisés."""

    def __init__(self, parent_window=None):
        super().__init__(title="Gérer les dossiers synchronisés")
        self.set_default_size(650, 450)
        self.set_border_width(15)
        self.parent_window = parent_window

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(vbox)

        # Titre
        title = Gtk.Label()
        title.set_markup("<b><big>📁 Dossiers synchronisés</big></b>")
        title.set_halign(Gtk.Align.START)
        vbox.pack_start(title, False, False, 0)

        info = Gtk.Label()
        info.set_markup("<small>Inclut les dossiers cachés (.xxx). Les modifications sont appliquées immédiatement.</small>")
        info.set_halign(Gtk.Align.START)
        info.set_line_wrap(True)
        vbox.pack_start(info, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 5)

        # Liste des dossiers avec toggle enabled
        self.dirs_store = Gtk.ListStore(bool, str, str, str, str)  # enabled, name, src, dst, type
        self.dirs_view = Gtk.TreeView(model=self.dirs_store)
        self.dirs_view.set_headers_visible(True)

        # Colonne toggle activé/désactivé
        renderer_toggle = Gtk.CellRendererToggle()
        renderer_toggle.connect("toggled", self.on_toggle_enabled)
        col_enabled = Gtk.TreeViewColumn("Actif", renderer_toggle, active=0)
        col_enabled.set_min_width(50)
        self.dirs_view.append_column(col_enabled)

        # Nom
        col_name = Gtk.TreeViewColumn("Nom", Gtk.CellRendererText(), text=1)
        col_name.set_expand(True)
        col_name.set_resizable(True)
        self.dirs_view.append_column(col_name)

        # Source locale
        col_src = Gtk.TreeViewColumn("Source locale", Gtk.CellRendererText(), text=2)
        col_src.set_expand(True)
        col_src.set_resizable(True)
        self.dirs_view.append_column(col_src)

        # Destination GDrive
        col_dst = Gtk.TreeViewColumn("Destination GDrive", Gtk.CellRendererText(), text=3)
        col_dst.set_expand(True)
        col_dst.set_resizable(True)
        self.dirs_view.append_column(col_dst)

        # Type
        col_type = Gtk.TreeViewColumn("Type", Gtk.CellRendererText(), text=4)
        col_type.set_min_width(80)
        self.dirs_view.append_column(col_type)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.dirs_view)
        vbox.pack_start(scrolled, True, True, 0)

        # Boutons d'action
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(hbox, False, False, 0)

        btn_add = Gtk.Button(label="➕ Ajouter un dossier")
        btn_add.connect("clicked", self.on_add_dir)
        hbox.pack_start(btn_add, False, False, 0)

        btn_remove = Gtk.Button(label="🗑️ Supprimer la sélection")
        btn_remove.connect("clicked", self.on_remove_dir)
        hbox.pack_start(btn_remove, False, False, 0)

        btn_refresh = Gtk.Button(label="🔄 Actualiser")
        btn_refresh.connect("clicked", lambda b: self.load_dirs())
        hbox.pack_start(btn_refresh, False, False, 0)

        hbox.pack_start(Gtk.Box(), True, True, 0)  # Spacer

        btn_close = Gtk.Button(label="Fermer")
        btn_close.connect("clicked", lambda b: self.destroy())
        hbox.pack_start(btn_close, False, False, 0)

        self.load_dirs()

    def load_dirs(self):
        """Charge les dossiers depuis dirs.json."""
        self.dirs_store.clear()
        config = load_dirs_config()
        for d in config.get("directories", []):
            self.dirs_store.append([
                d.get("enabled", True),
                d.get("name", ""),
                d.get("src", ""),
                d.get("dst", ""),
                d.get("type", "full"),
            ])

    def save_current_dirs(self):
        """Sauvegarde l'état actuel de la liste dans dirs.json."""
        directories = []
        for row in self.dirs_store:
            directories.append({
                "enabled": row[0],
                "name": row[1],
                "src": row[2],
                "dst": row[3],
                "type": row[4],
            })
        save_dirs_config({"directories": directories})

    def on_toggle_enabled(self, renderer, path):
        """Bascule l'état activé/désactivé d'un dossier."""
        self.dirs_store[path][0] = not self.dirs_store[path][0]
        self.save_current_dirs()

    def on_add_dir(self, button):
        """Ouvre une boîte de dialogue pour ajouter un dossier."""
        dialog = Gtk.Dialog(
            title="Ajouter un dossier",
            parent=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        dialog.set_default_size(450, 200)
        dialog.set_border_width(10)

        content = dialog.get_content_area()
        grid = Gtk.Grid()
        grid.set_column_spacing(10)
        grid.set_row_spacing(8)
        content.add(grid)

        lbl_name = Gtk.Label(label="Nom :")
        lbl_name.set_halign(Gtk.Align.END)
        entry_name = Gtk.Entry()
        entry_name.set_placeholder_text("ex: dot-ssh")
        grid.attach(lbl_name, 0, 0, 1, 1)
        grid.attach(entry_name, 1, 0, 1, 1)

        lbl_src = Gtk.Label(label="Source locale :")
        lbl_src.set_halign(Gtk.Align.END)
        entry_src = Gtk.Entry()
        entry_src.set_placeholder_text("ex: ~/.ssh ou ~/MonDossier")
        grid.attach(lbl_src, 0, 1, 1, 1)
        grid.attach(entry_src, 1, 1, 1, 1)

        lbl_dst = Gtk.Label(label="Destination GDrive :")
        lbl_dst.set_halign(Gtk.Align.END)
        entry_dst = Gtk.Entry()
        entry_dst.set_placeholder_text("ex: gdrive:SyncPC/dot-ssh")
        grid.attach(lbl_dst, 0, 2, 1, 1)
        grid.attach(entry_dst, 1, 2, 1, 1)

        lbl_type = Gtk.Label(label="Type :")
        lbl_type.set_halign(Gtk.Align.END)
        combo_type = Gtk.ComboBoxText()
        combo_type.append_text("full")
        combo_type.append_text("selective")
        combo_type.set_active(0)
        grid.attach(lbl_type, 0, 3, 1, 1)
        grid.attach(combo_type, 1, 3, 1, 1)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            name = entry_name.get_text().strip()
            src = entry_src.get_text().strip()
            dst = entry_dst.get_text().strip()
            dtype = combo_type.get_active_text()

            if name and src and dst:
                self.dirs_store.append([True, name, src, dst, dtype])
                self.save_current_dirs()

        dialog.destroy()

    def on_remove_dir(self, button):
        """Supprime le dossier sélectionné."""
        selection = self.dirs_view.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter is not None:
            name = model[treeiter][1]
            confirm = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text=f"Supprimer le dossier « {name} » ?",
            )
            confirm.format_secondary_text("Le dossier ne sera plus synchronisé.")
            resp = confirm.run()
            confirm.destroy()
            if resp == Gtk.ResponseType.YES:
                model.remove(treeiter)
                self.save_current_dirs()


def notify_send(title, message, urgency="normal"):
    """Envoie une notification desktop via notify-send."""
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, title, message],
            capture_output=True, timeout=5
        )
    except Exception:
        pass  # Silencieux si notify-send échoue


class TrayApp:
    """Application tray indicator principale."""

    def __init__(self):
        self.window = StatusWindow()
        self.icon = None
        self._last_icon_state = None

    def setup_tray(self):
        # Sous-menu intervalle de synchronisation
        interval_menu = pystray.Menu(
            pystray.MenuItem("1 min", lambda: self.set_interval(1)),
            pystray.MenuItem("5 min", lambda: self.set_interval(5)),
            pystray.MenuItem("15 min", lambda: self.set_interval(15)),
            pystray.MenuItem("30 min", lambda: self.set_interval(30)),
            pystray.MenuItem("1 heure", lambda: self.set_interval(60)),
        )

        menu = pystray.Menu(
            pystray.MenuItem("Ouvrir la fenêtre", self.show_window, default=True),
            pystray.MenuItem("Lancer la synchro", self.trigger_sync),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Intervalle", interval_menu),
            pystray.MenuItem("Temps réel", self.toggle_realtime),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", self.quit_app),
        )

        img = create_icon_image(False, False, 0)
        # Le titre doit correspondre au StartupWMClass du .desktop pour l'association dock
        self.icon = pystray.Icon("RcloneGDriveSync", img, "Synchro GDrive", menu)
        self.icon.run_detached()

        # Timer pour mettre à jour l'icône
        GLib.timeout_add_seconds(3, self.update_tray_icon)

    def show_window(self, icon=None, item=None):
        GLib.idle_add(self.window.show_all)
        GLib.idle_add(self.window.present)

    def trigger_sync(self, icon=None, item=None):
        if not is_sync_running():
            subprocess.Popen([SYNC_SCRIPT])

    def set_interval(self, minutes):
        """Met à jour l'intervalle de synchronisation dans settings.json et reconfigure le timer systemd."""
        settings_file = os.path.expanduser("~/.config/rclone-gdrive-sync/settings.json")
        try:
            with open(settings_file, 'r') as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            settings = {}
        settings['sync_interval_minutes'] = minutes
        with open(settings_file, 'w') as f:
            json.dump(settings, f, indent=2)
        # Reconfigurer le timer systemd
        subprocess.run(["systemctl", "--user", "stop", "rclone-gdrive-watch.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "restart", "rclone-gdrive-sync.timer"], capture_output=True)
        notify_send("Intervalle mis à jour", f"Synchronisation toutes les {minutes} minute(s)")

    def toggle_realtime(self, icon=None, item=None):
        """Active ou désactive la surveillance temps réel via inotifywait."""
        settings_file = os.path.expanduser("~/.config/rclone-gdrive-sync/settings.json")
        try:
            with open(settings_file, 'r') as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            settings = {}
        current = settings.get('realtime_watch', False)
        settings['realtime_watch'] = not current
        with open(settings_file, 'w') as f:
            json.dump(settings, f, indent=2)
        if not current:
            # Activer le watch
            subprocess.run(["systemctl", "--user", "start", "rclone-gdrive-watch.service"], capture_output=True)
            subprocess.run(["systemctl", "--user", "stop", "rclone-gdrive-sync.timer"], capture_output=True)
            notify_send("Temps réel activé", "Surveillance des fichiers en cours")
        else:
            # Désactiver le watch, réactiver le timer
            subprocess.run(["systemctl", "--user", "stop", "rclone-gdrive-watch.service"], capture_output=True)
            interval = settings.get('sync_interval_minutes', 5)
            subprocess.run(["systemctl", "--user", "start", "rclone-gdrive-sync.timer"], capture_output=True)
            notify_send("Temps réel désactivé", f"Retour au mode intervalle ({interval} min)")

    def quit_app(self, icon=None, item=None):
        if self.icon:
            self.icon.stop()
        Gtk.main_quit()

    def update_tray_icon(self):
        s = state.snapshot()
        new_state = (s['authenticated'], s['syncing'], int(s['percent']))
        if new_state != self._last_icon_state:
            img = create_icon_image(*new_state)
            # Ne mettre à jour l'icône que si un indicateur de statut est nécessaire
            # Sinon, laisser GNOME utiliser l'icône du thème GTK via IconName D-Bus
            if img is not None:
                self.icon.icon = img

            tooltip_parts = []
            if not s['authenticated']:
                tooltip_parts.append("❌ Non authentifié")
            elif s['syncing']:
                tooltip_parts.append(f"🔄 Synchro {s['percent']:.0f}%")
                if s['direction']:
                    tooltip_parts.append(s['direction'])
            else:
                tooltip_parts.append("✅ Prêt")

            if s['token_expiry']:
                tooltip_parts.append(f"Token: {s['token_expiry'][:16]}")

            self.icon.title = " | ".join(tooltip_parts)
            self._last_icon_state = new_state

        return True  # Continue le timer


def main():
    # Démarrer le monitoring en arrière-plan
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    app = TrayApp()
    app.setup_tray()
    Gtk.main()


if __name__ == "__main__":
    main()