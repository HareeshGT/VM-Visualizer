"""file_widgets.py — Per-item widgets for the file list (row and grid tile views)."""

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from themes import T
from utils import icon_for, size_fmt


class FileRowWidget(QWidget):
    """Compact single-row entry used in list view."""

    COL_ICON = 28
    COL_SIZE = 72
    COL_TYPE = 80

    def __init__(self, meta: dict, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")
        self.setFixedHeight(34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(0)

        icon_lbl = QLabel(icon_for(meta["kind"]))
        icon_lbl.setFixedWidth(self.COL_ICON)
        icon_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(icon_lbl)

        name_lbl = QLabel(meta["name"])
        name_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        layout.addWidget(name_lbl, 1)

        size_text = "" if meta["is_dir"] else size_fmt(meta["size"])
        size_lbl  = QLabel(size_text)
        size_lbl.setFixedWidth(self.COL_SIZE)
        size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        layout.addWidget(size_lbl)

        layout.addSpacing(8)

        type_text = "Folder" if meta["is_dir"] else meta["kind"].capitalize()
        type_lbl  = QLabel(type_text)
        type_lbl.setFixedWidth(self.COL_TYPE)
        type_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        type_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        layout.addWidget(type_lbl)


class FileGridWidget(QWidget):
    """Square tile used in grid (icon) view."""

    TILE_W = 96
    TILE_H = 100

    def __init__(self, meta: dict, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 6)
        layout.setSpacing(2)

        icon_lbl = QLabel(icon_for(meta["kind"]))
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setFont(QFont("Segoe UI Emoji", 26))
        layout.addWidget(icon_lbl)

        name_lbl = QLabel(meta["name"])
        name_lbl.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 13px;")
        name_lbl.setMaximumHeight(36)
        layout.addWidget(name_lbl)

        type_text = "Folder" if meta["is_dir"] else meta["kind"].capitalize()
        size_text = "" if meta["is_dir"] else size_fmt(meta["size"])
        caption   = f"{size_text}  ·  {type_text}" if size_text else type_text
        meta_lbl  = QLabel(caption)
        meta_lbl.setAlignment(Qt.AlignCenter)
        meta_lbl.setWordWrap(True)
        meta_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 9px;")
        layout.addWidget(meta_lbl)