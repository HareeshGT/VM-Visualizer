"""sidebar.py — Quick-access Sidebar widget."""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel
from PyQt5.QtCore import Qt, pyqtSignal

from themes import T


class Sidebar(QWidget):
    navigate = pyqtSignal(str)

    QUICK_ALWAYS = [
        ("🏠  Home", ""), ("📁  Root", "/"), ("📂  Tmp", "/tmp"),
    ]
    QUICK_PROBE = [
        ("📂  Etc",          "/etc"),
        ("📂  Var",          "/var"),
        ("📂  Opt",          "/opt"),
        ("📂  Srv",          "/srv"),
        ("📂  Usr/local",    "/usr/local"),
        ("📂  Applications", "/Applications"),
        ("📂  Library",      "/Library"),
        ("📂  Users",        "/Users"),
        ("📂  Volumes",      "/Volumes"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(180)
        self._refresh_bg()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 8, 0, 8)
        self._layout.setSpacing(0)

        lbl = QLabel("QUICK ACCESS")
        lbl.setObjectName("section_title")
        self._layout.addWidget(lbl)

        for text, path in self.QUICK_ALWAYS:
            self._add_btn(text, path)

        self._probe_buttons = []
        self._layout.addStretch()

    # ── Internal helpers ──────────────────────────────────────
    def _refresh_bg(self):
        self.setStyleSheet(f"background: {T['BG_SIDEBAR']};")

    def _btn_style(self) -> str:
        return f"""
            QPushButton {{
                background: transparent; color: {T['TEXT_DIM']};
                border: none; border-radius: 0; text-align: left;
                padding: 8px 16px; font-size: 13px;
            }}
            QPushButton:hover   {{ background: {T['BG_HOVER']}; color: {T['TEXT_PRIMARY']}; }}
            QPushButton:pressed {{ background: {T['BG_ITEM_SEL']}; }}
        """

    def _add_btn(self, text: str, path: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setStyleSheet(self._btn_style())
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _, p=path: self.navigate.emit(p))
        self._layout.insertWidget(self._layout.count() - 1, btn)
        return btn

    # ── Public API ────────────────────────────────────────────
    def populate_remote(self, sftp):
        """Probe common paths on the remote and add buttons for those that exist."""
        self._clear_probe_buttons()
        for text, path in self.QUICK_PROBE:
            try:
                sftp.stat(path)
                btn = self._add_btn(text, path)
                self._probe_buttons.append(btn)
            except Exception:
                pass

    def clear_remote(self):
        self._clear_probe_buttons()

    def refresh_theme(self):
        self._refresh_bg()
        style = self._btn_style()
        for i in range(self._layout.count()):
            w = self._layout.itemAt(i).widget()
            if isinstance(w, QPushButton):
                w.setStyleSheet(style)

    # ── Private ───────────────────────────────────────────────
    def _clear_probe_buttons(self):
        for btn in self._probe_buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._probe_buttons = []