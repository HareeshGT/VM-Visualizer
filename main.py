"""main.py — Application entry point for EC2 Manager.

Imports:
    themes      — theme palette definitions, QSS builder, apply helpers
    utils       — file-type helpers, size formatting, recent-instances CSV
    workers     — background QThread workers (SSH commands, file transfers)
    sudo_fs     — SudoFS SFTP wrapper for transparent sudo operations
    dialogs     — ConnectDialog, FileTransferDialog, LogViewerDialog, ExecDialog
    sidebar     — quick-access Sidebar widget
    preview     — PreviewPane widget (right column of file manager)
    file_widgets — FileRowWidget and FileGridWidget (list/grid item views)
    kubernetes_tab — KubernetesTab widget
    main_window — EC2FileManager QMainWindow
"""

import sys

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor, QPalette

from themes import T, CURRENT_THEME, build_qss, apply_theme_vars
from main_window import EC2FileManager


def _build_palette() -> QPalette:
    """Construct a QPalette that matches the active theme so native widgets
    inherit the right colours even before QSS kicks in."""
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(T["BG_DARK"]))
    pal.setColor(QPalette.WindowText,      QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Base,            QColor(T["BG_PANEL"]))
    pal.setColor(QPalette.AlternateBase,   QColor(T["BG_ITEM"]))
    pal.setColor(QPalette.Text,            QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Button,          QColor(T["BG_ITEM"]))
    pal.setColor(QPalette.ButtonText,      QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.Highlight,       QColor(T["ACCENT"]))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase,     QColor(T["BG_PANEL"]))
    pal.setColor(QPalette.ToolTipText,     QColor(T["TEXT_PRIMARY"]))
    pal.setColor(QPalette.PlaceholderText, QColor(T["TEXT_MUTED"]))
    return pal


def main() -> int:
    # ── Qt application ────────────────────────────────────────
    app = QApplication(sys.argv)
    app.setApplicationName("EC2 Manager")
    app.setOrganizationName("EC2Manager")

    # Apply the theme that was loaded from saved settings at import time.
    # (themes.py already called apply_theme_vars() on import, so T is populated.)
    app.setStyleSheet(build_qss())
    app.setPalette(_build_palette())

    # ── Main window ───────────────────────────────────────────
    window = EC2FileManager()
    window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())