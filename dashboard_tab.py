"""dashboard_tab.py — Live VM + Kubernetes node dashboard tab.

Shows the connected instance's specs (hostname/OS/kernel, CPU, RAM, disk)
and — when a cluster is reachable — a cluster overview plus one-card-per-node
summary with live CPU/memory usage, capacity/allocatable resources,
Kubernetes/runtime metadata, and MemoryPressure/DiskPressure/PIDPressure
conditions. Double-clicking a node opens a separate window with the
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
    QFrame, QScrollArea, QTreeWidget, QTreeWidgetItem, QGraphicsDropShadowEffect, QSizePolicy,
)
from PyQt5.QtCore import (
    Qt, QTimer, pyqtSignal, QVariantAnimation, QEasingCurve,
)
from PyQt5.QtGui import QColor

from themes import T
from workers import CommandWorker, track_worker
from utils import monospace_font, size_fmt
from progress_ring import CircularProgress


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
  echo __NODEINFO__
  echo __TOP__
  echo __PODS__
  echo __PODTOP__
  echo __WORKLOADS__
  echo __SERVICES__
  echo __EVENTS__
else
  echo __NODES__
  kubectl get nodes -o wide --no-headers 2>/dev/null
  echo __COND__
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}|{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="MemoryPressure")].status}|{.status.conditions[?(@.type=="DiskPressure")].status}|{.status.conditions[?(@.type=="PIDPressure")].status}{"\n"}{end}' 2>/dev/null
  echo __NODEINFO__
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}|{.status.nodeInfo.kubeletVersion}|{.status.nodeInfo.osImage}|{.status.nodeInfo.kernelVersion}|{.status.nodeInfo.containerRuntimeVersion}|{.status.addresses[?(@.type=="InternalIP")].address}|{.status.capacity.cpu}|{.status.capacity.memory}|{.status.capacity.pods}|{.status.allocatable.cpu}|{.status.allocatable.memory}|{.status.allocatable.pods}{"\n"}{end}' 2>/dev/null
  echo __TOP__
  kubectl top nodes --no-headers 2>/dev/null
  echo __PODS__
  kubectl get pods --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.status.phase}|{.spec.nodeName}|{range .status.containerStatuses[*]}{.restartCount}{","}{end}|{range .status.containerStatuses[*]}{.ready}{","}{end}|{.status.reason}|{.status.message}|{.status.podIP}|{.status.hostIP}|{.status.qosClass}|{.metadata.creationTimestamp}|{range .metadata.ownerReferences[0]}{.kind}{"/"}{.name}{end}|{range .status.containerStatuses[*]}{.name}={.state.waiting.reason},{.state.waiting.message}{";"}{end}{"\n"}{end}' 2>/dev/null
  echo __PODTOP__
  kubectl top pods --all-namespaces --no-headers 2>/dev/null
  echo __WORKLOADS__
  kubectl get deployments --all-namespaces -o jsonpath='{range .items[*]}Deployment|{.metadata.namespace}|{.metadata.name}|{.metadata.creationTimestamp}|{.status.replicas}|{.status.readyReplicas}|{.status.availableReplicas}|{.status.updatedReplicas}|{.status.unavailableReplicas}|{.spec.replicas}{"\n"}{end}' 2>/dev/null
  kubectl get statefulsets --all-namespaces -o jsonpath='{range .items[*]}StatefulSet|{.metadata.namespace}|{.metadata.name}|{.metadata.creationTimestamp}|{.status.replicas}|{.status.readyReplicas}|{.status.currentReplicas}|{.status.updatedReplicas}|{.status.readyReplicas}|{.spec.replicas}{"\n"}{end}' 2>/dev/null
  kubectl get daemonsets --all-namespaces -o jsonpath='{range .items[*]}DaemonSet|{.metadata.namespace}|{.metadata.name}|{.metadata.creationTimestamp}|{.status.desiredNumberScheduled}|{.status.numberReady}|{.status.numberAvailable}|{.status.updatedNumberScheduled}|{.status.numberUnavailable}|{.status.desiredNumberScheduled}{"\n"}{end}' 2>/dev/null
  echo __SERVICES__
  kubectl get services --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.spec.type}|{.spec.clusterIP}|{.status.loadBalancer.ingress[0].ip}|{.status.loadBalancer.ingress[0].hostname}|{range .spec.ports[*]}{.name}:{.port}/{.protocol}:{.nodePort}{","}{end}|{.metadata.creationTimestamp}{"\n"}{end}' 2>/dev/null
  echo __ENDPOINTS__
  kubectl get endpoints --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{range .subsets[*].addresses[*]}1{";"}{end}|{range .subsets[*].notReadyAddresses[*]}1{";"}{end}{"\n"}{end}' 2>/dev/null

  echo __EVENTS__
  kubectl get events --all-namespaces --sort-by=.lastTimestamp -o jsonpath='{range .items[*]}{.lastTimestamp}|{.type}|{.reason}|{.involvedObject.kind}|{.involvedObject.namespace}|{.involvedObject.name}|{.message}{"\n"}{end}' 2>/dev/null
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


# `df -hP` prints sizes as human-readable strings ("20G", "512M", "1.0T",
# "830K", or a bare byte count with no suffix) — this converts one back to
# a raw byte count so per-mount sizes can be summed into an overall
# storage total/used figure for the dashboard's storage ring. Returns
# None for anything that doesn't match (e.g. "-", seen for some pseudo
# filesystems), so callers can skip those rows rather than mis-count them.
_DF_SIZE_RE = re.compile(r"^([\d.]+)([KMGTP]?)i?$")
_DF_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3,
                  "T": 1024**4, "P": 1024**5}

def _short_age(timestamp: str) -> str:
    """Convert a Kubernetes ISO-8601 timestamp to a compact age."""
    if not timestamp:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if seconds < 60: return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60: return f"{minutes}m"
        hours = minutes // 60
        if hours < 24: return f"{hours}h"
        days = hours // 24
        if days < 30: return f"{days}d"
        months = days // 30
        if months < 12: return f"{months}mo"
        return f"{days // 365}y"
    except (ValueError, TypeError, OverflowError):
        return timestamp[:19].replace("T", " ")


def _parse_df_size(s: str):
    m = _DF_SIZE_RE.match((s or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * _DF_SIZE_MULT[m.group(2)]
    except (ValueError, KeyError):
        return None


def _pct_color(pct: float) -> str:
    if pct >= 85:
        return T["DANGER"]
    if pct >= 65:
        return T["WARNING"]
    return T["SUCCESS"]


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """Blend two hex colours at fraction t (0 = c1, 1 = c2) — drives the
    hover border-color animation frame by frame since QSS itself can't
    transition a colour."""
    a, b = QColor(c1), QColor(c2)
    r = round(a.red()   + (b.red()   - a.red())   * t)
    g = round(a.green() + (b.green() - a.green()) * t)
    bl = round(a.blue()  + (b.blue()  - a.blue())  * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


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


def _k8s_mem_to_bytes(value: str):
    """Convert a Kubernetes memory quantity to bytes for compact display."""
    m = re.match(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$", (value or "").strip())
    if not m:
        return None
    multipliers = {
        None: 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    }
    try:
        return float(m.group(1)) * multipliers[m.group(2)]
    except (ValueError, KeyError):
        return None


def _k8s_mem_fmt(value: str) -> str:
    """Format a Kubernetes memory quantity as a compact binary unit."""
    raw = _k8s_mem_to_bytes(value)
    if raw is None:
        return value or "n/a"
    for unit, factor in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if raw >= factor:
            amount = raw / factor
            return f"{amount:.1f} {unit}" if amount < 100 else f"{amount:.0f} {unit}"
    return f"{raw:.0f} B"


def _k8s_cpu_fmt(value: str) -> str:
    """Format a Kubernetes CPU quantity in cores."""
    value = (value or "").strip()
    if not value:
        return "n/a"
    try:
        if value.endswith("m"):
            cores = float(value[:-1]) / 1000.0
        else:
            cores = float(value)
    except ValueError:
        return value
    return f"{cores:.2f} cores" if cores < 10 else f"{cores:.0f} cores"


class NodeDetailWindow(QWidget):
    """Standalone operational view of the pods scheduled on one node."""

    closed = pyqtSignal(str)

    def __init__(self, node_name: str, parent=None):
        super().__init__(parent, Qt.Window)
        self.node_name = node_name
        self.setWindowTitle(f"Node — {node_name}")
        self.resize(1120, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self.title_lbl = QLabel(f"⎈  {node_name}")
        root.addWidget(self.title_lbl)
        self.summary_lbl = QLabel("Waiting for data…")
        root.addWidget(self.summary_lbl)

        self.pod_tree = QTreeWidget()
        self.pod_tree.setHeaderLabels([
            "Pod", "Namespace", "Status", "CPU", "Memory",
            "Restarts", "Ready", "IP", "Age", "Owner"
        ])
        self.pod_tree.setRootIsDecorated(False)
        self.pod_tree.setUniformRowHeights(True)
        self.pod_tree.setFont(monospace_font(12))
        self.pod_tree.itemDoubleClicked.connect(self._show_pod_details)
        root.addWidget(self.pod_tree)

        self._pod_snapshot = {}
        self._apply_styles()

    def _apply_styles(self):
        self.setStyleSheet(f"background: {T['BG_DARK']};")
        self.title_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 15px; font-weight: 700;"
        )
        self.summary_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.pod_tree.setStyleSheet(
            f"QTreeWidget {{ font-size: 12px; }} QTreeWidget::item {{ height: 30px; }}"
        )

    def refresh_theme(self):
        self._apply_styles()

    def update_pods(self, pods: list, pod_usage: dict):
        self._pod_snapshot = {(p["namespace"], p["name"]): p for p in pods}
        self.pod_tree.clear()

        for pod in sorted(pods, key=lambda p: (p["namespace"], p["name"])):
            usage = pod_usage.get((pod["namespace"], pod["name"]))
            item = QTreeWidgetItem([
                pod["name"], pod["namespace"], pod["phase"],
                usage["cpu"] if usage else "n/a",
                usage["mem"] if usage else "n/a",
                str(pod["restarts"]), pod["ready"],
                pod.get("pod_ip") or "—",
                _short_age(pod.get("created", "")),
                pod.get("owner") or "—",
            ])
            item.setData(0, Qt.UserRole, (pod["namespace"], pod["name"]))
            item.setForeground(2, QColor(_status_color(pod["phase"])))
            if pod["restarts"] > 0:
                item.setForeground(5, QColor(T["WARNING"] if pod["restarts"] < 5 else T["DANGER"]))
            ready_str = pod["ready"]
            if "/" in ready_str:
                got, want = ready_str.split("/", 1)
                if got.isdigit() and want.isdigit():
                    item.setForeground(6, QColor(
                        T["SUCCESS"] if got == want and int(want) > 0 else T["WARNING"]
                    ))
            for col in (3, 4):
                item.setForeground(col, QColor(T["TEXT_PRIMARY"] if usage else T["TEXT_MUTED"]))
            tooltip = self._pod_tooltip(pod)
            for col in range(10):
                item.setToolTip(col, tooltip)
            self.pod_tree.addTopLevelItem(item)

        for col in range(10):
            self.pod_tree.resizeColumnToContents(col)

        self.summary_lbl.setText(
            f"{len(pods)} pod(s) scheduled here  · double-click a pod for details  · "
            f"updated {time.strftime('%H:%M:%S')}"
        )

    def _pod_tooltip(self, pod):
        lines = [
            f"Pod: {pod['name']}", f"Namespace: {pod['namespace']}",
            f"Phase: {pod['phase']}", f"Ready: {pod['ready']}",
            f"Restarts: {pod['restarts']}",
        ]
        for key, label in (("pod_ip", "Pod IP"), ("host_ip", "Host IP"),
                           ("qos", "QoS"), ("owner", "Owner"),
                           ("reason", "Reason"), ("message", "Message"),
                           ("waiting", "Container state")):
            if pod.get(key):
                lines.append(f"{label}: {pod[key]}")
        return "\n".join(lines)

    def _show_pod_details(self, item, _column):
        key = item.data(0, Qt.UserRole)
        if not key:
            return
        pod = self._pod_snapshot.get(tuple(key))
        if not pod:
            return
        from PyQt5.QtWidgets import QMessageBox
        lines = [
            f"Pod: {pod['name']}", f"Namespace: {pod['namespace']}",
            f"Phase: {pod['phase']}", f"Ready: {pod['ready']}",
            f"Restarts: {pod['restarts']}", f"Node: {self.node_name}",
            f"Pod IP: {pod.get('pod_ip') or '—'}",
            f"Host IP: {pod.get('host_ip') or '—'}",
            f"QoS: {pod.get('qos') or '—'}",
            f"Age: {_short_age(pod.get('created', ''))}",
            f"Owner: {pod.get('owner') or '—'}",
        ]
        if pod.get("reason"): lines.append(f"Reason: {pod['reason']}")
        if pod.get("message"): lines.append(f"Message: {pod['message']}")
        if pod.get("waiting"): lines.append(f"Container state: {pod['waiting']}")
        QMessageBox.information(self, f"Pod — {pod['name']}", "\n".join(lines))

    def closeEvent(self, event):
        self.closed.emit(self.node_name)
        super().closeEvent(event)


class NodeCard(QFrame):
    """A single node's summary shown as a square card tile in the
    Kubernetes nodes grid (3 cards per row — see DashboardTab._add_node_
    card). Replaces the old one-row-per-node table; double-clicking a card
    opens the same NodeDetailWindow with the pods scheduled on it.

    The card's normal size is driven by set_side(), which the owning
    DashboardTab calls with a side length computed from the available grid
    width so the row of cards always fills the section edge-to-edge.
    Hovering just highlights the card's border (see the QFrame#node_card:hover
    rule in _apply_styles) — the card doesn't move or resize.

    Pass is_bucket=True for the synthetic "Unscheduled / other" tile that
    groups pods not attributable to any real node — it renders with a
    dashed muted border and an explanatory blurb instead of CPU/Memory
    rings and a Ready/pressure badge, so it can't be mistaken for an
    actual node reporting 0% usage or sitting in an unknown state.
    """

    doubleClicked = pyqtSignal(str)

    def __init__(self, node_name: str, parent=None, is_bucket: bool = False):
        super().__init__(parent)
        self.node_name   = node_name
        self._base_side  = 300
        self._is_bucket  = is_bucket
        self.setObjectName("node_card")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._base_side, self._base_side)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(14)

        icon = "🕓" if is_bucket else "⎈"
        head = QHBoxLayout()
        head.setSpacing(6)
        self.name_lbl = QLabel(f"{icon}  {node_name}")
        self.name_lbl.setWordWrap(True)
        head.addWidget(self.name_lbl, 1)
        self.status_lbl = QLabel("")
        head.addWidget(self.status_lbl, 0, Qt.AlignTop)
        outer.addLayout(head)

        self.roles_lbl = QLabel("")
        outer.addWidget(self.roles_lbl)

        self.meta_lbl = QLabel("")
        self.meta_lbl.setWordWrap(True)
        self.meta_lbl.setMinimumHeight(28)
        outer.addWidget(self.meta_lbl)

        outer.addStretch(1)

        self.cpu_ring = self.mem_ring = None
        self._ring_caps = []

        if is_bucket:
            # No CPU/Memory rings and no Ready/pressure badge here — those
            # concepts don't apply to a grouping of pods rather than a
            # host. A plain blurb makes clear this tile isn't reporting
            # live metrics for anything, so it can't be read as a node
            # stuck at 0% or in an unknown state.
            self.roles_lbl.setText("Pods not tied to a specific node")
            self.meta_lbl.hide()
            self.status_lbl.hide()

            blurb = QLabel(
                "Includes pending pods with no node\nassignment yet, and pods "
                "reported\nagainst a node outside the cluster's\ncurrent node list."
            )
            blurb.setAlignment(Qt.AlignCenter)
            blurb.setWordWrap(True)
            self._blurb_lbl = blurb
            blurb_row = QHBoxLayout()
            blurb_row.addStretch(1)
            blurb_row.addWidget(blurb)
            blurb_row.addStretch(1)
            outer.addLayout(blurb_row)
        else:
            rings_row = QHBoxLayout()
            rings_row.setSpacing(32)
            self.cpu_ring = CircularProgress(size=112, thickness=11, show_text=True, font_size=18)
            self.mem_ring = CircularProgress(size=112, thickness=11, show_text=True, font_size=18)
            for cap_text, ring in (("CPU", self.cpu_ring), ("Memory", self.mem_ring)):
                col = QVBoxLayout()
                col.setSpacing(6)
                r_row = QHBoxLayout()
                r_row.addStretch(1)
                r_row.addWidget(ring)
                r_row.addStretch(1)
                col.addLayout(r_row)
                cap = QLabel(cap_text)
                cap.setAlignment(Qt.AlignHCenter)
                col.addWidget(cap)
                self._ring_caps.append(cap)
                rings_row.addLayout(col)
            outer.addLayout(rings_row)

        outer.addStretch(1)

        # In-card detail strip — replaces relying on the ring's native
        # QToolTip, which is theme-blind, only appears over the small ring
        # itself, and is easy to miss. Hidden until the card is hovered.
        self._detail_strip = None
        if not is_bucket:
            self._detail_strip = QLabel("")
            self._detail_strip.setWordWrap(True)
            self._detail_strip.hide()
            outer.addWidget(self._detail_strip)

        footer = QHBoxLayout()
        self.pods_lbl = QLabel("")
        footer.addWidget(self.pods_lbl)
        footer.addStretch(1)
        self.pressure_lbl = QLabel("")
        self.pressure_lbl.setAlignment(Qt.AlignRight)
        if is_bucket:
            self.pressure_lbl.hide()
        footer.addWidget(self.pressure_lbl)
        outer.addLayout(footer)

        self.setAttribute(Qt.WA_Hover, True)
        self._cpu_detail = self._mem_detail = ""

        # Smooth hover "lift": a drop shadow that deepens plus a real few-
        # pixel rise, driven by one QVariantAnimation (0 = rest, 1 =
        # hovered) instead of the old instant QSS :hover border swap.
        self._hover_t     = 0.0
        self._rest_y       = None   # captured from the grid layout's own position
        self._animating_lift = False
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(10)
        self._shadow.setOffset(0, 2)
        self._shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(self._shadow)

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(170)
        self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_step)

        self._apply_styles()

    # ── Sizing ─────────────────────────────────────────────────
    def set_side(self, side: int):
        """Set the card's square side length. Called by DashboardTab
        whenever the grid's available width changes, so the row of cards
        keeps filling the section."""
        self._base_side = side
        self.setFixedSize(side, side)

    # ── Styling ────────────────────────────────────────────────
    def _apply_styles(self):
        if self._is_bucket:
            # Dashed border + muted panel background (rather than the
            # solid border/BG_ITEM real node cards use) at rest — so the
            # "not a real node" signal doesn't depend on hover state.
            self._border_rest  = T['TEXT_MUTED']
            self._border_hover = T['TEXT_DIM']
            self._border_style = "dashed"
            self.setStyleSheet(
                f"QFrame#node_card {{ background: {T['BG_PANEL']}; "
                f"border: 1px dashed {self._border_rest}; border-radius: 14px; }}"
            )
        else:
            self._border_rest  = T['BORDER']
            self._border_hover = T['ACCENT']
            self._border_style = "solid"
            self.setStyleSheet(
                f"QFrame#node_card {{ background: {T['BG_ITEM']}; "
                f"border: 1px solid {self._border_rest}; border-radius: 14px; }}"
            )
        self.name_lbl.setStyleSheet(
            f"color: {T['TEXT_DIM'] if self._is_bucket else T['TEXT_PRIMARY']}; "
            f"font-size: 15px; font-weight: 700;"
        )
        self.roles_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.meta_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 10px;")
        self.pods_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        for cap in self._ring_caps:
            cap.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px; font-weight: 600;")
        if self._is_bucket:
            self._blurb_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        if self._detail_strip is not None:
            self._detail_strip.setStyleSheet(
                f"background: {T['BG_HOVER']}; color: {T['TEXT_DIM']}; "
                f"font-size: 11px; border-radius: 8px; padding: 6px 8px;"
            )

    def refresh_theme(self):
        self._apply_styles()
        self._set_border_color(_lerp_color(self._border_rest, self._border_hover, self._hover_t))
        if self.cpu_ring is not None:
            self.cpu_ring.refresh_theme()
        if self.mem_ring is not None:
            self.mem_ring.refresh_theme()

    # ── Data ───────────────────────────────────────────────────
    def update_data(self, status: str, roles: str, cpu_pct, mem_pct,
                     pod_count: int, pressure_text: str, pressure_ok: bool,
                     cpu_tip: str = None, mem_tip: str = None,
                     node_info: dict = None):
        """Update a real-node card. Not used for bucket cards — those only
        ever show a pod count, set directly via update_pod_count()."""
        self.status_lbl.setText(status or "—")
        status_color = T["SUCCESS"] if (status or "").lower() == "ready" else T["DANGER"]
        self.status_lbl.setStyleSheet(
            f"color: {status_color}; font-size: 12px; font-weight: 700;"
        )
        self.roles_lbl.setText(roles or "—")

        info = node_info or {}
        version = info.get("kubelet") or "Kubernetes version unavailable"
        internal_ip = info.get("ip") or "IP unavailable"
        cpu_cap = _k8s_cpu_fmt(info.get("cpu_capacity", ""))
        cpu_alloc = _k8s_cpu_fmt(info.get("cpu_allocatable", ""))
        mem_cap = _k8s_mem_fmt(info.get("mem_capacity", ""))
        mem_alloc = _k8s_mem_fmt(info.get("mem_allocatable", ""))
        self.meta_lbl.setText(
            f"{version}  ·  {internal_ip}\n"
            f"CPU {cpu_cap} cap / {cpu_alloc} alloc  ·  "
            f"RAM {mem_cap} cap / {mem_alloc} alloc"
        )
        self.meta_lbl.show()
        self.setToolTip(
            f"{self.node_name}\n"
            f"Kubernetes: {version}\n"
            f"OS: {info.get('os', 'n/a')}\n"
            f"Kernel: {info.get('kernel', 'n/a')}\n"
            f"Runtime: {info.get('runtime', 'n/a')}\n"
            f"Internal IP: {internal_ip}\n"
            f"CPU capacity: {cpu_cap}\nCPU allocatable: {cpu_alloc}\n"
            f"Memory capacity: {mem_cap}\nMemory allocatable: {mem_alloc}"
        )

        if cpu_pct is not None:
            self.cpu_ring.setValue(cpu_pct, _pct_color(cpu_pct))
            self._cpu_detail = (cpu_tip or f"CPU usage: {cpu_pct:.1f}%").replace("\n", "  ")
        else:
            self.cpu_ring.setValue(0)
            self._cpu_detail = "CPU usage unavailable"
        if mem_pct is not None:
            self.mem_ring.setValue(mem_pct, _pct_color(mem_pct))
            self._mem_detail = (mem_tip or f"Memory usage: {mem_pct:.1f}%").replace("\n", "  ")
        else:
            self.mem_ring.setValue(0)
            self._mem_detail = "Memory usage unavailable"

        self.pods_lbl.setText(f"📦  {pod_count} pod(s)")
        self.pressure_lbl.setText(pressure_text or "")
        self.pressure_lbl.setStyleSheet(
            f"color: {T['SUCCESS'] if pressure_ok else T['DANGER']}; "
            f"font-size: 12px; font-weight: 600;"
        )

    def update_pod_count(self, pod_count: int):
        """Update a bucket card — the only thing it ever shows is how many
        pods currently fall in it."""
        self.pods_lbl.setText(f"📦  {pod_count} pod(s)")

    # ── Interaction ────────────────────────────────────────────
    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit(self.node_name)
        super().mouseDoubleClickEvent(event)

    LIFT_PX = 7  # how many px the card rises at full hover

    def moveEvent(self, event):
        super().moveEvent(event)
        # The grid layout is what actually owns this widget's position
        # (initial placement, _rescale_node_cards, _reflow_node_grid).
        # Whenever it moves the card for a real reason we record that as
        # the new "rest" y — but ignore moves we triggered ourselves from
        # _on_hover_step, or the lift offset would get baked in as rest.
        if not self._animating_lift:
            self._rest_y = self.y()

    def enterEvent(self, event):
        if self._detail_strip is not None:
            text = "   ·   ".join(t for t in (self._cpu_detail, self._mem_detail) if t)
            if text:
                self._detail_strip.setText(text)
                self._detail_strip.show()
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._detail_strip is not None:
            self._detail_strip.hide()
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target: float):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def _set_border_color(self, color: str):
        self.setStyleSheet(
            f"QFrame#node_card {{ background: {T['BG_PANEL'] if self._is_bucket else T['BG_ITEM']}; "
            f"border: 1px {self._border_style} {color}; border-radius: 14px; }}"
        )

    def _on_hover_step(self, value):
        self._hover_t = value
        if self._rest_y is None:
            self._rest_y = self.y()

        # Rise a few px toward the hovered state and deepen the shadow to
        # match — the two together read as the card lifting off the page
        # rather than just its border changing colour.
        self._animating_lift = True
        self.move(self.x(), self._rest_y - round(self.LIFT_PX * value))
        self._animating_lift = False

        self._shadow.setBlurRadius(10 + 26 * value)
        self._shadow.setOffset(0, 2 + 10 * value)
        self._shadow.setColor(QColor(0, 0, 0, round(110 + 90 * value)))

        self._set_border_color(_lerp_color(self._border_rest, self._border_hover, value))


class _NodeGridContainer(QWidget):
    """Plain container for the node-card QGridLayout that emits resized()
    on every size change, so DashboardTab can rescale cards to keep
    filling the available width (see DashboardTab._rescale_node_cards)."""

    resized = pyqtSignal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


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

        # NodeCard tiles currently placed in the Kubernetes nodes grid,
        # keyed by node name. Rebuilt from scratch on every refresh (same
        # clear-then-repopulate approach the old table used).
        self._node_cards = {}
        # (row, col) each card currently occupies — lets _reflow_node_grid
        # skip re-adding cards whose slot hasn't actually changed, so a
        # mid-hover lift animation doesn't get reset by an unrelated
        # refresh tick.
        self._card_grid_pos = {}

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
        self.cpu_ring["ring"].refresh_theme()
        self.mem_ring["ring"].refresh_theme()
        self.storage_ring["ring"].refresh_theme()
        for card in self._node_cards.values():
            card.refresh_theme()
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

        # Top row: CPU/Memory rings on the left, instance detail fields on
        # the right — previously the fields sat in their own row above the
        # rings, leaving the whole right-hand side of the rings row empty.
        top_row = QHBoxLayout()
        top_row.setSpacing(40)

        rings_row = QHBoxLayout()
        rings_row.setSpacing(48)
        self.cpu_ring = self._labeled_ring("CPU usage")
        rings_row.addLayout(self.cpu_ring["layout"])
        self.mem_ring = self._labeled_ring("Memory usage")
        rings_row.addLayout(self.mem_ring["layout"])
        self.storage_ring = self._labeled_ring("Storage usage")
        rings_row.addLayout(self.storage_ring["layout"])
        top_row.addLayout(rings_row)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet(f"color: {T['BORDER']};")
        top_row.addWidget(divider)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
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
            grid.addWidget(lk, row, 0)
            grid.addWidget(lv, row, 1)
            self._vm_fields[key] = lv
        grid.setColumnStretch(1, 1)
        top_row.addLayout(grid, 1)

        vm_body.addLayout(top_row)

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

        # ── Kubernetes cluster overview ─────────────────────
        self.k8s_summary_card = self._make_card("☸  Kubernetes Overview")
        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(12)
        summary_grid.setVerticalSpacing(10)
        self._k8s_summary = {}
        summary_specs = [
            ("nodes", "Nodes"), ("pods", "Pods"), ("running", "Running"), ("pending", "Pending"),
            ("failed", "Failed"), ("namespaces", "Namespaces"), ("pressure", "Pressure"), ("metrics", "Metrics"),
        ]
        for i, (key, label) in enumerate(summary_specs):
            tile = QFrame()
            tile.setObjectName("k8s_summary_tile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(12, 8, 12, 8)
            tile_layout.setSpacing(2)
            value = QLabel("—")
            value.setAlignment(Qt.AlignCenter)
            value.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 17px; font-weight: 700;")
            caption = QLabel(label)
            caption.setAlignment(Qt.AlignCenter)
            caption.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 600;")
            tile_layout.addWidget(value)
            tile_layout.addWidget(caption)
            summary_grid.addWidget(tile, i // 4, i % 4)
            self._k8s_summary[key] = value
        self.k8s_summary_card["body"].addLayout(summary_grid)
        self._content_layout.addWidget(self.k8s_summary_card["frame"])

        # ── Phase 2: workloads ─────────────────────────────
        self.workloads_card = self._make_card("🚀  Workloads")
        self.workloads_tree = QTreeWidget()
        self.workloads_tree.setHeaderLabels([
            "Kind", "Namespace", "Name", "Ready", "Desired",
            "Available", "Updated", "Unavailable", "Age"
        ])
        self._style_tree(self.workloads_tree)
        self.workloads_tree.setMinimumHeight(130)
        self.workloads_tree.setMaximumHeight(260)
        self.workloads_card["body"].addWidget(self.workloads_tree)
        self._content_layout.addWidget(self.workloads_card["frame"])

        # ── Phase 2: services ──────────────────────────────
        self.services_card = self._make_card("🌐  Services")
        self.services_tree = QTreeWidget()
        self.services_tree.setHeaderLabels([
            "Service", "Namespace", "Type", "Cluster IP",
            "External", "Ports", "Endpoints", "Age"
        ])
        self._style_tree(self.services_tree)
        self.services_tree.setMinimumHeight(110)
        self.services_tree.setMaximumHeight(240)
        self.services_card["body"].addWidget(self.services_tree)
        self._content_layout.addWidget(self.services_card["frame"])

        # ── Phase 2: recent Kubernetes events ───────────────
        self.events_card = self._make_card("⚠  Recent Kubernetes Events")
        self.events_tree = QTreeWidget()
        self.events_tree.setHeaderLabels([
            "Time", "Type", "Reason", "Object", "Namespace", "Message"
        ])
        self._style_tree(self.events_tree)
        self.events_tree.setMinimumHeight(130)
        self.events_tree.setMaximumHeight(300)
        self.events_card["body"].addWidget(self.events_tree)
        self._content_layout.addWidget(self.events_card["frame"])

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

        self.k8s_hint = QLabel("Double-click a node card to see the pods running on it.")
        self.k8s_hint.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.k8s_card["body"].addWidget(self.k8s_hint)

        # Node summaries as cards, 3 per row, instead of a one-row-per-node
        # table. self.k8s_grid is repopulated from scratch on every refresh
        # (see _clear_node_grid / _add_node_card). The container is a
        # resize-aware widget so the row of cards keeps filling the full
        # section width as the window is resized (see _rescale_node_cards).
        self.k8s_grid_container = _NodeGridContainer()
        self.k8s_grid = QGridLayout(self.k8s_grid_container)
        self.k8s_grid.setContentsMargins(0, 20, 0, 0)
        self.k8s_grid.setHorizontalSpacing(16)
        self.k8s_grid.setVerticalSpacing(16)
        # Cards are square and fixed-size at any given moment, so the card
        # columns shouldn't stretch themselves (that would space cards
        # unevenly). Cards live in columns 1..NODE_GRID_COLS; column 0 and
        # the trailing column split any leftover width evenly between
        # them, which centres the row instead of pinning it to the left.
        self.k8s_grid.setColumnStretch(0, 1)
        self.k8s_grid.setColumnStretch(self.NODE_GRID_COLS + 1, 1)
        self.k8s_grid_container.resized.connect(self._rescale_node_cards)
        self.k8s_card["body"].addWidget(self.k8s_grid_container)
        self._content_layout.addWidget(self.k8s_card["frame"])

        self._content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)

        self.vm_card["frame"].hide()
        self.k8s_summary_card["frame"].hide()
        self.workloads_card["frame"].hide()
        self.services_card["frame"].hide()
        self.events_card["frame"].hide()
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

    def _labeled_ring(self, label: str) -> dict:
        """A vertical block — label on top, a large centred ring, value
        readout underneath. Two of these placed in a QHBoxLayout (see
        _build_ui) put CPU and Memory side by side instead of stacked."""
        col = QVBoxLayout()
        col.setSpacing(8)

        lk = QLabel(label)
        lk.setAlignment(Qt.AlignHCenter)
        lk.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px; font-weight: 700;")
        col.addWidget(lk)

        ring = CircularProgress(size=140, thickness=12, show_text=True, font_size=24)
        ring_row = QHBoxLayout()
        ring_row.addStretch(1)
        ring_row.addWidget(ring)
        ring_row.addStretch(1)
        col.addLayout(ring_row)

        val_lbl = QLabel("—")
        val_lbl.setAlignment(Qt.AlignHCenter)
        val_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        col.addWidget(val_lbl)

        return {"layout": col, "ring": ring, "val_lbl": val_lbl}

    def _style_tree(self, tree):
        tree.setFont(monospace_font(12))
        tree.setStyleSheet(f"QTreeWidget {{ font-size: 12px; }} "
                            f"QTreeWidget::item {{ height: 30px; }}")

    def _style_ring(self, ring: CircularProgress, pct: float):
        ring.setValue(pct, _pct_color(pct))

    # ── Node card grid ─────────────────────────────────────────
    NODE_GRID_COLS     = 3
    NODE_CARD_MIN_SIDE = 320   # leave room for rings plus the Phase 1 metadata line
    NODE_CARD_MAX_SIDE = 420   # cap growth on very wide windows

    def _clear_node_grid(self):
        """Remove and delete every card currently in the grid, ready for a
        fresh set to be added via _add_node_card()."""
        while self.k8s_grid.count():
            item = self.k8s_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._node_cards = {}
        self._card_grid_pos = {}

    def _current_card_side(self) -> int:
        """Square side length that makes NODE_GRID_COLS cards, plus the
        gaps between them, exactly fill the grid container's width —
        clamped so cards never get uncomfortably tiny or huge."""
        width = self.k8s_grid_container.width()
        if width <= 0:
            return self.NODE_CARD_MIN_SIDE
        spacing = self.k8s_grid.horizontalSpacing()
        side = (width - spacing * (self.NODE_GRID_COLS - 1)) / self.NODE_GRID_COLS
        return int(max(self.NODE_CARD_MIN_SIDE, min(self.NODE_CARD_MAX_SIDE, side)))

    def _rescale_node_cards(self):
        """Re-apply the current fill-width side length to every card in
        the grid. Connected to the container's resized signal so the row
        keeps filling the section as the window is resized."""
        side = self._current_card_side()
        for card in self._node_cards.values():
            card.set_side(side)

    def _add_node_card(self, node_name: str, is_bucket: bool = False) -> NodeCard:
        """Create a NodeCard for *node_name*, place it at the next free
        grid slot (3 cards per row), size it to fill the row, and track it."""
        card = NodeCard(node_name, is_bucket=is_bucket)
        card.set_side(self._current_card_side())
        card.doubleClicked.connect(self._on_node_double_clicked)
        idx = len(self._node_cards)
        row, col = divmod(idx, self.NODE_GRID_COLS)
        self.k8s_grid.addWidget(card, row, col + 1)
        self._node_cards[node_name] = card
        self._card_grid_pos[node_name] = (row, col)
        return card

    def _get_or_create_node_card(self, node_name: str, is_bucket: bool = False) -> NodeCard:
        """Like _add_node_card(), but reuses the existing card for
        *node_name* if the grid already has one instead of always building
        a fresh one.

        This matters because CircularProgress only skips its fill
        animation when setValue() is called again on the *same* ring
        instance with an unchanged value (see progress_ring.py). A brand
        new CircularProgress always starts at 0%, so recreating every
        NodeCard (and therefore every ring) on each refresh made the CPU/
        Memory rings replay their 0%→value animation every single refresh
        cycle — even when the underlying reading hadn't moved at all.
        Reusing cards by node name lets that existing guard actually do
        its job.
        """
        card = self._node_cards.get(node_name)
        if card is not None:
            return card
        return self._add_node_card(node_name, is_bucket=is_bucket)

    def _reflow_node_grid(self, order: list):
        """Reconcile self._node_cards with *order* (the list of node/
        bucket names that should be shown this refresh, in display order).

        Cards for names no longer present are removed and deleted; cards
        for names still present are repositioned (if needed) rather than
        recreated, so widgets — and their live CircularProgress rings —
        persist across refreshes instead of being torn down every time.
        """
        order_set = set(order)

        # Drop cards for nodes/buckets that disappeared.
        for name in list(self._node_cards.keys()):
            if name not in order_set:
                card = self._node_cards.pop(name)
                self._card_grid_pos.pop(name, None)
                self.k8s_grid.removeWidget(card)
                card.deleteLater()

        for idx, name in enumerate(order):
            card = self._node_cards.get(name)
            if card is None:
                continue
            row, col = divmod(idx, self.NODE_GRID_COLS)
            if self._card_grid_pos.get(name) == (row, col):
                continue  # already sitting in the right cell — leave it alone
            self.k8s_grid.removeWidget(card)
            self.k8s_grid.addWidget(card, row, col + 1)
            self._card_grid_pos[name] = (row, col)

    def _apply_styles(self):
        self.ctrl_bar.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
        for frame in (self.vm_card["frame"], self.k8s_summary_card["frame"],
                       self.workloads_card["frame"], self.services_card["frame"],
                       self.events_card["frame"], self.k8s_card["frame"]):
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
        self.k8s_summary_card["frame"].hide()
        self.workloads_card["frame"].hide()
        self.services_card["frame"].hide()
        self.events_card["frame"].hide()
        self.k8s_card["frame"].hide()
        self.updated_lbl.setText("")
        self._update_live_label()

    def _show_connected_placeholder(self):
        self.disconnected_lbl.hide()
        self.vm_card["frame"].show()
        self.k8s_summary_card["frame"].show()
        self.workloads_card["frame"].show()
        self.services_card["frame"].show()
        self.events_card["frame"].show()
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
            self._style_ring(self.cpu_ring["ring"], cpu_pct)
            self.cpu_ring["val_lbl"].setText(f"{cpu_pct:.0f}% busy")
            self.cpu_ring["ring"].setToolTip(
                f"CPU usage: {cpu_pct:.1f}% busy\n"
                f"Cores: {cores}\n"
                f"Model: {model}"
            )
        else:
            # Reset the ring too, not just the label — otherwise a ring that
            # was populated by an earlier successful refresh keeps showing
            # that stale value forever once this stat becomes unavailable.
            self.cpu_ring["ring"].setValue(0)
            self.cpu_ring["val_lbl"].setText("unavailable")
            self.cpu_ring["ring"].setToolTip("CPU usage unavailable")

        # Memory — a "Mem: total used free shared buff/cache available"
        # line, same shape whether it came from Linux `free -m` or the
        # macOS branch of _HOST_CMD (which computes the equivalent from
        # `sysctl hw.memsize`/`vm_stat` and prints it in the same layout).
        mem_line = first("MEM", "")
        if mem_line:
            parts = mem_line.split()
            try:
                total_mb, free_mb = float(parts[1]), float(parts[3])
                used_mb = total_mb - free_mb
                mem_pct = (used_mb / total_mb * 100.0) if total_mb else 0.0
                self._style_ring(self.mem_ring["ring"], mem_pct)
                self.mem_ring["val_lbl"].setText(
                    f"{used_mb/1024:.1f} / {total_mb/1024:.1f} GB"
                )
                tip_lines = [
                    f"Memory usage: {mem_pct:.1f}%",
                    f"Total: {total_mb/1024:.2f} GB",
                    f"Used: {used_mb/1024:.2f} GB",
                    f"Free: {free_mb/1024:.2f} GB",
                ]
                # shared/buff-cache/available are only meaningful on the
                # Linux `free -m` branch — the macOS branch always fills
                # those columns with 0, so skip them there.
                if len(parts) >= 7:
                    shared_mb, buffcache_mb, avail_mb = (
                        float(parts[4]), float(parts[5]), float(parts[6])
                    )
                    if shared_mb or buffcache_mb:
                        tip_lines.append(f"Buff/cache: {buffcache_mb/1024:.2f} GB")
                        tip_lines.append(f"Available: {avail_mb/1024:.2f} GB")
                self.mem_ring["ring"].setToolTip("\n".join(tip_lines))
            except (ValueError, IndexError):
                self.mem_ring["ring"].setValue(0)
                self.mem_ring["val_lbl"].setText("unavailable")
                self.mem_ring["ring"].setToolTip("Memory usage unavailable")
        else:
            self.mem_ring["ring"].setValue(0)
            self.mem_ring["val_lbl"].setText("unavailable")
            self.mem_ring["ring"].setToolTip("Memory usage unavailable")

        # Disk mounts — also accumulated into an overall storage total/used
        # (in bytes, parsed back out of df's human-readable Size/Used
        # columns) so the storage ring reflects everything in the table
        # below it rather than just one arbitrarily-chosen mount.
        self.disk_tree.clear()
        total_bytes = used_bytes = 0.0
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

            size_b, used_b = _parse_df_size(size), _parse_df_size(used)
            if size_b is not None and used_b is not None:
                total_bytes += size_b
                used_bytes  += used_b

        if total_bytes > 0:
            storage_pct = min(100.0, used_bytes / total_bytes * 100.0)
            self._style_ring(self.storage_ring["ring"], storage_pct)
            self.storage_ring["val_lbl"].setText(
                f"{size_fmt(used_bytes)} / {size_fmt(total_bytes)}"
            )
            avail_bytes = max(0.0, total_bytes - used_bytes)
            self.storage_ring["ring"].setToolTip(
                f"Storage usage: {storage_pct:.1f}%\n"
                f"Total: {size_fmt(total_bytes)}\n"
                f"Used: {size_fmt(used_bytes)}\n"
                f"Available: {size_fmt(avail_bytes)}\n"
                f"Across {self.disk_tree.topLevelItemCount()} mount(s)"
            )
        else:
            self.storage_ring["ring"].setValue(0)
            self.storage_ring["val_lbl"].setText("unavailable")
            self.storage_ring["ring"].setToolTip("Storage usage unavailable")

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
            self._clear_node_grid()
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
            self.k8s_summary_card["frame"].hide()
            self.workloads_card["frame"].hide()
            self.services_card["frame"].hide()
            self.events_card["frame"].hide()
            self.workloads_tree.clear()
            self.services_tree.clear()
            self.events_tree.clear()
            return
        self.k8s_note.hide()
        self.k8s_summary_card["frame"].show()

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

        # Node capacity/allocatable and runtime metadata, keyed by node name.
        # This comes from the Kubernetes API rather than the human-oriented
        # `kubectl get nodes -o wide` table, so resource quantities remain
        # stable across kubectl versions.
        node_info = {}
        for line in sec.get("NODEINFO", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            bits += [""] * (12 - len(bits))
            (name, kubelet, os_image, kernel, runtime, ip, cpu_capacity,
             mem_capacity, pod_capacity, cpu_allocatable, mem_allocatable,
             pod_allocatable) = bits[:12]
            if not name.strip():
                continue
            node_info[name.strip()] = {
                "kubelet": kubelet.strip(),
                "os": os_image.strip(),
                "kernel": kernel.strip(),
                "runtime": runtime.strip(),
                "ip": ip.strip(),
                "cpu_capacity": cpu_capacity.strip(),
                "mem_capacity": mem_capacity.strip(),
                "pod_capacity": pod_capacity.strip(),
                "cpu_allocatable": cpu_allocatable.strip(),
                "mem_allocatable": mem_allocatable.strip(),
                "pod_allocatable": pod_allocatable.strip(),
            }

        # CPU%/MEM% from `kubectl top nodes`, keyed by node name. Absent
        # entirely (older cluster, no metrics-server) just means we show
        # "n/a" instead of a bar — never an error state.
        top = {}
        for line in sec.get("TOP", []):
            parts = line.split()
            if len(parts) >= 5 and parts[2].endswith("%") and parts[4].endswith("%"):
                top[parts[0]] = {
                    "cpu_pct": parts[2].rstrip("%"), "mem_pct": parts[4].rstrip("%"),
                    "cpu_cores": parts[1], "mem_bytes": parts[3],
                }

        # Full pod inventory, grouped by the node each pod is scheduled on.
        # Pods not yet scheduled (nodeName empty — usually Pending) are
        # collected under a synthetic "(unscheduled)" key instead of being
        # dropped, so they're still visible somewhere.
        pods_by_node = {}
        for line in sec.get("PODS", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            bits += [""] * (15 - len(bits))
            (ns, pname, phase, node, restarts_csv, ready_csv, reason, message,
             pod_ip, host_ip, qos, created, owner, waiting) = bits[:14]
            ns, pname, phase, node = ns.strip(), pname.strip(), phase.strip(), node.strip()
            if not pname:
                continue
            restarts = sum(int(x) for x in restarts_csv.split(",") if x.strip().isdigit())
            ready_flags = [x for x in ready_csv.split(",") if x.strip()]
            ready_count = sum(1 for x in ready_flags if x.strip() == "true")
            waiting = ";".join(x.strip() for x in waiting.split(";") if x.strip())
            pods_by_node.setdefault(node or "(unscheduled)", []).append({
                "namespace": ns, "name": pname, "phase": phase or "Unknown",
                "restarts": restarts,
                "ready": f"{ready_count}/{len(ready_flags)}" if ready_flags else "-",
                "reason": reason.strip(), "message": message.strip(),
                "pod_ip": pod_ip.strip(), "host_ip": host_ip.strip(),
                "qos": qos.strip(), "created": created.strip(),
                "owner": owner.strip(), "waiting": waiting,
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

        # ── Phase 2: workloads ──────────────────────────────
        workloads = []
        for line in sec.get("WORKLOADS", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            if len(bits) < 10:
                continue
            kind, ns, name, created, replicas, ready, available, updated, unavailable, desired = bits[:10]
            if not name.strip():
                continue
            workloads.append({
                "kind": kind.strip(), "namespace": ns.strip(), "name": name.strip(),
                "created": created.strip(), "replicas": replicas.strip() or "0",
                "ready": ready.strip() or "0", "available": available.strip() or "0",
                "updated": updated.strip() or "0", "unavailable": unavailable.strip() or "0",
                "desired": desired.strip() or replicas.strip() or "0",
            })

        # ── Phase 2: services/endpoints ────────────────────
        endpoint_counts = {}
        for line in sec.get("ENDPOINTS", []):
            bits = line.split("|")
            if len(bits) < 3:
                continue
            ns, name = bits[0].strip(), bits[1].strip()
            ready_count = sum(1 for x in bits[2].split(";") if x.strip())
            not_ready_count = sum(1 for x in (bits[3] if len(bits) > 3 else "").split(";") if x.strip())
            endpoint_counts[(ns, name)] = (ready_count, not_ready_count)

        services = []
        for line in sec.get("SERVICES", []):
            if "|" not in line:
                continue
            bits = line.split("|")
            bits += [""] * (8 - len(bits))
            ns, name, svc_type, cluster_ip, ext_ip, ext_host, ports, created = bits[:8]
            if not name.strip():
                continue
            external = ext_ip.strip() or ext_host.strip() or "—"
            ep_ready, ep_notready = endpoint_counts.get((ns.strip(), name.strip()), (0, 0))
            services.append({
                "namespace": ns.strip(), "name": name.strip(),
                "type": svc_type.strip() or "ClusterIP",
                "cluster_ip": cluster_ip.strip() or "—",
                "external": external,
                "ports": ports.strip().rstrip(",") or "—",
                "endpoints": f"{ep_ready}" + (f" (+{ep_notready} not ready)" if ep_notready else ""),
                "created": created.strip(),
            })

        # ── Phase 2: recent events ─────────────────────────
        events = []
        for line in sec.get("EVENTS", []):
            if "|" not in line:
                continue
            bits = line.split("|", 6)
            if len(bits) < 7:
                continue
            ts, etype, reason, obj_kind, ns, obj_name, message = [x.strip() for x in bits]
            if not reason and not message:
                continue
            events.append({
                "timestamp": ts, "type": etype or "Normal", "reason": reason or "—",
                "object": f"{obj_kind}/{obj_name}" if obj_kind and obj_name else obj_name,
                "namespace": ns or "—", "message": message or "—",
            })
        events = events[-50:]

        # NOTE: no _clear_node_grid() here — cards are now reused across
        # refreshes (see _get_or_create_node_card / _reflow_node_grid) so
        # their CircularProgress rings only animate when a value actually
        # changes, instead of replaying 0%→value on every refresh tick.
        self._pods_by_node_cache = {}
        self._pod_usage_cache    = pod_usage
        _node_order = []

        _pressure_names = {"mem": "MemoryPressure", "disk": "DiskPressure", "pid": "PIDPressure"}

        # Cluster-level summary. Keep this derived from the same snapshot so
        # the overview and node/pod cards always describe the same moment.
        all_pods = [p for node_pods in pods_by_node.values() for p in node_pods]
        # Pods already associated with known nodes are still in pods_by_node
        # at this point; pop() below happens afterwards.
        total_pods = len(all_pods)
        running_pods = sum(1 for p in all_pods if p["phase"].lower() == "running")
        pending_pods = sum(1 for p in all_pods if p["phase"].lower() == "pending")
        failed_pods = sum(1 for p in all_pods if p["phase"].lower() in ("failed", "unknown"))
        namespaces = len({p["namespace"] for p in all_pods if p["namespace"]})
        pressure_nodes = 0
        for node_name in cond:
            c = cond.get(node_name, {})
            if any(c.get(k) == "True" for k in ("mem", "disk", "pid")):
                pressure_nodes += 1
        ready_nodes = sum(1 for line in node_lines if len(line.split()) >= 2 and line.split()[1].lower().split(",", 1)[0] == "ready")
        metrics_available = bool(top)
        self._k8s_summary["nodes"].setText(f"{ready_nodes}/{len(node_lines)}")
        self._k8s_summary["pods"].setText(str(total_pods))
        self._k8s_summary["running"].setText(str(running_pods))
        self._k8s_summary["pending"].setText(str(pending_pods))
        self._k8s_summary["failed"].setText(str(failed_pods))
        self._k8s_summary["namespaces"].setText(str(namespaces))
        self._k8s_summary["pressure"].setText(str(pressure_nodes))
        self._k8s_summary["metrics"].setText("Available" if metrics_available else "Unavailable")
        self._k8s_summary["metrics"].setStyleSheet(
            f"color: {T['SUCCESS'] if metrics_available else T['WARNING']}; font-size: 17px; font-weight: 700;"
        )

        # Render workloads.
        self.workloads_tree.clear()
        for w in sorted(workloads, key=lambda x: (x["namespace"], x["kind"], x["name"])):
            desired = w["desired"]
            ready = w["ready"]
            available = w["available"]
            updated = w["updated"]
            unavailable = w["unavailable"]
            item = QTreeWidgetItem([
                w["kind"], w["namespace"], w["name"],
                f"{ready}/{desired}", desired, available, updated, unavailable,
                _short_age(w["created"])
            ])
            try:
                ready_n, desired_n = int(ready), int(desired)
                healthy = desired_n > 0 and ready_n >= desired_n
                item.setForeground(3, QColor(T["SUCCESS"] if healthy else T["WARNING"]))
            except ValueError:
                pass
            if unavailable not in ("", "0"):
                item.setForeground(7, QColor(T["DANGER"]))
            self.workloads_tree.addTopLevelItem(item)
        for col in range(9):
            self.workloads_tree.resizeColumnToContents(col)

        # Render services.
        self.services_tree.clear()
        for s in sorted(services, key=lambda x: (x["namespace"], x["name"])):
            item = QTreeWidgetItem([
                s["name"], s["namespace"], s["type"], s["cluster_ip"],
                s["external"], s["ports"], s["endpoints"], _short_age(s["created"])
            ])
            ep = s["endpoints"]
            if ep.startswith("0"):
                item.setForeground(6, QColor(T["DANGER"]))
            elif "not ready" in ep:
                item.setForeground(6, QColor(T["WARNING"]))
            else:
                item.setForeground(6, QColor(T["SUCCESS"]))
            self.services_tree.addTopLevelItem(item)
        for col in range(8):
            self.services_tree.resizeColumnToContents(col)

        # Render events. Keep warnings/errors visually prominent.
        self.events_tree.clear()
        for e in reversed(events):
            item = QTreeWidgetItem([
                e["timestamp"][11:19] if len(e["timestamp"]) >= 19 else e["timestamp"],
                e["type"], e["reason"], e["object"], e["namespace"], e["message"]
            ])
            event_type = e["type"].lower()
            item.setForeground(1, QColor(
                T["DANGER"] if event_type == "warning" else T["SUCCESS"]
            ))
            item.setToolTip(5, f"{e['reason']}: {e['message']}")
            self.events_tree.addTopLevelItem(item)
        for col in range(6):
            self.events_tree.resizeColumnToContents(col)
        if self.events_tree.topLevelItemCount() > 0:
            self.events_tree.scrollToTop()

        for line in node_lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            name, status, roles = parts[0], parts[1], parts[2]
            node_pods = pods_by_node.pop(name, [])
            self._pods_by_node_cache[name] = node_pods

            c = cond.get(name, {})
            pressures = [label for key, label in _pressure_names.items() if c.get(key) == "True"]
            pressure_text = ", ".join(pressures) if pressures else "OK"
            pressure_ok   = not pressures

            t = top.get(name)
            cpu_pct = mem_pct = None
            cpu_tip = mem_tip = None
            if t is not None:
                try:
                    cpu_pct = float(t["cpu_pct"])
                    cpu_tip = f"CPU usage: {cpu_pct:.1f}%\nCores used: {t['cpu_cores']}"
                except ValueError:
                    cpu_pct = None
                try:
                    mem_pct = float(t["mem_pct"])
                    mem_tip = f"Memory usage: {mem_pct:.1f}%\nUsed: {t['mem_bytes']}"
                except ValueError:
                    mem_pct = None

            card = self._get_or_create_node_card(name)
            card.update_data(status, roles, cpu_pct, mem_pct,
                              len(node_pods), pressure_text, pressure_ok,
                              cpu_tip, mem_tip, node_info.get(name, {}))
            _node_order.append(name)

        # Anything left in pods_by_node belongs to a node that either
        # wasn't in the NODES list or is the synthetic "(unscheduled)"
        # bucket — surface it as its own card (double-clickable, same as
        # any other node) rather than silently dropping those pods.
        # Rendered as a bucket card (see NodeCard) rather than update_data(),
        # so it can't be mistaken for a real node reporting 0% usage.
        for node_name, node_pods in pods_by_node.items():
            label = "Unscheduled / other" if node_name == "(unscheduled)" else node_name
            self._pods_by_node_cache[label] = node_pods
            card = self._get_or_create_node_card(label, is_bucket=True)
            card.update_pod_count(len(node_pods))
            _node_order.append(label)

        self._reflow_node_grid(_node_order)

        # Push fresh data into any node detail windows that are still open,
        # instead of leaving them showing a stale snapshot until re-clicked.
        for node_name, win in self._node_windows.items():
            win.update_pods(self._pods_by_node_cache.get(node_name, []), self._pod_usage_cache)

        self._mark_updated()
        self.status_msg.emit("Dashboard updated")

    # ── Node detail window ──────────────────────────────────────
    def _on_node_double_clicked(self, node_name: str):
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
        self._clear_node_grid()
        self.k8s_note.setText(f"Kubernetes data unavailable: {err}")
        self.k8s_note.show()