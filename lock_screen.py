"""lock_screen.py — AppLockDialog (PIN entry, shown at launch and after
inactivity) and SetPinDialog (used from Settings to create/change the PIN).
"""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QDialogButtonBox, QApplication,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from themes import T, apply_qss_to
from security import verify_pin, get_lock_settings


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
        self.setFixedWidth(360)
        apply_qss_to(self)
        self.setStyleSheet(self.styleSheet() + f"""
            QDialog {{
                background: {T['BG_DARK']};
                border: 1px solid {T['BORDER']};
                border-radius: 14px;
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(8)

        icon = QLabel("🔒")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFont(QFont("Segoe UI Emoji", 30))
        lay.addWidget(icon)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700;")
        lay.addWidget(title_lbl)

        sub_lbl = QLabel("Enter your PIN to continue")
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        lay.addWidget(sub_lbl)

        lay.addSpacing(8)

        self.pin_input = QLineEdit()
        self.pin_input.setEchoMode(QLineEdit.Password)
        self.pin_input.setAlignment(Qt.AlignCenter)
        self.pin_input.setPlaceholderText("PIN")
        self.pin_input.returnPressed.connect(self._try_unlock)
        lay.addWidget(self.pin_input)

        self.error_lbl = QLabel("")
        self.error_lbl.setAlignment(Qt.AlignCenter)
        self.error_lbl.setFixedHeight(16)
        self.error_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px;")
        lay.addWidget(self.error_lbl)

        lay.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("danger")
        quit_btn.clicked.connect(self._quit_app)
        btn_row.addWidget(quit_btn)

        unlock_btn = QPushButton("Unlock")
        unlock_btn.setObjectName("primary")
        unlock_btn.clicked.connect(self._try_unlock)
        btn_row.addWidget(unlock_btn)
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