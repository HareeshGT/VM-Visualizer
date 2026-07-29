"""dashboard_tab.py — Live VM + Kubernetes node dashboard tab.

Shows the connected instance's specs (hostname/OS/kernel, CPU, RAM, disk)
and — when a cluster is reachable — a one-row-per-node table (CPU %,
memory %, the kubelet's own MemoryPressure/DiskPressure/PIDPressure
conditions). Double-clicking a node opens a separate window with the
pods actually running on it, including each pod's own CPU/memory usage,
restart count, and ready state (see NodeDetailWindow below).

Polling behaviour: the tab only ticks its refresh timer while it is BOTH
(a) connected to an instance and (b) the currently visible tab in
main_tabs. The owner (main_window.py) calls set_active(True/False) from
its main_tabs.currentChanged handler; navigating away calls
set_active(False), which stops the timer outright rather than merely
skipping a paint — so an unattended tab does zero SSH round-trips.

Node/pod layout: the nodes table itself only ever shows one row per node
(no inline pod children — that's what made the panel feel cramped).
Double-clicking a node opens a separate, independent top-level window
(NodeDetailWindow) listing the pods scheduled on that node along with
their CPU/memory usage, restart count, and ready state. Each open node
window is kept in self._node_windows and is refreshed in place on every
dashboard tick (piggy-backing on the same SSH round-trip the main tab
already makes — no extra polling), rather than issuing its own SSH calls.
"""

import re
import time

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QProgressBar, QTreeWidget, QTreeWidgetItem,
    QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor

from themes import T
from workers import CommandWorker, track_worker
from utils import monospace_font


REFRESH_MS = 6000  # live-dashboard cadence while the tab is visible

# ── Combined single-round-trip shell commands ──────────────────
# Both commands detect the remote OS (uname -s) and branch, since a
# connected target may be a Linux EC2 instance OR a local/remote macOS
# machine (the sudo_fs quick-access probes already anticipate macOS paths
# like /Applications and /Volumes) — the Linux-only tools this used to
# assume unconditionally (/etc/os-release, /proc/*, free, nproc) simply
# don't exist on macOS and silently produced "Unknown"/"?" placeholders.
_HOST_CMD = r"""
UNAME_S=$(uname -s 2>/dev/null)
echo __HOST__
hostname 2>&1
echo __OS__
if [ "$UNAME_S" = "Darwin" ]; then
  echo "$(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"
else
  (grep -m1 PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"') || uname -s
fi
echo __KERNEL__
uname -r 2>&1
echo __UPTIME__
(uptime -p 2>/dev/null || uptime) 2>&1
echo __LOAD__
if [ "$UNAME_S" = "Darwin" ]; then
  sysctl -n vm.loadavg 2>/dev/null | tr -d '{}'
else
  cat /proc/loadavg 2>/dev/null
fi
echo __CPU__
if [ "$UNAME_S" = "Darwin" ]; then
  sysctl -n hw.ncpu 2>/dev/null
  CHIP=$(sysctl -n machdep.cpu.brand_string 2>/dev/null)
  if [ -z "$CHIP" ]; then
    CHIP=$(system_profiler SPHardwareDataType 2>/dev/null | awk -F': ' '/Chip:/{print $2; exit} /Processor Name:/{print $2; exit}')
  fi
  echo "$CHIP"
else
  nproc 2>/dev/null
  grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2
fi
echo __MEM__
if [ "$UNAME_S" = "Darwin" ]; then
  PGSZ=$(sysctl -n hw.pagesize 2>/dev/null)
  TOTB=$(sysctl -n hw.memsize 2>/dev/null)
  VMS=$(vm_stat 2>/dev/null)
  ACT=$(echo "$VMS" | awk '/Pages active/{gsub(/\./,"",$3); print $3}')
  WIR=$(echo "$VMS" | awk '/Pages wired down/{gsub(/\./,"",$4); print $4}')
  CMP=$(echo "$VMS" | awk '/Pages occupied by compressor/{gsub(/\./,"",$5); print $5}')
  awk -v pg="$PGSZ" -v tot="$TOTB" -v act="${ACT:-0}" -v wir="${WIR:-0}" -v cmp="${CMP:-0}" \
    'BEGIN { totmb = tot/1024/1024; usedmb = (act+wir+cmp)*pg/1024/1024; if (usedmb>totmb) usedmb=totmb; printf "Mem: %d %d %d 0 0 %d\n", totmb, usedmb, totmb-usedmb, totmb-usedmb }'
else
  free -m 2>/dev/null | grep -i '^mem'
fi
echo __DISK__
if [ "$UNAME_S" = "Darwin" ]; then
  df -hP 2>/dev/null | awk 'NR>1 && $1 !~ /^(devfs|map)/'
else
  df -hP -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null | tail -n +2
fi
echo __CPUPCT__
if [ "$UNAME_S" = "Darwin" ]; then
  top -l 1 -n 0 2>/dev/null | grep "CPU usage"
else
  (vmstat 1 2 2>/dev/null | tail -1) || true
fi
"""

_K8S_CMD = r"""
if ! command -v kubectl >/dev/null 2>&1; then
  echo __NODES__
  echo "kubectl: not found"
  echo __COND__
  echo __TOP__
  echo __PODS__
  echo __PODTOP__
else
  echo __NODES__
  kubectl get nodes -o wide --no-headers 2>/dev/null
  echo __COND__
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}|{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="MemoryPressure")].status}|{.status.conditions[?(@.type=="DiskPressure")].status}|{.status.conditions[?(@.type=="PIDPressure")].status}{"\n"}{end}' 2>/dev/null
  echo __TOP__
  kubectl top nodes --no-headers 2>/dev/null
  echo __PODS__
  kubectl get pods --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.status.phase}|{.spec.nodeName}|{range .status.containerStatuses[*]}{.restartCount}{","}{end}|{range .status.containerStatuses[*]}{.ready}{","}{end}{"\n"}{end}' 2>/dev/null
  echo __PODTOP__
  kubectl top pods --all-namespaces --no-headers 2>/dev/null
fi
"""
# Full pod inventory (__PODS__ above) is fetched via jsonpath rather than
# `-o wide` text parsing — newer kubectl versions render RESTARTS as
# "N (Ndhm ago)" with an embedded space, which silently shifts every
# column after it when split() on whitespace. jsonpath fields are
# pipe-delimited instead, so column count is never at the mercy of
# kubectl's display format.
#
# Every kubectl call above redirects stderr to /dev/null rather than
# merging it into stdout (as this used to do with `2>&1`). When a cluster
# is unreachable, kubectl's client-go layer writes repeated glog-style
# warning lines to stderr (e.g. "E0729 12:40:28.372865   48153 ...:
# dial tcp ...: connection refused") that don't reliably contain the word
# "error" — merged into stdout, these looked exactly like extra node rows
# and got parsed as such. Suppressing stderr means an unreachable cluster
# now simply yields empty stdout, which the no-cluster check below already
# treats correctly.


def _split_sections(out: str) -> dict:
    """Split output that was built from `echo __MARKER__; ...` into a dict
    of marker -> list-of-lines."""
    sections, current = {}, None
    for line in (out or "").splitlines():
        s = line.strip()
        if s.startswith("__") and s.endswith("__") and len(s) > 4 and " " not in s:
            current = s.strip("_")
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _pct_color(pct: float) -> str:
    if pct >= 85:
        return T["DANGER"]
    if pct >= 65:
        return T["WARNING"]
    return T["SUCCESS"]


def _status_color(status: str) -> str:
    s = (status or "").lower()
    if "running" in s or s == "true":
        return T["SUCCESS"]
    if "pending" in s or "init" in s:
        return T["WARNING"]
    if any(x in s for x in ("error", "crash", "fail", "evict", "unknown")):
        return T["DANGER"]
    return T["TEXT_DIM"]


# STATUS column values `kubectl get nodes` actually prints (comma-joined
# when multiple apply, e.g. "Ready,SchedulingDisabled") — used to tell a
# genuine node row apart from an unrelated line of the same rough shape
# (see _looks_like_node_row).
_NODE_STATUS_WORDS = ("ready", "notready", "unknown", "schedulingdisabled")


def _looks_like_node_row(line: str) -> bool:
    """True only for lines structurally matching a `kubectl get nodes -o
    wide --no-headers` row (NAME STATUS ROLES AGE VERSION ...)."""
    parts = line.split()
    if len(parts) < 5:
        return False
    return any(w in parts[1].lower() for w in _NODE_STATUS_WORDS)


# Shape of the CPU/MEM columns `kubectl top pods` prints, e.g. "23m" and
# "128Mi" — used the same way as _looks_like_node_row, to validate a line
# actually is usage data rather than unrelated stderr output that happens
# to split into 4+ whitespace tokens.
_CPU_VAL_RE = re.compile(r"^\d+m?$")
_MEM_VAL_RE = re.compile(r"^\d+(Ki|Mi|Gi)?$")


class NodeDetailWindow(QWidget):
    """Standalone top-level window showing the pods scheduled on one node.

    Opened by double-clicking a node row in DashboardTab's nodes table.
    It does not poll on its own — DashboardTab pushes fresh pod data into
    it via update_pods() every time the main dashboard refreshes, as long
    as this window stays open (tracked in DashboardTab._node_windows).
    """

    closed = pyqtSignal(str)  # emits the node name when the window closes

    def __init__(self, node_name: str, parent=None):
        # Qt.Window makes this an independent top-level window rather than
        # an embedded child, even though a parent is passed (so it can be
        # tracked/cleaned up via the owner without being confined to the
        # main window's layout).
        super().__init__(parent, Qt.Window)
        self.node_name = node_name
        self.setWindowTitle(f"Node — {node_name}")
        self.resize(760, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self.title_lbl = QLabel(f"⎈  {node_name}")
        root.addWidget(self.title_lbl)

        self.summary_lbl = QLabel("Waiting for data…")
        root.addWidget(self.summary_lbl)

        self.pod_tree = QTreeWidget()
        self.pod_tree.setHeaderLabels(
            ["Pod", "Namespace", "Status", "CPU", "Memory", "Restarts", "Ready"]
        )
        self.pod_tree.setRootIsDecorated(False)
        self.pod_tree.setUniformRowHeights(True)
        self.pod_tree.setFont(monospace_font(12))
        root.addWidget(self.pod_tree)

        self._apply_styles()

    # ── Styling ────────────────────────────────────────────────
    def _apply_styles(self):
        self.setStyleSheet(f"background: {T['BG_DARK']};")
        self.title_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 15px; font-weight: 700;"
        )
        self.summary_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.pod_tree.setStyleSheet(
            f"QTreeWidget {{ font-size: 12px; }} QTreeWidget::item {{ height: 28px; }}"
        )

    def refresh_theme(self):
        self._apply_styles()

    # ── Data ───────────────────────────────────────────────────
    def update_pods(self, pods: list, pod_usage: dict):
        """Replace the pod list with a fresh snapshot. *pod_usage* is the
        full {(namespace, name): {"cpu":..., "mem":...}} map from the last
        `kubectl top pods` — the same one the main dashboard uses — so
        usage is looked up per pod rather than passed pre-filtered."""
        self.pod_tree.clear()
        for pod in sorted(pods, key=lambda p: (p["namespace"], p["name"])):
            item = QTreeWidgetItem([
                pod["name"], pod["namespace"], pod["phase"],
                "", "", str(pod["restarts"]), pod["ready"],
            ])
            item.setForeground(2, QColor(_status_color(pod["phase"])))

            ready_str = pod["ready"]
            if "/" in ready_str:
                got, want = ready_str.split("/", 1)
                if got.isdigit() and want.isdigit():
                    item.setForeground(
                        6, QColor(T["SUCCESS"] if got == want and int(want) > 0 else T["WARNING"])
                    )
            if pod["restarts"] > 0:
                item.setForeground(5, QColor(T["WARNING"] if pod["restarts"] < 5 else T["DANGER"]))

            usage = pod_usage.get((pod["namespace"], pod["name"]))
            for col, key in ((3, "cpu"), (4, "mem")):
                if usage is not None:
                    item.setText(col, usage[key])
                    item.setForeground(col, QColor(T["TEXT_PRIMARY"]))
                else:
                    item.setText(col, "n/a")
                    item.setForeground(col, QColor(T["TEXT_MUTED"]))

            self.pod_tree.addTopLevelItem(item)

        for col in range(7):
            self.pod_tree.resizeColumnToContents(col)

        self.summary_lbl.setText(
            f"{len(pods)} pod(s) scheduled here  ·  updated " + time.strftime("%H:%M:%S")
        )

    # ── Teardown ───────────────────────────────────────────────
    def closeEvent(self, event):
        self.closed.emit(self.node_name)
        super().closeEvent(event)


class DashboardTab(QWidget):
    status_msg = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ssh      = None
        self._active  = False
        self._busy    = False
        self._workers = []

        # Latest per-node pod snapshot, keyed by the exact string shown in
        # the node table's first column (so a click on a row can look its
        # pods up with no extra bookkeeping). Rebuilt on every refresh.
        self._pods_by_node_cache = {}
        self._pod_usage_cache    = {}

        # Open NodeDetailWindow instances, keyed the same way, so an
        # already-open window is refreshed in place instead of duplicated.
        self._node_windows = {}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)

        self._build_ui()
        self._show_disconnected()

    # ── Public API ──────────────────────────────────────────
    def set_ssh(self, ssh):
        self.ssh = ssh
        if ssh:
            self._show_connected_placeholder()
            if self._active:
                self._refresh()
                self._timer.start(REFRESH_MS)
        else:
            self._timer.stop()
            self._busy = False
            self._show_disconnected()
            for win in list(self._node_windows.values()):
                win.close()  # triggers closeEvent -> _on_node_window_closed -> dict pop

    def set_active(self, active: bool):
        """Called by main_window whenever this tab becomes the visible
        (active=True) or is navigated away from (active=False)."""
        self._active = active
        if active and self.ssh:
            self._refresh()          # snap up-to-date immediately on return
            self._timer.start(REFRESH_MS)
        else:
            self._timer.stop()

    def apply_theme(self):
        self._apply_styles()
        self._style_tree(self.k8s_tree)
        for win in self._node_windows.values():
            win.refresh_theme()

    # ── UI construction ──────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.ctrl_bar = QWidget()
        self.ctrl_bar.setFixedHeight(48)
        cb = QHBoxLayout(self.ctrl_bar)
        cb.setContentsMargins(12, 0, 12, 0)
        cb.setSpacing(10)

        title = QLabel("🖥  Dashboard")
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700;")
        cb.addWidget(title)
        cb.addStretch()

        self.updated_lbl = QLabel("")
        self.updated_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        cb.addWidget(self.updated_lbl)

        self.live_lbl = QLabel("⚫  Not connected")
        cb.addWidget(self.live_lbl)

        self.refresh_btn = QPushButton("↺  Refresh")
        self.refresh_btn.setFixedHeight(30)
        self.refresh_btn.clicked.connect(self._refresh)
        cb.addWidget(self.refresh_btn)

        root.addWidget(self.ctrl_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(16, 16, 16, 16)
        self._content_layout.setSpacing(16)

        self.disconnected_lbl = QLabel("Connect to an instance to see its dashboard.")
        self.disconnected_lbl.setAlignment(Qt.AlignCenter)
        self.disconnected_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; padding: 40px; font-size: 13px;")
        self._content_layout.addWidget(self.disconnected_lbl)

        # ── Instance card ──────────────────────────────────
        self.vm_card = self._make_card("🖳  Instance")
        vm_body = QVBoxLayout()
        vm_body.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        self._vm_fields = {}
        for row, (key, label) in enumerate([
            ("hostname", "Hostname"), ("os", "OS"),
            ("kernel", "Kernel"), ("uptime", "Uptime"),
            ("load", "Load average"), ("cpu_model", "CPU"),
        ]):
            lk = QLabel(label + ":")
            lk.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
            lv = QLabel("—")
            lv.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 12px;")
            lv.setWordWrap(True)
            grid.addWidget(lk, row // 2, (row % 2) * 2)
            grid.addWidget(lv, row // 2, (row % 2) * 2 + 1)
            self._vm_fields[key] = lv
        vm_body.addLayout(grid)

        self.cpu_bar = self._labeled_bar("CPU usage")
        vm_body.addLayout(self.cpu_bar["layout"])
        self.mem_bar = self._labeled_bar("Memory usage")
        vm_body.addLayout(self.mem_bar["layout"])

        disk_lbl = QLabel("Disk")
        disk_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px; font-weight: 700;")
        vm_body.addWidget(disk_lbl)

        self.disk_tree = QTreeWidget()
        self.disk_tree.setHeaderLabels(["Filesystem", "Size", "Used", "Avail", "Use", "Mounted on"])
        self.disk_tree.setRootIsDecorated(False)
        self.disk_tree.setUniformRowHeights(True)
        self.disk_tree.setMinimumHeight(90)
        self.disk_tree.setMaximumHeight(160)
        vm_body.addWidget(self.disk_tree)

        self.vm_card["body"].addLayout(vm_body)
        self._content_layout.addWidget(self.vm_card["frame"])

        # ── Kubernetes nodes card ────────────────────────────
        # Nodes only — pods used to be shown as inline expandable children
        # of each node, which made this card feel cramped. They now live in
        # a separate NodeDetailWindow opened per-node (see _on_node_double_
        # clicked), so this table stays one clean row per node.
        self.k8s_card = self._make_card("⎈  Kubernetes Nodes")
        self.k8s_note = QLabel("")
        self.k8s_note.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.k8s_note.hide()
        self.k8s_card["body"].addWidget(self.k8s_note)

        self.k8s_hint = QLabel("Double-click a node to see the pods running on it.")
        self.k8s_hint.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.k8s_card["body"].addWidget(self.k8s_hint)

        self.k8s_tree = QTreeWidget()
        self.k8s_tree.setHeaderLabels(
            ["Node", "Status", "Roles", "CPU", "Memory", "Pods", "Pressure"]
        )
        self.k8s_tree.setRootIsDecorated(False)  # no expand arrows — nodes only, no children
        self.k8s_tree.setUniformRowHeights(True)
        self.k8s_tree.setMinimumHeight(160)
        self.k8s_tree.setCursor(Qt.PointingHandCursor)
        self.k8s_tree.itemDoubleClicked.connect(self._on_node_double_clicked)
        self._style_tree(self.k8s_tree)
        self.k8s_card["body"].addWidget(self.k8s_tree)
        self._content_layout.addWidget(self.k8s_card["frame"])

        self._content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)

        self.vm_card["frame"].hide()
        self.k8s_card["frame"].hide()

        self._apply_styles()

    def _make_card(self, title: str) -> dict:
        frame = QFrame()
        frame.setObjectName("dash_card")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(16, 12, 16, 16)
        outer.setSpacing(10)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 13px; font-weight: 700;")
        outer.addWidget(lbl)
        body = QVBoxLayout()
        outer.addLayout(body)
        return {"frame": frame, "title_lbl": lbl, "body": outer}

    def _labeled_bar(self, label: str) -> dict:
        lay = QHBoxLayout()
        lay.setSpacing(10)
        lk = QLabel(label)
        lk.setFixedWidth(100)
        lk.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedHeight(16)
        bar.setTextVisible(True)
        val_lbl = QLabel("—")
        val_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        val_lbl.setFixedWidth(150)
        lay.addWidget(lk)
        lay.addWidget(bar, 1)
        lay.addWidget(val_lbl)
        return {"layout": lay, "bar": bar, "val_lbl": val_lbl}

    def _style_tree(self, tree):
        tree.setFont(monospace_font(12))
        tree.setStyleSheet(f"QTreeWidget {{ font-size: 12px; }} "
                            f"QTreeWidget::item {{ height: 30px; }}")

    def _style_bar(self, bar: QProgressBar, pct: float):
        color = _pct_color(pct)
        bar.setValue(max(0, min(100, int(round(pct)))))
        bar.setFormat(f"{pct:.0f}%")
        bar.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {T['BORDER']}; border-radius: 4px; "
            f"background: {T['BG_ITEM']}; text-align: center; color: {T['TEXT_PRIMARY']}; }}"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )

    def _apply_styles(self):
        self.ctrl_bar.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        for frame in (self.vm_card["frame"], self.k8s_card["frame"]):
            frame.setStyleSheet(
                f"QFrame#dash_card {{ background: {T['BG_PANEL']}; "
                f"border: 1px solid {T['BORDER']}; border-radius: 10px; }}"
            )
        self.disk_tree.setStyleSheet(f"QTreeWidget {{ font-size: 12px; }}")
        self._update_live_label()

    def _update_live_label(self):
        if not self.ssh:
            self.live_lbl.setText("⚫  Not connected")
            self.live_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        elif self._active:
            self.live_lbl.setText("🟢  Live")
            self.live_lbl.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 12px;")
        else:
            self.live_lbl.setText("⏸  Paused (not on this tab)")
            self.live_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")

    # ── State transitions ─────────────────────────────────────
    def _show_disconnected(self):
        self.disconnected_lbl.show()
        self.vm_card["frame"].hide()
        self.k8s_card["frame"].hide()
        self.updated_lbl.setText("")
        self._update_live_label()

    def _show_connected_placeholder(self):
        self.disconnected_lbl.hide()
        self.vm_card["frame"].show()
        self.k8s_card["frame"].show()
        self._update_live_label()

    # ── Refresh ────────────────────────────────────────────────
    def _refresh(self):
        if not self.ssh or self._busy:
            return
        self._busy = True
        self._update_live_label()

        host_worker = CommandWorker(self.ssh, _HOST_CMD)
        host_worker.done.connect(self._on_host_stats)
        host_worker.error.connect(self._on_host_error)
        track_worker(self._workers, host_worker)
        host_worker.start()

        k8s_worker = CommandWorker(self.ssh, _K8S_CMD)
        k8s_worker.done.connect(self._on_k8s_stats)
        k8s_worker.error.connect(self._on_k8s_error)
        track_worker(self._workers, k8s_worker)
        k8s_worker.start()

    def _mark_updated(self):
        self.updated_lbl.setText("Updated " + time.strftime("%H:%M:%S"))
        self._busy = False

    # ── Host stats ────────────────────────────────────────────
    def _on_host_stats(self, out: str):
        sec = _split_sections(out)

        def first(key, default=""):
            lines = sec.get(key, [])
            return lines[0].strip() if lines else default

        self._vm_fields["hostname"].setText(first("HOST", "—"))
        self._vm_fields["os"].setText(first("OS", "Unknown"))
        self._vm_fields["kernel"].setText(first("KERNEL", "—"))
        self._vm_fields["uptime"].setText(first("UPTIME", "—"))
        self._vm_fields["load"].setText(first("LOAD", "—"))

        cpu_lines = [l for l in sec.get("CPU", []) if l.strip()]
        cores = cpu_lines[0].strip() if cpu_lines else "?"
        model = cpu_lines[1].strip() if len(cpu_lines) > 1 else "Unknown CPU"
        self._vm_fields["cpu_model"].setText(f"{model}  ({cores} cores)")

        # CPU % busy. Linux: from `vmstat 1 2`'s last line — its final two
        # columns (wa, st) plus 'id' (idle) are what's left after us+sy;
        # 100-idle is simplest and matches what most monitoring tools call
        # "CPU used". macOS: `top -l 1 -n 0` prints a single summary line
        # like "CPU usage: 5.26% user, 10.52% sys, 84.21% idle" instead.
        cpupct_lines = [l for l in sec.get("CPUPCT", []) if l.strip()]
        cpu_pct = None
        if cpupct_lines:
            last = cpupct_lines[-1]
            if "CPU usage" in last:
                m = re.search(r"([\d.]+)%\s*idle", last)
                if m:
                    try:
                        cpu_pct = max(0.0, 100.0 - float(m.group(1)))
                    except ValueError:
                        pass
            else:
                parts = last.split()
                if len(parts) >= 15:
                    try:
                        cpu_pct = max(0.0, 100.0 - float(parts[14]))
                    except ValueError:
                        pass
        if cpu_pct is not None:
            self._style_bar(self.cpu_bar["bar"], cpu_pct)
            self.cpu_bar["val_lbl"].setText(f"{cpu_pct:.0f}% busy")
        else:
            # Reset the bar too, not just the label — otherwise a bar that
            # was populated by an earlier successful refresh keeps showing
            # that stale value forever once this stat becomes unavailable.
            self.cpu_bar["bar"].setValue(0)
            self.cpu_bar["val_lbl"].setText("unavailable")

        # Memory — a "Mem: total used free shared buff/cache available"
        # line, same shape whether it came from Linux `free -m` or the
        # macOS branch of _HOST_CMD (which computes the equivalent from
        # `sysctl hw.memsize`/`vm_stat` and prints it in the same layout).
        mem_line = first("MEM", "")
        if mem_line:
            parts = mem_line.split()
            try:
                total_mb, used_mb = float(parts[1]), float(parts[2])
                mem_pct = (used_mb / total_mb * 100.0) if total_mb else 0.0
                self._style_bar(self.mem_bar["bar"], mem_pct)
                self.mem_bar["val_lbl"].setText(
                    f"{used_mb/1024:.1f} / {total_mb/1024:.1f} GB"
                )
            except (ValueError, IndexError):
                self.mem_bar["bar"].setValue(0)
                self.mem_bar["val_lbl"].setText("unavailable")
        else:
            self.mem_bar["bar"].setValue(0)
            self.mem_bar["val_lbl"].setText("unavailable")

        # Disk mounts
        self.disk_tree.clear()
        for line in sec.get("DISK", []):
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            fs, size, used, avail, use_pct, mounted = parts
            item = QTreeWidgetItem([fs, size, used, avail, use_pct, mounted])
            try:
                pct = float(use_pct.strip("%"))
                item.setForeground(4, QColor(_pct_color(pct)))
            except ValueError:
                pass
            self.disk_tree.addTopLevelItem(item)

        self._mark_updated()
        self.status_msg.emit("Dashboard updated")

    def _on_host_error(self, err: str):
        self._busy = False
        self.status_msg.emit(f"Dashboard: host stats error — {err}")

    # ── Kubernetes stats ──────────────────────────────────────
    def _on_k8s_stats(self, out: str):
        sec = _split_sections(out)

        raw_node_lines = [l for l in sec.get("NODES", []) if l.strip()]
        # _K8S_CMD redirects kubectl's stderr to /dev/null rather than
        # merging it into stdout, so a normally-behaving cluster never puts
        # log noise here. This shape check is kept anyway as a second line
        # of defense: some kubectl builds print retry/deprecation warnings
        # to stdout, e.g.:
        #   E0729 12:40:28.372865   48153 memcache.go:87] couldn't get...
        # which has 5+ whitespace-separated tokens — the same shape as a
        # real "NAME STATUS ROLES AGE VERSION" row — so a check that only
        # looked at line count would treat it as a node. Validate the
        # STATUS column instead: it's always one of a known small set of
        # words for a real row, never true for a log line.
        node_lines = [l for l in raw_node_lines if _looks_like_node_row(l)]
        no_cluster = not node_lines
        if no_cluster:
            self.k8s_tree.clear()
            self._pods_by_node_cache = {}
            for win in self._node_windows.values():
                win.update_pods([], {})  # clear stale data rather than leave it showing
            joined_raw = "\n".join(raw_node_lines).lower()
            if "not found" in joined_raw or "command not found" in joined_raw:
                msg = "No Kubernetes cluster detected on this instance (kubectl not installed)."
            else:
                msg = "No Kubernetes cluster detected on this instance (kubectl unavailable or no nodes)."
            self.k8s_note.setText(msg)
            self.k8s_note.show()
            return
        self.k8s_note.hide()

        # Pressure conditions, keyed by node name
        cond = {}
        for line in sec.get("COND", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            bits += [""] * (5 - len(bits))
            name, ready, mem_p, disk_p, pid_p = bits[:5]
            cond[name.strip()] = {
                "ready": ready.strip(), "mem": mem_p.strip(),
                "disk": disk_p.strip(), "pid": pid_p.strip(),
            }

        # CPU%/MEM% from `kubectl top nodes`, keyed by node name. Absent
        # entirely (older cluster, no metrics-server) just means we show
        # "n/a" instead of a bar — never an error state.
        top = {}
        for line in sec.get("TOP", []):
            parts = line.split()
            if len(parts) >= 5 and parts[2].endswith("%") and parts[4].endswith("%"):
                top[parts[0]] = {"cpu_pct": parts[2].rstrip("%"), "mem_pct": parts[4].rstrip("%")}

        # Full pod inventory, grouped by the node each pod is scheduled on.
        # Pods not yet scheduled (nodeName empty — usually Pending) are
        # collected under a synthetic "(unscheduled)" key instead of being
        # dropped, so they're still visible somewhere.
        pods_by_node = {}
        for line in sec.get("PODS", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            bits += [""] * (6 - len(bits))
            ns, pname, phase, node, restarts_csv, ready_csv = bits[:6]
            ns, pname, phase, node = ns.strip(), pname.strip(), phase.strip(), node.strip()
            if not pname:
                continue
            restarts = sum(int(x) for x in restarts_csv.split(",") if x.strip().isdigit())
            ready_flags = [x for x in ready_csv.split(",") if x.strip()]
            ready_count = sum(1 for x in ready_flags if x.strip() == "true")
            pods_by_node.setdefault(node or "(unscheduled)", []).append({
                "namespace": ns, "name": pname, "phase": phase or "Unknown",
                "restarts": restarts,
                "ready": f"{ready_count}/{len(ready_flags)}" if ready_flags else "-",
            })

        # Per-pod CPU/memory usage from `kubectl top pods`, keyed by
        # (namespace, name). Missing entirely (no metrics-server) just
        # means every pod row shows "n/a" instead of a bar — same
        # graceful-degradation rule as the node-level CPU/Memory columns.
        # Same defense-in-depth as NODES above (stderr is suppressed, but
        # validate anyway): CPU/MEM columns are checked by shape (e.g.
        # "23m", "128Mi") rather than just trusting any line with 4+ tokens.
        pod_usage = {}
        for line in sec.get("PODTOP", []):
            parts = line.split()
            if len(parts) >= 4 and _CPU_VAL_RE.match(parts[2]) and _MEM_VAL_RE.match(parts[3]):
                pod_usage[(parts[0], parts[1])] = {"cpu": parts[2], "mem": parts[3]}

        self.k8s_tree.clear()
        self._pods_by_node_cache = {}
        self._pod_usage_cache    = pod_usage

        for line in node_lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            name, status, roles = parts[0], parts[1], parts[2]
            node_pods = pods_by_node.pop(name, [])
            self._pods_by_node_cache[name] = node_pods

            item = QTreeWidgetItem([name, status, roles, "", "", str(len(node_pods)), ""])
            item.setForeground(1, QColor(T["SUCCESS"] if status.lower() == "ready" else T["DANGER"]))

            c = cond.get(name, {})
            _pressure_names = {"mem": "MemoryPressure", "disk": "DiskPressure", "pid": "PIDPressure"}
            pressures = [label for key, label in _pressure_names.items() if c.get(key) == "True"]
            if pressures:
                item.setText(6, ", ".join(pressures))
                item.setForeground(6, QColor(T["DANGER"]))
            else:
                item.setText(6, "OK")
                item.setForeground(6, QColor(T["SUCCESS"]))

            self.k8s_tree.addTopLevelItem(item)

            t = top.get(name)
            for col, key in ((3, "cpu_pct"), (4, "mem_pct")):
                if t is not None:
                    try:
                        pct = float(t[key])
                        bar = QProgressBar()
                        bar.setRange(0, 100)
                        bar.setFixedHeight(18)
                        self._style_bar(bar, pct)
                        self.k8s_tree.setItemWidget(item, col, bar)
                        continue
                    except ValueError:
                        pass
                item.setText(col, "n/a")
                item.setForeground(col, QColor(T["TEXT_MUTED"]))

        # Anything left in pods_by_node belongs to a node that either
        # wasn't in the NODES list or is the synthetic "(unscheduled)"
        # bucket — surface it as its own row (double-clickable, same as
        # any other node) rather than silently dropping those pods.
        for node_name, node_pods in pods_by_node.items():
            label = "🕓  Unscheduled / other" if node_name == "(unscheduled)" else node_name
            self._pods_by_node_cache[label] = node_pods
            item = QTreeWidgetItem([label, "", "", "", "", str(len(node_pods)), ""])
            item.setForeground(0, QColor(T["TEXT_MUTED"]))
            for col in (1, 2, 3, 4, 6):
                item.setText(col, "n/a" if col in (3, 4) else "")
            self.k8s_tree.addTopLevelItem(item)

        for col in range(7):
            self.k8s_tree.resizeColumnToContents(col)

        # Push fresh data into any node detail windows that are still open,
        # instead of leaving them showing a stale snapshot until re-clicked.
        for node_name, win in self._node_windows.items():
            win.update_pods(self._pods_by_node_cache.get(node_name, []), self._pod_usage_cache)

        self._mark_updated()
        self.status_msg.emit("Dashboard updated")

    # ── Node detail window ──────────────────────────────────────
    def _on_node_double_clicked(self, item: QTreeWidgetItem, _column: int):
        node_name = item.text(0)
        pods = self._pods_by_node_cache.get(node_name, [])

        win = self._node_windows.get(node_name)
        if win is None:
            win = NodeDetailWindow(node_name, parent=self)
            win.closed.connect(self._on_node_window_closed)
            self._node_windows[node_name] = win

        win.update_pods(pods, self._pod_usage_cache)
        win.show()
        win.raise_()
        win.activateWindow()

    def _on_node_window_closed(self, node_name: str):
        self._node_windows.pop(node_name, None)

    def _on_k8s_error(self, err: str):
        self._busy = False
        self.k8s_tree.clear()
        self.k8s_note.setText(f"Kubernetes data unavailable: {err}")
        self.k8s_note.show()