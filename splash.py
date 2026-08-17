"""splash.py — Frameless opening-animation splash screen.

Shown before EC2FileManager is constructed. Draws a ring (visually
consistent with progress_ring.py's CircularProgress) that sweeps in,
reveals a server glyph at its centre, then fades in the app name below
it. Colours are read from themes.T at construction time, so the splash
always matches whatever theme was loaded from settings.json.

Usage (see main.py):
    splash = SplashScreen()
    splash.show()
    splash.finished.connect(launch_main_window)
    splash.start()
"""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QGraphicsOpacityEffect
from PyQt5.QtCore import (
    Qt, QRectF, QRect, QVariantAnimation, QPropertyAnimation, QEasingCurve,
    QParallelAnimationGroup, QTimer, pyqtSignal,
)
from PyQt5.QtGui import QPainter, QPen, QColor, QFont

from themes import T


class _RingGlyph(QWidget):
    """Ring + centred server glyph. Owns two animatable values: the ring's
    sweep angle (0-360) and the glyph's opacity (0-1), painted together so
    the glyph only appears once the ring has drawn itself in."""

    def __init__(self, size=96, thickness=6, parent=None):
        super().__init__(parent)
        self._size      = size
        self._thickness = thickness
        self._sweep     = 0.0
        self._glyph_op  = 0.0
        self.setFixedSize(size, size)

    def set_sweep(self, deg: float):
        self._sweep = deg
        self.update()

    def set_glyph_opacity(self, op: float):
        self._glyph_op = op
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        pad  = self._thickness / 2 + 1
        rect = QRectF(pad, pad, self._size - 2 * pad, self._size - 2 * pad)

        track_pen = QPen(QColor(T["BG_ITEM"]))
        track_pen.setWidth(self._thickness)
        track_pen.setCapStyle(Qt.RoundCap)
        p.setPen(track_pen)
        p.drawArc(rect, 0, 360 * 16)

        if self._sweep > 0:
            fg_pen = QPen(QColor(T["ACCENT"]))
            fg_pen.setWidth(self._thickness)
            fg_pen.setCapStyle(Qt.RoundCap)
            p.setPen(fg_pen)
            span = -int(360 * 16 * (self._sweep / 360.0))
            p.drawArc(rect, 90 * 16, span)

        if self._glyph_op > 0:
            color = QColor(T["TEXT_PRIMARY"])
            color.setAlphaF(self._glyph_op)
            pen = QPen(color)
            pen.setWidthF(1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)

            cx, cy = self._size / 2, self._size / 2
            gw, gh = 30, 17
            gx, gy = cx - gw / 2, cy - gh / 2 + 3
            p.drawRoundedRect(QRectF(gx, gy, gw, gh), 3, 3)

            lid = QRectF(gx + 4, gy - 5, gw - 8, 7)
            p.drawRoundedRect(lid, 2, 2)

            dot_color = QColor(T["ACCENT"])
            dot_color.setAlphaF(self._glyph_op)
            p.setPen(Qt.NoPen)
            p.setBrush(dot_color)
            dot_y = gy + gh / 2
            for i in range(3):
                dot_x = gx + 6 + i * 9
                p.drawEllipse(QRectF(dot_x - 1.4, dot_y - 1.4, 2.8, 2.8))


class SplashScreen(QWidget):
    """Frameless, translucent opening screen. Call start() once shown;
    emits finished() when the whole sequence (ring, glyph, text, hold)
    has completed, at which point the caller should close it."""

    finished = pyqtSignal()

    RING_MS  = 900
    FADE_MS  = 400
    HOLD_MS  = 500
    ZOOM_MS  = 420

    def __init__(self, app_name="EC2 MANAGER", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(320, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 30, 0, 0)
        layout.setSpacing(18)
        layout.setAlignment(Qt.AlignHCenter)

        self._panel = QWidget(self)
        self._panel.setGeometry(0, 0, 320, 260)
        self._panel.setStyleSheet(
            f"background: {T['BG_DARK']}; border: 1px solid {T['BORDER']}; border-radius: 18px;"
        )
        self._panel.lower()

        self._ring = _RingGlyph(size=96, thickness=6)
        layout.addWidget(self._ring, 0, Qt.AlignHCenter)

        self._label = QLabel(app_name)
        self._label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; background: transparent;")
        self._text_effect = QGraphicsOpacityEffect(self._label)
        self._text_effect.setOpacity(0.0)
        self._label.setGraphicsEffect(self._text_effect)
        layout.addWidget(self._label)

        self._center_on_screen()
        self._build_animations()
        self._zoom_group = None

    # ── Layout ────────────────────────────────────────────────
    def resizeEvent(self, event):
        # Keeps the rounded backing panel filling the window as it grows
        # during zoom_into(), instead of staying pinned at its original
        # 320x260 size while the window resizes around it.
        self._panel.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def _center_on_screen(self):
        from PyQt5.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + (geo.height() - self.height()) // 2
            self.move(x, y)

    # ── Animation ─────────────────────────────────────────────
    def _build_animations(self):
        self._ring_anim = QVariantAnimation(self)
        self._ring_anim.setStartValue(0.0)
        self._ring_anim.setEndValue(360.0)
        self._ring_anim.setDuration(self.RING_MS)
        self._ring_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._ring_anim.valueChanged.connect(self._ring.set_sweep)

        self._glyph_anim = QVariantAnimation(self)
        self._glyph_anim.setStartValue(0.0)
        self._glyph_anim.setEndValue(1.0)
        self._glyph_anim.setDuration(self.FADE_MS)
        self._glyph_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._glyph_anim.valueChanged.connect(self._ring.set_glyph_opacity)

        self._text_anim = QPropertyAnimation(self._text_effect, b"opacity", self)
        self._text_anim.setStartValue(0.0)
        self._text_anim.setEndValue(1.0)
        self._text_anim.setDuration(self.FADE_MS)
        self._text_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._ring_anim.finished.connect(self._on_ring_finished)

    def _on_ring_finished(self):
        self._glyph_anim.start()
        self._text_anim.start()
        QTimer.singleShot(self.FADE_MS + self.HOLD_MS, self.finished.emit)

    # ── Public API ────────────────────────────────────────────
    def start(self):
        self._ring_anim.start()

    def zoom_into(self, target_rect: QRect, on_done, companions=None):
        """Fade the splash out in place while any `companions` (e.g. the
        real window's windowOpacity fading 0→1) run in the same
        QParallelAnimationGroup — a plain crossfade with no geometry
        change, so nothing can drift toward a corner. `target_rect` is
        unused; kept as a parameter so callers don't need to change.
        """
        fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        fade_anim.setStartValue(1.0)
        fade_anim.setEndValue(0.0)
        fade_anim.setDuration(self.ZOOM_MS)
        fade_anim.setEasingCurve(QEasingCurve.InOutCubic)

        self._zoom_group = QParallelAnimationGroup(self)
        self._zoom_group.addAnimation(fade_anim)
        for anim in (companions or []):
            self._zoom_group.addAnimation(anim)
        self._zoom_group.finished.connect(on_done)
        self._zoom_group.start()