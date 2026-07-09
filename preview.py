"""preview.py — PreviewPane widget shown in the right column of the file manager."""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame, QTextEdit
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from themes import T
from utils import icon_for, size_fmt


class PreviewPane(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumWidth(240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QLabel("  Preview")
        self.header.setFixedHeight(36)
        layout.addWidget(self.header)

        self.icon_lbl = QLabel()
        self.icon_lbl.setAlignment(Qt.AlignCenter)
        self.icon_lbl.setFont(QFont("Segoe UI Emoji", 40))
        self.icon_lbl.setFixedHeight(80)
        layout.addWidget(self.icon_lbl)

        self.name_lbl = QLabel()
        self.name_lbl.setAlignment(Qt.AlignCenter)
        self.name_lbl.setWordWrap(True)
        layout.addWidget(self.name_lbl)

        self.meta_lbl = QLabel()
        self.meta_lbl.setAlignment(Qt.AlignCenter)
        self.meta_lbl.setWordWrap(True)
        layout.addWidget(self.meta_lbl)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        layout.addWidget(divider)

        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setPlaceholderText("Select a file to preview…")
        layout.addWidget(self.text_preview)

        self._apply_styles()

    # ── Styling ───────────────────────────────────────────────
    def _apply_styles(self):
        self.header.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; "
            f"border-bottom: 1px solid {T['BORDER']}; font-size: 11px; "
            f"font-weight: 700; letter-spacing: 1px; padding-left: 12px;"
        )
        self.icon_lbl.setStyleSheet(f"background: {T['BG_PANEL']};")
        self.name_lbl.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_PRIMARY']}; "
            f"font-weight: 600; font-size: 13px; padding: 4px 12px;"
        )
        self.meta_lbl.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; "
            f"font-size: 11px; padding: 2px 12px 8px 12px;"
        )

    def refresh_theme(self):
        self._apply_styles()

    # ── Public API ────────────────────────────────────────────
    def show_entry(self, name: str, kind: str, size: int = None, perms: str = ""):
        self.icon_lbl.setText(icon_for(kind))
        self.name_lbl.setText(name)
        meta = []
        if kind == "folder":
            meta.append("Directory")
        else:
            if size is not None:
                meta.append(size_fmt(size))
            meta.append(kind.capitalize())
        if perms:
            meta.append(perms)
        self.meta_lbl.setText("  ·  ".join(meta))
        self.text_preview.clear()
        self.text_preview.setPlaceholderText("Select a file to preview…")

    def show_text(self, content: str):
        self.text_preview.setPlainText(content)

    def clear(self):
        self.icon_lbl.setText("")
        self.name_lbl.setText("")
        self.meta_lbl.setText("")
        self.text_preview.clear()