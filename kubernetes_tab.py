"""kubernetes_tab.py — The Kubernetes management tab widget."""

import json
import re
import shlex
from datetime import datetime, timezone


from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QProgressBar, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QListWidget, QListWidgetItem, QTextEdit,
    QSplitter, QFrame, QSpinBox, QHeaderView, QAbstractItemView,
    QDialog, QVBoxLayout as _QVL, QDialogButtonBox, QMessageBox,
    QMenu, QInputDialog, QApplication,
)

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QProcess, QSize
from PyQt5.QtGui import QColor, QFont, QFontDatabase
from PyQt5.QtWidgets import QCompleter

from themes import T, apply_qss_to, load_settings, save_settings
from workers import CommandWorker, track_worker
from dialogs import LogViewerDialog, ExecDialog, ManageTunnelServicesDialog, ContainerPickerDialog
from k8s_cards import (
    PodCardWidget, DeploymentCardWidget, ConfigCardWidget,
    ServiceCardWidget, IngressCardWidget,
    StatefulSetCardWidget, DaemonSetCardWidget, EventCardWidget,
)
from utils import (
    append_terminal_html, append_terminal_text,
    load_tunnel_services, REMOTE_TUNNEL_CSV_PATH,
    monospace_font,
)


class KubernetesTab(QWidget):
    status_msg = pyqtSignal(str)

    # Cap on simultaneous tunnel-restart CommandWorkers. Each worker's
    # run() opens TWO channels on the shared SSH transport (one exec_command
    # to probe $HOME, one for the actual restart command) — see workers.py.
    # sshd's default MaxSessions caps concurrent channels per connection at
    # 10, so firing off every selected service's worker at once (previously
    # all of them, unbounded) blew past that ceiling once more than ~5
    # services were selected, and every worker past the limit failed with
    # ChannelException(2, 'Connect failed') / "Unable to open channel."
    # Staying at 4 concurrent workers (≤8 channels) keeps headroom under
    # the default limit even on servers with other channels already open.
    MAX_CONCURRENT_TUNNEL_RESTARTS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ssh          = None
        self._current_ns  = "default"
        self._namespaces  = []
        self._workers     = []
        self._events_raw  = ""
        self._events_warnings_only = False
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh)
        # Local (client-side) connection details, used to run the SSH
        # tunnel on the machine running this app rather than over the
        # existing remote `self.ssh` session.
        self._conn_host   = None
        self._conn_port   = 22
        self._conn_user   = None
        self._conn_pem    = None
        self._tunnel_services = []
        self._tunnel_col_widths = (20, 20)
        self._tunnel_process  = None
        # Remote CSV path tunnel services are read from/written to — lets
        # each person point this at their own file (e.g. a per-project or
        # per-team convention) instead of being locked to the hardcoded
        # default. Persisted across restarts via themes.save_settings.
        self._tunnel_csv_path = load_settings().get("tunnel_csv_path") or REMOTE_TUNNEL_CSV_PATH
        self._build_ui()

    # ── Local connection info (for tunnelling) ────────────────
    def set_connection_info(self, host, port, user, pem):
        self._conn_host = host
        self._conn_port = port or 22
        self._conn_user = user
        self._conn_pem  = pem

    def clear_connection_info(self):
        self._stop_tunnel()
        self._conn_host = None
        self._conn_port = 22
        self._conn_user = None
        self._conn_pem  = None
        self._tunnel_services = []
        self.tunnel_list.clear()
        self.tunnel_cmd_preview.clear()

    # ── SSH wiring ────────────────────────────────────────────
    def set_ssh(self, ssh):
        self.ssh = ssh

        if ssh:
            self._load_namespaces()
            self._load_tunnel_csv()      # Load from this VM
        else:
            self._clear_all()

            self._tunnel_services = []
            self.tunnel_list.clear()
            self.tunnel_cmd_preview.clear()

    # ── UI construction ───────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Control bar
        self.ctrl_bar = QWidget()
        self.ctrl_bar.setFixedHeight(52)
        self.ctrl_bar.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        cb = QHBoxLayout(self.ctrl_bar)
        cb.setContentsMargins(12, 0, 12, 0)
        cb.setSpacing(10)

        # Namespace picker, grouped into one rounded "chip" (dot + label +
        # combo sharing a pill background) instead of three bare widgets
        # floating loose on the toolbar — reads as a single catchy control
        # rather than a thin, easy-to-miss dropdown.
        self.ns_group = QWidget()
        self.ns_group.setObjectName("ns_group")
        self.ns_group.setFixedHeight(40)
        ns_row = QHBoxLayout(self.ns_group)
        ns_row.setContentsMargins(14, 0, 8, 0)
        ns_row.setSpacing(9)
        self.ns_dot = QLabel("●")
        self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 11px; background: transparent;")
        ns_row.addWidget(self.ns_dot)
        ns_lbl = QLabel("NAMESPACE")
        ns_lbl.setStyleSheet(
            f"color: {T['TEXT_DIM']}; font-size: 11px; font-weight: 700; "
            f"letter-spacing: 0.5px; background: transparent;"
        )
        ns_row.addWidget(ns_lbl)
        self.ns_combo = QComboBox()
        self.ns_combo.setObjectName("ns_combo")
        self.ns_combo.setMinimumWidth(190)
        self.ns_combo.setFixedHeight(30)
        self.ns_combo.setMaxVisibleItems(12)
        self.ns_combo.setEditable(True)
        self.ns_combo.setInsertPolicy(QComboBox.NoInsert)
        self.ns_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.ns_combo.completer().setFilterMode(Qt.MatchContains)
        self.ns_combo.currentTextChanged.connect(self._on_ns_change)
        ns_row.addWidget(self.ns_combo)
        self._style_ns_group()
        cb.addWidget(self.ns_group)
        cb.addWidget(self._vline())

        self.refresh_btn = self._toolbar_btn("↺  Refresh")
        self.refresh_btn.clicked.connect(self._refresh_current_tab)
        cb.addWidget(self.refresh_btn)

        self.auto_btn = self._toolbar_btn("⏱  Auto (30 s)")
        self.auto_btn.setCheckable(True)
        self.auto_btn.toggled.connect(self._toggle_auto_refresh)
        cb.addWidget(self.auto_btn)
        cb.addWidget(self._vline())

        self.health_lbl = QLabel("● Cluster")
        self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        cb.addWidget(self.health_lbl)
        cb.addStretch()

        self.kubectl_inp = QLineEdit()
        self.kubectl_inp.setPlaceholderText("kubectl …  (raw command)")
        self.kubectl_inp.setMaximumWidth(320)
        self.kubectl_inp.returnPressed.connect(self._run_kubectl)
        cb.addWidget(self.kubectl_inp)

        self.run_btn = self._toolbar_btn("Run")
        self.run_btn.clicked.connect(self._run_kubectl)
        cb.addWidget(self.run_btn)
        root.addWidget(self.ctrl_bar)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

        self.sub_tabs = QTabWidget()
        self.sub_tabs.setTabPosition(QTabWidget.North)
        self.sub_tabs.currentChanged.connect(self._refresh_current_tab)
        root.addWidget(self.sub_tabs)

        self._build_pods_tab()
        self._build_deployments_tab()
        self._build_statefulsets_tab()
        self._build_daemonsets_tab()
        self._build_services_tab()
        self._build_ingress_tab()
        self._build_config_tab()
        self._build_events_tab()
        self._build_tunnels_tab()
        self._build_terminal_tab()

    def _vline(self):
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setStyleSheet(f"color: {T['BORDER']};")
        f.setFixedWidth(1)
        return f

    def _style_ns_group(self):
        """Pill chip around the namespace picker + a bigger, bolder combo
        box than the app-wide default. Set directly on the two widgets
        (rather than in themes.py's global QComboBox rule) so every other
        dropdown in the app keeps its normal size — only this one, the
        most-used control on the tab, gets the larger treatment. Re-called
        from apply_theme() on every theme switch since the colours below
        are baked in as literal hex at call time."""
        self.ns_group.setStyleSheet(
            f"QWidget#ns_group {{ background: {T['BG_ITEM']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 20px; }}"
        )
        self.ns_combo.setStyleSheet(
            f"QComboBox#ns_combo {{ background: {T['BG_PANEL']}; color: {T['TEXT_PRIMARY']}; "
            f"border: 1.5px solid {T['ACCENT']}; border-radius: 15px; "
            f"padding: 2px 30px 2px 14px; font-size: 13px; font-weight: 600; min-width: 190px; }}"
            f"QComboBox#ns_combo:hover {{ border-color: {T['ACCENT2']}; background: {T['BG_HOVER']}; }}"
            f"QComboBox#ns_combo::drop-down {{ border: none; width: 26px; }}"
            f"QComboBox#ns_combo::down-arrow {{ width: 10px; height: 10px; }}"
        )

    def _toolbar_btn(self, label: str, object_name: str = None, tooltip: str = "") -> QPushButton:
        """Build a toolbar action button with a uniform, fixed shape.

        Buttons here mix plain text with emoji glyphs ("📋  Logs", "🔌  Tunnel",
        "Run", …). Emoji fall back to a different font than the rest of the
        label, and that fallback font's line-height isn't the same as
        'Segoe UI' — so without a fixed height, buttons with an emoji end up
        a few px taller than plain-text ones, and the shared border-radius
        then reads as visually different corner shapes across the toolbar.
        Forcing every button through this one helper keeps height, padding,
        and radius identical everywhere regardless of label content.
        """
        btn = QPushButton(label)
        if object_name:
            btn.setObjectName(object_name)
        if tooltip:
            btn.setToolTip(tooltip)
        btn.setFixedHeight(32)
        btn.setStyleSheet("padding: 0 14px;")
        return btn

    def _set_count_badge(self, lbl: QLabel, text: str, color_key: str = "TEXT_DIM"):
        """Style a QLabel as a small pill badge, matching the card badges
        in k8s_cards.py, and set its text in one call."""
        color = T.get(color_key, T["TEXT_DIM"])
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        lbl.setText(text)
        lbl.setStyleSheet(
            f"background: rgba({r},{g},{b},0.15); color: {color}; "
            f"border: 1px solid rgba({r},{g},{b},0.4); border-radius: 9px; "
            f"padding: 3px 10px; font-size: 12px; font-weight: 700;"
        )

    def _build_pods_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.pod_filter = QLineEdit()
        self.pod_filter.setPlaceholderText("🔍  Filter pods…")
        self.pod_filter.setMaximumWidth(200)
        self.pod_filter.textChanged.connect(self._filter_pods)
        tb.addWidget(self.pod_filter)

        self.pod_count_lbl = QLabel("")
        tb.addWidget(self.pod_count_lbl)
        tb.addStretch()

        # Safe, frequent actions live inside one clustered pill; the
        # destructive action (Delete) sits outside it with a gap, so it
        # can never be misclicked as "just another button in the row".
        cluster = QFrame()
        cluster.setObjectName("action_cluster")
        cl = QHBoxLayout(cluster)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(2)
        for label, obj, slot in [
            ("📋  Logs",    "pod_logs_btn",    self._pod_logs),
            ("💻  Exec",    "pod_exec_btn",    self._pod_exec),
            ("↺  Restart",  "pod_restart_btn", self._pod_restart),
        ]:
            btn = self._toolbar_btn(label)
            btn.setFlat(True)
            btn.setStyleSheet("border: none; padding: 0 14px; background: transparent;")
            setattr(self, obj, btn)
            btn.clicked.connect(slot)
            cl.addWidget(btn)
        tb.addWidget(cluster)
        self.pod_action_cluster = cluster

        self.pod_del_btn = self._toolbar_btn("🗑  Delete", object_name="danger")
        self.pod_del_btn.clicked.connect(self._pod_delete)
        tb.addWidget(self.pod_del_btn)

        cluster.setStyleSheet(
            f"QFrame#action_cluster {{ background: {T['BG_ITEM']}; border-radius: 8px; }}"
        )

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.pods_toolbar = tb_widget

        # Pods render as cards (see k8s_cards.py) rather than table rows —
        # each card carries its own meta dict via Qt.UserRole, the same
        # "meta dict + setItemWidget()" pattern file_widgets.py already uses
        # for the file list. Namespace is folded into a chip on the card
        # itself (only shown in "(all namespaces)" view) instead of a
        # dedicated hidden column.
        self.pod_list = QListWidget()
        self.pod_list.setSpacing(6)
        self.pod_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.pod_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pod_list.customContextMenuRequested.connect(self._pod_ctx_menu)
        self.pod_list.itemDoubleClicked.connect(self._on_pod_double_click)
        self.pod_list.currentItemChanged.connect(self._on_pod_selection_changed)
        self.pod_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.pod_list)
        self.sub_tabs.addTab(w, "🐳  Pods")

    def _build_deployments_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.deploy_filter = QLineEdit()
        self.deploy_filter.setPlaceholderText("🔍  Filter deployments…")
        self.deploy_filter.setMaximumWidth(200)
        self.deploy_filter.textChanged.connect(self._filter_deployments)
        tb.addWidget(self.deploy_filter)

        self.deploy_count_lbl = QLabel("")
        tb.addWidget(self.deploy_count_lbl)
        tb.addStretch()

        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(0, 100)
        self.scale_spin.setValue(1)
        self.scale_spin.setFixedWidth(70)
        self.scale_spin.setToolTip("Replicas")
        tb.addWidget(QLabel("Replicas:"))
        tb.addWidget(self.scale_spin)

        for label, obj, slot in [
            ("⇅  Scale",    "dep_scale_btn",   self._deploy_scale),
            ("↺  Restart",  "dep_restart_btn",  self._deploy_restart),
            ("📋  Describe", "dep_desc_btn",    self._deploy_describe),
            ("🗑  Delete",   "dep_del_btn",     self._deploy_delete),
        ]:
            obj_name = "danger" if "Delete" in label else ("primary" if "" in label else None)
            btn = self._toolbar_btn(label, object_name=obj_name)
            setattr(self, obj, btn)
            btn.clicked.connect(slot)
            tb.addWidget(btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.deploy_toolbar = tb_widget

        # Same card-list treatment as Pods — see k8s_cards.py.
        self.deploy_list = QListWidget()
        self.deploy_list.setSpacing(6)
        self.deploy_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.deploy_list.itemClicked.connect(self._on_deploy_click)
        self.deploy_list.itemDoubleClicked.connect(self._on_deploy_double_click)
        self.deploy_list.currentItemChanged.connect(self._on_deploy_selection_changed)
        self.deploy_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.deploy_list)
        self.sub_tabs.addTab(w, "🚀  Deployments")

    def _build_statefulsets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.sts_filter = QLineEdit()
        self.sts_filter.setPlaceholderText("🔍  Filter statefulsets…")
        self.sts_filter.setMaximumWidth(200)
        self.sts_filter.textChanged.connect(self._filter_statefulsets)
        tb.addWidget(self.sts_filter)

        self.sts_count_lbl = QLabel("")
        tb.addWidget(self.sts_count_lbl)
        tb.addStretch()

        self.sts_scale_spin = QSpinBox()
        self.sts_scale_spin.setRange(0, 100)
        self.sts_scale_spin.setValue(1)
        self.sts_scale_spin.setFixedWidth(70)
        self.sts_scale_spin.setToolTip("Replicas")
        tb.addWidget(QLabel("Replicas:"))
        tb.addWidget(self.sts_scale_spin)

        for label, obj, slot in [
            ("⇅  Scale",     "sts_scale_btn",   self._sts_scale),
            ("↺  Restart",   "sts_restart_btn", self._sts_restart),
            ("📋  Describe",  "sts_desc_btn",    self._sts_describe),
            ("🗑  Delete",    "sts_del_btn",     self._sts_delete),
        ]:
            obj_name = "danger" if "Delete" in label else None
            btn = self._toolbar_btn(label, object_name=obj_name)
            setattr(self, obj, btn)
            btn.clicked.connect(slot)
            tb.addWidget(btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.sts_toolbar = tb_widget

        self.sts_list = QListWidget()
        self.sts_list.setSpacing(6)
        self.sts_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sts_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sts_list.customContextMenuRequested.connect(self._sts_ctx_menu)
        self.sts_list.itemClicked.connect(self._on_sts_click)
        self.sts_list.itemDoubleClicked.connect(self._on_sts_double_click)
        self.sts_list.currentItemChanged.connect(self._on_sts_selection_changed)
        self.sts_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.sts_list)
        self.sub_tabs.addTab(w, "📚  StatefulSets")

    def _build_daemonsets_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.ds_filter = QLineEdit()
        self.ds_filter.setPlaceholderText("🔍  Filter daemonsets…")
        self.ds_filter.setMaximumWidth(200)
        self.ds_filter.textChanged.connect(self._filter_daemonsets)
        tb.addWidget(self.ds_filter)

        self.ds_count_lbl = QLabel("")
        tb.addWidget(self.ds_count_lbl)
        tb.addStretch()

        # No Scale control — DaemonSets run exactly one pod per matching
        # node, so "replica count" isn't a thing a person can set here.
        for label, obj, slot in [
            ("↺  Restart",   "ds_restart_btn", self._ds_restart),
            ("📋  Describe",  "ds_desc_btn",    self._ds_describe),
            ("🗑  Delete",    "ds_del_btn",     self._ds_delete),
        ]:
            obj_name = "danger" if "Delete" in label else None
            btn = self._toolbar_btn(label, object_name=obj_name)
            setattr(self, obj, btn)
            btn.clicked.connect(slot)
            tb.addWidget(btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.ds_toolbar = tb_widget

        self.ds_list = QListWidget()
        self.ds_list.setSpacing(6)
        self.ds_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ds_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ds_list.customContextMenuRequested.connect(self._ds_ctx_menu)
        self.ds_list.itemDoubleClicked.connect(self._on_ds_double_click)
        self.ds_list.currentItemChanged.connect(self._on_ds_selection_changed)
        self.ds_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.ds_list)
        self.sub_tabs.addTab(w, "🛡  DaemonSets")

    def _build_services_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.svc_filter = QLineEdit()
        self.svc_filter.setPlaceholderText("🔍  Filter services…")
        self.svc_filter.setMaximumWidth(200)
        self.svc_filter.textChanged.connect(self._filter_services)
        tb.addWidget(self.svc_filter)

        self.svc_count_lbl = QLabel("")
        tb.addWidget(self.svc_count_lbl)
        tb.addStretch()

        self.svc_desc_btn = self._toolbar_btn("📋  Describe")
        self.svc_desc_btn.clicked.connect(self._svc_describe)
        tb.addWidget(self.svc_desc_btn)

        self.svc_del_btn = self._toolbar_btn("🗑  Delete", object_name="danger")
        self.svc_del_btn.clicked.connect(self._svc_delete)
        tb.addWidget(self.svc_del_btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.svc_toolbar = tb_widget

        # Same card-list treatment as Pods/Deployments — see k8s_cards.py.
        self.svc_list = QListWidget()
        self.svc_list.setSpacing(6)
        self.svc_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.svc_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.svc_list.customContextMenuRequested.connect(self._svc_ctx_menu)
        self.svc_list.itemDoubleClicked.connect(self._on_svc_double_click)
        self.svc_list.currentItemChanged.connect(self._on_svc_selection_changed)
        self.svc_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.svc_list)
        self.sub_tabs.addTab(w, "🧭  Services")

    def _build_ingress_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.ing_filter = QLineEdit()
        self.ing_filter.setPlaceholderText("🔍  Filter ingress…")
        self.ing_filter.setMaximumWidth(200)
        self.ing_filter.textChanged.connect(self._filter_ingress)
        tb.addWidget(self.ing_filter)

        self.ing_count_lbl = QLabel("")
        tb.addWidget(self.ing_count_lbl)
        tb.addStretch()

        self.ing_desc_btn = self._toolbar_btn("📋  Describe")
        self.ing_desc_btn.clicked.connect(self._ing_describe)
        tb.addWidget(self.ing_desc_btn)

        self.ing_del_btn = self._toolbar_btn("🗑  Delete", object_name="danger")
        self.ing_del_btn.clicked.connect(self._ing_delete)
        tb.addWidget(self.ing_del_btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.ing_toolbar = tb_widget

        self.ing_list = QListWidget()
        self.ing_list.setSpacing(6)
        self.ing_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ing_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ing_list.customContextMenuRequested.connect(self._ing_ctx_menu)
        self.ing_list.itemDoubleClicked.connect(self._on_ing_double_click)
        self.ing_list.currentItemChanged.connect(self._on_ing_selection_changed)
        self.ing_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.ing_list)
        self.sub_tabs.addTab(w, "🌐  Ingress")

    def _build_config_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        # Left: type toggle + list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        # Taller toolbar with real breathing room — the old 42px bar packed
        # a combo box and filter field edge-to-edge with almost no margin,
        # which is most of what read as "congested".
        self.cfg_type_bar = QWidget()
        self.cfg_type_bar.setFixedHeight(58)
        self.cfg_type_bar.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        tb_lay = QHBoxLayout(self.cfg_type_bar)
        tb_lay.setContentsMargins(12, 10, 12, 10)
        tb_lay.setSpacing(10)

        # ConfigMaps/Secrets is a binary choice, not a long list — a
        # segmented two-button toggle reads faster than opening a dropdown
        # for one of two options, and gives the "big, catchy" control the
        # namespace picker also got, instead of a thin QComboBox.
        self.cfg_type_toggle = QWidget()
        self.cfg_type_toggle.setObjectName("cfg_type_toggle")
        self.cfg_type_toggle.setFixedHeight(36)
        toggle_lay = QHBoxLayout(self.cfg_type_toggle)
        toggle_lay.setContentsMargins(3, 3, 3, 3)
        toggle_lay.setSpacing(2)
        self.cfg_type_cm_btn = QPushButton("📦  ConfigMaps")
        self.cfg_type_secret_btn = QPushButton("🔐  Secrets")
        for btn in (self.cfg_type_cm_btn, self.cfg_type_secret_btn):
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(30)
            toggle_lay.addWidget(btn)
        self.cfg_type_cm_btn.setChecked(True)
        self.cfg_type_cm_btn.clicked.connect(lambda: self._set_cfg_type("ConfigMaps"))
        self.cfg_type_secret_btn.clicked.connect(lambda: self._set_cfg_type("Secrets"))
        self._cfg_type = "ConfigMaps"
        self._style_cfg_toggle()
        tb_lay.addWidget(self.cfg_type_toggle)

        self.cfg_filter = QLineEdit()
        self.cfg_filter.setPlaceholderText("🔍  Filter…")
        self.cfg_filter.textChanged.connect(self._filter_configs)
        tb_lay.addWidget(self.cfg_filter, 1)
        ll.addWidget(self.cfg_type_bar)

        # Cards instead of bare text rows — icon, name, and a ConfigMap/
        # Secret pill per entry, spaced out like the Pods/Deployments
        # lists (k8s_cards.py) instead of one dense column of names.
        self.cfg_list = QListWidget()
        self.cfg_list.setSpacing(6)
        self.cfg_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.cfg_list.currentItemChanged.connect(self._on_cfg_selection_changed)
        self.cfg_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 10px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        ll.addWidget(self.cfg_list)
        splitter.addWidget(left)

        # Right: detail + raw yaml
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.cfg_detail_hdr = QLabel("  Data")
        self.cfg_detail_hdr.setFixedHeight(34)
        self.cfg_detail_hdr.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
        )
        rl.addWidget(self.cfg_detail_hdr)

        self.cfg_detail = QTreeWidget()
        self._style_tree(self.cfg_detail)
        self.cfg_detail.setRootIsDecorated(False)
        self.cfg_detail.setAlternatingRowColors(True)
        self.cfg_detail.setColumnCount(2)
        self.cfg_detail.setHeaderLabels(["Key", "Value"])
        self.cfg_detail.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.cfg_detail.header().setSectionResizeMode(1, QHeaderView.Stretch)
        rl.addWidget(self.cfg_detail)

        self.cfg_raw_lbl = QLabel("  Structured View")
        self.cfg_raw_lbl.setFixedHeight(34)
        self.cfg_raw_lbl.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
            f"border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
        )
        rl.addWidget(self.cfg_raw_lbl)

        self.cfg_raw = QTextEdit()
        self.cfg_raw.setReadOnly(True)
        self.cfg_raw.setFont(monospace_font(11))
        self.cfg_raw.setMinimumHeight(220)
        self.cfg_raw.setStyleSheet(f"padding: 10px; border: none; background: {T['BG_DARK']};")
        rl.addWidget(self.cfg_raw)

        splitter.addWidget(right)
        splitter.setSizes([320, 620])
        lay.addWidget(splitter)
        self.sub_tabs.addTab(w, "🔧  Config & Secrets")

    def _style_cfg_toggle(self):
        """Pill-shaped container + two checkable buttons that look like one
        segmented control (selected side lit with the accent colour).
        Re-called from apply_theme() since colours are literal hex here."""
        self.cfg_type_toggle.setStyleSheet(
            f"QWidget#cfg_type_toggle {{ background: {T['BG_ITEM']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 18px; }}"
        )
        btn_css = f"""
            QPushButton {{
                background: transparent; color: {T['TEXT_DIM']};
                border: none; border-radius: 15px; padding: 0 16px;
                font-size: 12px; font-weight: 700;
            }}
            QPushButton:hover:!checked {{ background: {T['BG_HOVER']}; color: {T['TEXT_PRIMARY']}; }}
            QPushButton:checked {{ background: {T['ACCENT']}; color: white; }}
        """
        self.cfg_type_cm_btn.setStyleSheet(btn_css)
        self.cfg_type_secret_btn.setStyleSheet(btn_css)

    def _set_cfg_type(self, name: str):
        """Click handler for the ConfigMaps/Secrets segmented toggle —
        keeps the two buttons mutually exclusive (QPushButton's own
        setCheckable doesn't do this on its own outside a QButtonGroup)
        and reloads the list for the newly-selected type."""
        self._cfg_type = name
        self.cfg_type_cm_btn.setChecked(name == "ConfigMaps")
        self.cfg_type_secret_btn.setChecked(name == "Secrets")
        self._load_config_resources()

    def _build_events_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)
        self.event_filter = QLineEdit()
        self.event_filter.setPlaceholderText("🔍  Filter events (reason / object / message)…")
        self.event_filter.setMaximumWidth(280)
        self.event_filter.textChanged.connect(self._filter_events)
        tb.addWidget(self.event_filter)

        self.event_count_lbl = QLabel("")
        tb.addWidget(self.event_count_lbl)
        tb.addStretch()

        self.event_warn_btn = self._toolbar_btn("⚠  Warnings only")
        self.event_warn_btn.setCheckable(True)
        self.event_warn_btn.toggled.connect(self._toggle_events_warnings_only)
        tb.addWidget(self.event_warn_btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.events_toolbar = tb_widget

        # Newest first, warnings visually distinct — see EventCardWidget
        # (k8s_cards.py) for the accent-color logic.
        self.event_list = QListWidget()
        self.event_list.setSpacing(4)
        self.event_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.event_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.event_list.customContextMenuRequested.connect(self._event_ctx_menu)
        self.event_list.itemDoubleClicked.connect(self._on_event_double_click)
        self.event_list.currentItemChanged.connect(self._on_event_selection_changed)
        self.event_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; padding: 8px; }"
            "QListWidget::item { border: none; padding: 0; margin: 0; }"
        )
        lay.addWidget(self.event_list)
        self.sub_tabs.addTab(w, "📡  Events")

    def _build_terminal_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self.k8s_terminal = QTextEdit()
        self.k8s_terminal.setReadOnly(True)
        self.k8s_terminal.setFont(monospace_font(11))
        self.k8s_terminal.setStyleSheet(
            f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
        )
        self.k8s_terminal.setPlaceholderText("kubectl output appears here…")
        lay.addWidget(self.k8s_terminal)

        inp_row = QHBoxLayout()
        self.k8s_inp = QLineEdit()
        self.k8s_inp.setPlaceholderText("kubectl …")
        self.k8s_inp.returnPressed.connect(self._run_kubectl_terminal)
        inp_row.addWidget(self.k8s_inp)

        clr = self._toolbar_btn("Clear")
        clr.clicked.connect(self.k8s_terminal.clear)
        inp_row.addWidget(clr)

        run = self._toolbar_btn("Run", object_name="primary")
        run.clicked.connect(self._run_kubectl_terminal)
        inp_row.addWidget(run)
        lay.addLayout(inp_row)
        self.sub_tabs.addTab(w, "⌨  Terminal")

    def _build_tunnels_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Toolbar: CSV path + reload + select-all/clear
        tb = QHBoxLayout()
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)

        self.tunnel_path_lbl = QLabel(f"📄  {self._tunnel_csv_path}  (on VM)")
        self.tunnel_path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        tb.addWidget(self.tunnel_path_lbl)
        tb.addStretch()

        change_file_btn = self._toolbar_btn(
            "📂  Change File",
            tooltip=(
                "Point at a different tunnel-services CSV on the connected VM\n"
                "(e.g. a personal or per-project file instead of the shared default).\n"
                "Remembered for next time."
            ),
        )
        change_file_btn.clicked.connect(self._change_tunnel_csv_path)
        tb.addWidget(change_file_btn)

        reload_btn = self._toolbar_btn("↺  Reload CSV")
        reload_btn.clicked.connect(self._load_tunnel_csv)
        tb.addWidget(reload_btn)

        manage_btn = self._toolbar_btn(
            "⚙️  Manage Services",
            tooltip="Add, edit, or remove tunnel services stored on the connected VM",
        )
        manage_btn.clicked.connect(self._open_manage_tunnel_services)
        tb.addWidget(manage_btn)

        refresh_status_btn = self._toolbar_btn(
            "🔄  Refresh Status",
            tooltip=(
                "Check which services' ports are currently listening on the VM\n"
                "(🟢 exposed / 🔴 not exposed), without reloading the CSV."
            ),
        )
        refresh_status_btn.clicked.connect(self._refresh_tunnel_status)
        tb.addWidget(refresh_status_btn)

        selall_btn = self._toolbar_btn("☑  Select All")
        selall_btn.clicked.connect(lambda: self._set_all_tunnel_checks(True))
        tb.addWidget(selall_btn)

        clear_btn = self._toolbar_btn("☐  Clear")
        clear_btn.clicked.connect(lambda: self._set_all_tunnel_checks(False))
        tb.addWidget(clear_btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.tunnel_toolbar = tb_widget
        
        self.tunnel_search = QLineEdit()
        self.tunnel_search.setPlaceholderText("🔍  Filter services...")
        self.tunnel_search.setClearButtonEnabled(True)
        self.tunnel_search.setMaximumHeight(34)
        self.tunnel_search.textChanged.connect(self._filter_tunnel_services)

        lay.addWidget(self.tunnel_search)
        # Service checklist
        self.tunnel_list = QListWidget()
        self.tunnel_list.setAlternatingRowColors(True)
        self.tunnel_list.itemChanged.connect(self._update_tunnel_cmd_preview)
        self.tunnel_list.setStyleSheet("""
        QListWidget {
            font-size: 13px;
        }
        QListWidget::item {
            height: 38px;
        }
        QListWidget::indicator {
            width: 22px;
            height: 22px;
        }
        """)
        lay.addWidget(self.tunnel_list, 1)

        # Command preview (read-only, for transparency/debugging)
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(10, 8, 10, 4)
        preview_row.addWidget(QLabel("Command:"))
        self.tunnel_cmd_preview = QLineEdit()
        self.tunnel_cmd_preview.setReadOnly(True)
        self.tunnel_cmd_preview.setFont(monospace_font(10))
        self.tunnel_cmd_preview.setPlaceholderText("Select service(s) below to preview the SSH tunnel command…")
        preview_row.addWidget(self.tunnel_cmd_preview, 1)
        lay.addLayout(preview_row)

        # Controls: status + start/stop
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(10, 4, 10, 10)
        ctrl_row.setSpacing(8)

        self.tunnel_status_lbl = QLabel("●  Not tunnelling")
        self.tunnel_status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        ctrl_row.addWidget(self.tunnel_status_lbl)
        ctrl_row.addStretch()

        self.tunnel_start_btn = self._toolbar_btn("🔌  Tunnel", object_name="primary")
        self.tunnel_start_btn.clicked.connect(self._start_tunnel)
        ctrl_row.addWidget(self.tunnel_start_btn)

        self.tunnel_stop_btn = self._toolbar_btn("⏹  Stop", object_name="danger")
        self.tunnel_stop_btn.setEnabled(False)
        self.tunnel_stop_btn.clicked.connect(self._stop_tunnel)
        ctrl_row.addWidget(self.tunnel_stop_btn)

        self.port_kill = self._toolbar_btn("✖ Kill Port", object_name="danger")
        self.port_kill.clicked.connect(self._kill_selected_ports)
        ctrl_row.addWidget(self.port_kill)

        self.tunnel_restart_btn = self._toolbar_btn(
            "↻  Restart Tunneling",
            tooltip=(
                "Runs 'kubectl port-forward' directly on the connected VM for each\n"
                "selected service, e.g.:\n"
                "nohup kubectl -n <namespace> port-forward svc/<name> <port>:<port> &\n\n"
                "This is separate from the local SSH tunnel above — use both together:\n"
                "this exposes the service on the VM's own localhost, and the SSH\n"
                "tunnel forwards that port to your machine."
            ),
        )
        self.tunnel_restart_btn.clicked.connect(self._restart_kubectl_tunnels)
        ctrl_row.addWidget(self.tunnel_restart_btn)

        lay.addLayout(ctrl_row)

        # Process log (ssh stdout/stderr, merged)
        self.tunnel_log = QTextEdit()
        self.tunnel_log.setReadOnly(True)
        self.tunnel_log.setFont(monospace_font(10))
        self.tunnel_log.setFixedHeight(130)
        self.tunnel_log.setPlaceholderText("Tunnel process output appears here…")
        self.tunnel_log.setStyleSheet(
            f"background: #0d0d1a; color: {T['TEXT_DIM']}; border: none; padding: 8px;"
        )
        lay.addWidget(self.tunnel_log)

        self.sub_tabs.addTab(w, "🔀  Tunnels")
        # self._load_tunnel_csv()

    # ── Theme refresh ─────────────────────────────────────────
    def apply_theme(self):
        self.ctrl_bar.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 11px; background: transparent;")
        self._style_ns_group()
        if hasattr(self, "pod_action_cluster"):
            self.pod_action_cluster.setStyleSheet(
                f"QFrame#action_cluster {{ background: {T['BG_ITEM']}; border-radius: 8px; }}"
            )
        self.k8s_terminal.setStyleSheet(
            f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
        )
        toolbar_style = f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        for bar in (getattr(self, "pods_toolbar", None), getattr(self, "deploy_toolbar", None),
                    getattr(self, "sts_toolbar", None), getattr(self, "ds_toolbar", None),
                    getattr(self, "svc_toolbar", None), getattr(self, "ing_toolbar", None),
                    getattr(self, "events_toolbar", None), getattr(self, "tunnel_toolbar", None)):
            if bar is not None:
                bar.setStyleSheet(toolbar_style)
        if getattr(self, "tunnel_log", None) is not None:
            self.tunnel_log.setStyleSheet(
                f"background: #0d0d1a; color: {T['TEXT_DIM']}; border: none; padding: 8px;"
            )
        if getattr(self, "tunnel_path_lbl", None) is not None:
            self.tunnel_path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        if getattr(self, "tunnel_status_lbl", None) is not None:
            running = self._tunnel_process is not None and self._tunnel_process.state() != QProcess.NotRunning
            color = T['SUCCESS'] if running else T['TEXT_MUTED']
            self.tunnel_status_lbl.setStyleSheet(f"color: {color}; font-size: 12px;")
        if getattr(self, "cfg_detail_hdr", None) is not None:
            self.cfg_detail_hdr.setStyleSheet(
                f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
                f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
            )
        if getattr(self, "cfg_type_bar", None) is not None:
            self.cfg_type_bar.setStyleSheet(
                f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
            )
        if getattr(self, "cfg_type_toggle", None) is not None:
            self._style_cfg_toggle()
        if getattr(self, "cfg_raw", None) is not None:
            self.cfg_raw.setStyleSheet(f"padding: 10px; border: none; background: {T['BG_DARK']};")
        if getattr(self, "cfg_raw_lbl", None) is not None:
            self.cfg_raw_lbl.setStyleSheet(
                f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
                f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
                f"border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
            )
        if self.ssh:
            self._check_cluster_health()
            # Pod/deployment cards (k8s_cards.py) bake T's colors in at
            # construction time rather than re-reading them live, so a
            # theme switch needs a rebuild of whichever list is on screen
            # for its cards to pick up the new palette.
            self._refresh_current_tab()

    def _style_tree(self, tree):
        font = monospace_font(13)
        tree.setFont(font)

        tree.setStyleSheet("""
        QTreeWidget {
            font-size: 13px;
        }

        QTreeWidget::item {
            height: 38px;
        }
        """)

        hdr = tree.header()
        header_font = QFont("Segoe UI", 12)
        header_font.setBold(True)
        hdr.setFont(header_font)
        hdr.setMinimumHeight(42)

    def _filter_tunnel_services(self, text):
        """
        Filter tunnel services by service name, namespace or port.
        Preserves the checkbox state.
        """
        text = text.strip().lower()

        for i in range(self.tunnel_list.count()):
            item = self.tunnel_list.item(i)

            svc = item.data(Qt.UserRole)
            if svc is None:
                continue

            searchable = (
                f"{svc['name']} "
                f"{svc['namespace']} "
                f"{svc['port']}"
            ).lower()

            item.setHidden(text not in searchable)
    # ── Namespace helpers ─────────────────────────────────────
    def _load_namespaces(self):
        self._run_cmd(
            "kubectl get namespaces -o jsonpath='{.items[*].metadata.name}'",
            self._populate_namespaces,
        )

    def _populate_namespaces(self, out: str):
        names = out.strip().strip("'").split()
        self._namespaces = names
        current = self.ns_combo.currentText()
        self.ns_combo.blockSignals(True)
        self.ns_combo.clear()
        self.ns_combo.addItem("(all namespaces)")
        self.ns_combo.addItems(names)
        if current in names:
            self.ns_combo.setCurrentText(current)
        elif "default" in names:
            self.ns_combo.setCurrentText("default")
        self.ns_combo.blockSignals(False)
        self._current_ns = self.ns_combo.currentText()
        self._refresh_current_tab()
        self._check_cluster_health()

    def _on_ns_change(self, ns: str):
        self._current_ns = ns
        self._refresh_current_tab()

    def _ns_flag(self) -> str:
        ns = self._current_ns
        if ns == "(all namespaces)" or not ns:
            return "--all-namespaces"
        return f"-n {ns}"

    # ── Cluster health ────────────────────────────────────────
    def _check_cluster_health(self):
        self._run_cmd("kubectl cluster-info 2>&1 | head -2", self._update_health)

    def _update_health(self, out: str):
        if "running" in out.lower() or "control plane" in out.lower():
            self.health_lbl.setText("● Cluster OK")
            self.health_lbl.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 12px;")
        else:
            self.health_lbl.setText("● Cluster?")
            self.health_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")

    def _toggle_auto_refresh(self, on: bool):
        if on:
            self._auto_refresh_timer.start(30000)
            self.auto_btn.setText("⏱  Auto ON")
        else:
            self._auto_refresh_timer.stop()
            self.auto_btn.setText("⏱  Auto (30 s)")

    def _auto_refresh(self):
        self._refresh_current_tab()

    def _clear_all(self):
        self.pod_list.clear()
        self.deploy_list.clear()
        self.pod_count_lbl.setText("")
        self.pod_count_lbl.setStyleSheet("")
        self.deploy_count_lbl.setText("")
        self.deploy_count_lbl.setStyleSheet("")
        self.sts_list.clear()
        self.sts_count_lbl.setText("")
        self.sts_count_lbl.setStyleSheet("")
        self.ds_list.clear()
        self.ds_count_lbl.setText("")
        self.ds_count_lbl.setStyleSheet("")
        self.svc_list.clear()
        self.svc_count_lbl.setText("")
        self.svc_count_lbl.setStyleSheet("")
        self.ing_list.clear()
        self.ing_count_lbl.setText("")
        self.ing_count_lbl.setStyleSheet("")
        self.cfg_list.clear()
        self.cfg_detail.clear()
        self.cfg_raw.clear()
        self.event_list.clear()
        self.event_count_lbl.setText("")
        self.event_count_lbl.setStyleSheet("")
        self._events_raw = ""
        if getattr(self, "event_warn_btn", None) is not None:
            self.event_warn_btn.setChecked(False)
        self.ns_combo.clear()
        self.health_lbl.setText("● Cluster")
        self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")

    def _refresh_current_tab(self, _=None):
        idx = self.sub_tabs.currentIndex()
        if   idx == 0: self._load_pods()
        elif idx == 1: self._load_deployments()
        elif idx == 2: self._load_statefulsets()
        elif idx == 3: self._load_daemonsets()
        elif idx == 4: self._load_services()
        elif idx == 5: self._load_ingress()
        elif idx == 6: self._load_config_resources()
        elif idx == 7: self._load_events()
        elif idx == 8: self._refresh_tunnel_status()

    # ── Pods ──────────────────────────────────────────────────
    # `kubectl get pods -o wide` renders the RESTARTS column as a plain
    # number ("0") normally, but as "N (Ndhm ago)" — a single logical
    # value containing a space — for any pod whose last restart was
    # recent enough for kubectl to bother annotating it. line.split()
    # blows that annotation into two extra whitespace-separated tokens
    # ("(22d", "ago)"), which silently shifts every fixed-position column
    # after it (AGE/IP/NODE) by two — the misalignment seen when a
    # recently-restarted pod's IP/Node show up empty or wrong while an
    # untouched pod in the same table lines up fine. _split_pod_line
    # detects that two-token annotation before the fixed-offset slicing
    # below runs, pulls it out into its own "Last Restart" value (rather
    # than just discarding it — it's genuinely useful info), and returns
    # the remaining tokens so the real columns land back in place.
    _PAREN_OPEN_RE  = re.compile(r"^\(\S*$")
    _PAREN_CLOSE_RE = re.compile(r"^\S*\)$")

    @staticmethod
    def _split_pod_line(line: str):
        """Returns (cleaned_parts, last_restart). last_restart is e.g.
        "22d ago", or "" if this pod has never restarted (or kubectl's
        RESTARTS column didn't include the annotation)."""
        parts = line.split()
        cleaned = []
        last_restart = ""
        i = 0
        while i < len(parts):
            if (KubernetesTab._PAREN_OPEN_RE.match(parts[i]) and i + 1 < len(parts)
                    and KubernetesTab._PAREN_CLOSE_RE.match(parts[i + 1])):
                # "(22d" + "ago)" -> "22d ago"
                last_restart = f"{parts[i][1:]} {parts[i + 1][:-1]}"
                i += 2
                continue
            cleaned.append(parts[i])
            i += 1
        return cleaned, last_restart

    def _load_pods(self):
        self._run_cmd(f"kubectl get pods {self._ns_flag()} -o wide 2>&1", self._populate_pods)

    def _populate_pods(self, out: str):
        self.pod_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total   = 0
        running = 0
        # The namespace chip on each card is only shown in "(all namespaces)"
        # view (i.e. rows can differ) — when one namespace is selected it's
        # implied by ns_combo already, so the chip would just repeat itself.
        for line in out.strip().splitlines()[1:]:
            parts, last_restart = self._split_pod_line(line)
            if all_ns:
                # `kubectl get pods --all-namespaces -o wide` prepends NAMESPACE.
                if len(parts) < 6:
                    continue
                ns, name, ready, status, restarts, age = parts[:6]
                ip   = parts[6] if len(parts) > 6 else "-"
                node = parts[7] if len(parts) > 7 else "-"
            else:
                if len(parts) < 5:
                    continue
                ns = self._current_ns or "default"
                name, ready, status, restarts, age = parts[:5]
                ip   = parts[5] if len(parts) > 5 else "-"
                node = parts[6] if len(parts) > 6 else "-"
            meta = {
                "namespace": ns, "name": name, "ready": ready, "status": status,
                "restarts": restarts, "last_restart": last_restart or "-",
                "age": age, "ip": ip, "node": node,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, PodCardWidget.CARD_HEIGHT))
            self.pod_list.addItem(item)
            self.pod_list.setItemWidget(item, PodCardWidget(meta, all_ns))
            total += 1
            if "running" in status.lower():
                running += 1
        if total == 0:
            color_key = "TEXT_MUTED"
        elif running == total:
            color_key = "SUCCESS"
        elif running == 0:
            color_key = "DANGER"
        else:
            color_key = "WARNING"
        self._set_count_badge(self.pod_count_lbl, f"{running}/{total} running", color_key)
        # Refreshing rebuilds every row from scratch, which would otherwise
        # silently show everything again even though the filter box still
        # has text in it — reapply whatever's currently typed there.
        self._filter_pods(self.pod_filter.text())

    def _filter_pods(self, text: str):
        q = text.lower()
        for i in range(self.pod_list.count()):
            item = self.pod_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_pod(self) -> tuple:  # (Optional[str], str)
        item = self.pod_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a pod first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _on_pod_double_click(self, item):
        """Double-clicking a pod card is a shortcut for Describe — reads
        name/namespace off the card that was actually double-clicked rather
        than relying on _selected_pod()'s currentItem(), since a
        double-click's second press is what sets the current item and
        there's no reason to depend on that timing."""
        meta = item.data(Qt.UserRole) or {}
        self._describe("pod", meta.get("name"), meta.get("namespace") or "default")

    def _on_pod_selection_changed(self, current, previous):
        """Cards paint their own selected state (they fully cover the
        QListWidgetItem's rect, so the list's native selection styling
        never shows through) — forward selection changes into them."""
        if previous is not None:
            w = self.pod_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.pod_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _pod_logs(self):
        pod, ns = self._selected_pod()
        if pod:
            LogViewerDialog(self, self.ssh, ns, pod).exec_()

    def _pod_exec(self):
        pod, ns = self._selected_pod()
        if pod:
            self._exec_pod(pod, ns)

    def _exec_pod(self, pod: str, ns: str):
        """Look up the pod's container names before opening ExecDialog —
        see ContainerPickerDialog's docstring for why. Single-container
        pods (the common case) skip straight to ExecDialog with no extra
        click."""
        self._run_cmd(
            f"kubectl get pod -n {ns} {pod} "
            f"-o jsonpath='{{.spec.containers[*].name}}' 2>&1",
            lambda out, pod=pod, ns=ns: self._on_exec_containers_fetched(out, pod, ns),
        )

    def _on_exec_containers_fetched(self, out: str, pod: str, ns: str):
        # Same trailing-quote defensiveness as _populate_namespaces.
        containers = out.strip().strip("'").split()
        if len(containers) <= 1:
            ExecDialog(self, self.ssh, ns, pod, containers[0] if containers else None).exec_()
            return
        dlg = ContainerPickerDialog(self, pod, containers)
        if dlg.exec_() == QDialog.Accepted:
            ExecDialog(self, self.ssh, ns, pod, dlg.selected_container()).exec_()

    def _pod_delete(self):
        pod, ns = self._selected_pod()
        if not pod:
            return
        if QMessageBox.question(self, "Delete Pod", f'Delete pod "{pod}"?',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete pod -n {ns} {pod} 2>&1",
                          lambda o: (self._log(o), self._load_pods()))

    def _pod_restart(self):
        pod, ns = self._selected_pod()
        if pod:
            self._run_cmd(f"kubectl delete pod -n {ns} {pod} 2>&1",
                          lambda o: (self._log(o), self._load_pods()))

    def _pod_ctx_menu(self, pos):
        item = self.pod_list.itemAt(pos)
        if not item:
            return
        self.pod_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        pod  = meta.get("name")
        ns   = meta.get("namespace") or "default"
        menu = QMenu(self)
        menu.addAction("📋  View Logs",  lambda: LogViewerDialog(self, self.ssh, ns, pod).exec_())
        menu.addAction("💻  Exec Shell", lambda: self._exec_pod(pod, ns))
        menu.addAction("📄  Describe",   lambda: self._describe("pod", pod, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._pod_delete)
        menu.exec_(self.pod_list.viewport().mapToGlobal(pos))

    # ── Deployments ───────────────────────────────────────────
    def _load_deployments(self):
        self._run_cmd(f"kubectl get deployments {self._ns_flag()} -o wide 2>&1",
                      self._populate_deployments)

    def _populate_deployments(self, out: str):
        self.deploy_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total = 0
        ready_count = 0
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if all_ns:
                # `kubectl get deployments --all-namespaces -o wide` prepends
                # NAMESPACE — without accounting for it, every column below
                # silently shifts left by one (Name shows the namespace,
                # Ready shows the name, and so on).
                if len(parts) < 6:
                    continue
                ns, name, ready, upd, avail, age = parts[:6]
                imgs = " | ".join(parts[6:]) if len(parts) > 6 else "-"
            else:
                if len(parts) < 5:
                    continue
                ns = self._current_ns or "default"
                name, ready, upd, avail, age = parts[:5]
                imgs = " | ".join(parts[5:]) if len(parts) > 5 else "-"
            meta = {
                "namespace": ns, "name": name, "ready": ready,
                "up_to_date": upd, "available": avail, "age": age, "images": imgs,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, DeploymentCardWidget.CARD_HEIGHT))
            self.deploy_list.addItem(item)
            self.deploy_list.setItemWidget(item, DeploymentCardWidget(meta, all_ns))
            total += 1
            try:
                cur, desired = ready.split("/")
                if cur == desired:
                    ready_count += 1
            except Exception:
                pass
        if total == 0:
            color_key = "TEXT_MUTED"
        elif ready_count == total:
            color_key = "SUCCESS"
        elif ready_count == 0:
            color_key = "DANGER"
        else:
            color_key = "WARNING"
        self._set_count_badge(self.deploy_count_lbl, f"{total} deployment{'s' if total != 1 else ''} · {ready_count} ready", color_key)
        # Same reasoning as _populate_pods: rebuild wipes the visual filter
        # state even though the filter box still has text — reapply it.
        self._filter_deployments(self.deploy_filter.text())

    def _on_deploy_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        try:
            _, desired = meta.get("ready", "").split("/")
            self.scale_spin.setValue(int(desired))
        except Exception:
            pass

    def _on_deploy_double_click(self, item):
        """Double-clicking a deployment card is a shortcut for Describe —
        reads name/namespace off the card that was actually double-clicked,
        same reasoning as _on_pod_double_click above."""
        meta = item.data(Qt.UserRole) or {}
        self._describe("deployment", meta.get("name"), meta.get("namespace") or "default")

    def _on_deploy_selection_changed(self, current, previous):
        """Same reasoning as _on_pod_selection_changed above."""
        if previous is not None:
            w = self.deploy_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.deploy_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _filter_deployments(self, text: str):
        q = text.lower()
        for i in range(self.deploy_list.count()):
            item = self.deploy_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_deploy(self) -> tuple:  # (Optional[str], str)
        item = self.deploy_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a deployment first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _deploy_scale(self):
        dep, ns = self._selected_deploy()
        if not dep:
            return
        replicas = self.scale_spin.value()
        if QMessageBox.question(self, "Scale", f'Scale "{dep}" to {replicas} replica(s)?',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl scale deployment {dep} -n {ns} --replicas={replicas} 2>&1",
                          lambda o: (self._log(o), self._load_deployments()))

    def _deploy_restart(self):
        dep, ns = self._selected_deploy()
        if dep:
            self._run_cmd(f"kubectl rollout restart deployment/{dep} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_deployments()))

    def _deploy_describe(self):
        dep, ns = self._selected_deploy()
        if dep:
            self._describe("deployment", dep, ns)

    def _deploy_delete(self):
        dep, ns = self._selected_deploy()
        if not dep:
            return
        if QMessageBox.question(self, "Delete Deployment",
                                f'Delete deployment "{dep}"?\nThis will remove all its pods.',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete deployment {dep} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_deployments()))

    # ── StatefulSets ──────────────────────────────────────────
    def _load_statefulsets(self):
        self._run_cmd(f"kubectl get statefulsets {self._ns_flag()} -o wide 2>&1",
                      self._populate_statefulsets)

    def _populate_statefulsets(self, out: str):
        self.sts_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total = 0
        ready_count = 0
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            # `kubectl get statefulsets -o wide` columns: NAME READY AGE
            # CONTAINERS IMAGES (NAMESPACE prepended in --all-namespaces).
            if all_ns:
                if len(parts) < 4:
                    continue
                ns, name, ready, age = parts[:4]
                imgs = " | ".join(parts[4:]) if len(parts) > 4 else "-"
            else:
                if len(parts) < 3:
                    continue
                ns = self._current_ns or "default"
                name, ready, age = parts[:3]
                imgs = " | ".join(parts[3:]) if len(parts) > 3 else "-"
            meta = {
                "namespace": ns, "name": name, "ready": ready,
                "age": age, "images": imgs,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, StatefulSetCardWidget.CARD_HEIGHT))
            self.sts_list.addItem(item)
            self.sts_list.setItemWidget(item, StatefulSetCardWidget(meta, all_ns))
            total += 1
            try:
                cur, desired = ready.split("/")
                if cur == desired:
                    ready_count += 1
            except Exception:
                pass
        if total == 0:
            color_key = "TEXT_MUTED"
        elif ready_count == total:
            color_key = "SUCCESS"
        elif ready_count == 0:
            color_key = "DANGER"
        else:
            color_key = "WARNING"
        self._set_count_badge(self.sts_count_lbl, f"{total} statefulset{'s' if total != 1 else ''} · {ready_count} ready", color_key)
        self._filter_statefulsets(self.sts_filter.text())

    def _on_sts_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        try:
            _, desired = meta.get("ready", "").split("/")
            self.sts_scale_spin.setValue(int(desired))
        except Exception:
            pass

    def _on_sts_double_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        self._describe("statefulset", meta.get("name"), meta.get("namespace") or "default")

    def _on_sts_selection_changed(self, current, previous):
        if previous is not None:
            w = self.sts_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.sts_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _filter_statefulsets(self, text: str):
        q = text.lower()
        for i in range(self.sts_list.count()):
            item = self.sts_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_sts(self) -> tuple:  # (Optional[str], str)
        item = self.sts_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a statefulset first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _sts_scale(self):
        sts, ns = self._selected_sts()
        if not sts:
            return
        replicas = self.sts_scale_spin.value()
        if QMessageBox.question(self, "Scale", f'Scale "{sts}" to {replicas} replica(s)?',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl scale statefulset {sts} -n {ns} --replicas={replicas} 2>&1",
                          lambda o: (self._log(o), self._load_statefulsets()))

    def _sts_restart(self):
        sts, ns = self._selected_sts()
        if sts:
            self._run_cmd(f"kubectl rollout restart statefulset/{sts} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_statefulsets()))

    def _sts_describe(self):
        sts, ns = self._selected_sts()
        if sts:
            self._describe("statefulset", sts, ns)

    def _sts_delete(self):
        sts, ns = self._selected_sts()
        if not sts:
            return
        if QMessageBox.question(self, "Delete StatefulSet",
                                f'Delete statefulset "{sts}"?\nThis will remove all its pods.',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete statefulset {sts} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_statefulsets()))

    def _sts_ctx_menu(self, pos):
        item = self.sts_list.itemAt(pos)
        if not item:
            return
        self.sts_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        name = meta.get("name")
        ns   = meta.get("namespace") or "default"
        menu = QMenu(self)
        menu.addAction("↺  Restart",  self._sts_restart)
        menu.addAction("📄  Describe", lambda: self._describe("statefulset", name, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._sts_delete)
        menu.exec_(self.sts_list.viewport().mapToGlobal(pos))

    # ── DaemonSets ────────────────────────────────────────────
    def _load_daemonsets(self):
        self._run_cmd(f"kubectl get daemonsets {self._ns_flag()} -o wide 2>&1",
                      self._populate_daemonsets)

    def _populate_daemonsets(self, out: str):
        self.ds_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total = 0
        ready_count = 0
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            # `kubectl get daemonsets -o wide` columns: NAME DESIRED CURRENT
            # READY UP-TO-DATE AVAILABLE NODE-SELECTOR AGE CONTAINERS IMAGES
            # SELECTOR (NAMESPACE prepended in --all-namespaces). NODE-SELECTOR
            # renders as a single space-free token ("<none>" or a real
            # selector expression), so straight positional split() still
            # lines the fixed columns up correctly.
            if all_ns:
                if len(parts) < 9:
                    continue
                ns, name, desired, current, ready, upd, avail, node_sel, age = parts[:9]
                imgs = " | ".join(parts[9:]) if len(parts) > 9 else "-"
            else:
                if len(parts) < 8:
                    continue
                ns = self._current_ns or "default"
                name, desired, current, ready, upd, avail, node_sel, age = parts[:8]
                imgs = " | ".join(parts[8:]) if len(parts) > 8 else "-"
            meta = {
                "namespace": ns, "name": name, "desired": desired, "current": current,
                "ready": ready, "up_to_date": upd, "available": avail,
                "node_selector": node_sel, "age": age, "images": imgs,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, DaemonSetCardWidget.CARD_HEIGHT))
            self.ds_list.addItem(item)
            self.ds_list.setItemWidget(item, DaemonSetCardWidget(meta, all_ns))
            total += 1
            if desired == ready or desired == "0":
                ready_count += 1
        if total == 0:
            color_key = "TEXT_MUTED"
        elif ready_count == total:
            color_key = "SUCCESS"
        elif ready_count == 0:
            color_key = "DANGER"
        else:
            color_key = "WARNING"
        self._set_count_badge(self.ds_count_lbl, f"{total} daemonset{'s' if total != 1 else ''} · {ready_count} ready", color_key)
        self._filter_daemonsets(self.ds_filter.text())

    def _on_ds_double_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        self._describe("daemonset", meta.get("name"), meta.get("namespace") or "default")

    def _on_ds_selection_changed(self, current, previous):
        if previous is not None:
            w = self.ds_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.ds_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _filter_daemonsets(self, text: str):
        q = text.lower()
        for i in range(self.ds_list.count()):
            item = self.ds_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_ds(self) -> tuple:  # (Optional[str], str)
        item = self.ds_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a daemonset first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _ds_restart(self):
        ds, ns = self._selected_ds()
        if ds:
            self._run_cmd(f"kubectl rollout restart daemonset/{ds} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_daemonsets()))

    def _ds_describe(self):
        ds, ns = self._selected_ds()
        if ds:
            self._describe("daemonset", ds, ns)

    def _ds_delete(self):
        ds, ns = self._selected_ds()
        if not ds:
            return
        if QMessageBox.question(self, "Delete DaemonSet",
                                f'Delete daemonset "{ds}"?\nThis will remove it from every node.',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete daemonset {ds} -n {ns} 2>&1",
                          lambda o: (self._log(o), self._load_daemonsets()))

    def _ds_ctx_menu(self, pos):
        item = self.ds_list.itemAt(pos)
        if not item:
            return
        self.ds_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        name = meta.get("name")
        ns   = meta.get("namespace") or "default"
        menu = QMenu(self)
        menu.addAction("↺  Restart",  self._ds_restart)
        menu.addAction("📄  Describe", lambda: self._describe("daemonset", name, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._ds_delete)
        menu.exec_(self.ds_list.viewport().mapToGlobal(pos))

    # ── Services ──────────────────────────────────────────────
    def _load_services(self):
        self._run_cmd(f"kubectl get services {self._ns_flag()} 2>&1", self._populate_services)

    def _populate_services(self, out: str):
        self.svc_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total = 0
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            # `kubectl get services --all-namespaces` prepends NAMESPACE —
            # same shifted-columns reasoning as _populate_pods/_populate_deployments.
            if all_ns:
                if len(parts) < 7:
                    continue
                ns, name, stype, cluster, ext, ports, age = parts[:7]
            else:
                if len(parts) < 6:
                    continue
                ns = self._current_ns or "default"
                name, stype, cluster, ext, ports, age = parts[:6]
            meta = {
                "namespace": ns, "name": name, "type": stype,
                "cluster_ip": cluster, "external_ip": ext,
                "ports": ports, "age": age,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, ServiceCardWidget.CARD_HEIGHT))
            self.svc_list.addItem(item)
            self.svc_list.setItemWidget(item, ServiceCardWidget(meta, all_ns))
            total += 1
        color_key = "TEXT_MUTED" if total == 0 else "INFO"
        self._set_count_badge(self.svc_count_lbl,
                               f"{total} service{'s' if total != 1 else ''}", color_key)
        # Same reasoning as _populate_pods: rebuild wipes the visual filter
        # state even though the filter box still has text — reapply it.
        self._filter_services(self.svc_filter.text())

    def _filter_services(self, text: str):
        q = text.lower()
        for i in range(self.svc_list.count()):
            item = self.svc_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_svc(self) -> tuple:  # (Optional[str], str)
        item = self.svc_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a service first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _on_svc_double_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        self._describe("service", meta.get("name"), meta.get("namespace") or "default")

    def _on_svc_selection_changed(self, current, previous):
        """Same reasoning as _on_pod_selection_changed above."""
        if previous is not None:
            w = self.svc_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.svc_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _svc_describe(self):
        svc, ns = self._selected_svc()
        if svc:
            self._describe("service", svc, ns)

    def _svc_delete(self):
        svc, ns = self._selected_svc()
        if not svc:
            return
        if QMessageBox.question(self, "Delete Service", f'Delete service "{svc}"?',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete service -n {ns} {svc} 2>&1",
                          lambda o: (self._log(o), self._load_services()))

    def _svc_ctx_menu(self, pos):
        item = self.svc_list.itemAt(pos)
        if not item:
            return
        self.svc_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        svc  = meta.get("name")
        ns   = meta.get("namespace") or "default"
        menu = QMenu(self)
        menu.addAction("📄  Describe", lambda: self._describe("service", svc, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._svc_delete)
        menu.exec_(self.svc_list.viewport().mapToGlobal(pos))

    # ── Ingress ───────────────────────────────────────────────
    def _load_ingress(self):
        self._run_cmd(f"kubectl get ingress {self._ns_flag()} 2>&1", self._populate_ingress)

    def _populate_ingress(self, out: str):
        self.ing_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        total = 0
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if not parts:
                continue
            if all_ns:
                if len(parts) < 2:
                    continue
                ns, name = parts[0], parts[1]
                rest = parts[2:]
            else:
                ns = self._current_ns or "default"
                name = parts[0]
                rest = parts[1:]
            cls     = rest[0] if len(rest) > 0 else "-"
            hosts   = rest[1] if len(rest) > 1 else "-"
            address = rest[2] if len(rest) > 2 else "-"
            ports   = rest[3] if len(rest) > 3 else "-"
            age     = rest[4] if len(rest) > 4 else "-"
            meta = {
                "namespace": ns, "name": name, "class": cls, "hosts": hosts,
                "address": address, "ports": ports, "age": age,
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, IngressCardWidget.CARD_HEIGHT))
            self.ing_list.addItem(item)
            self.ing_list.setItemWidget(item, IngressCardWidget(meta, all_ns))
            total += 1
        color_key = "TEXT_MUTED" if total == 0 else "INFO"
        self._set_count_badge(self.ing_count_lbl,
                               f"{total} rule{'s' if total != 1 else ''}", color_key)
        self._filter_ingress(self.ing_filter.text())

    def _filter_ingress(self, text: str):
        q = text.lower()
        for i in range(self.ing_list.count()):
            item = self.ing_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _selected_ing(self) -> tuple:  # (Optional[str], str)
        item = self.ing_list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select an ingress first.")
            return None, ""
        meta = item.data(Qt.UserRole) or {}
        return meta.get("name"), meta.get("namespace") or "default"

    def _on_ing_double_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        self._describe("ingress", meta.get("name"), meta.get("namespace") or "default")

    def _on_ing_selection_changed(self, current, previous):
        """Same reasoning as _on_pod_selection_changed above."""
        if previous is not None:
            w = self.ing_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.ing_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _ing_describe(self):
        ing, ns = self._selected_ing()
        if ing:
            self._describe("ingress", ing, ns)

    def _ing_delete(self):
        ing, ns = self._selected_ing()
        if not ing:
            return
        if QMessageBox.question(self, "Delete Ingress", f'Delete ingress "{ing}"?',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run_cmd(f"kubectl delete ingress -n {ns} {ing} 2>&1",
                          lambda o: (self._log(o), self._load_ingress()))

    def _ing_ctx_menu(self, pos):
        item = self.ing_list.itemAt(pos)
        if not item:
            return
        self.ing_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        ing  = meta.get("name")
        ns   = meta.get("namespace") or "default"
        menu = QMenu(self)
        menu.addAction("📄  Describe", lambda: self._describe("ingress", ing, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._ing_delete)
        menu.exec_(self.ing_list.viewport().mapToGlobal(pos))

    # ── Config & Secrets ──────────────────────────────────────
    def _load_config_resources(self, _=None):
        rtype = "configmaps" if self._cfg_type == "ConfigMaps" else "secrets"
        cmd = (
            f"kubectl get {rtype} {self._ns_flag()} "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}}\n{{end}}' 2>&1"
        )
        self._run_cmd(cmd, self._populate_cfg_list)

    def _populate_cfg_list(self, out: str):
        self.cfg_list.clear()
        self.cfg_detail.clear()
        self.cfg_raw.clear()
        card_type = "configmap" if self._cfg_type == "ConfigMaps" else "secret"
        for name in out.strip().splitlines():
            name = name.strip()
            if not name:
                continue
            meta = {"name": name, "type": card_type}
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, ConfigCardWidget.CARD_HEIGHT))
            self.cfg_list.addItem(item)
            self.cfg_list.setItemWidget(item, ConfigCardWidget(meta))
        # Same reasoning as _populate_pods: reapply whatever's in the
        # filter box, since the rebuild above doesn't know about it.
        self._filter_configs(self.cfg_filter.text())

    def _filter_configs(self, text: str):
        q = text.lower()
        for i in range(self.cfg_list.count()):
            item = self.cfg_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            item.setHidden(q not in meta.get("name", "").lower())

    def _on_cfg_selection_changed(self, current, previous):
        """Same card-selection forwarding as pods/deployments — the card
        widget owns its own selected-state paint, so the list has to tell
        it explicitly (see k8s_cards.py's _CardBase docstring)."""
        if previous is not None:
            w = self.cfg_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is None:
            return
        w = self.cfg_list.itemWidget(current)
        if w:
            w.set_selected(True)
        meta  = current.data(Qt.UserRole) or {}
        name  = meta.get("name", "")
        rtype = meta.get("type", "configmap")
        ns    = self._current_ns if self._current_ns != "(all namespaces)" else "default"
        self._run_cmd(f"kubectl get {rtype} {name} -n {ns} -o json 2>&1",
                      lambda o: self._show_cfg_detail(o, rtype))

    def _pretty_cfg_value(self, val) -> str:
        """Re-indent a ConfigMap/Secret value for display.

        Values are often themselves a JSON document embedded as a string
        (e.g. a 'config.json' key). kubectl's default '-o yaml' renders any
        string containing real newlines as a double-quoted flow scalar —
        literal '\\n' escapes, long lines wrapped with a trailing
        backslash — which is unreadable for exactly this case. We already
        have the value un-escaped (real newlines) from '-o json', so if it
        parses as JSON we re-emit it with consistent 2-space indentation;
        otherwise it's shown as-is with its real line breaks intact.
        """
        text = str(val)
        stripped = text.strip()
        if stripped[:1] in "{[":
            try:
                parsed = json.loads(stripped)
                return json.dumps(parsed, indent=2)
            except Exception:
                pass
        return text

    def _show_cfg_detail(self, out: str, rtype: str):
        self.cfg_detail.clear()
        self.cfg_raw.clear()
        try:
            obj = json.loads(out)
        except Exception:
            QTreeWidgetItem(self.cfg_detail, ["(parse error)", out[:200]])
            self.cfg_raw.setPlainText(out)
            return

        data = obj.get("data") or obj.get("stringData") or {}
        decoded = {}
        for key, val in data.items():
            if rtype == "secret":
                import base64
                try:
                    val = base64.b64decode(val).decode(errors="replace")
                except Exception:
                    val = "(binary)"
            decoded[key] = val

        # ── Tree: one row per key, single-line preview (real newlines
        # would otherwise render oddly/collapse inside a tree cell) ──
        for key, val in decoded.items():
            preview = str(val).replace("\n", " ⏎ ").strip()
            if len(preview) > 200:
                preview = preview[:200] + " …"
            vi = QTreeWidgetItem([key, preview])
            vi.setFont(0, monospace_font(11))
            vi.setFont(1, monospace_font(10))
            self.cfg_detail.addTopLevelItem(vi)

        # ── Structured view: metadata + each key's full content, pretty
        # printed instead of kubectl's escaped/wrapped YAML flow scalar.
        meta = obj.get("metadata", {}) or {}
        lines = [
            f"apiVersion: {obj.get('apiVersion', 'v1')}",
            f"kind: {obj.get('kind', rtype.capitalize())}",
            f"name: {meta.get('name', '')}",
            f"namespace: {meta.get('namespace', '')}",
            "",
        ]
        for key, val in decoded.items():
            lines.append(f"{key}:")
            pretty = self._pretty_cfg_value(val)
            for pline in (pretty.splitlines() or [""]):
                lines.append(f"  {pline}")
            lines.append("")
        self.cfg_raw.setPlainText("\n".join(lines).rstrip() + "\n")

    # ── Events ────────────────────────────────────────────────
    @staticmethod
    def _humanize_age(iso_ts: str) -> str:
        """Compact kubectl-style age ('45s' / '12m' / '3h' / '5d' / '2y')
        from a Kubernetes ISO-8601 UTC timestamp. Events come from '-o json'
        rather than a pre-formatted AGE column (unlike every other tab
        here), so this has to be computed client-side."""
        if not iso_ts:
            return "-"
        try:
            ts = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            return "-"
        secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        if days < 365:
            return f"{days}d"
        return f"{days // 365}y"

    def _load_events(self):
        self._run_cmd(f"kubectl get events {self._ns_flag()} -o json 2>&1", self._on_events_loaded)

    def _on_events_loaded(self, out: str):
        # Cached so the "Warnings only" toggle can re-filter instantly
        # without an extra SSH round-trip.
        self._events_raw = out
        self._populate_events(out)

    def _populate_events(self, out: str):
        self.event_list.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        try:
            items = json.loads(out).get("items", [])
        except Exception:
            items = []

        def sort_key(it):
            return (it.get("lastTimestamp") or it.get("eventTime")
                    or (it.get("metadata") or {}).get("creationTimestamp") or "")

        items.sort(key=sort_key, reverse=True)

        warnings_only = getattr(self, "_events_warnings_only", False)
        total = 0
        warning_count = 0
        for it in items:
            etype = it.get("type") or "Normal"
            if warnings_only and etype != "Warning":
                continue
            involved = it.get("involvedObject") or {}
            ts = sort_key(it)
            meta = {
                "namespace":   (it.get("metadata") or {}).get("namespace", ""),
                "type":        etype,
                "reason":      it.get("reason", "") or "-",
                "message":     it.get("message", "") or "",
                "count":       it.get("count") or 1,
                "object_kind": involved.get("kind", ""),
                "object_name": involved.get("name", ""),
                "age":         self._humanize_age(ts),
            }
            item = QListWidgetItem()
            item.setData(Qt.UserRole, meta)
            item.setSizeHint(QSize(0, EventCardWidget.CARD_HEIGHT))
            self.event_list.addItem(item)
            self.event_list.setItemWidget(item, EventCardWidget(meta, all_ns))
            total += 1
            if etype == "Warning":
                warning_count += 1

        if total == 0:
            color_key = "TEXT_MUTED"
        elif warning_count == 0:
            color_key = "SUCCESS"
        else:
            color_key = "DANGER"
        self._set_count_badge(
            self.event_count_lbl,
            f"{total} event{'s' if total != 1 else ''} · {warning_count} warning{'s' if warning_count != 1 else ''}",
            color_key,
        )
        self._filter_events(self.event_filter.text())

    def _toggle_events_warnings_only(self, on: bool):
        self._events_warnings_only = on
        if getattr(self, "_events_raw", None):
            self._populate_events(self._events_raw)
        else:
            self._load_events()

    def _filter_events(self, text: str):
        q = text.lower()
        for i in range(self.event_list.count()):
            item = self.event_list.item(i)
            meta = item.data(Qt.UserRole) or {}
            searchable = (
                f"{meta.get('reason', '')} {meta.get('object_kind', '')} "
                f"{meta.get('object_name', '')} {meta.get('message', '')}"
            ).lower()
            item.setHidden(q not in searchable)

    def _on_event_selection_changed(self, current, previous):
        if previous is not None:
            w = self.event_list.itemWidget(previous)
            if w:
                w.set_selected(False)
        if current is not None:
            w = self.event_list.itemWidget(current)
            if w:
                w.set_selected(True)

    def _event_involved_object(self, meta: dict):
        """Returns (kubectl_kind, name, namespace) for the object an event
        is about, or (None, None, None) if the event didn't carry one."""
        kind = (meta.get("object_kind") or "").lower()
        name = meta.get("object_name")
        if not kind or not name:
            return None, None, None
        ns = meta.get("namespace") or (
            self._current_ns if self._current_ns != "(all namespaces)" else "default"
        )
        return kind, name, ns

    def _on_event_double_click(self, item):
        meta = item.data(Qt.UserRole) or {}
        kind, name, ns = self._event_involved_object(meta)
        if kind:
            self._describe(kind, name, ns)

    def _event_ctx_menu(self, pos):
        item = self.event_list.itemAt(pos)
        if not item:
            return
        self.event_list.setCurrentItem(item)
        meta = item.data(Qt.UserRole) or {}
        kind, name, ns = self._event_involved_object(meta)
        menu = QMenu(self)
        if kind:
            menu.addAction(f"📄  Describe {meta.get('object_kind')}/{name}",
                           lambda: self._describe(kind, name, ns))
            menu.addSeparator()
        menu.addAction("📋  Copy Message",
                       lambda: QApplication.clipboard().setText(meta.get("message", "")))
        menu.exec_(self.event_list.viewport().mapToGlobal(pos))

    # ── Describe helper ───────────────────────────────────────
    def _describe(self, kind: str, name: str, ns: str):
        self._describe_title = f"describe {kind}/{name}"
        self._run_cmd(f"kubectl describe {kind} {name} -n {ns} 2>&1",
                      self._show_describe_dialog)

    def _show_describe_dialog(self, out: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(getattr(self, "_describe_title", "Describe"))
        dlg.resize(900, 620)
        apply_qss_to(dlg)
        lay = _QVL(dlg)
        te  = QTextEdit()
        te.setReadOnly(True)
        te.setFont(monospace_font(11))
        te.setPlainText(out)
        lay.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec_()

    # ── Terminal output helpers ───────────────────────────────
    @staticmethod
    def _esc(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

    def _append_html(self, html: str):
        """Append raw HTML (e.g. the colored '$ cmd' line)."""
        append_terminal_html(self.k8s_terminal, html)

    def _append_pre(self, text: str):
        """Append plain command output, preserving whitespace so columns don't zigzag."""
        append_terminal_text(self.k8s_terminal, text)

    # ── Terminal tab ──────────────────────────────────────────
    def _run_kubectl(self):
        cmd = self.kubectl_inp.text().strip()
        if not cmd:
            return
        if not cmd.startswith("kubectl"):
            cmd = "kubectl " + cmd
        self.kubectl_inp.clear()
        self._run_cmd(cmd + " 2>&1",
                      lambda o: (self._log(o), self.sub_tabs.setCurrentIndex(5)))

    def _run_kubectl_terminal(self):
        cmd = self.k8s_inp.text().strip()
        if not cmd:
            return
        if not cmd.startswith("kubectl"):
            cmd = "kubectl " + cmd
        self._append_html(f"\n<span style='color:{T['ACCENT2']}'>$ {self._esc(cmd)}</span>")
        self._run_cmd(cmd + " 2>&1", lambda o: self._append_pre(o))
        self.k8s_inp.clear()

    # ── Port tunnels ──────────────────────────────────────────
    # ── Status-column glyphs ───────────────────────────────────
    STATUS_UNKNOWN = "⚪"
    STATUS_UP      = "🟢"
    STATUS_DOWN    = "🔴"

    def _format_tunnel_label(self, svc: dict, glyph: str) -> str:
        max_name, max_ns = self._tunnel_col_widths
        return (
            f"{glyph}  "
            f"{svc['name']:<{max_name}}"
            f"{'ns/' + svc['namespace']:<{max_ns}}"
            f" : {svc['container_port']}"
            f" → {svc['port']}"
        )

    def _open_manage_tunnel_services(self):
        """Open the card-based dialog for adding/editing/removing tunnel
        services on the connected VM's CSV. Reloads the checklist from the
        VM afterwards so it reflects whatever was actually saved (rather
        than just trusting the dialog's in-memory copy)."""
        if not self.ssh:
            QMessageBox.information(
                self, "Not connected",
                "Connect to a VM first to manage its tunnel services."
            )
            return
        dlg = ManageTunnelServicesDialog(
            self.ssh, self._tunnel_services, self._tunnel_csv_path,
            namespaces=self._namespaces, parent=self,
        )
        dlg.services_saved.connect(lambda _: self._load_tunnel_csv())
        dlg.exec_()

    def _change_tunnel_csv_path(self):
        """Let the user point the Tunnels tab at a different remote CSV
        (e.g. their own file instead of the shared team default), and
        remember the choice across restarts."""
        path, ok = QInputDialog.getText(
            self, "Change Tunnel Services File",
            "Remote CSV path (on the connected VM):",
            QLineEdit.Normal, self._tunnel_csv_path,
        )
        if not ok:
            return
        path = path.strip()
        if not path or path == self._tunnel_csv_path:
            return
        self._tunnel_csv_path = path
        self.tunnel_path_lbl.setText(f"📄  {self._tunnel_csv_path}  (on VM)")
        save_settings(tunnel_csv_path=self._tunnel_csv_path)
        if self.ssh:
            self._load_tunnel_csv()

    def _load_tunnel_csv(self):
        """Load tunnel services from the currently connected VM."""

        self.tunnel_list.blockSignals(True)
        self.tunnel_list.clear()
        self._tunnel_services = []

        # No VM connected
        if not self.ssh:
            self.tunnel_cmd_preview.clear()
            self.tunnel_list.blockSignals(False)
            return

        # Load services from the connected VM
        self._tunnel_services = load_tunnel_services(
            self.ssh,
            self._tunnel_csv_path
        )

        if not self._tunnel_services:
            item = QListWidgetItem("(No tunnel services found on the connected VM)")
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(QColor(T["TEXT_MUTED"]))
            self.tunnel_list.addItem(item)
        else:
            # Use the system's fixed-width font
            font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            font.setPointSize(10)

            # Determine column widths (shared with _refresh_tunnel_status
            # so updating the status glyph later doesn't reflow anything)
            max_name = max(len(svc["name"]) for svc in self._tunnel_services) + 4
            max_ns = max(len(f"ns/{svc['namespace']}") for svc in self._tunnel_services) + 4
            self._tunnel_col_widths = (max_name, max_ns)

            for svc in self._tunnel_services:
                label = self._format_tunnel_label(svc, self.STATUS_UNKNOWN)

                item = QListWidgetItem(label)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setData(Qt.UserRole, svc)
                item.setFont(font)
                item.setForeground(QColor(T["TEXT_MUTED"]))
                item.setToolTip("Checking whether this port is exposed on the VM…")

                self.tunnel_list.addItem(item)

        self.tunnel_list.blockSignals(False)
        self._update_tunnel_cmd_preview()
        self._refresh_tunnel_status()

    def _refresh_tunnel_status(self):
        """Check, in a single SSH round-trip, which configured local ports
        are currently listening on the VM — i.e. whether each service's
        'kubectl port-forward' is actually up right now — and update the
        checklist with a 🟢/🔴 status glyph per row."""
        if not self.ssh or not self._tunnel_services:
            return
        cmd = "ss -ltn 2>/dev/null | awk 'NR>1{print $4}'"
        self._run_cmd(cmd, self._on_tunnel_status_result)

    def _on_tunnel_status_result(self, out: str):
        listening_ports = set()
        for line in out.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            port_s = line.rsplit(":", 1)[-1]
            if port_s.isdigit():
                listening_ports.add(int(port_s))

        for i in range(self.tunnel_list.count()):
            item = self.tunnel_list.item(i)
            svc = item.data(Qt.UserRole)
            if not svc:
                continue
            exposed = svc["port"] in listening_ports
            glyph   = self.STATUS_UP if exposed else self.STATUS_DOWN
            item.setText(self._format_tunnel_label(svc, glyph))
            item.setForeground(QColor(T["SUCCESS"] if exposed else T["DANGER"]))
            item.setToolTip(
                "Port {} is {} on the VM.".format(
                    svc["port"], "listening (exposed)" if exposed else "NOT listening"
                )
            )

    def _set_all_tunnel_checks(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        self.tunnel_list.blockSignals(True)
        for i in range(self.tunnel_list.count()):
            item = self.tunnel_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(state)
        self.tunnel_list.blockSignals(False)
        self._update_tunnel_cmd_preview()

    def _selected_tunnel_services(self) -> list:
        selected = []
        for i in range(self.tunnel_list.count()):
            item = self.tunnel_list.item(i)
            if (item.flags() & Qt.ItemIsUserCheckable) and item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        return selected

    def _build_tunnel_cmd(self, services: list):
        """Returns (cmd_list, None) on success or (None, error_message) on failure."""
        if not services:
            return None, "Select at least one service to tunnel."
        if not self._conn_host or not self._conn_user:
            return None, "Connect to an instance first."
        if not self._conn_pem:
            return None, "A private-key (.pem) connection is required for tunnelling."

        seen_ports = {}
        forwards = []
        for svc in services:
            port = svc["port"]
            if port in seen_ports and seen_ports[port] != svc["name"]:
                return None, (f"Port {port} is used by both '{seen_ports[port]}' and "
                              f"'{svc['name']}' — can't forward the same local port twice.")
            seen_ports[port] = svc["name"]
            forwards += ["-L", f"{port}:127.0.0.1:{port}"]

        cmd = ["ssh", "-nNT"] + forwards
        if self._conn_port and int(self._conn_port) != 22:
            cmd += ["-p", str(self._conn_port)]
        cmd += ["-i", self._conn_pem, f"{self._conn_user}@{self._conn_host}"]
        return cmd, None

    def _update_tunnel_cmd_preview(self, *_):
        services = self._selected_tunnel_services()
        cmd, err = self._build_tunnel_cmd(services)
        self.tunnel_cmd_preview.setText(" ".join(cmd) if cmd else (err or ""))

    def _start_tunnel(self):
        if self._tunnel_process is not None and self._tunnel_process.state() != QProcess.NotRunning:
            QMessageBox.information(self, "Tunnel already running",
                                    "Stop the current tunnel before starting a new one.")
            return

        services = self._selected_tunnel_services()
        cmd, err = self._build_tunnel_cmd(services)
        if not cmd:
            QMessageBox.warning(self, "Can't start tunnel", err)
            return

        self.tunnel_log.clear()
        append_terminal_html(self.tunnel_log, f"<span style='color:{T['ACCENT2']}'>$ {self._esc(' '.join(cmd))}</span>")

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda: self._on_tunnel_output(proc))
        proc.errorOccurred.connect(self._on_tunnel_error)
        proc.finished.connect(self._on_tunnel_finished)
        proc.start(cmd[0], cmd[1:])
        self._tunnel_process = proc

        self.tunnel_start_btn.setEnabled(False)
        self.tunnel_stop_btn.setEnabled(True)
        self.tunnel_status_lbl.setText(f"●  Tunnelling {len(services)} service(s)")
        self.tunnel_status_lbl.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 12px;")

    def _stop_tunnel(self):
        if self._tunnel_process is not None and self._tunnel_process.state() != QProcess.NotRunning:
            self._tunnel_process.terminate()
            if not self._tunnel_process.waitForFinished(2000):
                self._tunnel_process.kill()
                self._tunnel_process.waitForFinished(1000)
        # _on_tunnel_finished (connected above) resets buttons/status when the
        # process actually exits; if there was never a process, reset here.
        if self._tunnel_process is None:
            self.tunnel_start_btn.setEnabled(True)
            self.tunnel_stop_btn.setEnabled(False)
            self.tunnel_status_lbl.setText("●  Not tunnelling")
            self.tunnel_status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")

    def _on_ports_killed(self, out):
        if out.strip():
            append_terminal_text(self.tunnel_log, out)

        self._refresh_tunnel_status()

    def _kill_selected_ports(self):
        if not self.ssh:
            QMessageBox.warning(self, "Not connected", "Connect to an instance first.")
            return
        services = self._selected_tunnel_services()
        if not services:
            QMessageBox.warning(
                self,
                "No services selected",
                "Select one or more services first."
            )
            return
        ports = [str(svc["port"]) for svc in services]
        # Kill anything listening on the selected ports
        cmd = " ; ".join(
            [
                f"pid=$(lsof -ti:{port} 2>/dev/null); "
                f'[ -n "$pid" ] && kill -9 $pid || echo "Nothing running on {port}"'
                for port in ports
            ]
        )
        cmd = f"bash -lc {shlex.quote(cmd)}"
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['ACCENT2']}'>$ {self._esc(cmd)}</span>"
        )
        self._run_cmd(cmd, self._on_ports_killed)

    def _on_tunnel_output(self, proc):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        if data:
            append_terminal_text(self.tunnel_log, data)

    def _on_tunnel_error(self, error):
        append_terminal_html(self.tunnel_log, f"<span style='color:{T['DANGER']}'>[process error: {error}]</span>")

    def _on_tunnel_finished(self, exit_code, _exit_status):
        color = T['SUCCESS'] if exit_code == 0 else T['DANGER']
        append_terminal_html(self.tunnel_log, f"<span style='color:{color}'>[tunnel closed, exit code {exit_code}]</span>")
        self.tunnel_start_btn.setEnabled(True)
        self.tunnel_stop_btn.setEnabled(False)
        self.tunnel_status_lbl.setText("●  Not tunnelling")
        self.tunnel_status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self._tunnel_process = None

    # ── Remote kubectl port-forward restart ───────────────────
    # Distinct from the SSH -L tunnel above: this runs 'kubectl port-forward'
    # directly on the connected VM, in the exact format:
    #   nohup kubectl -n <namespace> port-forward svc/<name> <port>:<port> &
    # Supports restarting several services in one go.
    def _build_kubectl_tunnel_restart_cmd(self, services: list) -> str:
        """Kill whatever's already on the port, then relaunch — mirrors the
        kill_kubectl_port.sh + nohup pattern used by the real restart script."""
        cmds = []

        for svc in services:
            name = svc["name"]
            port = svc["port"]
            container_port = svc.get("container_port", port)
            ns = svc["namespace"]

            cmds.append(
                f'pid=$(lsof -ti:{port} 2>/dev/null); [ -n "$pid" ] && kill -9 $pid'
            )
            cmds.append(
                f"nohup kubectl -n {ns} port-forward svc/{name} "
                f"{port}:{container_port} > /dev/null 2>&1 &"
            )

        # ';' not '&&' — the kill script may exit nonzero if nothing was listening
        inner = " ; ".join(cmds)
        # exec_command() opens a non-login shell, which skips /etc/profile —
        # exactly where kubectl's PATH entry usually lives (Homebrew, snap,
        # etc.). "bash -lc" forces a login shell so those get sourced.
        return f"bash -lc {shlex.quote(inner)}"

    def _restart_kubectl_tunnels(self):
        if not self.ssh:
            QMessageBox.warning(self, "Not connected", "Connect to an instance first.")
            return

        services = self._selected_tunnel_services()
        if not services:
            QMessageBox.warning(self, "No services selected",
                                "Check one or more services below to restart their tunnel.")
            return

        # Each service's restart is an independent SSH command (kill-port +
        # nohup port-forward), so there's no dependency between them — but
        # each one's CommandWorker opens its own channel(s) on the *same*
        # SSH transport, and sshd caps how many of those can be open at
        # once (MaxSessions, commonly 10). Firing every selected service at
        # once used to blow past that ceiling as soon as more than a
        # handful were selected, and every worker beyond the limit failed
        # with ChannelException/"Unable to open channel." Instead, queue
        # everything and keep only MAX_CONCURRENT_TUNNEL_RESTARTS workers
        # in flight; each finished worker pulls the next one off the queue.
        self._tunnel_restart_queue    = list(services)
        self._tunnel_restart_pending  = len(services)
        self._tunnel_restart_failed   = []
        self._tunnel_restart_inflight = 0

        self.tunnel_restart_btn.setEnabled(False)
        self.progress.show()

        for _ in range(min(self.MAX_CONCURRENT_TUNNEL_RESTARTS, len(self._tunnel_restart_queue))):
            self._start_next_tunnel_restart()

    def _start_next_tunnel_restart(self):
        if not self._tunnel_restart_queue:
            return
        service = self._tunnel_restart_queue.pop(0)
        self._tunnel_restart_inflight += 1

        cmd = self._build_kubectl_tunnel_restart_cmd([service])  # single-service cmd
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['ACCENT2']}'>$ {self._esc(cmd)}</span>"
        )
        worker = CommandWorker(self.ssh, cmd)
        worker.done.connect(lambda out, svc=service: self._on_tunnel_step_done(svc, out))
        worker.error.connect(lambda err, svc=service: self._on_tunnel_step_error(svc, err))
        track_worker(self._workers, worker)
        worker.start()

    def _on_tunnel_step_done(self, service, output):
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['SUCCESS']}'>✓ {self._esc(service['name'])}</span>"
        )
        self._on_tunnel_step_finished()

    def _on_tunnel_step_error(self, service, err):
        self._tunnel_restart_failed.append(service['name'])
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['DANGER']}'>✗ {self._esc(service['name'])}: {self._esc(str(err))}</span>"
        )
        self._on_tunnel_step_finished()

    def _on_tunnel_step_finished(self):
        # Workers finish in whatever order the remote shell happens to
        # complete them, not the order they were started. Each completion
        # frees up one of the MAX_CONCURRENT_TUNNEL_RESTARTS slots, so pull
        # the next queued service (if any) in before checking whether the
        # whole batch is done.
        self._tunnel_restart_inflight -= 1
        self._tunnel_restart_pending  -= 1
        if self._tunnel_restart_queue:
            self._start_next_tunnel_restart()
        elif self._tunnel_restart_pending <= 0:
            self._finish_kubectl_tunnel_restarts()

    def _finish_kubectl_tunnel_restarts(self):
        if self._tunnel_restart_failed:
            self._on_kubectl_tunnel_restart_error(
                f"{len(self._tunnel_restart_failed)} service(s) failed: {', '.join(self._tunnel_restart_failed)}"
            )
        else:
            self._on_kubectl_tunnel_restart_done("all tunnels restarted")

    def _on_kubectl_tunnel_restart_done(self, out: str):
        self.progress.hide()
        self.tunnel_restart_btn.setEnabled(True)
        if out.strip():
            append_terminal_text(self.tunnel_log, out)
        # Give kubectl a moment to bind, then check what's actually listening.
        QTimer.singleShot(1200, self._refresh_tunnel_status)

    def _on_kubectl_tunnel_restart_error(self, err: str):
        # Even if this fires (e.g. a slow-starting kubectl, or any other
        # transient hiccup), the button must never stay stuck disabled, and
        # we should still check whether the tunnel actually came up.
        self.progress.hide()
        self.tunnel_restart_btn.setEnabled(True)
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['DANGER']}'>[error] {self._esc(err)}</span>"
        )
        QTimer.singleShot(1200, self._refresh_tunnel_status)

    # ── Worker runner ─────────────────────────────────────────
    def _run_cmd(self, cmd: str, callback):
        if not self.ssh:
            return
        self.progress.show()
        worker = CommandWorker(self.ssh, cmd)

        def on_done(out):
            self.progress.hide()
            callback(out)

        def on_error(e):
            self.progress.hide()
            self._log(f"[error] {e}")

        worker.done.connect(on_done)
        worker.error.connect(on_error)
        track_worker(self._workers, worker)
        worker.start()

    def _log(self, text: str):
        self._append_pre(text)
        self.status_msg.emit(text.split("\n")[0][:80])