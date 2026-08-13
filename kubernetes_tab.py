"""kubernetes_tab.py — The Kubernetes management tab widget."""

import json
import re
import shlex


from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QProgressBar, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QListWidget, QListWidgetItem, QTextEdit,
    QSplitter, QFrame, QSpinBox, QHeaderView, QAbstractItemView,
    QDialog, QVBoxLayout as _QVL, QDialogButtonBox, QMessageBox,
    QMenu,
)

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QProcess
from PyQt5.QtGui import QColor, QFont, QFontDatabase
from PyQt5.QtWidgets import QCompleter

from themes import T, apply_qss_to
from workers import CommandWorker, track_worker
from dialogs import LogViewerDialog, ExecDialog, ManageTunnelServicesDialog
from utils import (
    append_terminal_html, append_terminal_text,
    load_tunnel_services, REMOTE_TUNNEL_CSV_PATH,
    monospace_font,
)


class KubernetesTab(QWidget):
    status_msg = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ssh          = None
        self._current_ns  = "default"
        self._namespaces  = []
        self._workers     = []
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

        ns_row = QHBoxLayout()
        ns_row.setSpacing(8)
        self.ns_dot = QLabel("●")
        self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 9px;")
        ns_row.addWidget(self.ns_dot)
        ns_lbl = QLabel("Namespace")
        ns_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        ns_row.addWidget(ns_lbl)
        self.ns_combo = QComboBox()
        self.ns_combo.setMinimumWidth(170)
        self.ns_combo.setMaxVisibleItems(12)
        self.ns_combo.setEditable(True)
        self.ns_combo.setInsertPolicy(QComboBox.NoInsert)
        self.ns_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.ns_combo.completer().setFilterMode(Qt.MatchContains)
        self.ns_combo.currentTextChanged.connect(self._on_ns_change)
        ns_row.addWidget(self.ns_combo)
        cb.addLayout(ns_row)
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

        self.run_btn = self._toolbar_btn("Run", object_name="primary")
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
        self._build_services_tab()
        self._build_config_tab()
        self._build_tunnels_tab()
        self._build_terminal_tab()

    def _vline(self):
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setStyleSheet(f"color: {T['BORDER']};")
        f.setFixedWidth(1)
        return f

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

        self.pod_tree = QTreeWidget()
        font = self.pod_tree.font()
        font.setPointSize(14)   # Try 13 or 14 if needed
        self.pod_tree.setFont(font)
        self._style_tree(self.pod_tree)

        self.pod_tree.setRootIsDecorated(False)
        self.pod_tree.setAlternatingRowColors(True)
        self.pod_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.pod_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pod_tree.customContextMenuRequested.connect(self._pod_ctx_menu)
        self.pod_tree.itemDoubleClicked.connect(self._on_pod_double_click)
        self.pod_tree.setColumnCount(9)
        self.pod_tree.setHeaderLabels(
            ["Namespace", "Name", "Ready", "Status", "Restarts", "Last Restart", "Age", "IP", "Node"]
        )
        hdr = self.pod_tree.header()
        for i in range(9):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)   # Name gets the extra room
        # Namespace column is only meaningful in "(all namespaces)" view — when a
        # specific namespace is selected every row would repeat the same value, so
        # we hide the column and let the ns_combo above speak for itself instead.
        self.pod_tree.setColumnHidden(0, True)
        lay.addWidget(self.pod_tree)
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
            obj_name = "danger" if "Delete" in label else ("primary" if "Scale" in label else None)
            btn = self._toolbar_btn(label, object_name=obj_name)
            setattr(self, obj, btn)
            btn.clicked.connect(slot)
            tb.addWidget(btn)

        tb_widget = QWidget()
        tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        tb_widget.setLayout(tb)
        lay.addWidget(tb_widget)
        self.deploy_toolbar = tb_widget

        self.deploy_tree = QTreeWidget()
        self._style_tree(self.deploy_tree)
        self.deploy_tree.setRootIsDecorated(False)
        self.deploy_tree.setAlternatingRowColors(True)
        self.deploy_tree.setColumnCount(7)
        self.deploy_tree.setHeaderLabels(["Namespace", "Name", "Ready", "Up-to-date", "Available", "Age", "Images"])
        hdr = self.deploy_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.Stretch)
        for i in range(2, 6):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        # Namespace column is only meaningful in "(all namespaces)" view — when a
        # specific namespace is selected every row would repeat the same value, so
        # we hide the column and let the ns_combo above speak for itself instead.
        self.deploy_tree.setColumnHidden(0, True)
        self.deploy_tree.itemClicked.connect(self._on_deploy_click)
        self.deploy_tree.itemDoubleClicked.connect(self._on_deploy_double_click)
        lay.addWidget(self.deploy_tree)
        self.sub_tabs.addTab(w, "🚀  Deployments")

    def _build_services_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(1)

        # Services
        svc_grp = QWidget()
        sg = QVBoxLayout(svc_grp)
        sg.setContentsMargins(0, 0, 0, 0)
        sg.setSpacing(0)
        self.svc_hdr = QLabel("  Services")
        self.svc_hdr.setFixedHeight(30)
        self.svc_hdr.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
        )
        sg.addWidget(self.svc_hdr)
        self.svc_tree = QTreeWidget()
        self._style_tree(self.svc_tree)
        self.svc_tree.setRootIsDecorated(False)
        self.svc_tree.setAlternatingRowColors(True)
        self.svc_tree.setColumnCount(5)
        self.svc_tree.setHeaderLabels(["Name", "Type", "Cluster-IP", "External-IP", "Ports"])
        hdr = self.svc_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        for i in (1, 2, 3):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        sg.addWidget(self.svc_tree)
        splitter.addWidget(svc_grp)

        # Ingress
        ing_grp = QWidget()
        ig = QVBoxLayout(ing_grp)
        ig.setContentsMargins(0, 0, 0, 0)
        ig.setSpacing(0)
        self.ing_hdr = QLabel("  Ingress")
        self.ing_hdr.setFixedHeight(30)
        self.ing_hdr.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
        )
        ig.addWidget(self.ing_hdr)
        self.ing_tree = QTreeWidget()
        self._style_tree(self.ing_tree)
        self.ing_tree.setRootIsDecorated(False)
        self.ing_tree.setAlternatingRowColors(True)
        self.ing_tree.setColumnCount(4)
        self.ing_tree.setHeaderLabels(["Name", "Class", "Hosts", "Address"])
        for i in range(4):
            self.ing_tree.header().setSectionResizeMode(i, QHeaderView.Stretch)
        ig.addWidget(self.ing_tree)
        splitter.addWidget(ing_grp)
        splitter.setSizes([300, 200])
        lay.addWidget(splitter)
        self.sub_tabs.addTab(w, "🌐  Services & Ingress")

    def _build_config_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)

        # Left: type selector + list
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        self.cfg_type_bar = QWidget()
        self.cfg_type_bar.setFixedHeight(42)
        self.cfg_type_bar.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        tb_lay = QHBoxLayout(self.cfg_type_bar)
        tb_lay.setContentsMargins(10, 0, 10, 0)
        tb_lay.setSpacing(8)
        self.cfg_type_combo = QComboBox()
        self.cfg_type_combo.addItems(["ConfigMaps", "Secrets"])
        self.cfg_type_combo.currentTextChanged.connect(self._load_config_resources)
        tb_lay.addWidget(self.cfg_type_combo)
        self.cfg_filter = QLineEdit()
        self.cfg_filter.setPlaceholderText("🔍  Filter…")
        self.cfg_filter.textChanged.connect(self._filter_configs)
        tb_lay.addWidget(self.cfg_filter)
        ll.addWidget(self.cfg_type_bar)

        self.cfg_list = QListWidget()
        self.cfg_list.itemClicked.connect(self._on_cfg_select)
        ll.addWidget(self.cfg_list)
        splitter.addWidget(left)

        # Right: detail + raw yaml
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.cfg_detail_hdr = QLabel("  Data")
        self.cfg_detail_hdr.setFixedHeight(30)
        self.cfg_detail_hdr.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
        )
        rl.addWidget(self.cfg_detail_hdr)

        self.cfg_detail = QTreeWidget()
        self._style_tree(self.cfg_detail)
        self.cfg_detail.setRootIsDecorated(False)
        self.cfg_detail.setColumnCount(2)
        self.cfg_detail.setHeaderLabels(["Key", "Value"])
        self.cfg_detail.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.cfg_detail.header().setSectionResizeMode(1, QHeaderView.Stretch)
        rl.addWidget(self.cfg_detail)

        self.cfg_raw_lbl = QLabel("  Structured View")
        self.cfg_raw_lbl.setFixedHeight(30)
        self.cfg_raw_lbl.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
            f"border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
        )
        rl.addWidget(self.cfg_raw_lbl)

        self.cfg_raw = QTextEdit()
        self.cfg_raw.setReadOnly(True)
        self.cfg_raw.setFont(monospace_font(11))
        self.cfg_raw.setMinimumHeight(220)
        rl.addWidget(self.cfg_raw)

        splitter.addWidget(right)
        splitter.setSizes([280, 620])
        lay.addWidget(splitter)
        self.sub_tabs.addTab(w, "🔧  Config & Secrets")

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

        self.tunnel_path_lbl = QLabel(f"📄  {REMOTE_TUNNEL_CSV_PATH}  (on VM)")
        self.tunnel_path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        tb.addWidget(self.tunnel_path_lbl)
        tb.addStretch()

        reload_btn = self._toolbar_btn("↺  Reload CSV")
        reload_btn.clicked.connect(self._load_tunnel_csv)
        tb.addWidget(reload_btn)

        manage_btn = self._toolbar_btn(
            "⚙️  Manage Services",
            object_name="primary",
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
        self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 9px;")
        if hasattr(self, "pod_action_cluster"):
            self.pod_action_cluster.setStyleSheet(
                f"QFrame#action_cluster {{ background: {T['BG_ITEM']}; border-radius: 8px; }}"
            )
        self.k8s_terminal.setStyleSheet(
            f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
        )
        toolbar_style = f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        for bar in (getattr(self, "pods_toolbar", None), getattr(self, "deploy_toolbar", None),
                    getattr(self, "tunnel_toolbar", None)):
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
        header_style = (
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
            f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
        )
        for lbl in (getattr(self, "svc_hdr", None), getattr(self, "ing_hdr", None),
                    getattr(self, "cfg_detail_hdr", None)):
            if lbl is not None:
                lbl.setStyleSheet(header_style)
        if getattr(self, "cfg_type_bar", None) is not None:
            self.cfg_type_bar.setStyleSheet(
                f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
            )
        if getattr(self, "cfg_raw_lbl", None) is not None:
            self.cfg_raw_lbl.setStyleSheet(
                f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
                f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
                f"border-bottom: 1px solid {T['BORDER']}; padding-left: 12px;"
            )
        if self.ssh:
            self._check_cluster_health()

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
        self.pod_tree.clear()
        self.deploy_tree.clear()
        self.svc_tree.clear()
        self.ing_tree.clear()
        self.cfg_list.clear()
        self.cfg_detail.clear()
        self.cfg_raw.clear()
        self.ns_combo.clear()
        self.health_lbl.setText("● Cluster")
        self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")

    def _refresh_current_tab(self, _=None):
        idx = self.sub_tabs.currentIndex()
        if   idx == 0: self._load_pods()
        elif idx == 1: self._load_deployments()
        elif idx == 2: self._load_services(); self._load_ingress()
        elif idx == 3: self._load_config_resources()
        elif idx == 4: self._refresh_tunnel_status()

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
        self.pod_tree.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        # Only show the Namespace column when it's actually meaningful (i.e. rows
        # can differ). When one namespace is selected, it's implied by ns_combo.
        self.pod_tree.setColumnHidden(0, not all_ns)
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
            item = QTreeWidgetItem(
                [ns, name, ready, status, restarts, last_restart or "-", age, ip, node]
            )
            s = status.lower()
            if "running" in s:
                item.setForeground(3, QColor(T['SUCCESS']))
            elif "pending" in s or "init" in s:
                item.setForeground(3, QColor(T['WARNING']))
            elif any(x in s for x in ("error", "crash", "fail", "evict")):
                item.setForeground(3, QColor(T['DANGER']))
            item.setFont(1, monospace_font(11))
            self.pod_tree.addTopLevelItem(item)
        # Refreshing rebuilds every row from scratch, which would otherwise
        # silently show everything again even though the filter box still
        # has text in it — reapply whatever's currently typed there.
        self._filter_pods(self.pod_filter.text())

    def _filter_pods(self, text: str):
        q = text.lower()
        for i in range(self.pod_tree.topLevelItemCount()):
            item = self.pod_tree.topLevelItem(i)
            item.setHidden(q not in item.text(1).lower())

    def _selected_pod(self) -> tuple:  # (Optional[str], str)
        item = self.pod_tree.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a pod first.")
            return None, ""
        # In "(all namespaces)" view each row carries its own real namespace;
        # otherwise fall back to whatever's selected in ns_combo.
        ns = item.text(0) if self._current_ns == "(all namespaces)" else self._current_ns
        if not ns or ns == "(all namespaces)":
            ns = "default"
        return item.text(1), ns

    def _on_pod_double_click(self, item, column=0):
        """Double-clicking a pod row is a shortcut for Describe — reads
        name/namespace off the row that was actually double-clicked rather
        than relying on _selected_pod()'s currentItem(), since a
        double-click's second press is what sets the current item and
        there's no reason to depend on that timing."""
        ns = item.text(0) if self._current_ns == "(all namespaces)" else self._current_ns
        if not ns or ns == "(all namespaces)":
            ns = "default"
        self._describe("pod", item.text(1), ns)

    def _pod_logs(self):
        pod, ns = self._selected_pod()
        if pod:
            LogViewerDialog(self, self.ssh, ns, pod).exec_()

    def _pod_exec(self):
        pod, ns = self._selected_pod()
        if pod:
            ExecDialog(self, self.ssh, ns, pod).exec_()

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
        item = self.pod_tree.itemAt(pos)
        if not item:
            return
        pod = item.text(1)
        ns  = item.text(0) if self._current_ns == "(all namespaces)" else self._current_ns
        if not ns or ns == "(all namespaces)":
            ns = "default"
        menu = QMenu(self)
        menu.addAction("📋  View Logs",  lambda: LogViewerDialog(self, self.ssh, ns, pod).exec_())
        menu.addAction("💻  Exec Shell", lambda: ExecDialog(self, self.ssh, ns, pod).exec_())
        menu.addAction("📄  Describe",   lambda: self._describe("pod", pod, ns))
        menu.addSeparator()
        menu.addAction("🗑  Delete", self._pod_delete)
        menu.exec_(self.pod_tree.viewport().mapToGlobal(pos))

    # ── Deployments ───────────────────────────────────────────
    def _load_deployments(self):
        self._run_cmd(f"kubectl get deployments {self._ns_flag()} -o wide 2>&1",
                      self._populate_deployments)

    def _populate_deployments(self, out: str):
        self.deploy_tree.clear()
        all_ns = (self._current_ns == "(all namespaces)")
        self.deploy_tree.setColumnHidden(0, not all_ns)
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
            item = QTreeWidgetItem([ns, name, ready, upd, avail, age, imgs])
            item.setFont(1, monospace_font(11))
            try:
                cur, desired = ready.split("/")
                item.setForeground(2, QColor(T['SUCCESS'] if cur == desired else T['WARNING']))
            except Exception:
                pass
            self.deploy_tree.addTopLevelItem(item)
        # Same reasoning as _populate_pods: rebuild wipes the visual filter
        # state even though the filter box still has text — reapply it.
        self._filter_deployments(self.deploy_filter.text())

    def _on_deploy_click(self, item):
        try:
            _, desired = item.text(2).split("/")
            self.scale_spin.setValue(int(desired))
        except Exception:
            pass

    def _on_deploy_double_click(self, item, column=0):
        """Double-clicking a deployment row is a shortcut for Describe —
        reads name/namespace off the row that was actually double-clicked,
        same reasoning as _on_pod_double_click above."""
        all_ns = (self._current_ns == "(all namespaces)")
        ns = item.text(0) if all_ns else (self._current_ns or "default")
        self._describe("deployment", item.text(1), ns)

    def _filter_deployments(self, text: str):
        q = text.lower()
        for i in range(self.deploy_tree.topLevelItemCount()):
            item = self.deploy_tree.topLevelItem(i)
            item.setHidden(q not in item.text(1).lower())

    def _selected_deploy(self) -> tuple:  # (Optional[str], str)
        item = self.deploy_tree.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Select a deployment first.")
            return None, ""
        all_ns = (self._current_ns == "(all namespaces)")
        ns = item.text(0) if all_ns else (self._current_ns or "default")
        return item.text(1), ns

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

    # ── Services & Ingress ────────────────────────────────────
    def _load_services(self):
        self._run_cmd(f"kubectl get services {self._ns_flag()} 2>&1", self._populate_services)

    def _populate_services(self, out: str):
        self.svc_tree.clear()
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5:
                continue
            name, stype, cluster, ext = parts[:4]
            ports = " ".join(parts[4:])
            item  = QTreeWidgetItem([name, stype, cluster, ext, ports])
            if stype == "LoadBalancer":
                item.setForeground(1, QColor(T['ACCENT2']))
            elif stype == "NodePort":
                item.setForeground(1, QColor(T['INFO']))
            self.svc_tree.addTopLevelItem(item)

    def _load_ingress(self):
        self._run_cmd(f"kubectl get ingress {self._ns_flag()} 2>&1", self._populate_ingress)

    def _populate_ingress(self, out: str):
        self.ing_tree.clear()
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            name    = parts[0]
            cls     = parts[1] if len(parts) > 1 else "-"
            hosts   = parts[2] if len(parts) > 2 else "-"
            address = parts[3] if len(parts) > 3 else "-"
            self.ing_tree.addTopLevelItem(QTreeWidgetItem([name, cls, hosts, address]))

    # ── Config & Secrets ──────────────────────────────────────
    def _load_config_resources(self, _=None):
        rtype = "configmaps" if self.cfg_type_combo.currentText() == "ConfigMaps" else "secrets"
        cmd = (
            f"kubectl get {rtype} {self._ns_flag()} "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}}\n{{end}}' 2>&1"
        )
        self._run_cmd(cmd, self._populate_cfg_list)

    def _populate_cfg_list(self, out: str):
        self.cfg_list.clear()
        self.cfg_detail.clear()
        self.cfg_raw.clear()
        for name in out.strip().splitlines():
            name = name.strip()
            if name:
                self.cfg_list.addItem(QListWidgetItem(name))
        # Same reasoning as _populate_pods: reapply whatever's in the
        # filter box, since the rebuild above doesn't know about it.
        self._filter_configs(self.cfg_filter.text())

    def _filter_configs(self, text: str):
        q = text.lower()
        for i in range(self.cfg_list.count()):
            item = self.cfg_list.item(i)
            item.setHidden(q not in item.text().lower())

    def _on_cfg_select(self, item):
        name  = item.text()
        rtype = "configmap" if self.cfg_type_combo.currentText() == "ConfigMaps" else "secret"
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
            self.ssh, self._tunnel_services, REMOTE_TUNNEL_CSV_PATH, parent=self
        )
        dlg.services_saved.connect(lambda _: self._load_tunnel_csv())
        dlg.exec_()

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
            REMOTE_TUNNEL_CSV_PATH
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

        self._tunnel_restart_queue = list(services)
        self._tunnel_restart_failed = []

        self.tunnel_restart_btn.setEnabled(False)
        self.progress.show()

        self._run_next_tunnel_restart()

    def _run_next_tunnel_restart(self):
        if not self._tunnel_restart_queue:
            self._finish_kubectl_tunnel_restarts()
            return

        service = self._tunnel_restart_queue.pop(0)
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
        self._run_next_tunnel_restart()

    def _on_tunnel_step_error(self, service, err):
        self._tunnel_restart_failed.append(service['name'])
        append_terminal_html(
            self.tunnel_log,
            f"<span style='color:{T['DANGER']}'>✗ {self._esc(service['name'])}: {self._esc(str(err))}</span>"
        )
        self._run_next_tunnel_restart()

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