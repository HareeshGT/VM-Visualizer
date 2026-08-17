"""k8s_cards.py — Card-style row widgets for Kubernetes resource lists.

Replaces the old flat QTreeWidget rows for Pods/Deployments with a
QListWidget of these cards (same "meta dict + setItemWidget()" pattern
already used by file_widgets.py's FileRowWidget/FileGridWidget).

Each card owns its own selected/hovered paint state (see _CardBase)
rather than relying on QListWidget::item's native selection styling —
the card fully covers the item's rect, so the owning list must forward
selection changes into set_selected() itself (KubernetesTab does this
via currentItemChanged).
"""

from PyQt5.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy
from PyQt5.QtCore import Qt

from themes import T
from utils import monospace_font


def _hex_to_rgb(h: str) -> str:
    h = h.lstrip("#")
    return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"


def _status_color_key(status: str) -> str:
    s = (status or "").lower()
    if "running" in s or "completed" in s:
        return "SUCCESS"
    if any(x in s for x in ("pending", "init", "creating", "terminating")):
        return "WARNING"
    if any(x in s for x in ("error", "crash", "fail", "evict", "unknown")):
        return "DANGER"
    return "TEXT_MUTED"


def _pill(text: str, color_key: str) -> QLabel:
    color = T.get(color_key, T["TEXT_MUTED"])
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background: rgba({_hex_to_rgb(color)}, 0.15); color: {color}; "
        f"border: 1px solid rgba({_hex_to_rgb(color)}, 0.4); border-radius: 9px; "
        f"padding: 2px 10px; font-size: 11px; font-weight: 700;"
    )
    return lbl


def _chip(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background: {T['BG_DARK']}; color: {T['TEXT_DIM']}; "
        f"border: 1px solid {T['BORDER']}; border-radius: 8px; "
        f"padding: 1px 8px; font-size: 10px; font-weight: 600;"
    )
    return lbl


def _meta_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
    return lbl


class _CardBase(QFrame):
    """Shared card chrome: rounded frame, status-colored left edge, and a
    manually-driven hover/selected paint state (see module docstring)."""

    CARD_HEIGHT = 68

    def __init__(self, accent_color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("k8s_card")
        self._accent   = accent_color
        self._selected = False
        self._hovered  = False
        self.setFixedHeight(self.CARD_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

    def _apply_style(self):
        if self._selected:
            bg     = T["BG_ITEM_SEL"]
            border = T["ACCENT"]
        elif self._hovered:
            bg     = T["BG_HOVER"]
            border = T["BORDER"]
        else:
            bg     = T["BG_ITEM"]
            border = T["BORDER"]
        self.setStyleSheet(
            f"QFrame#k8s_card {{ background: {bg}; border-radius: 10px; "
            f"border-top: 1px solid {border}; border-right: 1px solid {border}; "
            f"border-bottom: 1px solid {border}; border-left: 3px solid {self._accent}; }}"
        )

    def set_selected(self, selected: bool):
        if selected != self._selected:
            self._selected = selected
            self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_style()
        super().leaveEvent(event)


class PodCardWidget(_CardBase):
    """One pod, as a card. `meta` matches what _populate_pods builds:
    namespace, name, ready, status, restarts, last_restart, age, ip, node."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        super().__init__(_status_accent(meta.get("status", "")), parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        ready = meta.get("ready", "")
        if ready:
            ready_ok = _ratio_ok(ready)
            top.addWidget(_pill(f"Ready {ready}", "SUCCESS" if ready_ok else "WARNING"))

        status = meta.get("status", "") or "Unknown"
        top.addWidget(_pill(status, _status_color_key(status)))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        restarts     = meta.get("restarts", "0")
        last_restart = meta.get("last_restart", "-")
        restart_txt  = f"↺ {restarts}"
        if last_restart and last_restart != "-":
            restart_txt += f"  ({last_restart})"
        bottom.addWidget(_meta_label(restart_txt))
        bottom.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        bottom.addWidget(_meta_label(meta.get("ip", "-") or "-"))

        node_lbl = _meta_label(meta.get("node", "-") or "-")
        node_lbl.setToolTip(meta.get("node", ""))
        node_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(node_lbl, 1)

        outer.addLayout(bottom)


class DeploymentCardWidget(_CardBase):
    """One deployment, as a card. `meta` matches _populate_deployments:
    namespace, name, ready, up_to_date, available, age, images."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        ready    = meta.get("ready", "")
        ready_ok = _ratio_ok(ready)
        super().__init__(T["SUCCESS"] if ready_ok else T["WARNING"], parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        if ready:
            top.addWidget(_pill(f"Ready {ready}", "SUCCESS" if ready_ok else "WARNING"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        bottom.addWidget(_meta_label(f"Up-to-date {meta.get('up_to_date', '-')}"))
        bottom.addWidget(_meta_label(f"Available {meta.get('available', '-')}"))

        images_lbl = _meta_label(meta.get("images", "-") or "-")
        images_lbl.setToolTip(meta.get("images", ""))
        images_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(images_lbl, 1)

        outer.addLayout(bottom)


class StatefulSetCardWidget(_CardBase):
    """One StatefulSet, as a card. `meta` matches what _populate_statefulsets
    builds: namespace, name, ready, age, images.

    Same shape as DeploymentCardWidget minus up-to-date/available (StatefulSets
    don't report those columns) — kept as its own class rather than reusing
    DeploymentCardWidget so a future divergence (e.g. showing the update
    strategy or current/update revision) doesn't have to fight a shared
    Deployment-specific layout."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        ready    = meta.get("ready", "")
        ready_ok = _ratio_ok(ready)
        super().__init__(T["SUCCESS"] if ready_ok else T["WARNING"], parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        if ready:
            top.addWidget(_pill(f"Ready {ready}", "SUCCESS" if ready_ok else "WARNING"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        images_lbl = _meta_label(meta.get("images", "-") or "-")
        images_lbl.setToolTip(meta.get("images", ""))
        images_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(images_lbl, 1)

        outer.addLayout(bottom)


class DaemonSetCardWidget(_CardBase):
    """One DaemonSet, as a card. `meta` matches what _populate_daemonsets
    builds: namespace, name, desired, current, ready, up_to_date, available,
    node_selector, age, images.

    DaemonSets have no replica count to scale — "ready" here means "ready
    on how many of the nodes it's scheduled to", so the accent still reads
    off ready vs desired the same way Deployments/StatefulSets do."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        desired  = meta.get("desired", "0")
        ready    = meta.get("ready", "0")
        ready_ok = (desired == ready) and desired not in ("", "0")
        # A DaemonSet with desired=0 (e.g. node selector matches nothing
        # right now) isn't "broken" — don't paint it as a warning.
        accent = T["SUCCESS"] if (ready_ok or desired == "0") else T["WARNING"]
        super().__init__(accent, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        top.addWidget(_pill(f"Ready {ready}/{desired}", "SUCCESS" if (ready_ok or desired == "0") else "WARNING"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        bottom.addWidget(_meta_label(f"Current {meta.get('current', '-')}"))
        bottom.addWidget(_meta_label(f"Up-to-date {meta.get('up_to_date', '-')}"))
        bottom.addWidget(_meta_label(f"Available {meta.get('available', '-')}"))

        node_sel = meta.get("node_selector", "-") or "-"
        ns_lbl = _meta_label(node_sel)
        ns_lbl.setToolTip(node_sel)
        ns_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(ns_lbl, 1)

        outer.addLayout(bottom)


def _event_type_color_key(etype: str) -> str:
    return "DANGER" if (etype or "").lower() == "warning" else "INFO"


class EventCardWidget(_CardBase):
    """One cluster Event, as a card. `meta` matches what _populate_events
    builds: namespace, type, reason, message, count, object_kind,
    object_name, age.

    Warning-type events get the DANGER accent so they visually jump out of
    a feed that's otherwise mostly routine Normal events — this is the
    whole point of the Events tab ("why did this just restart")."""

    CARD_HEIGHT = 64

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        etype = meta.get("type", "") or "Normal"
        super().__init__(T.get(_event_type_color_key(etype), T["TEXT_MUTED"]), parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 6, 14, 6)
        outer.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        reason_lbl = QLabel(meta.get("reason", "") or "-")
        reason_lbl.setFont(monospace_font(13, bold=True))
        reason_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        reason_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        top.addWidget(reason_lbl)

        top.addWidget(_pill(etype, _event_type_color_key(etype)))

        obj_kind = meta.get("object_kind", "")
        obj_name = meta.get("object_name", "")
        if obj_kind or obj_name:
            top.addWidget(_chip(f"{obj_kind}/{obj_name}" if obj_kind else obj_name))

        if show_namespace and meta.get("namespace"):
            top.addWidget(_chip(meta.get("namespace", "")))

        top.addStretch(1)

        count = meta.get("count") or 1
        if count and int(count) > 1:
            top.addWidget(_pill(f"×{count}", "WARNING"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        msg = meta.get("message", "") or ""
        msg_lbl = QLabel(msg)
        msg_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        msg_lbl.setToolTip(msg)
        fm = msg_lbl.fontMetrics()
        msg_lbl.setText(fm.elidedText(msg.replace("\n", " "), Qt.ElideRight, 900))
        outer.addWidget(msg_lbl)


def _metric_scalar(d: dict) -> str:
    """Pull whichever value field a v2 HPA metric target/current dict
    actually carries — different metric types (Resource/Pods/Object/
    External) populate different keys of the same shape."""
    if not d:
        return "-"
    if "averageUtilization" in d:
        return f"{d['averageUtilization']}%"
    if "averageValue" in d:
        return str(d["averageValue"])
    if "value" in d:
        return str(d["value"])
    return "-"


class HPACardWidget(_CardBase):
    """One HorizontalPodAutoscaler, as a card. `meta` matches what
    _populate_hpas builds: namespace, name, reference, min_replicas,
    max_replicas, current_replicas, desired_replicas, metrics (list of
    {label, current, target}), age, healthy."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        healthy = meta.get("healthy", True)
        super().__init__(T["SUCCESS"] if healthy else T["WARNING"], parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        cur = meta.get("current_replicas", "-")
        mn  = meta.get("min_replicas", "-")
        mx  = meta.get("max_replicas", "-")
        top.addWidget(_pill(f"{cur} pods  ({mn}–{mx})", "SUCCESS" if healthy else "WARNING"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        ref_lbl = _meta_label(f"→ {meta.get('reference', '-')}")
        bottom.addWidget(ref_lbl)

        metrics = meta.get("metrics") or []
        metrics_text = " · ".join(
            f"{m['label']} {m['current']}/{m['target']}" for m in metrics
        ) or "no metrics"
        metrics_lbl = _meta_label(metrics_text)
        metrics_lbl.setToolTip(metrics_text)
        metrics_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(metrics_lbl, 1)

        outer.addLayout(bottom)


def _svc_type_color_key(stype: str) -> str:
    s = (stype or "").lower()
    if s == "loadbalancer":
        return "ACCENT2"
    if s == "nodeport":
        return "INFO"
    if s == "externalname":
        return "WARNING"
    return "TEXT_MUTED"  # ClusterIP — the default, unremarkable case


class ServiceCardWidget(_CardBase):
    """One Service, as a card. `meta` matches what _populate_services builds:
    namespace, name, type, cluster_ip, external_ip, ports, age."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        stype = meta.get("type", "") or "ClusterIP"
        super().__init__(T.get(_svc_type_color_key(stype), T["TEXT_MUTED"]), parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        top.addWidget(_pill(stype, _svc_type_color_key(stype)))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        bottom.addWidget(_meta_label(f"Cluster-IP {meta.get('cluster_ip', '-') or '-'}"))
        bottom.addWidget(_meta_label(f"External-IP {meta.get('external_ip', '-') or '-'}"))

        ports_lbl = _meta_label(meta.get("ports", "-") or "-")
        ports_lbl.setToolTip(meta.get("ports", ""))
        ports_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(ports_lbl, 1)

        outer.addLayout(bottom)


def _ingress_accent(address: str) -> str:
    a = (address or "").strip().lower()
    if not a or a in ("-", "<none>", "<pending>"):
        return T["WARNING"]
    return T["SUCCESS"]


class IngressCardWidget(_CardBase):
    """One Ingress rule, as a card. `meta` matches what _populate_ingress
    builds: namespace, name, class, hosts, address, ports, age."""

    def __init__(self, meta: dict, show_namespace: bool, parent=None):
        super().__init__(_ingress_accent(meta.get("address", "")), parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(name_lbl, 1)

        cls = meta.get("class", "") or "-"
        if cls != "-":
            top.addWidget(_pill(cls, "INFO"))
        top.addWidget(_meta_label(f"⏱ {meta.get('age', '-')}"))
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        if show_namespace:
            bottom.addWidget(_chip(meta.get("namespace", "")))

        hosts_lbl = _meta_label(meta.get("hosts", "-") or "-")
        hosts_lbl.setToolTip(meta.get("hosts", ""))
        hosts_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(hosts_lbl, 1)

        bottom.addWidget(_meta_label(meta.get("address", "-") or "-"))
        bottom.addWidget(_meta_label(meta.get("ports", "-") or "-"))

        outer.addLayout(bottom)


class ConfigCardWidget(_CardBase):
    """One ConfigMap or Secret, as a card, for the Config & Secrets list.
    `meta` = {"name": str, "type": "configmap" | "secret"}.

    Replaces the old bare QListWidgetItem(name) rows — those had no icon,
    no breathing room between entries, and nothing visually distinguishing
    a Secret (sensitive) from a ConfigMap (plain), which is what made the
    list read as one dense, uniform column of text."""

    CARD_HEIGHT = 52

    def __init__(self, meta: dict, parent=None):
        is_secret = meta.get("type") == "secret"
        accent = T["WARNING"] if is_secret else T["INFO"]
        super().__init__(accent, parent)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 0, 14, 0)
        outer.setSpacing(10)

        icon_lbl = QLabel("🔐" if is_secret else "📦")
        icon_lbl.setStyleSheet("font-size: 17px; background: transparent;")
        outer.addWidget(icon_lbl)

        name_lbl = QLabel(meta.get("name", ""))
        name_lbl.setFont(monospace_font(13, bold=True))
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; background: transparent;")
        name_lbl.setToolTip(meta.get("name", ""))
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        outer.addWidget(name_lbl, 1)

        outer.addWidget(_pill("Secret" if is_secret else "ConfigMap",
                               "WARNING" if is_secret else "INFO"))


# ── Shared helpers ──────────────────────────────────────────────
def _ratio_ok(ready: str) -> bool:
    try:
        cur, total = ready.split("/")
        return cur == total
    except Exception:
        return False


def _status_accent(status: str) -> str:
    return T.get(_status_color_key(status), T["TEXT_MUTED"])