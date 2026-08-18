"""lock_screen.py — AppLockDialog (PIN entry, shown at launch and after
inactivity) and SetPinDialog (used from Settings to create/change the PIN).
"""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QDialogButtonBox, QApplication, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import Qt
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


class AppLockDialog(QDialog):
    """Blocking PIN-entry screen. exec_() returns QDialog.Accepted only
    once the correct PIN is entered. Escape and the window's close button
    are both disabled — dismissing this dialog any other way would defeat
    the point of a lock screen — so the only way out besides unlocking is
    the explicit "Quit" button."""

    def __init__(self, parent=None, title="EC2 Manager is locked"):
        super().__init__(parent)
        self.setWindowTitle("Locked")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setModal(True)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(380)
        apply_qss_to(self)
        self.setStyleSheet(self.styleSheet() + f"""
            QDialog {{
                background: {T['BG_PANEL']};
                border: 1px solid {T['BORDER']};
                border-radius: 20px;
            }}
        """)

        # Soft ambient shadow under the card so it reads as a floating
        # sheet instead of a flat rectangle stamped onto the screen —
        # works because the dialog itself is translucent, so the shadow
        # is visible outside the rounded corners rather than clipped
        # square by the window.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(48)
        shadow.setXOffset(0)
        shadow.setYOffset(14)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.setGraphicsEffect(shadow)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 34, 36, 28)
        lay.setSpacing(6)

        # Padlock badge — a soft accent-tinted circle behind the glyph
        # instead of a bare emoji floating on the background, so the
        # icon reads as a deliberate focal point rather than clip art.
        badge = QLabel("🔒")
        badge.setFixedSize(64, 64)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFont(QFont("Segoe UI Emoji", 26))
        badge.setStyleSheet(f"""
            background: {_alpha(T['ACCENT'], 0.16)};
            border: 1px solid {_alpha(T['ACCENT'], 0.35)};
            border-radius: 32px;
        """)
        badge_row = QHBoxLayout()
        badge_row.addStretch()
        badge_row.addWidget(badge)
        badge_row.addStretch()
        lay.addLayout(badge_row)
        lay.addSpacing(14)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 17px; font-weight: 700; "
            f"letter-spacing: 0.2px;"
        )
        lay.addWidget(title_lbl)

        sub_lbl = QLabel("Enter your PIN to continue")
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12.5px; margin-top: 2px;")
        lay.addWidget(sub_lbl)

        lay.addSpacing(20)

        self.pin_input = QLineEdit()
        self.pin_input.setEchoMode(QLineEdit.Password)
        self.pin_input.setAlignment(Qt.AlignCenter)
        self.pin_input.setPlaceholderText("• • • •")
        self.pin_input.setFixedHeight(46)
        self.pin_input.returnPressed.connect(self._try_unlock)
        self.pin_input.setStyleSheet(f"""
            QLineEdit {{
                background: {T['BG_ITEM']};
                color: {T['TEXT_PRIMARY']};
                border: 1.5px solid {T['BORDER']};
                border-radius: 12px;
                padding: 4px 14px;
                font-size: 18px;
                letter-spacing: 4px;
            }}
            QLineEdit:focus {{ border-color: {T['ACCENT']}; }}
        """)
        lay.addWidget(self.pin_input)

        self.error_lbl = QLabel("")
        self.error_lbl.setAlignment(Qt.AlignCenter)
        self.error_lbl.setFixedHeight(20)
        self.error_lbl.setStyleSheet(
            f"color: {T['DANGER']}; font-size: 12px; font-weight: 600; margin-top: 4px;"
        )
        lay.addWidget(self.error_lbl)

        lay.addSpacing(6)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        quit_btn = QPushButton("Quit")
        quit_btn.setFixedHeight(42)
        quit_btn.setCursor(Qt.PointingHandCursor)
        quit_btn.clicked.connect(self._quit_app)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {T['DANGER']};
                border: 1.5px solid {T['DANGER']};
                border-radius: 21px;
                font-weight: 600;
                font-size: 13px;
            }}
            QPushButton:hover  {{ background: {_alpha(T['DANGER'], 0.12)}; }}
            QPushButton:pressed {{ background: {_alpha(T['DANGER'], 0.22)}; }}
        """)
        btn_row.addWidget(quit_btn)

        unlock_btn = QPushButton("Unlock")
        unlock_btn.setFixedHeight(42)
        unlock_btn.setCursor(Qt.PointingHandCursor)
        unlock_btn.clicked.connect(self._try_unlock)
        unlock_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {T['ACCENT']}, stop:1 {T['ACCENT2']});
                color: white;
                border: none;
                border-radius: 21px;
                font-weight: 700;
                font-size: 13px;
            }}
            QPushButton:hover  {{ background: {T['ACCENT2']}; }}
            QPushButton:pressed {{ background: {T['ACCENT']}; }}
        """)
        btn_row.addWidget(unlock_btn, 1)
        lay.addLayout(btn_row)

        self.pin_input.setFocus()

    # ── Lock-screen hardening ───────────────────────────────────
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # No 'X' escape hatch — only unlocking or the Quit button leave
        # this dialog.
        event.ignore()

    # ── Behavior ──────────────────────────────────────────────
    def _try_unlock(self):
        settings = get_lock_settings()
        pin = self.pin_input.text()
        if verify_pin(pin, settings["salt"], settings["pin_hash"]):
            self.accept()
            return
        self.error_lbl.setText("Incorrect PIN — try again.")
        self.pin_input.clear()
        self.pin_input.setFocus()

    def _quit_app(self):
        QApplication.instance().quit()


class SetPinDialog(QDialog):
    """Two-field PIN entry (new + confirm), used from Settings when
    turning the lock on for the first time or changing an existing PIN."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set PIN")
        self.setFixedWidth(340)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(8)

        title = QLabel("Set a PIN")
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700;")
        lay.addWidget(title)

        lay.addWidget(QLabel("New PIN  (4+ characters)"))
        self.pin1 = QLineEdit()
        self.pin1.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.pin1)

        lay.addWidget(QLabel("Confirm PIN"))
        self.pin2 = QLineEdit()
        self.pin2.setEchoMode(QLineEdit.Password)
        self.pin2.returnPressed.connect(self._on_ok)
        lay.addWidget(self.pin2)

        self.error_lbl = QLabel("")
        self.error_lbl.setFixedHeight(16)
        self.error_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px;")
        lay.addWidget(self.error_lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Save")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._pin_value = None
        self.pin1.setFocus()

    def _on_ok(self):
        p1, p2 = self.pin1.text(), self.pin2.text()
        if len(p1) < 4:
            self.error_lbl.setText("PIN must be at least 4 characters.")
            return
        if p1 != p2:
            self.error_lbl.setText("PINs don't match.")
            self.pin2.clear()
            self.pin2.setFocus()
            return
        self._pin_value = p1
        self.accept()

    def pin_value(self):
        return self._pin_value