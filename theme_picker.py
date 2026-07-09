"""theme_picker.py — A visual theme-picker widget to replace the plain QComboBox."""

from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QPushButton,
    QLabel, QFrame, QApplication, QSizePolicy,
)
from PyQt5.QtCore import Qt, pyqtSignal, QPoint, QSize, QEvent
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QFontMetrics

from themes import THEMES, T


# ── Individual dot in the strip ───────────────────────────────────────────────

class _Dot(QWidget):
    def __init__(self, color: str, active=False, parent=None):
        super().__init__(parent)
        self.color = color
        self.active = active
        self.setFixedSize(14, 14)

    def set_active(self, v: bool):
        self.active = v
        self.update()

    def set_color(self, c: str):
        self.color = c
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.color)
        p.setBrush(QBrush(c))
        p.setPen(Qt.NoPen)
        p.drawEllipse(1, 1, 12, 12)
        if self.active:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.drawEllipse(0, 0, 13, 13)


# ── Colour swatch square inside each tile ────────────────────────────────────

class _Swatch(QWidget):
    def __init__(self, bg: str, accent: str, item: str, parent=None):
        super().__init__(parent)
        self.bg     = bg
        self.accent = accent
        self.item   = item
        self.setFixedSize(28, 36)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # background block
        p.setBrush(QBrush(QColor(self.bg)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, 28, 36, 4, 4)
        # accent bar
        p.setBrush(QBrush(QColor(self.accent)))
        p.drawRoundedRect(2, 2, 24, 12, 3, 3)
        # item block
        p.setBrush(QBrush(QColor(self.item)))
        p.drawRoundedRect(2, 17, 24, 9, 3, 3)
        # text stub lines
        p.setBrush(QBrush(QColor(self.item).lighter(130)))
        p.drawRoundedRect(2, 29, 14, 3, 1, 1)
        p.drawRoundedRect(18, 29, 8, 3, 1, 1)


# ── Single theme tile inside the panel ───────────────────────────────────────

class _ThemeTile(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, name: str, palette: dict, selected=False, parent=None):
        super().__init__(parent)
        self.name     = name
        self.palette  = palette
        self._selected = selected
        self._hovered  = False
        self.setFixedSize(86, 60)
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(3)

        self._swatch = _Swatch(palette["BG_DARK"], palette["ACCENT"], palette["BG_ITEM"])
        layout.addWidget(self._swatch, 0, Qt.AlignHCenter)

        lbl = QLabel(name)
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setStyleSheet(
            "color: {}; font-size: 9px; font-weight: 600; letter-spacing: 0.3px; "
            "background: transparent;".format(palette["TEXT_DIM"])
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        self._lbl = lbl

    def set_selected(self, v: bool):
        self._selected = v
        self.update()
        self._lbl.setStyleSheet(
            "color: {}; font-size: 9px; font-weight: 600; letter-spacing: 0.3px; "
            "background: transparent;".format(
                self.palette["TEXT_PRIMARY"] if v else self.palette["TEXT_DIM"]
            )
        )

    def enterEvent(self, _e):
        self._hovered = True
        self.update()

    def leaveEvent(self, _e):
        self._hovered = False
        self.update()

    def mousePressEvent(self, _e):
        self.clicked.emit(self.name)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        bg = QColor(self.palette["BG_PANEL"])
        p.setBrush(QBrush(bg))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, self.width(), self.height(), 8, 8)

        if self._selected:
            pen = QPen(QColor(self.palette["ACCENT"]), 1.5)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 7, 7)
        elif self._hovered:
            pen = QPen(QColor(self.palette["TEXT_MUTED"]), 0.8)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 7, 7)


# ── Floating panel ────────────────────────────────────────────────────────────

class _ThemePanel(QWidget):
    theme_selected = pyqtSignal(str)

    COLS = 3

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(1.0)
        self._tiles: dict[str, _ThemeTile] = {}
        self._current = ""
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("panelFrame")
        frame_layout = QGridLayout(frame)
        frame_layout.setContentsMargins(8, 8, 8, 8)
        frame_layout.setSpacing(6)

        names = list(THEMES.keys())
        for idx, name in enumerate(names):
            tile = _ThemeTile(name, THEMES[name])
            tile.clicked.connect(self._on_tile_clicked)
            row, col = divmod(idx, self.COLS)
            frame_layout.addWidget(tile, row, col)
            self._tiles[name] = tile

        frame.setStyleSheet("""
            QFrame#panelFrame {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 12px;
            }}
        """.format(bg=T["BG_DARK"], border=T["BORDER"]))

        outer.addWidget(frame)

    def select(self, name: str):
        if self._current and self._current in self._tiles:
            self._tiles[self._current].set_selected(False)
        self._current = name
        if name in self._tiles:
            self._tiles[name].set_selected(True)

    def refresh_theme(self):
        """Re-apply border/background colours after a theme change."""
        for name, tile in self._tiles.items():
            # keep tile's own palette — only the frame border changes
            pass
        frame = self.findChild(QFrame, "panelFrame")
        if frame:
            frame.setStyleSheet("""
                QFrame#panelFrame {{
                    background: {bg};
                    border: 1px solid {border};
                    border-radius: 12px;
                }}
            """.format(bg=T["BG_DARK"], border=T["BORDER"]))

    def _on_tile_clicked(self, name: str):
        self.select(name)
        self.theme_selected.emit(name)
        self.hide()


# ── The public ThemePicker strip widget ───────────────────────────────────────

class ThemePicker(QWidget):
    """
    Drop-in replacement for the theme QComboBox.
    Emits theme_changed(name: str) when a new theme is picked.
    """
    theme_changed = pyqtSignal(str)

    def __init__(self, current_theme: str, parent=None):
        super().__init__(parent)
        self._current = current_theme
        self._dots: list[_Dot] = []
        self._panel: _ThemePanel | None = None
        self._open = False

        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)
        self._build_strip()

    # ── Build the pill strip ──────────────────────────────────────────────────

    def _build_strip(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(5)

        names  = list(THEMES.keys())
        colors = [THEMES[n]["ACCENT"] for n in names]

        for i, (name, color) in enumerate(zip(names, colors)):
            dot = _Dot(color, active=(name == self._current))
            dot.setToolTip(name)
            layout.addWidget(dot)
            self._dots.append(dot)

        # chevron label
        self._chevron = QLabel("▾")
        self._chevron.setStyleSheet(
            "color: {}; font-size: 10px; background: transparent;".format(T["TEXT_MUTED"])
        )
        layout.addWidget(self._chevron)

    # ── Appearance ────────────────────────────────────────────────────────────

    def _border_color(self):
        return T["TEXT_MUTED"] if self._open else T["BORDER"]

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(T["BG_ITEM"])))
        p.setPen(QPen(QColor(self._border_color()), 1.0))
        r = self.rect().adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, 10, 10)

    def enterEvent(self, _e):
        self.update()

    def leaveEvent(self, _e):
        if not self._open:
            self.update()

    # ── Interaction ───────────────────────────────────────────────────────────

    def mousePressEvent(self, _e):
        if self._open:
            self._close_panel()
        else:
            self._open_panel()

    def _open_panel(self):
        if self._panel is None:
            self._panel = _ThemePanel()
            self._panel.theme_selected.connect(self._on_theme_selected)

        self._panel.select(self._current)

        # Position below the strip, right-aligned by default — then clamp
        # against the screen's available geometry so a panel wider than the
        # strip (very common: 3-column grid vs. a compact dot strip) can
        # never be pushed off-screen or outside the app window.
        gp = self.mapToGlobal(QPoint(0, self.height() + 4))
        pw = self._panel.sizeHint().width()
        ph = self._panel.sizeHint().height()
        x  = gp.x() + self.width() - pw

        screen = QApplication.screenAt(gp) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left() + 4, min(x, avail.right() - pw - 4))
            y = gp.y()
            if y + ph > avail.bottom():
                # Not enough room below — flip the panel above the strip.
                y = self.mapToGlobal(QPoint(0, 0)).y() - ph - 4
            gp.setY(y)
        gp.setX(x)

        self._panel.move(gp)
        self._panel.show()
        self._open = True
        self._chevron.setText("▴")
        self.update()

        # Close when panel is hidden (click-outside handled by Qt.Popup)
        self._panel.installEventFilter(self)

    def _close_panel(self):
        if self._panel:
            self._panel.hide()
        self._open = False
        self._chevron.setText("▾")
        self.update()

    def eventFilter(self, obj, event):
        if obj is self._panel and event.type() == QEvent.Hide:
            self._open = False
            self._chevron.setText("▾")
            self.update()
        return super().eventFilter(obj, event)

    def _on_theme_selected(self, name: str):
        self._current = name
        names = list(THEMES.keys())
        for i, (n, dot) in enumerate(zip(names, self._dots)):
            dot.set_color(THEMES[n]["ACCENT"])
            dot.set_active(n == name)
        self._close_panel()
        self.theme_changed.emit(name)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_theme(self, name: str):
        """Programmatically select a theme (e.g. on startup)."""
        self._on_theme_selected(name)

    def refresh_theme(self):
        """Call after a theme change so the strip border/bg updates."""
        self.update()
        if self._panel:
            self._panel.refresh_theme()