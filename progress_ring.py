"""progress_ring.py — Compact circular ("donut") progress indicator.

Replaces QProgressBar in the dashboard. A horizontal bar needs a wide
strip to show a single percentage; a ring conveys the same value in a
small square footprint, which matters most inside the Kubernetes node
table where the CPU/Memory columns are narrow.

The ring is a plain QWidget with a custom paintEvent — no external
dependency beyond PyQt5 — so it drops into any layout exactly like a
QProgressBar did (fixed size, `.setValue()`), just rendered as a hollow
circle (a "track") with a coloured arc drawn over it to the given
percentage, and an optional centred label.

Value changes are animated (see _anim below) rather than redrawn
instantly, so a dashboard refresh that jumps the underlying reading from
e.g. 15% to 35% eases the ring — and the centred number — smoothly
between the two instead of snapping in one frame.
"""

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import Qt, QRectF, QVariantAnimation, QEasingCurve
from PyQt5.QtGui import QPainter, QPen, QColor, QFont

from themes import T


class CircularProgress(QWidget):
    """A ring-shaped progress indicator with an optional centred label."""

    ANIM_MS = 450  # duration of the ease between old and new value

    def __init__(self, size=56, thickness=6, show_text=True,
                 font_size=11, suffix="%", parent=None):
        super().__init__(parent)
        self._size      = size
        self._thickness = thickness
        self._show_text = show_text
        self._font_size = font_size
        self._suffix    = suffix
        self._value     = 0.0   # the latest target value passed to setValue()
        self._display   = 0.0   # the value actually painted this frame
        self._color     = T["ACCENT"]
        self.setFixedSize(size, size)

        # Drives _display from its current position to the new target
        # every time setValue() is called, instead of jumping straight
        # there — paintEvent always draws _display, never _value directly.
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_step)

    # ── Public API ──────────────────────────────────────────
    def setValue(self, pct: float, color: str = None):
        """Update the displayed percentage (0-100) and, optionally, the
        ring's fill colour (e.g. green/amber/red past a threshold).
        Animates from whatever is currently on screen to the new value
        rather than redrawing instantly."""
        pct = max(0.0, min(100.0, pct))
        if color:
            self._color = color
        if pct == self._value:
            # Nothing to animate — but a colour-only change (e.g. crossing
            # a warning threshold at the same percentage) still needs a
            # repaint since setValue() wouldn't otherwise trigger one.
            if color:
                self.update()
            return

        self._value = pct
        self._anim.stop()
        self._anim.setStartValue(self._display)
        self._anim.setEndValue(pct)
        self._anim.start()

    def value(self) -> float:
        """The latest target value passed to setValue() (not the
        mid-animation value currently being painted)."""
        return self._value

    def setSuffix(self, suffix: str):
        """Text appended after the number in the centre label (default '%')."""
        self._suffix = suffix
        self.update()

    def refresh_theme(self):
        """Call after an app-wide theme switch so the track colour (which
        is read from the shared T palette) repaints correctly."""
        self.update()

    # ── Animation ─────────────────────────────────────────────
    def _on_anim_step(self, value):
        self._display = value
        self.update()

    # ── Painting ──────────────────────────────────────────────
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pad  = self._thickness / 2 + 1
        rect = QRectF(pad, pad, self._size - 2 * pad, self._size - 2 * pad)

        # Track — the hollow ring shown even at 0%, so the control never
        # looks "missing" while data is still loading.
        track_pen = QPen(QColor(T["BG_ITEM"]))
        track_pen.setWidth(self._thickness)
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(rect, 0, 360 * 16)

        # Value arc — starts at 12 o'clock, sweeps clockwise with the
        # percentage. Qt angles are in 1/16ths of a degree and increase
        # counter-clockwise, so a clockwise sweep needs a negative span.
        # Drawn from _display (the animated in-flight value), not _value,
        # so the arc eases toward the target instead of jumping to it.
        if self._display > 0:
            fg_pen = QPen(QColor(self._color))
            fg_pen.setWidth(self._thickness)
            fg_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(fg_pen)
            span = -int(360 * 16 * (self._display / 100.0))
            painter.drawArc(rect, 90 * 16, span)

        if self._show_text:
            painter.setPen(QColor(T["TEXT_PRIMARY"]))
            font = QFont()
            font.setPointSize(self._font_size)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter,
                              f"{self._display:.0f}{self._suffix}")