"""main_window.py — EC2FileManager: the application's main window."""

import os
import re
import stat
from typing import Optional

import paramiko

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QLabel,
    QMessageBox, QTextEdit, QSplitter, QStatusBar, QFrame,
    QSizePolicy, QDialog, QDialogButtonBox, QInputDialog, QMenu,
    QAbstractItemView, QProgressBar, QTabWidget, QComboBox,
    QApplication, QShortcut,
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence

import themes as _themes
from themes import T, THEMES, apply_theme_vars, build_qss, apply_qss_to, save_settings
from utils import classify, icon_for, size_fmt, add_recent_instance, monospace_font
from sudo_fs import SudoFS
from workers import CommandWorker, ConnectWorker, track_worker
from dialogs import ConnectDialog, FileTransferDialog, FileEditorDialog, FileExecDialog, SearchDialog, ConnectingDialog, MediaPlayerDialog
from sidebar import Sidebar
from preview import PreviewPane
from file_widgets import FileRowWidget, FileGridWidget
from terminal_widget import TerminalWidget
from kubernetes_tab import KubernetesTab
from theme_picker import ThemePicker

# Extensions that can be executed remotely
_EXECUTABLE_EXTS = {
    ".py", ".sh", ".bash", ".rb", ".js", ".ts", ".php", ".pl",
    ".lua", ".r", ".go", ".java", ".kt", ".rs", ".c", ".cpp", ".swift",
}
# Extensions that are editable as text
_EDITABLE_KINDS = {"text", "code", "key"}
# Kinds that open in the built-in media player instead of the text editor
_MEDIA_KINDS = {"video", "audio"}


class _TerminalPopoutWindow(QDialog):
    """Floating window that hosts the terminal when popped out. Behaves like
    a normal top-level window (resizable, minimizable, has its own close
    button) but reports back to the main window when closed — whether that
    happens via the 'X' button or programmatically — so the terminal widget
    can be re-docked into the main layout instead of being destroyed."""

    def __init__(self, parent, on_close):
        super().__init__(parent, Qt.Window)
        self._on_close = on_close
        self.setWindowTitle("Terminal")
        self.resize(760, 420)

    def closeEvent(self, event):
        self._on_close()
        event.accept()


class EC2FileManager(QMainWindow):

    SORT_KEY_FUNCS = {
        "Name": lambda m: m["name"].lower(),
        "Size": lambda m: m["size"],
        "Type": lambda m: (m["kind"], m["name"].lower()),
    }

    def __init__(self):
        super().__init__()
        self.ssh           = None
        self.sftp          = None
        self.current_path  = "/"
        self.history       = []
        self.future        = []
        self._items        = []
        self.host_label    = ""
        self._sudo_user    = None  # type: Optional[str]
        # Local (client-side) connection details, kept around so file
        # transfers can shell out to the system `scp` binary directly
        # instead of going through paramiko's SFTP implementation.
        self._conn_host    = None  # type: Optional[str]
        self._conn_port    = 22
        self._conn_user    = None  # type: Optional[str]
        self._conn_pem     = None  # type: Optional[str]
        self._terminal_cwd = None  # type: Optional[str]
        self._pending_conn  = {}
        self._connecting_dlg = None
        self._connect_worker = None
        self.view_mode     = "list"
        self.sort_key      = "Name"
        self.sort_reverse  = False
        self._workers      = []
        self._cwd_workers  = []

        self.setWindowTitle("EC2 Manager")
        self.resize(1260, 740)
        apply_qss_to(self)
        self._build_ui()
        self._set_connected(False)
        self.terminal.show_prompt("(not connected)$ ")

    # ── UI construction ───────────────────────────────────────
    def _build_ui(self):
        # ── Toolbar (plain QWidget — avoids QToolBar proxy glitches) ──
        tb_widget = QWidget()
        tb_widget.setFixedHeight(44)
        self._toolbar = tb_widget
        tb = QHBoxLayout(tb_widget)
        tb.setContentsMargins(8, 0, 8, 0)
        tb.setSpacing(4)

        def _tbtn(text, tooltip="", checkable=False, width=None):
            b = QPushButton(text)
            b.setToolTip(tooltip)
            b.setStyleSheet("padding: 4px 10px;")
            if checkable:
                b.setCheckable(True)
            if width:
                b.setFixedWidth(width)
            return b

        def _icon_btn(text, tooltip="", checkable=False):
            """Compact square icon button."""
            b = QPushButton(text)
            b.setToolTip(tooltip)
            b.setFixedSize(30, 28)
            b.setStyleSheet("padding: 0; font-size: 14px;")
            if checkable:
                b.setCheckable(True)
            return b

        def _sep():
            f = QFrame()
            f.setFrameShape(QFrame.VLine)
            f.setFixedWidth(1)
            f.setFixedHeight(20)
            f.setStyleSheet(f"background: {T['BORDER']};")
            return f

        def _lbl(text):
            l = QLabel(text)
            l.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
            return l

        # Nav buttons (FM-only)
        self.act_back    = _icon_btn("◀", "Back")
        self.act_forward = _icon_btn("▶", "Forward")
        self.act_up      = _icon_btn("↑", "Go up")
        self.act_refresh = _icon_btn("↺", "Refresh")

        self.act_back.clicked.connect(self._go_back)
        self.act_forward.clicked.connect(self._go_forward)
        self.act_up.clicked.connect(self._go_up)
        self.act_refresh.clicked.connect(self._refresh)

        self._fm_seps = []   # separators that only appear in FM mode

        for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
            tb.addWidget(b)
        sep1 = _sep(); tb.addWidget(sep1); self._fm_seps.append(sep1)

        self.addr_bar = QLineEdit()
        self.addr_bar.setPlaceholderText("Path…")
        self.addr_bar.returnPressed.connect(self._navigate_addr)
        tb.addWidget(self.addr_bar, 3)
        sep2 = _sep(); tb.addWidget(sep2); self._fm_seps.append(sep2)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter…")
        self.search_bar.textChanged.connect(self._filter_list)
        self.search_bar.setFixedWidth(140)
        tb.addWidget(self.search_bar)
        sep3 = _sep(); tb.addWidget(sep3); self._fm_seps.append(sep3)

        self._sort_lbl = _lbl("Sort")
        tb.addWidget(self._sort_lbl)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Size", "Type"])
        self.sort_combo.setCurrentText(self.sort_key)
        self.sort_combo.currentTextChanged.connect(self._change_sort_key)
        self.sort_combo.setFixedWidth(80)
        tb.addWidget(self.sort_combo)

        self.sort_dir_btn = _icon_btn("↑", "Toggle sort direction")
        self.sort_dir_btn.clicked.connect(self._toggle_sort_direction)
        tb.addWidget(self.sort_dir_btn)
        sep4 = _sep(); tb.addWidget(sep4); self._fm_seps.append(sep4)

        self.view_list_btn = _icon_btn("☰", "List view", checkable=True)
        self.view_list_btn.setChecked(True)
        self.view_list_btn.clicked.connect(lambda: self._set_view_mode("list"))
        tb.addWidget(self.view_list_btn)

        self.view_grid_btn = _icon_btn("▦", "Grid view", checkable=True)
        self.view_grid_btn.clicked.connect(lambda: self._set_view_mode("grid"))
        tb.addWidget(self.view_grid_btn)
        sep5 = _sep(); tb.addWidget(sep5); self._fm_seps.append(sep5)

        self.theme_picker = ThemePicker(_themes.CURRENT_THEME)
        self.theme_picker.theme_changed.connect(self._change_theme)
        tb.addWidget(self.theme_picker)
        tb.addWidget(_sep())  # always-visible sep before connect buttons

        tb.addStretch()

        self.act_connect    = _tbtn("⚡ Connect",   "Connect to server")
        self.act_disconnect = _tbtn("✕ Disconnect", "Disconnect")
        self.act_connect.clicked.connect(self._connect)
        self.act_disconnect.clicked.connect(self._disconnect)
        tb.addWidget(self.act_connect)
        tb.addWidget(self.act_disconnect)

        # Main tabs
        self.main_tabs = QTabWidget()
        self.main_tabs.setTabPosition(QTabWidget.North)
        self.main_tabs.currentChanged.connect(self._on_main_tab_change)

        # Root layout: toolbar on top, tabs below
        root_widget = QWidget()
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(tb_widget)
        root_layout.addWidget(self.main_tabs)
        self.setCentralWidget(root_widget)

        # ── File manager tab ──────────────────────────────────
        fm_widget = QWidget()
        fm_root   = QVBoxLayout(fm_widget)
        fm_root.setContentsMargins(0, 0, 0, 0)
        fm_root.setSpacing(0)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 0)
        self.progress.hide()
        fm_root.addWidget(self.progress)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self._nav_to)
        splitter.addWidget(self.sidebar)

        list_col = QWidget()
        list_col.setStyleSheet("background: {};".format(T['BG_DARK']))
        list_layout = QVBoxLayout(list_col)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)

        self.list_header = self._build_list_header()
        list_layout.addWidget(self.list_header)

        self.file_list = QListWidget()
        self.file_list.setMouseTracking(True)
        self.file_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.file_list.setSpacing(0)
        self.file_list.itemClicked.connect(self._on_click)
        self.file_list.itemDoubleClicked.connect(self._on_double_click)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._ctx_menu)
        QShortcut(QKeySequence("F2"), self.file_list).activated.connect(self._rename_selected)
        list_layout.addWidget(self.file_list)

        # ── Row 1: file action buttons ────────────────────────
        act_bar = QWidget()
        act_bar.setFixedHeight(44)
        act_bar.setStyleSheet("background: {}; border-top: 1px solid {};".format(
            T['BG_PANEL'], T['BORDER']))
        act_layout = QHBoxLayout(act_bar)
        act_layout.setContentsMargins(8, 0, 8, 0)
        act_layout.setSpacing(6)

        for label, slot in [
            ("⬆  Upload",     self._upload),
            ("⬇  Download",   self._download),
            ("✏️  Edit",       self._edit_selected),
            ("▶  Run",        self._run_selected),
            ("🔍  Search",    self._open_search),
            ("➕  New Folder",  self._new_folder),
            ("⌨  Rename",     self._rename_selected),
            ("✕  Delete",     self._delete),
        ]:
            btn = QPushButton(label)
            btn.setFixedSize(118, 32)
            btn.clicked.connect(slot)
            act_layout.addWidget(btn)
        act_layout.addStretch()
        list_layout.addWidget(act_bar)

        self._list_col = list_col
        self._act_bar  = act_bar
        splitter.addWidget(list_col)

        # Right column: preview + terminal
        right_col = QSplitter(Qt.Vertical)
        right_col.setHandleWidth(1)

        self.preview = PreviewPane()
        right_col.addWidget(self.preview)

        terminal_widget = QWidget()
        terminal_widget.setStyleSheet("background: {};".format(T['BG_PANEL']))
        tl = QVBoxLayout(terminal_widget)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        self.t_header = QLabel("  Terminal")
        self.t_header.setFixedHeight(28)
        header_row.addWidget(self.t_header, 1)

        self.terminal_popout_btn = QPushButton("⤢")
        self.terminal_popout_btn.setToolTip("Open terminal in its own window")
        self.terminal_popout_btn.setFixedSize(28, 28)
        self.terminal_popout_btn.setStyleSheet("padding: 0px;")
        self.terminal_popout_btn.clicked.connect(self._toggle_terminal_popout)
        header_row.addWidget(self.terminal_popout_btn)

        tl.addLayout(header_row)

        # Single scrolling surface — type directly at the prompt shown in the
        # same history as the output, instead of a separate input box above
        # a separate read-only output box.
        self.terminal = TerminalWidget()
        self.terminal.command_entered.connect(self._on_terminal_command)
        self.terminal.interrupt_requested.connect(self._on_terminal_interrupt)
        tl.addWidget(self.terminal)

        self.terminal_dock_placeholder = QLabel("Terminal is open in its own window.")
        self.terminal_dock_placeholder.setAlignment(Qt.AlignCenter)
        self.terminal_dock_placeholder.setStyleSheet(f"color: {T['TEXT_MUTED']}; padding: 24px;")
        self.terminal_dock_placeholder.hide()
        tl.addWidget(self.terminal_dock_placeholder)

        right_col.addWidget(terminal_widget)
        right_col.setSizes([320, 320])

        self._terminal_widget = terminal_widget
        self._terminal_home_layout = tl
        self._terminal_popout_win  = None
        splitter.addWidget(right_col)
        splitter.setSizes([180, 600, 300])
        fm_root.addWidget(splitter)

        self.main_tabs.addTab(fm_widget, "📁  File Manager")

        # ── Kubernetes tab ────────────────────────────────────
        self.k8s_tab = KubernetesTab()
        self.k8s_tab.status_msg.connect(lambda m: self.status.showMessage(m))
        self.main_tabs.addTab(self.k8s_tab, "⎈  Kubernetes")

        # ── Status bar ────────────────────────────────────────
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.conn_lbl = QLabel("⚫  Not connected")
        self.conn_lbl.setStyleSheet("color: {};".format(T['TEXT_MUTED']))
        self.status.addPermanentWidget(self.conn_lbl)

        self.sudo_badge = QLabel()
        self.sudo_badge.hide()
        self.status.addPermanentWidget(self.sudo_badge)

        self.status.showMessage("Ready")
        self._apply_inline_styles()

    def _apply_inline_styles(self):
        self._toolbar.setStyleSheet(
            "background: {}; border-bottom: 1px solid {};".format(T['BG_PANEL'], T['BORDER'])
        )
        self._list_col.setStyleSheet("background: {};".format(T['BG_DARK']))
        self._act_bar.setStyleSheet("background: {}; border-top: 1px solid {};".format(
            T['BG_PANEL'], T['BORDER']))
        self._terminal_widget.setStyleSheet("background: {};".format(T['BG_PANEL']))
        self.t_header.setStyleSheet(
            "background: {bg}; color: {fg}; "
            "border-top: 1px solid {b}; border-bottom: 1px solid {b}; "
            "font-size: 10px; font-weight: 700; letter-spacing: 1px; padding-left: 10px;".format(
                bg=T['BG_PANEL'], fg=T['TEXT_MUTED'], b=T['BORDER'])
        )
        self.terminal.setStyleSheet(
            "background: #0d0d1a; color: {}; border: none; padding: 8px;".format(T['SUCCESS'])
        )
        self.conn_lbl.setStyleSheet(
            "color: {};".format(T['SUCCESS'] if self.ssh else T['TEXT_MUTED'])
        )
        self._update_sudo_badge()

    # ── Theme ─────────────────────────────────────────────────
    def _change_theme(self, theme_name):
        apply_theme_vars(theme_name)
        save_settings()
        qss = build_qss()
        self.setStyleSheet(qss)

        app = QApplication.instance()
        pal = QPalette()
        pal.setColor(QPalette.Window,          QColor(T['BG_DARK']))
        pal.setColor(QPalette.WindowText,      QColor(T['TEXT_PRIMARY']))
        pal.setColor(QPalette.Base,            QColor(T['BG_PANEL']))
        pal.setColor(QPalette.AlternateBase,   QColor(T['BG_ITEM']))
        pal.setColor(QPalette.Text,            QColor(T['TEXT_PRIMARY']))
        pal.setColor(QPalette.Button,          QColor(T['BG_ITEM']))
        pal.setColor(QPalette.ButtonText,      QColor(T['TEXT_PRIMARY']))
        pal.setColor(QPalette.Highlight,       QColor(T['ACCENT']))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        app.setPalette(pal)

        self._apply_inline_styles()
        self.sidebar.refresh_theme()
        self.preview.refresh_theme()
        self.k8s_tab.apply_theme()
        self._rebuild_list_header()
        self.theme_picker.refresh_theme()

        if self.sftp:
            self._refresh(push_history=True)

        self.status.showMessage("Theme changed to {}".format(theme_name))

    # ── List header ───────────────────────────────────────────
    def _build_list_header(self):
        COL_ICON = FileRowWidget.COL_ICON
        COL_SIZE = FileRowWidget.COL_SIZE
        COL_TYPE = FileRowWidget.COL_TYPE

        hdr = QWidget()
        hdr.setFixedHeight(28)
        hdr.setStyleSheet("background: {}; border-bottom: 1px solid {};".format(
            T['BG_PANEL'], T['BORDER']))

        layout = QHBoxLayout(hdr)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(0)

        def _lbl(text, align=Qt.AlignLeft | Qt.AlignVCenter):
            l = QLabel(text)
            l.setStyleSheet(
                "color: {}; font-size: 13px; font-weight: 700; letter-spacing: 0.5px;".format(
                    T['TEXT_MUTED'])
            )
            l.setAlignment(align)
            return l

        spacer = QWidget()
        spacer.setFixedWidth(COL_ICON)
        spacer.setStyleSheet("background: transparent;")
        layout.addWidget(spacer)
        layout.addWidget(_lbl("Name"), 1)

        size_lbl = _lbl("Size", Qt.AlignRight | Qt.AlignVCenter)
        size_lbl.setFixedWidth(COL_SIZE)
        layout.addWidget(size_lbl)
        layout.addSpacing(8)

        type_lbl = _lbl("Type")
        type_lbl.setFixedWidth(COL_TYPE)
        layout.addWidget(type_lbl)
        return hdr

    def _rebuild_list_header(self):
        old    = self.list_header
        new    = self._build_list_header()
        layout = self._list_col.layout()
        layout.replaceWidget(old, new)
        old.deleteLater()
        self.list_header = new
        if self.view_mode == "grid":
            self.list_header.hide()

    # ── Tab visibility ────────────────────────────────────────
    def _on_main_tab_change(self, idx):
        fm_mode = (idx == 0)
        for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
            b.setVisible(fm_mode)
        self.addr_bar.setVisible(fm_mode)
        self.search_bar.setVisible(fm_mode)
        self._sort_lbl.setVisible(fm_mode)
        self.sort_combo.setVisible(fm_mode)
        self.sort_dir_btn.setVisible(fm_mode)
        self.view_list_btn.setVisible(fm_mode)
        self.view_grid_btn.setVisible(fm_mode)
        for sep in self._fm_seps:
            sep.setVisible(fm_mode)

    # ── Connection state ──────────────────────────────────────
    def _set_connected(self, ok):
        for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
            b.setEnabled(ok)
        self.act_connect.setEnabled(not ok)
        self.act_disconnect.setEnabled(ok)
        if ok:
            self.conn_lbl.setText("🟢  {}".format(self.host_label))
            self.conn_lbl.setStyleSheet("color: {};".format(T['SUCCESS']))
        else:
            self.conn_lbl.setText("⚫  Not connected")
            self.conn_lbl.setStyleSheet("color: {};".format(T['TEXT_MUTED']))

    def _connect(self):
        dlg = ConnectDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        # Unpack 6-tuple — alias is the new 6th value
        host, port, user, pem, password, alias = dlg.values()
        if not host or not user:
            QMessageBox.warning(self, "Missing info", "Host and username are required.")
            return

        # Stash the details needed once the background worker reports back.
        self._pending_conn = dict(host=host, port=port, user=user, pem=pem, alias=alias)

        self.progress.show()
        self.status.showMessage("Connecting to {}…".format(host))

        self._connecting_dlg = ConnectingDialog(self, host)
        self._connecting_dlg.show()

        # Run the SSH handshake off the UI thread so the "Connecting…" dialog
        # keeps animating instead of freezing for the duration of the call.
        self._connect_worker = ConnectWorker(host, port, user, pem, password)
        self._connect_worker.connected.connect(self._on_connect_success)
        self._connect_worker.error.connect(self._on_connect_error)
        self._connect_worker.start()

    def _on_connect_success(self, ssh, sftp, home):
        info = self._pending_conn
        host, port, user, pem, alias = info["host"], info["port"], info["user"], info["pem"], info["alias"]

        self.ssh  = ssh
        self.sftp = SudoFS(sftp, ssh)
        # Use alias as the display label when set, else user@host
        self.host_label = alias if alias else "{}@{}".format(user, host)
        self._conn_host, self._conn_port = host, port
        self._conn_user, self._conn_pem  = user, pem
        self._set_connected(True)
        self.k8s_tab.set_ssh(self.ssh)
        # Local (client-side) connection details for the port-tunnel
        # feature, which runs `ssh` on this machine rather than over
        # the remote self.ssh session.
        self.k8s_tab.set_connection_info(host, port, user, pem)
        self.sidebar.populate_remote(self.sftp)
        # Pass alias to persist it in the CSV
        add_recent_instance(host, port, user, pem, alias)
        self._nav_to("")
        self._terminal_cwd = home or None
        self.terminal.clear()
        self.terminal.write_output("Connected to {}.".format(self.host_label))
        self.terminal.show_prompt(self._prompt_str())
        self.status.showMessage("Connected successfully")
        self._finish_connect_ui()

    def _on_connect_error(self, message):
        QMessageBox.critical(self, "Connection Failed", message)
        self.status.showMessage("Connection failed")
        self._finish_connect_ui()

    def _finish_connect_ui(self):
        if getattr(self, "_connecting_dlg", None):
            self._connecting_dlg.hide()
            self._connecting_dlg.deleteLater()
            self._connecting_dlg = None
        self.progress.hide()

    def _disconnect(self):
        try:
            if self.sftp: self.sftp.close()
            if self.ssh:  self.ssh.close()
        except Exception:
            pass
        self.ssh = self.sftp = None
        self._sudo_user = None
        self._conn_host = self._conn_user = self._conn_pem = None
        self._conn_port = 22
        self.k8s_tab.set_ssh(None)
        self.k8s_tab.clear_connection_info()
        self.sidebar.clear_remote()
        self.file_list.clear()
        self._items = []
        self.preview.clear()
        self._set_connected(False)
        self.sudo_badge.hide()
        self.addr_bar.clear()
        self.terminal.write_output("[disconnected]")
        self.terminal.show_prompt("(not connected)$ ")
        self.status.showMessage("Disconnected")

    # ── Navigation ────────────────────────────────────────────
    def _nav_to(self, path):
        if not self.sftp:
            return
        try:
            resolved = self.sftp.normalize(path or "")
        except Exception as e:
            QMessageBox.warning(self, "Navigation Error", str(e))
            return
        if self.current_path and self.current_path != resolved:
            self.history.append(self.current_path)
            self.future.clear()
        self.current_path = resolved
        self._refresh()

    def _go_back(self):
        if self.history:
            self.future.append(self.current_path)
            self.current_path = self.history.pop()
            self._refresh(push_history=False)

    def _go_forward(self):
        if self.future:
            self.history.append(self.current_path)
            self.current_path = self.future.pop()
            self._refresh(push_history=False)

    def _go_up(self):
        parent = os.path.dirname(self.current_path) or "/"
        if parent != self.current_path:
            self._nav_to(parent)

    def _navigate_addr(self):
        self._nav_to(self.addr_bar.text().strip())

    # ── Directory refresh ─────────────────────────────────────
    def _refresh(self, push_history=True):
        if not self.sftp:
            return
        self.progress.show()
        self.file_list.clear()
        self._items = []
        self.preview.clear()
        self.search_bar.clear()
        try:
            entries = self.sftp.listdir_attr(self.current_path)
            metas   = []
            for entry in entries:
                is_dir = stat.S_ISDIR(entry.st_mode or 0)
                kind   = classify(entry.filename, is_dir, entry.st_mode or 0)
                size   = entry.st_size or 0
                mode   = oct(entry.st_mode)[-3:] if entry.st_mode else "---"
                metas.append({"name": entry.filename, "kind": kind, "size": size,
                               "mode": mode, "is_dir": is_dir})
            self._items = self._sort_items(metas)
            for meta in self._items:
                self._add_row(meta)
        except Exception as e:
            msg = str(e)
            if self._sudo_user:
                msg += "\n\n(Running as sudo user '{}')".format(self._sudo_user)
            QMessageBox.critical(self, "Error", msg)
        finally:
            self.progress.hide()

        self.addr_bar.setText(self.current_path)
        self.setWindowTitle("EC2 Manager — {}".format(self.current_path))
        n = len(self._items)
        self.status.showMessage("{} item{} in {}".format(
            n, "s" if n != 1 else "", self.current_path))

    # ── List population helpers ───────────────────────────────
    def _sort_items(self, items):
        key_func = self.SORT_KEY_FUNCS.get(self.sort_key, self.SORT_KEY_FUNCS["Name"])
        items = sorted(items, key=key_func, reverse=self.sort_reverse)
        items = sorted(items, key=lambda m: not m["is_dir"])
        return items

    def _relist(self):
        self.file_list.clear()
        for meta in self._items:
            self._add_row(meta)

    def _add_row(self, meta):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, meta)
        if self.view_mode == "grid":
            item.setSizeHint(QSize(FileGridWidget.TILE_W, FileGridWidget.TILE_H))
            self.file_list.addItem(item)
            self.file_list.setItemWidget(item, FileGridWidget(meta))
        else:
            item.setSizeHint(QSize(0, 36))
            self.file_list.addItem(item)
            self.file_list.setItemWidget(item, FileRowWidget(meta))

    def _filter_list(self, text):
        q = text.lower()
        self.file_list.clear()
        for meta in self._items:
            if q in meta["name"].lower():
                self._add_row(meta)

    # ── Sort / view ───────────────────────────────────────────
    def _change_sort_key(self, key):
        self.sort_key = key
        self._items   = self._sort_items(self._items)
        self._relist()
        self.status.showMessage("Sorted by {}".format(key))

    def _toggle_sort_direction(self):
        self.sort_reverse = not self.sort_reverse
        self.sort_dir_btn.setText("↓" if self.sort_reverse else "↑")
        self._items = self._sort_items(self._items)
        self._relist()

    def _set_view_mode(self, mode):
        self.view_mode = mode
        self.view_list_btn.setChecked(mode == "list")
        self.view_grid_btn.setChecked(mode == "grid")
        if mode == "grid":
            self.file_list.setViewMode(QListWidget.IconMode)
            self.file_list.setFlow(QListWidget.LeftToRight)
            self.file_list.setWrapping(True)
            self.file_list.setResizeMode(QListWidget.Adjust)
            self.file_list.setMovement(QListWidget.Static)
            self.file_list.setSpacing(10)
            self.file_list.setGridSize(QSize(FileGridWidget.TILE_W + 14, FileGridWidget.TILE_H + 14))
            self.list_header.hide()
        else:
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setFlow(QListWidget.TopToBottom)
            self.file_list.setWrapping(False)
            self.file_list.setResizeMode(QListWidget.Adjust)
            self.file_list.setMovement(QListWidget.Static)
            self.file_list.setSpacing(0)
            self.file_list.setGridSize(QSize())
            self.list_header.show()
        self._relist()
        self.status.showMessage("{} view".format(mode.capitalize()))

    # ── Item interaction ──────────────────────────────────────
    def _on_click(self, item):
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        self.preview.show_entry(meta["name"], meta["kind"], meta["size"], meta["mode"])
        if meta["kind"] in ("text", "code") and not meta["is_dir"]:
            self._fetch_preview(meta["name"])

    def _on_double_click(self, item):
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        if meta["is_dir"]:
            self._nav_to(self.current_path.rstrip("/") + "/" + meta["name"])
        else:
            self._open_file(meta)

    def _open_file(self, meta):
        """Double-click handler: edit text/code, offer Run for scripts, download otherwise."""
        kind = meta["kind"]
        name = meta["name"]
        ext  = os.path.splitext(name)[1].lower()

        if kind in _MEDIA_KINDS:
            self._play_media(meta)
        elif kind in _EDITABLE_KINDS:
            self._edit_file(meta)
        elif ext in _EXECUTABLE_EXTS:
            self._exec_file(meta)
        else:
            save, _ = QFileDialog.getSaveFileName(self, "Save File", name)
            if save:
                remote = self.current_path.rstrip("/") + "/" + name
                FileTransferDialog.download(
                    self, self.sftp, remote, save,
                    host=self._conn_host, port=self._conn_port,
                    user=self._conn_user, pem=self._conn_pem,
                    sudo_user=self._sudo_user,
                )
                self.status.showMessage("Downloaded {}".format(name))

    # ── Edit helpers ──────────────────────────────────────────
    def _selected_meta(self):
        item = self.file_list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def _edit_file(self, meta):
        if not self.sftp:
            return
        remote = self._current_remote(meta)
        FileEditorDialog.open_remote(
            self, self.sftp, self.ssh, remote,
            sudo_user=self._sudo_user,
        )

    def _edit_selected(self):
        meta = self._selected_meta()
        if not meta:
            QMessageBox.information(self, "Edit", "Select a file first.")
            return
        if meta["is_dir"]:
            QMessageBox.information(self, "Edit", "Cannot edit a directory.")
            return
        self._edit_file(meta)

    # ── Media playback helpers ─────────────────────────────────
    def _play_media(self, meta):
        if not self.sftp:
            return
        remote = self._current_remote(meta)
        MediaPlayerDialog.open_remote(
            self, self.sftp, self.ssh, remote, meta["kind"], sudo_user=self._sudo_user
        )

    # ── Execution helpers ─────────────────────────────────────
    def _exec_file(self, meta):
        if not self.ssh:
            return
        remote = self._current_remote(meta)
        dlg = FileExecDialog(self, self.ssh, remote, sudo_user=self._sudo_user)
        dlg.exec_()

    def _run_selected(self):
        meta = self._selected_meta()
        if not meta:
            QMessageBox.information(self, "Run", "Select a file first.")
            return
        if meta["is_dir"]:
            QMessageBox.information(self, "Run", "Cannot run a directory.")
            return
        ext = os.path.splitext(meta["name"])[1].lower()
        if ext not in _EXECUTABLE_EXTS and meta["kind"] not in ("exec",):
            QMessageBox.information(
                self, "Run",
                f"'{meta['name']}' doesn't have a known executable extension.\n"
                "You can still open the Run dialog and choose a custom interpreter."
            )
        self._exec_file(meta)

    # ── Fetch / preview ───────────────────────────────────────
    def _fetch_preview(self, name, show_dialog=False):
        remote = self.current_path.rstrip("/") + "/" + name
        try:
            with self.sftp.open(remote, "r") as f:
                content = f.read(32768).decode(errors="replace")
            self.preview.show_text(content)
            if show_dialog:
                dlg = QDialog(self)
                dlg.setWindowTitle("View: {}".format(name))
                dlg.resize(760, 560)
                apply_qss_to(dlg)
                lay = QVBoxLayout(dlg)
                te  = QTextEdit()
                te.setReadOnly(True)
                te.setPlainText(content)
                te.setFont(monospace_font(11))
                lay.addWidget(te)
                bb = QDialogButtonBox(QDialogButtonBox.Close)
                bb.rejected.connect(dlg.reject)
                lay.addWidget(bb)
                dlg.exec_()
        except Exception:
            self.preview.show_text("(binary or unreadable file)")

    def _current_remote(self, meta):
        return self.current_path.rstrip("/") + "/" + meta["name"]

    # ── File operations ───────────────────────────────────────
    def _upload(self):
        if not self.sftp:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Files to Upload")
        if not paths:
            return
        errors = []
        for local in paths:
            remote = self.current_path.rstrip("/") + "/" + os.path.basename(local)
            ok = FileTransferDialog.upload(
                self, self.sftp, local, remote,
                host=self._conn_host, port=self._conn_port,
                user=self._conn_user, pem=self._conn_pem,
                sudo_user=self._sudo_user,
            )
            if not ok:
                errors.append(os.path.basename(local))
        if errors:
            QMessageBox.warning(self, "Upload Cancelled/Failed",
                                "These files were not fully uploaded:\n" + "\n".join(errors))
        self._refresh()
        self.status.showMessage("Uploaded {} file(s)".format(len(paths) - len(errors)))

    def _download(self):
        item = self.file_list.currentItem()
        if not item:
            return
        meta = item.data(Qt.UserRole)
        if not meta or meta["is_dir"]:
            QMessageBox.information(self, "Download", "Select a file (not a folder) to download.")
            return
        default_path = os.path.join(
            os.path.expanduser("~"), "Downloads", meta["name"]
        )
        save, _ = QFileDialog.getSaveFileName(self, "Save As", default_path)
        if not save:
            return
        FileTransferDialog.download(
            self, self.sftp, self._current_remote(meta), save,
            host=self._conn_host, port=self._conn_port,
            user=self._conn_user, pem=self._conn_pem,
            sudo_user=self._sudo_user,
        )
        self.status.showMessage("Saved to {}".format(save))

    def _new_folder(self):
        if not self.sftp:
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            self.sftp.mkdir(self.current_path.rstrip("/") + "/" + name.strip())
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _delete(self):
        item = self.file_list.currentItem()
        if not item:
            return
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        kind_str = "folder" if meta["is_dir"] else "file"
        if QMessageBox.question(self, "Delete", 'Delete {} "{}"?'.format(kind_str, meta["name"]),
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            remote = self._current_remote(meta)
            if meta["is_dir"]:
                self.sftp.rmdir(remote)
            else:
                self.sftp.remove(remote)
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "Delete Error", str(e))

    def _rename_selected(self):
        item = self.file_list.currentItem()
        if not item:
            QMessageBox.information(self, "Rename", "Select a file or folder first.")
            return
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        self._rename_meta(meta)

    def _rename_meta(self, meta):
        old_name = meta["name"]
        kind_str = "folder" if meta["is_dir"] else "file"
        new_name, ok = QInputDialog.getText(
            self, "Rename {}".format(kind_str.capitalize()),
            "New name:", text=old_name
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        if "/" in new_name:
            QMessageBox.warning(self, "Rename", "Name cannot contain '/'.")
            return

        old_remote = self._current_remote(meta)
        new_remote = self.current_path.rstrip("/") + "/" + new_name

        # Avoid silently clobbering an existing file/folder with that name.
        existing_names = {m["name"] for m in self._items}
        if new_name in existing_names:
            if QMessageBox.question(
                self, "Rename",
                'An item named "{}" already exists here. Overwrite it?'.format(new_name),
                QMessageBox.Yes | QMessageBox.No
            ) != QMessageBox.Yes:
                return

        try:
            self.sftp.rename(old_remote, new_remote)
            self._refresh()
            self.status.showMessage('Renamed "{}" to "{}"'.format(old_name, new_name))
        except Exception as e:
            QMessageBox.critical(self, "Rename Error", str(e))

    # ── Terminal command runner ───────────────────────────────
    def _detect_user_switch(self, cmd):
        c = cmd.strip()
        m = re.match(r"^(?:sudo\s+)?su(?:\s+(?:-\s*|--login\s*))?(?:\s+(\w+))?$", c)
        if m:
            return m.group(1) or "root"
        m = re.match(r"^sudo\s+(?:-\w+\s+)*-u\s+(\w+)", c)
        if m:
            return m.group(1)
        if re.match(r"^sudo\s+(?:-\w+\s+)*-i\b", c):
            return "root"
        return None

    def _is_exit_cmd(self, cmd):
        return cmd.strip().lower() in ("exit", "logout", "quit")

    def _prompt_str(self) -> str:
        who  = self._sudo_user or "me"
        host = self.host_label or "remote"
        cwd  = self._terminal_cwd or "~"
        return "{}@{}:{}$ ".format(who, host, cwd)

    def _on_terminal_command(self, cmd):
        """Called when the user presses Enter at the live prompt in the
        integrated terminal widget (terminal_widget.py)."""
        cmd = cmd.strip()
        if not self.ssh:
            self.terminal.write_output("Not connected.")
            self.terminal.show_prompt(self._prompt_str())
            return
        if not cmd:
            self.terminal.show_prompt(self._prompt_str())
            return

        if self._is_exit_cmd(cmd) and self._sudo_user:
            self._exit_sudo_mode()
            return

        if cmd.lower() in ("clear", "cls"):
            # A real screen-clear is a terminal-emulator action, not remote
            # command output — running it over a non-PTY SSH exec_command
            # either fails ("TERM environment variable not set") or, even
            # if it succeeded, would just print raw ANSI codes as text
            # since this isn't a full VT100 emulator. Handle it locally.
            self.terminal.clear()
            self.terminal.show_prompt(self._prompt_str())
            return

        target_user = self._detect_user_switch(cmd)
        if target_user:
            self._switch_to_user(target_user)
            return

        self.progress.show()

        worker = CommandWorker(self.ssh, cmd, cwd=self._terminal_cwd, sudo_user=self._sudo_user)
        worker.done.connect(lambda out: self._cmd_done(out, cmd))
        worker.error.connect(lambda e: self._cmd_done("[error] {}".format(e), cmd))
        track_worker(self._workers, worker)
        worker.start()

    def _on_terminal_interrupt(self):
        # Commands here run one-shot over SSH exec_command (not a live PTY
        # channel), so there's no live process to actually signal — just
        # let the user know rather than pretending to interrupt anything.
        self.terminal.write_output("^C  (nothing to interrupt — commands run to completion over SSH)")

    # ── Pop the terminal out into its own window ───────────────
    def _toggle_terminal_popout(self):
        if self._terminal_popout_win is not None:
            self._terminal_popout_win.close()   # triggers _redock_terminal via closeEvent
        else:
            self._popout_terminal()

    def _popout_terminal(self):
        win = _TerminalPopoutWindow(self, on_close=self._redock_terminal)
        layout = QVBoxLayout(win)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._terminal_home_layout.removeWidget(self.terminal)
        layout.addWidget(self.terminal)
        self.terminal.show()

        self.terminal_dock_placeholder.show()
        self.terminal_popout_btn.setText("⤡")
        self.terminal_popout_btn.setToolTip("Bring terminal back into the main window")

        self._terminal_popout_win = win
        win.show()
        win.raise_()
        self.terminal.setFocus()

    def _redock_terminal(self):
        if self._terminal_popout_win is None:
            return
        win = self._terminal_popout_win
        self._terminal_popout_win = None

        self.terminal_dock_placeholder.hide()
        # Insert the terminal back above the placeholder, in its original spot.
        idx = self._terminal_home_layout.indexOf(self.terminal_dock_placeholder)
        self._terminal_home_layout.insertWidget(max(idx, 0), self.terminal)
        self.terminal.show()

        self.terminal_popout_btn.setText("⤢")
        self.terminal_popout_btn.setToolTip("Open terminal in its own window")

        win.deleteLater()

    def _update_cwd_after_cmd(self, cmd):
        if not re.search(r'(?:^|[;&|])\s*cd\b', cmd):
            return False
        cwd_prefix = "cd {} 2>/dev/null; ".format(self._terminal_cwd) if self._terminal_cwd else ""
        full       = "{}{}> /dev/null 2>&1; pwd".format(cwd_prefix, cmd + " ")
        worker     = CommandWorker(self.ssh, full, sudo_user=self._sudo_user)

        def on_done(out):
            self._set_terminal_cwd(out.strip().splitlines()[-1] if out.strip() else "")
            self.terminal.show_prompt(self._prompt_str())

        def on_error(_e):
            # Couldn't confirm the new directory — show a prompt anyway
            # (with whatever cwd we last knew) rather than leaving the
            # terminal stuck waiting forever.
            self.terminal.show_prompt(self._prompt_str())

        worker.done.connect(on_done)
        worker.error.connect(on_error)
        track_worker(self._cwd_workers, worker)
        worker.start()
        return True

    def _set_terminal_cwd(self, path):
        if path and path.startswith("/"):
            self._terminal_cwd = path

    def _cmd_done(self, result, cmd=""):
        self.progress.hide()
        self.terminal.write_output(result)
        self.status.showMessage("Command finished")
        # If this was a 'cd', wait for the async pwd check to resolve before
        # showing the next prompt — otherwise it prints with the stale cwd.
        if cmd and self._update_cwd_after_cmd(cmd):
            return
        self.terminal.show_prompt(self._prompt_str())

    # ── Sudo / user switching ─────────────────────────────────
    def _switch_to_user(self, username):
        self.progress.show()
        self.terminal.write_output("Switching context to user: {} …".format(username))
        # 'sudo -n' fails immediately instead of hanging on a password
        # prompt that can never be answered over a non-interactive
        # exec_command channel. The previous version had no check at all —
        # it fired "sudo -u ... echo ~", swallowed any error with
        # '2>/dev/null || echo ""', and unconditionally declared success in
        # _apply_user_switch regardless of whether sudo actually worked.
        # That's exactly the "switch doesn't switch" symptom: the UI says
        # you're now 'adp', but every command underneath silently kept
        # running as the original login user because sudo had quietly
        # failed (no NOPASSWD rule, wrong username, etc).
        check_cmd = "sudo -n -u {} true".format(username)
        worker = CommandWorker(self.ssh, check_cmd)
        worker.done.connect(lambda out: self._on_sudo_check(username, out))
        worker.error.connect(lambda err: self._on_sudo_check(username, err, hard_error=True))
        track_worker(self._workers, worker)
        worker.start()

    def _on_sudo_check(self, username, output, hard_error=False):
        output = (output or "").strip()
        if hard_error or output:
            # 'sudo -n -u <user> true' prints nothing and exits 0 on
            # success. Any output at all here means sudo refused —
            # password required, unknown user, or not permitted by
            # sudoers — so abort instead of pretending it worked.
            self.progress.hide()
            reason = output or "sudo declined the request (no further detail returned)."
            self.terminal.write_output(
                "[sudo mode] Could not switch to '{}':\n{}".format(username, reason)
            )
            self.terminal.show_prompt(self._prompt_str())
            QMessageBox.warning(
                self, "Switch User Failed",
                "Could not switch to user '{}'.\n\n{}\n\n"
                "This usually means sudo requires a password for this action, "
                "or this account isn't permitted (via sudoers) to run commands "
                "as '{}'.".format(username, reason, username)
            )
            return

        # Sudo check passed — now resolve the target's real home directory
        # via getent (authoritative, reads /etc/passwd directly) rather than
        # shell '~' expansion. '~' silently resolves to the *current*
        # user's $HOME if sudo doesn't reset the environment (common when
        # sudoers doesn't have 'always_set_home' enabled), which previously
        # made the terminal show the wrong cwd even when sudo did succeed.
        home_worker = CommandWorker(
            self.ssh,
            "getent passwd {u} 2>/dev/null | cut -d: -f6".format(u=username)
        )
        home_worker.done.connect(lambda out: self._apply_user_switch(username, out.strip()))
        home_worker.error.connect(lambda _: self._apply_user_switch(username, ""))
        track_worker(self._workers, home_worker)
        home_worker.start()

    def _apply_user_switch(self, username, home):
        self.progress.hide()
        if not home or not home.startswith("/"):
            home = "/root" if username == "root" else "/home/{}".format(username)
        self._sudo_user    = username
        self._terminal_cwd = home
        self.sftp.set_sudo_user(username)
        self._update_sudo_badge()
        self.terminal.write_output(
            "[sudo mode] Acting as '{}'.\n"
            "All file operations now run as: sudo -u {}\n"
            "Terminal working directory: {}\n"
            "Type 'exit' or 'su <original_user>' to return to normal.".format(username, username, home)
        )
        self.status.showMessage("sudo → {}  •  {}".format(username, home))
        self.terminal.show_prompt(self._prompt_str())
        self._nav_to(home)

    def _exit_sudo_mode(self):
        if self.sftp:
            self.sftp.set_sudo_user(None)
        self._sudo_user = None
        _, _stdout, _ = self.ssh.exec_command("echo $HOME")
        self._terminal_cwd = _stdout.read().decode().strip() or None
        self._update_sudo_badge()
        self.terminal.write_output("[sudo mode OFF] Returned to login user.")
        self.terminal.show_prompt(self._prompt_str())
        self.status.showMessage("Returned to login user")
        self._refresh()

    def _update_sudo_badge(self):
        if self._sudo_user:
            self.sudo_badge.setText("  🔐 sudo: {}  ".format(self._sudo_user))
            self.sudo_badge.setStyleSheet(
                "color: {w}; background: rgba(251,191,36,0.15); "
                "border: 1px solid rgba(251,191,36,0.4); border-radius: 8px; "
                "padding: 1px 6px; font-size: 13px; font-weight: 600;".format(w=T['WARNING'])
            )
            self.sudo_badge.show()
        else:
            self.sudo_badge.hide()

    # ── Context menu ──────────────────────────────────────────
    def _ctx_menu(self, pos):
        item = self.file_list.itemAt(pos)
        menu = QMenu(self)
        if item:
            meta = item.data(Qt.UserRole)
            if meta["is_dir"]:
                menu.addAction("📂  Open", lambda: self._nav_to(
                    self.current_path.rstrip("/") + "/" + meta["name"]))
            else:
                ext  = os.path.splitext(meta["name"])[1].lower()
                kind = meta["kind"]

                if kind in _MEDIA_KINDS:
                    menu.addAction("▶  Play", lambda m=meta: self._play_media(m))

                if kind in _EDITABLE_KINDS:
                    menu.addAction("✏️  Edit", lambda m=meta: self._edit_file(m))

                if ext in _EXECUTABLE_EXTS or kind == "exec":
                    menu.addAction("▶  Run", lambda m=meta: self._exec_file(m))

                menu.addAction("👁  View", lambda m=meta: self._fetch_preview(
                    m["name"], show_dialog=True))
                menu.addAction("⬇  Download", self._download)

            menu.addAction("⌨  Rename", lambda m=meta: self._rename_meta(m))
            menu.addSeparator()
            menu.addAction("✕  Delete", self._delete)

        menu.addSeparator()
        menu.addAction("⬆  Upload File", self._upload)
        menu.addAction("➕  New Folder",  self._new_folder)
        menu.addSeparator()
        menu.addAction("↺  Refresh", self._refresh)
        menu.addSeparator()
        if self._sudo_user:
            menu.addAction("🔓  Exit sudo mode ({})".format(self._sudo_user), self._exit_sudo_mode)
        else:
            menu.addAction("🔐  Switch user (sudo su)…", self._prompt_switch_user)
        menu.exec_(self.file_list.viewport().mapToGlobal(pos))

    def _open_search(self):
        if not self.ssh:
            QMessageBox.information(self, "Search", "Connect to a server first.")
            return
        dlg = SearchDialog(self, self.ssh, start_path=self.current_path)
        dlg.navigate.connect(self._nav_to)
        dlg.exec_()

    def _prompt_switch_user(self):
        username, ok = QInputDialog.getText(self, "Switch User", "Username:")
        if ok and username.strip():
            self._switch_to_user(username.strip())

    # ── Cleanup ───────────────────────────────────────────────
    def closeEvent(self, event):
        try:
            if self._terminal_popout_win is not None:
                self._terminal_popout_win.close()
            self.k8s_tab.clear_connection_info()  # also stops any active tunnel
            if self.sftp: self.sftp.close()
            if self.ssh:  self.ssh.close()
        except Exception:
            pass
        event.accept()