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