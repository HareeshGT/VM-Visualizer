"""lock_screen.py — AppLockDialog (PIN entry, shown at launch and after
inactivity) and SetPinDialog (used from Settings to create/change the PIN).
"""

from PyQt5.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QDialogButtonBox,
    QApplication,
    QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPoint, pyqtProperty
from PyQt5.QtGui import QFont, QColor

from themes import T, apply_qss_to
from security import verify_pin, get_lock_settings


def _alpha(hex_color: str, opacity: float) -> str:
    """'#a855f7' + 0.16 -> 'rgba(168,85,247,0.16)' — lets the badge/hover
    tints sit on top of any theme's accent or danger colour without a
    hardcoded value that would clash on lighter/darker palettes."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{opacity})"


def _soft_shadow(widget, blur=40, y=10, alpha=140):
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setXOffset(0)
    shadow.setYOffset(y)
    shadow.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(shadow)
    return shadow


class _PillField(QWidget):
    """A rounded 'pill' container that hosts a leading glyph and a
    QLineEdit with no border of its own — the wrapper owns the border,
    so focus/hover states read as one continuous shape instead of a
    plain boxed input."""

    def __init__(self, glyph, placeholder, parent=None):
        super().__init__(parent)
        self.setObjectName("pillField")
        self._focused = False

        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(10)

        glyph_lbl = QLabel(glyph)
        glyph_lbl.setFixedWidth(18)
        glyph_lbl.setAlignment(Qt.AlignCenter)
        glyph_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 15px; background: transparent; border: none;")
        row.addWidget(glyph_lbl)
        self._glyph_lbl = glyph_lbl

        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setAlignment(Qt.AlignLeft)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFixedHeight(52)
        self.edit.setMaxLength(64)
        self.edit.setFrame(False)
        self.edit.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                color: {T['TEXT_PRIMARY']};
                font-size: 17px;
                font-weight: 600;
                letter-spacing: 4px;
                selection-background-color: {T['ACCENT']};
            }}
        """)
        self.edit.installEventFilter(self)
        row.addWidget(self.edit)

        self._apply_style()

    def eventFilter(self, obj, event):
        if obj is self.edit:
            if event.type() == event.FocusIn:
                self._focused = True
                self._apply_style()
            elif event.type() == event.FocusOut:
                self._focused = False
                self._apply_style()
        return super().eventFilter(obj, event)

    def _apply_style(self):
        if self._focused:
            border = f"2px solid {T['ACCENT']}"
            bg = _alpha(T['ACCENT'], 0.07)
            pad_fix = 0
        else:
            border = f"1.5px solid {T['BORDER']}"
            bg = T['BG_ITEM']
            pad_fix = 1
        self.setStyleSheet(f"""
            QWidget#pillField {{
                background: {bg};
                border: {border};
                border-radius: 16px;
            }}
        """)
        self.layout().setContentsMargins(18 + pad_fix, 0, 18 + pad_fix, 0)

    def text(self):
        return self.edit.text()

    def clear(self):
        self.edit.clear()

    def setFocus(self):
        self.edit.setFocus()

    def returnPressed(self):
        return self.edit.returnPressed


class AppLockDialog(QDialog):
    """Modern blocking PIN-entry lock screen."""

    def __init__(self, parent=None, title="EC2 Manager is locked"):
        super().__init__(parent)

        self.setWindowTitle("Locked")
        self.setWindowFlags(
            Qt.Dialog |
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint
        )
        self.setModal(True)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.setFixedSize(420, 480)

        apply_qss_to(self)

        self.setStyleSheet("""
            QDialog {
                background: transparent;
                border: none;
            }
        """)

        # ---------------------------------------------------------
        # Main card
        # ---------------------------------------------------------
        self.card = QWidget(self)
        self.card.setObjectName("lockCard")
        self.card.setGeometry(0, 0, self.width(), self.height())

        self.card.setStyleSheet(f"""
            QWidget#lockCard {{
                background: qlineargradient(
                    x1: 0, y1: 0,
                    x2: 0, y2: 1,
                    stop: 0 {_alpha(T['ACCENT'], 0.05)},
                    stop: 0.35 {T['BG_PANEL']},
                    stop: 1 {T['BG_PANEL']}
                );
                border: 1px solid {_alpha(T['BORDER'], 0.9)};
                border-radius: 26px;
            }}
        """)

        _soft_shadow(self.card, blur=60, y=20, alpha=170)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(40, 38, 40, 30)
        lay.setSpacing(0)

        # ---------------------------------------------------------
        # Lock icon — layered ring + badge for depth
        # ---------------------------------------------------------
        ring = QLabel()
        ring.setFixedSize(84, 84)
        ring.setAlignment(Qt.AlignCenter)
        ring.setStyleSheet(f"""
            QLabel {{
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 {_alpha(T['ACCENT'], 0.22)},
                    stop: 1 {_alpha(T['ACCENT2'], 0.10)}
                );
                border: 1px solid {_alpha(T['ACCENT'], 0.38)};
                border-radius: 42px;
            }}
        """)

        badge = QLabel("🔒", ring)
        badge.setFixedSize(84, 84)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFont(QFont("Segoe UI Emoji", 28))
        badge.setStyleSheet("background: transparent; border: none;")

        badge_row = QHBoxLayout()
        badge_row.addStretch()
        badge_row.addWidget(ring)
        badge_row.addStretch()

        lay.addLayout(badge_row)
        lay.addSpacing(20)

        # ---------------------------------------------------------
        # Title
        # ---------------------------------------------------------
        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setWordWrap(True)

        title_lbl.setStyleSheet(f"""
            QLabel {{
                color: {T['TEXT_PRIMARY']};
                font-size: 20px;
                font-weight: 700;
                background: transparent;
                border: none;
            }}
        """)

        lay.addWidget(title_lbl)
        lay.addSpacing(6)

        # ---------------------------------------------------------
        # Subtitle
        # ---------------------------------------------------------
        sub_lbl = QLabel("Enter your PIN to continue")
        sub_lbl.setAlignment(Qt.AlignCenter)

        sub_lbl.setStyleSheet(f"""
            QLabel {{
                color: {T['TEXT_DIM']};
                font-size: 13px;
                background: transparent;
                border: none;
            }}
        """)

        lay.addWidget(sub_lbl)
        lay.addSpacing(26)

        # ---------------------------------------------------------
        # PIN input (pill field w/ leading glyph)
        # ---------------------------------------------------------
        self.pin_field = _PillField("🔑", "Enter PIN", self.card)
        self.pin_field.returnPressed().connect(self._try_unlock)
        lay.addWidget(self.pin_field)
        lay.addSpacing(6)

        # ---------------------------------------------------------
        # Error / status message
        # ---------------------------------------------------------
        self.error_lbl = QLabel("")
        self.error_lbl.setAlignment(Qt.AlignCenter)
        self.error_lbl.setFixedHeight(22)

        self.error_lbl.setStyleSheet(f"""
            QLabel {{
                color: {T['DANGER']};
                font-size: 12px;
                font-weight: 600;
                background: transparent;
                border: none;
            }}
        """)

        lay.addWidget(self.error_lbl)
        lay.addSpacing(8)

        # ---------------------------------------------------------
        # Buttons
        # ---------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        # Quit
        quit_btn = QPushButton("Quit")
        quit_btn.setFixedHeight(48)
        quit_btn.setCursor(Qt.PointingHandCursor)
        quit_btn.clicked.connect(self._quit_app)

        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {T['TEXT_DIM']};
                border: 1.5px solid {T['BORDER']};
                border-radius: 24px;
                font-size: 13px;
                font-weight: 600;
                padding: 0 22px;
            }}

            QPushButton:hover {{
                color: {T['DANGER']};
                border-color: {T['DANGER']};
                background: {_alpha(T['DANGER'], 0.08)};
            }}

            QPushButton:pressed {{
                background: {_alpha(T['DANGER'], 0.16)};
            }}
        """)

        btn_row.addWidget(quit_btn)

        # Unlock
        unlock_btn = QPushButton("Unlock")
        unlock_btn.setFixedHeight(48)
        unlock_btn.setCursor(Qt.PointingHandCursor)
        unlock_btn.clicked.connect(self._try_unlock)

        unlock_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(
                    x1: 0, y1: 0,
                    x2: 1, y2: 0,
                    stop: 0 {T['ACCENT']},
                    stop: 1 {T['ACCENT2']}
                );
                color: white;
                border: none;
                border-radius: 24px;
                font-size: 13px;
                font-weight: 700;
            }}

            QPushButton:hover {{
                background: qlineargradient(
                    x1: 0, y1: 0,
                    x2: 1, y2: 0,
                    stop: 0 {T['ACCENT2']},
                    stop: 1 {T['ACCENT2']}
                );
            }}

            QPushButton:pressed {{
                background: {T['ACCENT']};
            }}
        """)

        btn_row.addWidget(unlock_btn, 1)

        lay.addLayout(btn_row)

        # ---------------------------------------------------------
        # Bottom hint
        # ---------------------------------------------------------
        lay.addStretch()

        hint = QLabel("🛡  Your session is protected")
        hint.setAlignment(Qt.AlignCenter)

        hint.setStyleSheet(f"""
            QLabel {{
                color: {T['TEXT_DIM']};
                font-size: 11px;
                background: transparent;
                border: none;
            }}
        """)

        lay.addWidget(hint)

        self.pin_field.setFocus()
        # No fade-in / windowOpacity animation on this dialog on purpose.
        # It's a frameless, WA_TranslucentBackground, always-on-top modal
        # top-level window — animating its opacity (via either
        # QGraphicsOpacityEffect or windowOpacity) has been a repeated
        # source of "dialog flashes in then goes fully blank while still
        # modal" on macOS/Cocoa, which is much worse than losing a 180ms
        # cosmetic fade. If a fade-in is wanted later, it should be done
        # by fading the *card* widget's own background alpha in via a
        # QSS/paint-based approach instead of touching windowOpacity or
        # QGraphicsOpacityEffect on the top-level window.

    # -------------------------------------------------------------
    # Lock-screen hardening
    # -------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return

        super().keyPressEvent(event)

    def closeEvent(self, event):
        # Prevent the window manager from bypassing the lock.
        event.ignore()

    # -------------------------------------------------------------
    # Behavior
    # -------------------------------------------------------------

    def _try_unlock(self):
        settings = get_lock_settings()
        pin = self.pin_field.text()

        if verify_pin(
            pin,
            settings["salt"],
            settings["pin_hash"]
        ):
            self.accept()
            return

        self.error_lbl.setText("Incorrect PIN. Please try again.")
        self.pin_field.clear()
        self.pin_field.setFocus()
        self._shake_card()

    def _shake_card(self):
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(320)
        start = self.pos()
        offsets = [0, -10, 8, -6, 4, 0]
        steps = len(offsets) - 1
        for i, off in enumerate(offsets):
            anim.setKeyValueAt(i / steps, QPoint(start.x() + off, start.y()))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._shake_anim = anim  # keep a reference alive

    def _quit_app(self):
        QApplication.instance().quit()


class SetPinDialog(QDialog):
    """Two-field PIN entry (new + confirm), used from Settings when
    turning the lock on for the first time or changing an existing PIN."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set PIN")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(360)
        apply_qss_to(self)

        self.setStyleSheet("QDialog { background: transparent; border: none; }")

        card = QWidget(self)
        card.setObjectName("setPinCard")
        card.setStyleSheet(f"""
            QWidget#setPinCard {{
                background: {T['BG_PANEL']};
                border: 1px solid {_alpha(T['BORDER'], 0.9)};
                border-radius: 20px;
            }}
        """)
        _soft_shadow(card, blur=45, y=14, alpha=150)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(4)

        header_row = QHBoxLayout()
        icon_lbl = QLabel("🔑")
        icon_lbl.setFixedSize(40, 40)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet(f"""
            QLabel {{
                background: {_alpha(T['ACCENT'], 0.14)};
                border: 1px solid {_alpha(T['ACCENT'], 0.32)};
                border-radius: 20px;
                font-size: 17px;
            }}
        """)
        header_row.addWidget(icon_lbl)
        header_row.addSpacing(10)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel("Set a PIN")
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 16px; font-weight: 700; background: transparent; border: none;")
        subtitle = QLabel("Used to lock EC2 Manager when idle")
        subtitle.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px; background: transparent; border: none;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_row.addLayout(title_box)
        header_row.addStretch()

        lay.addLayout(header_row)
        lay.addSpacing(22)

        def field_label(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11.5px; font-weight: 600; background: transparent; border: none;")
            return lbl

        lay.addWidget(field_label("NEW PIN  ·  4+ CHARACTERS"))
        lay.addSpacing(6)
        self.pin1_field = _PillField("●", "New PIN", card)
        lay.addWidget(self.pin1_field)
        lay.addSpacing(16)

        lay.addWidget(field_label("CONFIRM PIN"))
        lay.addSpacing(6)
        self.pin2_field = _PillField("●", "Confirm PIN", card)
        self.pin2_field.returnPressed().connect(self._on_ok)
        lay.addWidget(self.pin2_field)
        lay.addSpacing(10)

        self.error_lbl = QLabel("")
        self.error_lbl.setFixedHeight(18)
        self.error_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        lay.addWidget(self.error_lbl)
        lay.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(44)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {T['TEXT_DIM']};
                border: 1.5px solid {T['BORDER']};
                border-radius: 22px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {_alpha(T['ACCENT'], 0.6)};
                color: {T['TEXT_PRIMARY']};
            }}
        """)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setFixedHeight(44)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._on_ok)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 {T['ACCENT']}, stop: 1 {T['ACCENT2']}
                );
                color: white;
                border: none;
                border-radius: 22px;
                font-size: 13px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background: {T['ACCENT2']};
            }}
            QPushButton:pressed {{
                background: {T['ACCENT']};
            }}
        """)
        btn_row.addWidget(save_btn, 1)

        lay.addLayout(btn_row)

        self._pin_value = None
        self.pin1_field.setFocus()

        self.adjustSize()

    def _on_ok(self):
        p1, p2 = self.pin1_field.text(), self.pin2_field.text()
        if len(p1) < 4:
            self.error_lbl.setText("PIN must be at least 4 characters.")
            return
        if p1 != p2:
            self.error_lbl.setText("PINs don't match.")
            self.pin2_field.clear()
            self.pin2_field.setFocus()
            return
        self._pin_value = p1
        self.accept()

    def pin_value(self):
        return self._pin_value