"""main.py — Application entry point for Deckhand.

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

from PyQt5.QtWidgets import QApplication, QStyleFactory
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtCore import QPropertyAnimation, QEasingCurve

from themes import T, CURRENT_THEME, build_qss, apply_theme_vars
from main_window import EC2FileManager
from splash import SplashScreen


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
    app.setApplicationName("Deckhand")
    app.setOrganizationName("EC2Manager")

    # On macOS, Qt's native 'macos' style renders combo-box and menu popups
    # through the OS itself and ignores most QAbstractItemView QSS (dark
    # background, rounded row highlight, custom fonts) — the popup falls
    # back to a plain system list no matter what build_qss() says. Fusion
    # is a full Qt-drawn style, so every themed popup (namespace picker,
    # sort/config combos, right-click menus) renders identically to the
    # rest of the themed app on macOS, Windows, and Linux alike.
    if "Fusion" in QStyleFactory.keys():
        app.setStyle(QStyleFactory.create("Fusion"))

    # Apply the theme that was loaded from saved settings at import time.
    # (themes.py already called apply_theme_vars() on import, so T is populated.)
    app.setStyleSheet(build_qss())
    app.setPalette(_build_palette())

    # ── Opening animation, then main window ────────────────────
    # The main window is only constructed once the splash's ring/glyph/
    # text sequence finishes, so the animation isn't competing with
    # EC2FileManager's own startup work (theme/QSS already applied above,
    # window construction, sidebar layout, etc.) for the UI thread.
    splash = SplashScreen()
    window_ref = {}

    def _begin_zoom():
        window = EC2FileManager()
        window_ref["window"] = window

        window.setWindowOpacity(0.0)
        window.showFullScreen()

        fade_in = QPropertyAnimation(window, b"windowOpacity", window)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setDuration(SplashScreen.ZOOM_MS)
        fade_in.setEasingCurve(QEasingCurve.InOutCubic)

        # Splash fades out in place while the real window fades in at the
        # same time, in the same animation group — a plain crossfade.
        splash.zoom_into(None, on_done=_finish, companions=[fade_in])

    def _finish():
        window_ref["window"].setWindowOpacity(1.0)
        window_ref["window"].raise_()
        window_ref["window"].activateWindow()
        splash.close()
        # Only check whether to show the lock screen once the window is
        # fully raised/activated and the splash is gone — see
        # check_initial_lock()'s docstring in main_window.py for why this
        # can't happen any earlier.
        window_ref["window"].check_initial_lock()

    splash.finished.connect(_begin_zoom)
    splash.show()
    splash.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())