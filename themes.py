"""themes.py — Theme definitions, QSS builder, and apply helpers."""

import json
import os

# ─── Theme palette definitions ────────────────────────────────
THEMES = {
    "App Default": {
        "BG_DARK": "#1e1e2e", "BG_PANEL": "#252535", "BG_SIDEBAR": "#1a1a28",
        "BG_ITEM": "#2a2a3e", "BG_ITEM_SEL": "#3d3d6b", "BG_HOVER": "#32324a",
        "ACCENT": "#7c6af7", "ACCENT2": "#a78bfa", "TEXT_PRIMARY": "#e2e0f0",
        "TEXT_DIM": "#8884aa", "TEXT_MUTED": "#5c5a7a", "BORDER": "#35354a",
        "SUCCESS": "#4ade80", "DANGER": "#f87171", "WARNING": "#fbbf24", "INFO": "#60a5fa",
    },

    "Apple Dark": {
        "BG_DARK": "#18181b", "BG_PANEL": "#27272a", "BG_SIDEBAR": "#1f1f23",
        "BG_ITEM": "#2f2f35", "BG_ITEM_SEL": "#3b82f6", "BG_HOVER": "#3a3a42",
        "ACCENT": "#0a84ff", "ACCENT2": "#5ac8fa", "TEXT_PRIMARY": "#ffffff",
        "TEXT_DIM": "#d4d4d8", "TEXT_MUTED": "#a1a1aa", "BORDER": "#3f3f46",
        "SUCCESS": "#30d158", "DANGER": "#ff453a", "WARNING": "#ffd60a", "INFO": "#64d2ff",
    },

    "Obsidian Purple": {
        "BG_DARK": "#09090b", "BG_PANEL": "#111113", "BG_SIDEBAR": "#0c0c0f",
        "BG_ITEM": "#1a1a22", "BG_ITEM_SEL": "#4c1d95", "BG_HOVER": "#262637",
        "ACCENT": "#a855f7", "ACCENT2": "#c084fc", "TEXT_PRIMARY": "#fafafa",
        "TEXT_DIM": "#d4d4d8", "TEXT_MUTED": "#a1a1aa", "BORDER": "#2f2f35",
        "SUCCESS": "#4ade80", "DANGER": "#fb7185", "WARNING": "#fbbf24", "INFO": "#60a5fa",
    },
    "Midnight Gold": {
        "BG_DARK": "#0b0b0f", "BG_PANEL": "#121218", "BG_SIDEBAR": "#09090d",
        "BG_ITEM": "#1a1b22", "BG_ITEM_SEL": "#3a3218", "BG_HOVER": "#24252d",
        "ACCENT": "#eab308", "ACCENT2": "#fcd34d", "TEXT_PRIMARY": "#fafaf9",
        "TEXT_DIM": "#d6d3d1", "TEXT_MUTED": "#a8a29e", "BORDER": "#2f3139",
        "SUCCESS": "#22c55e", "DANGER": "#ef4444", "WARNING": "#f59e0b", "INFO": "#60a5fa",
    },

        "Cyberpunk Neon": {
        "BG_DARK": "#08060f", "BG_PANEL": "#100b1e", "BG_SIDEBAR": "#060411",
        "BG_ITEM": "#1a1330", "BG_ITEM_SEL": "#ff2e88", "BG_HOVER": "#241a3f",
        "ACCENT": "#ff2e88", "ACCENT2": "#00f0ff", "TEXT_PRIMARY": "#f5f0ff",
        "TEXT_DIM": "#d9c9f0", "TEXT_MUTED": "#8a7aab", "BORDER": "#2e2350",
        "SUCCESS": "#39ff14", "DANGER": "#ff2e63", "WARNING": "#fce83a", "INFO": "#00f0ff",
    },

    "Rose Gold": {
        "BG_DARK": "#140c0e", "BG_PANEL": "#1e1315", "BG_SIDEBAR": "#0f0809",
        "BG_ITEM": "#2b1a1d", "BG_ITEM_SEL": "#7a3a3f", "BG_HOVER": "#372023",
        "ACCENT": "#f0a6a3", "ACCENT2": "#e8b4bc", "TEXT_PRIMARY": "#fff0ee",
        "TEXT_DIM": "#e8c4c0", "TEXT_MUTED": "#b98d8a", "BORDER": "#3d2529",
        "SUCCESS": "#4ade80", "DANGER": "#f87171", "WARNING": "#fbbf24", "INFO": "#93c5fd",
    },

    "Arctic Frost": {
        "BG_DARK": "#080d13", "BG_PANEL": "#0f161f", "BG_SIDEBAR": "#060a0f",
        "BG_ITEM": "#16202c", "BG_ITEM_SEL": "#1e5f7a", "BG_HOVER": "#1c2a38",
        "ACCENT": "#7dd3fc", "ACCENT2": "#e0f2fe", "TEXT_PRIMARY": "#f0f9ff",
        "TEXT_DIM": "#cfe8f7", "TEXT_MUTED": "#8fb4c9", "BORDER": "#233647",
        "SUCCESS": "#5eead4", "DANGER": "#fb7185", "WARNING": "#fde047", "INFO": "#7dd3fc",
    },

    "Toxic Lime": {
        "BG_DARK": "#0a0f08", "BG_PANEL": "#121a0e", "BG_SIDEBAR": "#080c06",
        "BG_ITEM": "#1a2614", "BG_ITEM_SEL": "#3f6b1a", "BG_HOVER": "#233118",
        "ACCENT": "#a3e635", "ACCENT2": "#d9f99d", "TEXT_PRIMARY": "#f7ffe8",
        "TEXT_DIM": "#dbf0b0", "TEXT_MUTED": "#9cb87a", "BORDER": "#2e4020",
        "SUCCESS": "#a3e635", "DANGER": "#f87171", "WARNING": "#fbbf24", "INFO": "#60a5fa",
    },

    "Snow Light": {
    # High-contrast light palette: muted/semantic colors are dark enough for normal UI text on white/light cards.
        "BG_DARK": "#f5f5f7", "BG_PANEL": "#ffffff", "BG_SIDEBAR": "#eceef2",
        "BG_ITEM": "#f0f1f4", "BG_ITEM_SEL": "#dbe4fe", "BG_HOVER": "#e6e9ee",
        "ACCENT": "#2563eb", "ACCENT2": "#3b82f6", "TEXT_PRIMARY": "#1c1c1e",
        "TEXT_DIM": "#4b5563", "TEXT_MUTED": "#52525b", "BORDER": "#d1d5db",
        "SUCCESS": "#15803d", "DANGER": "#b91c1c", "WARNING": "#b45309", "INFO": "#1d4ed8",
    },
}

# ─── Live theme state (single source of truth) ───────────────
T = {}

APP_DIR       = os.path.join(os.path.expanduser("~"), ".vm_visualizer")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
os.makedirs(APP_DIR, exist_ok=True)

CURRENT_THEME = "Obsidian Purple"


def save_settings(**extra):
    """Persist app-wide settings to disk. Merges any extra key/value pairs
    (e.g. a custom tunnel-services CSV path) on top of whatever is already
    saved, and always includes the current theme — so a caller that only
    cares about one setting (e.g. KubernetesTab saving a CSV path) doesn't
    clobber a setting written by another part of the app (e.g. the theme
    picker), and vice versa."""
    data = load_settings()
    data["theme"] = CURRENT_THEME
    data.update(extra)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def apply_theme_vars(theme_name: str):
    """Populate the global T dict from the named theme.

    Falls back to "App Default" if theme_name isn't a known theme —
    e.g. settings.json was saved by a build with a theme that's since
    been renamed/removed, or was hand-edited/corrupted. Without this
    check, THEMES[theme_name] raises KeyError, which happens at import
    time (see the bootstrap call below) and prevents the app from
    starting at all.
    """
    global CURRENT_THEME
    if theme_name not in THEMES:
        theme_name = "App Default"
    CURRENT_THEME = theme_name
    T.update(THEMES[theme_name])


# Bootstrap from saved settings at import time.
_settings = load_settings()
apply_theme_vars(_settings.get("theme", CURRENT_THEME))


# ─── QSS builder ─────────────────────────────────────────────
def build_qss() -> str:
    c = T
    return f"""
    QMainWindow, QWidget {{
        background: {c['BG_DARK']};
        color: {c['TEXT_PRIMARY']};
        font-family: 'Segoe UI', 'SF Pro Display', 'Inter', Arial, sans-serif;
        font-size: 13px;
    }}
    /* QLabel is a QWidget subclass, so without this override every label that
    doesn't set its own background inherits BG_DARK from the rule above —
    which shows up as a mismatched box wherever the label sits on a panel,
    toolbar, sidebar, or dialog styled with a different shade (BG_PANEL,
    BG_ITEM, BG_SIDEBAR, etc). Labels that intentionally want a painted
    background (badges, preview headers, …) set it explicitly via their own
    setStyleSheet() call, which takes precedence over this default. */
    QLabel {{
        background: transparent;
    }}
    QTabWidget::pane {{ border: none; background: {c['BG_DARK']}; }}
    QTabBar::tab {{
        background: {c['BG_PANEL']}; color: {c['TEXT_DIM']};
        border: none; border-bottom: 2px solid transparent;
        padding: 10px 22px; font-size: 13px; font-weight: 500; margin-right: 2px;
    }}
    QTabBar::tab:selected {{ color: {c['TEXT_PRIMARY']}; border-bottom: 2px solid {c['ACCENT']}; background: {c['BG_DARK']}; }}
    QTabBar::tab:hover:!selected {{ color: {c['TEXT_PRIMARY']}; background: {c['BG_HOVER']}; }}
    QToolBar {{ background: {c['BG_PANEL']}; border-bottom: 1px solid {c['BORDER']}; padding: 4px 8px; spacing: 6px; }}
    QToolBar QToolButton {{
        background: transparent; color: {c['TEXT_PRIMARY']};
        border: none; border-radius: 6px; padding: 6px 10px; font-size: 13px;
    }}
    QToolBar QToolButton:hover   {{ background: {c['BG_HOVER']}; }}
    QToolBar QToolButton:pressed {{ background: {c['BG_ITEM_SEL']}; }}
    QToolBar QToolButton:disabled{{ color: {c['TEXT_MUTED']}; }}
    QLineEdit {{
        background: {c['BG_ITEM']}; color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']}; border-radius: 8px; padding: 6px 12px;
        selection-background-color: {c['ACCENT']};
    }}
    QLineEdit:focus {{ border-color: {c['ACCENT']}; }}
    /* Pill-shaped combo box — matches the rounded strip look ThemePicker
    already established, instead of the old flat square box, so every
    dropdown in the app (namespace picker, sort, config type, …) reads as
    one consistent control language. */
    QComboBox {{
        background: {c['BG_ITEM']}; color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']}; border-radius: 15px; padding: 6px 14px; min-width: 120px;
    }}
    QComboBox:hover {{ border-color: {c['TEXT_MUTED']}; background: {c['BG_HOVER']}; }}
    QComboBox:focus {{ border: 1px solid {c['ACCENT']}; }}
    QComboBox::drop-down {{
        border: none; width: 28px; margin-right: 3px;
    }}
    QComboBox::down-arrow {{
        width: 9px; height: 9px;
    }}
    /* Popup list shown when any QComboBox is opened (namespace picker, sort,
    exec mode, interpreter, config type, …) — styled once here so every
    dropdown in the app gets the same polished look.
    NOTE: no border-radius on the container itself — Qt draws this rounded
    panel inside a plain rectangular top-level popup window, so rounding
    the container leaves its square window showing through behind the
    corners. Rounding is applied to each row instead, which stays safely
    inside the container's bounds. */
    QComboBox QAbstractItemView {{
        background: {c['BG_PANEL']};
        color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']};
        padding: 6px;
        outline: none;
        selection-background-color: {c['BG_ITEM_SEL']};
        selection-color: {c['TEXT_PRIMARY']};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px;
        padding: 4px 10px;
        margin: 1px 2px;
        border-radius: 6px;
    }}
    QComboBox QAbstractItemView::item:hover {{
        background: {c['BG_HOVER']};
    }}
    QSpinBox {{
        background: {c['BG_ITEM']}; color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']}; border-radius: 8px; padding: 5px 8px;
    }}
    QSpinBox:focus {{ border-color: {c['ACCENT']}; }}
    QListWidget {{ background: {c['BG_DARK']}; border: none; outline: none; }}
    QListWidget::item {{ border-radius: 6px; padding: 0px; margin: 0px; color: {c['TEXT_PRIMARY']}; }}
    QListWidget::item:hover    {{ background: {c['BG_HOVER']}; }}
    QListWidget::item:selected {{ background: {c['BG_ITEM_SEL']}; color: {c['TEXT_PRIMARY']}; }}
    QTreeWidget {{
        background: {c['BG_DARK']}; color: {c['TEXT_PRIMARY']};
        border: none; outline: none; alternate-background-color: {c['BG_PANEL']};
    }}
    QTreeWidget::item {{ padding: 4px 2px; border-radius: 4px; }}
    QTreeWidget::item:hover    {{ background: {c['BG_HOVER']}; }}
    QTreeWidget::item:selected {{ background: {c['BG_ITEM_SEL']}; color: {c['TEXT_PRIMARY']}; }}
    QTreeWidget::branch {{ background: {c['BG_DARK']}; }}
    QHeaderView::section {{
        background: {c['BG_PANEL']}; color: {c['TEXT_MUTED']};
        border: none; border-bottom: 1px solid {c['BORDER']};
        padding: 6px 8px; font-size: 13px; font-weight: 700; letter-spacing: 0.5px;
    }}
    QTableWidget {{
        background: {c['BG_DARK']}; color: {c['TEXT_PRIMARY']};
        border: none; outline: none; gridline-color: {c['BORDER']};
    }}
    QTableWidget::item {{ padding: 4px 8px; }}
    QTableWidget::item:selected {{ background: {c['BG_ITEM_SEL']}; color: {c['TEXT_PRIMARY']}; }}
    QTextEdit {{
        background: {c['BG_PANEL']}; color: {c['TEXT_PRIMARY']}; border: none;
        font-family: 'Cascadia Code','SF Mono','Menlo','Consolas','Fira Code','DejaVu Sans Mono',monospace;
        font-size: 12px; padding: 8px;
    }}
    QSplitter::handle {{ background: {c['BORDER']}; width: 1px; height: 1px; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['TEXT_MUTED']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: {c['TEXT_MUTED']}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QStatusBar {{
        background: {c['BG_PANEL']}; color: {c['TEXT_DIM']};
        border-top: 1px solid {c['BORDER']}; font-size: 13px; padding: 0 8px;
    }}
    QMenu {{
        background: {c['BG_PANEL']}; color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']}; border-radius: 8px; padding: 4px;
    }}
    QMenu::item {{ padding: 7px 20px 7px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {c['BG_ITEM_SEL']}; }}
    QMenu::separator {{ background: {c['BORDER']}; height: 1px; margin: 4px 8px; }}
    QProgressBar {{
        border: none; background: {c['BG_ITEM']}; border-radius: 3px;
        height: 4px; text-align: center; color: transparent;
    }}
    QProgressBar::chunk {{ background: {c['ACCENT']}; border-radius: 3px; }}
    QPushButton {{
        background: {c['BG_ITEM']}; color: {c['TEXT_PRIMARY']};
        border: 1px solid {c['BORDER']}; border-radius: 7px; padding: 7px 16px;
    }}
    QPushButton:hover   {{ background: {c['BG_HOVER']}; border-color: {c['ACCENT']}; }}
    QPushButton:pressed {{ background: {c['BG_ITEM_SEL']}; }}
    QPushButton#primary {{ background: {c['ACCENT']}; border-color: {c['ACCENT']}; color: white; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {c['ACCENT2']}; }}
    QPushButton#danger  {{ background: transparent; border-color: {c['DANGER']}; color: {c['DANGER']}; }}
    QPushButton#danger:hover  {{ background: rgba(248,113,113,0.12); }}
    QPushButton#success {{ background: transparent; border-color: {c['SUCCESS']}; color: {c['SUCCESS']}; }}
    QPushButton#success:hover {{ background: rgba(74,222,128,0.12); }}
    QPushButton#warning {{ background: transparent; border-color: {c['WARNING']}; color: {c['WARNING']}; }}
    QPushButton#warning:hover {{ background: rgba(251,191,36,0.12); }}
    QPushButton:checked {{ background: {c['ACCENT']}; border-color: {c['ACCENT']}; color: white; }}
    QDialog {{ background: {c['BG_PANEL']}; }}
    QDialogButtonBox QPushButton {{ min-width: 80px; }}
    QGroupBox {{
        color: {c['TEXT_DIM']}; border: 1px solid {c['BORDER']}; border-radius: 8px;
        margin-top: 12px; padding-top: 8px; font-size: 13px; font-weight: 700; letter-spacing: 0.8px;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 6px; color: {c['TEXT_MUTED']}; }}
    QLabel#breadcrumb {{ color: {c['TEXT_DIM']}; font-size: 12px; padding: 0 4px; }}
    QLabel#section_title {{
        color: {c['TEXT_MUTED']}; font-size: 10px; font-weight: 700;
        letter-spacing: 1px; padding: 12px 12px 4px 12px; text-transform: uppercase;
    }}
    QLabel#badge_running {{
        background: rgba(74,222,128,0.15); color: {c['SUCCESS']};
        border: 1px solid rgba(74,222,128,0.4); border-radius: 10px;
        padding: 2px 10px; font-size: 13px; font-weight: 600;
    }}
    QLabel#badge_pending {{
        background: rgba(251,191,36,0.15); color: {c['WARNING']};
        border: 1px solid rgba(251,191,36,0.4); border-radius: 10px;
        padding: 2px 10px; font-size: 13px; font-weight: 600;
    }}
    QLabel#badge_failed {{
        background: rgba(248,113,113,0.15); color: {c['DANGER']};
        border: 1px solid rgba(248,113,113,0.4); border-radius: 10px;
        padding: 2px 10px; font-size: 13px; font-weight: 600;
    }}
    QListWidget#recent_list {{
        background: {c['BG_DARK']}; border: 1px solid {c['BORDER']}; border-radius: 8px; outline: none;
    }}
    QListWidget#recent_list::item {{ border-radius: 6px; padding: 6px 10px; margin: 2px 4px; color: {c['TEXT_PRIMARY']}; }}
    QListWidget#recent_list::item:hover    {{ background: {c['BG_HOVER']}; }}
    QListWidget#recent_list::item:selected {{ background: {c['BG_ITEM_SEL']}; color: {c['TEXT_PRIMARY']}; }}
"""


def get_qss() -> str:
    return build_qss()


def apply_qss_to(widget):
    widget.setStyleSheet(get_qss())