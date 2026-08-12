"""editor_widgets.py — CodeEditor (gutter line numbers + current-line
highlight + zoom) and a lightweight, dependency-free SyntaxHighlighter used
by the FileEditorDialog in dialogs.py.

Why not two separate QPlainTextEdits synced by scrollbar (the previous
approach)?  Two independently-scrolling widgets drift out of sync under
fast programmatic scrolling (e.g. streaming a big file in, or a Find/Next
jump) and double the paint/layout cost. CodeEditor instead paints the line
numbers directly into a margin of the *same* QPlainTextEdit viewport — the
standard Qt "Code Editor" pattern — so numbers are always pixel-exact and
there's nothing to keep in sync.

SyntaxHighlighter is a small hand-rolled QSyntaxHighlighter (regex-based
keyword/string/comment/number rules per language) rather than a pygments
dependency, so it works with zero extra installs on whatever machine the
packaged app lands on.
"""

import re

from PyQt5.QtWidgets import QPlainTextEdit, QWidget
from PyQt5.QtCore import Qt, QRect, QSize
from PyQt5.QtGui import (
    QColor, QPainter, QTextFormat, QTextCharFormat, QFont,
    QSyntaxHighlighter, QTextCursor,
)

from themes import T
from utils import monospace_font


# ─── Extension → language mapping ─────────────────────────────
EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".html": "markup", ".htm": "markup", ".xml": "markup",
    ".css": "css", ".scss": "css",
    ".md": "markdown",
    ".go": "clike", ".rs": "clike", ".java": "clike",
    ".c": "clike", ".cpp": "clike", ".h": "clike", ".hpp": "clike",
    ".cs": "clike", ".swift": "clike", ".kt": "clike",
    ".rb": "ruby",
    ".php": "clike",
    ".ini": "ini", ".conf": "ini", ".cfg": "ini", ".toml": "ini",
    ".env": "ini",
}

LANG_LABEL = {
    "python": "Python", "javascript": "JavaScript", "json": "JSON",
    "yaml": "YAML", "shell": "Shell", "sql": "SQL", "markup": "Markup",
    "css": "CSS", "markdown": "Markdown", "clike": "Code", "ruby": "Ruby",
    "ini": "Config",
}

_KEYWORDS = {
    "python": [
        "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "class", "continue", "def", "del", "elif", "else",
        "except", "finally", "for", "from", "global", "if", "import",
        "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
        "return", "try", "while", "with", "yield", "self", "cls",
    ],
    "javascript": [
        "const", "let", "var", "function", "return", "if", "else", "for",
        "while", "do", "switch", "case", "default", "break", "continue",
        "class", "extends", "new", "this", "typeof", "instanceof", "of",
        "in", "try", "catch", "finally", "throw", "async", "await",
        "import", "export", "from", "null", "undefined", "true", "false",
        "static", "get", "set", "super", "yield", "void",
    ],
    "shell": [
        "if", "then", "else", "elif", "fi", "for", "while", "do", "done",
        "case", "esac", "function", "return", "exit", "export", "local",
        "readonly", "shift", "break", "continue", "in", "select", "until",
        "echo", "set", "trap", "source",
    ],
    "sql": [
        "SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "UPDATE",
        "SET", "DELETE", "CREATE", "TABLE", "ALTER", "DROP", "JOIN",
        "LEFT", "RIGHT", "INNER", "OUTER", "ON", "GROUP", "BY", "ORDER",
        "HAVING", "LIMIT", "AND", "OR", "NOT", "NULL", "AS", "DISTINCT",
        "UNION", "IN", "EXISTS", "CASE", "WHEN", "THEN", "END", "DEFAULT",
        "PRIMARY", "KEY", "FOREIGN", "REFERENCES", "INDEX", "VIEW",
    ],
    "clike": [
        "func", "package", "import", "return", "if", "else", "for",
        "range", "switch", "case", "break", "continue", "var", "const",
        "type", "struct", "interface", "map", "chan", "go", "defer",
        "select", "nil", "true", "false", "fn", "let", "mut", "pub",
        "enum", "impl", "trait", "use", "mod", "loop", "match", "self",
        "Self", "None", "Some", "Ok", "Err", "public", "private",
        "protected", "class", "extends", "implements", "static", "final",
        "void", "int", "long", "float", "double", "boolean", "char",
        "String", "new", "throw", "throws", "namespace", "using",
    ],
    "ruby": [
        "def", "end", "class", "module", "if", "elsif", "else", "unless",
        "while", "until", "for", "in", "do", "begin", "rescue", "ensure",
        "raise", "return", "yield", "require", "require_relative",
        "attr_accessor", "nil", "true", "false", "self", "puts",
    ],
}

_COMMENT_PREFIX = {
    "python": "#", "shell": "#", "yaml": "#", "ruby": "#", "ini": "#",
    "javascript": "//", "clike": "//", "sql": "--",
}

_JSON_KEYWORDS = ["true", "false", "null"]
_YAML_KEYWORDS = ["true", "false", "null", "yes", "no", "on", "off"]


def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class SyntaxHighlighter(QSyntaxHighlighter):
    """Regex-based highlighter. One instance is built per language and
    reads its colours from the live theme dict `T`, so call `rebuild()`
    after a theme switch to pick up the new palette."""

    # Block states used to track a Python triple-quoted string spanning
    # multiple lines (the one multi-line construct common enough in this
    # app's files — YAML/heredocs, etc. — to be worth handling).
    _NORMAL, _IN_TRIPLE = 0, 1

    def __init__(self, document, lang: str):
        super().__init__(document)
        self.lang = lang
        self.rebuild()

    def rebuild(self):
        """(Re)build the regex rule list from the current theme colours."""
        kw_fmt      = _fmt(T["ACCENT2"], bold=True)
        str_fmt     = _fmt(T["SUCCESS"])
        num_fmt     = _fmt(T["INFO"])
        comment_fmt = _fmt(T["TEXT_MUTED"], italic=True)
        key_fmt     = _fmt(T["ACCENT"])
        tag_fmt     = _fmt(T["ACCENT"], bold=True)
        header_fmt  = _fmt(T["WARNING"], bold=True)
        self._triple_fmt = str_fmt

        rules = []
        lang = self.lang

        if lang == "json":
            rules.append((re.compile(r'"(?:[^"\\]|\\.)*"\s*(?=:)'), key_fmt))
            rules.append((re.compile(r'"(?:[^"\\]|\\.)*"'), str_fmt))
            rules.append((re.compile(r'\b(?:true|false|null)\b'), kw_fmt))
            rules.append((re.compile(r'-?\b\d+\.?\d*\b'), num_fmt))

        elif lang == "yaml":
            rules.append((re.compile(r'^[^\S\n]*[\w.\-\[\]]+(?=\s*:)', re.M), key_fmt))
            rules.append((re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"'), str_fmt))
            rules.append((re.compile(r"'[^']*'"), str_fmt))
            rules.append((re.compile(r'\b(?:true|false|null|yes|no|on|off)\b', re.I), kw_fmt))
            rules.append((re.compile(r'\b\d+\.?\d*\b'), num_fmt))
            rules.append((re.compile(r'#.*'), comment_fmt))

        elif lang == "markup":
            rules.append((re.compile(r'</?[\w:\-]+'), tag_fmt))
            rules.append((re.compile(r'[\w\-]+(?==)'), key_fmt))
            rules.append((re.compile(r'"[^"]*"'), str_fmt))
            rules.append((re.compile(r'<!--.*?-->'), comment_fmt))

        elif lang == "css":
            rules.append((re.compile(r'[.#]?[\w\-]+(?=\s*\{)'), tag_fmt))
            rules.append((re.compile(r'[\w\-]+(?=\s*:)'), key_fmt))
            rules.append((re.compile(r'#[0-9a-fA-F]{3,8}\b'), num_fmt))
            rules.append((re.compile(r'"[^"]*"'), str_fmt))
            rules.append((re.compile(r'/\*.*?\*/'), comment_fmt))

        elif lang == "markdown":
            rules.append((re.compile(r'^#{1,6}\s.*', re.M), header_fmt))
            rules.append((re.compile(r'\*\*[^*]+\*\*'), kw_fmt))
            rules.append((re.compile(r'`[^`]+`'), str_fmt))
            rules.append((re.compile(r'^\s*[-*+]\s', re.M), key_fmt))

        elif lang == "ini":
            rules.append((re.compile(r'^\s*\[[^\]]+\]', re.M), header_fmt))
            rules.append((re.compile(r'^[^=\n#;]+(?==)', re.M), key_fmt))
            rules.append((re.compile(r'"[^"]*"'), str_fmt))
            rules.append((re.compile(r'^\s*[#;].*', re.M), comment_fmt))

        else:
            for kw in _KEYWORDS.get(lang, []):
                rules.append((re.compile(r'\b' + re.escape(kw) + r'\b'), kw_fmt))
            rules.append((re.compile(r'"(?:[^"\\]|\\.)*"'), str_fmt))
            rules.append((re.compile(r"'(?:[^'\\]|\\.)*'"), str_fmt))
            rules.append((re.compile(r'\b0x[0-9a-fA-F]+\b|\b\d+\.?\d*\b'), num_fmt))
            prefix = _COMMENT_PREFIX.get(lang)
            if prefix:
                rules.append((re.compile(re.escape(prefix) + r'.*'), comment_fmt))

        self._rules = rules
        self.rehighlight()

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            for m in pattern.finditer(text):
                start, end = m.start(), m.end()
                if end > start:
                    self.setFormat(start, end - start, fmt)

        # Python triple-quoted string spanning multiple blocks.
        if self.lang == "python":
            self._highlight_triple_quotes(text)
        else:
            self.setCurrentBlockState(self._NORMAL)

    def _highlight_triple_quotes(self, text):
        delim = '"""'
        alt_delim = "'''"
        start_idx = 0
        state = self._NORMAL if self.previousBlockState() == -1 else self.previousBlockState()

        if state != self._IN_TRIPLE:
            idx = text.find(delim)
            idx_alt = text.find(alt_delim)
            if idx_alt != -1 and (idx == -1 or idx_alt < idx):
                idx, delim = idx_alt, alt_delim
            if idx == -1:
                self.setCurrentBlockState(self._NORMAL)
                return
            start_idx = idx
        else:
            start_idx = 0

        while start_idx != -1:
            end_idx = text.find(delim, start_idx + 3)
            if end_idx == -1:
                self.setFormat(start_idx, len(text) - start_idx, self._triple_fmt)
                self.setCurrentBlockState(self._IN_TRIPLE)
                return
            length = end_idx - start_idx + 3
            self.setFormat(start_idx, length, self._triple_fmt)
            next_idx = text.find(delim, end_idx + 3)
            next_alt = text.find(alt_delim, end_idx + 3)
            if next_alt != -1 and (next_idx == -1 or next_alt < next_idx):
                next_idx, delim = next_alt, alt_delim
            start_idx = next_idx

        self.setCurrentBlockState(self._NORMAL)


def make_highlighter(document, filename: str):
    """Build the right SyntaxHighlighter for *filename*'s extension, or
    return None for unrecognised extensions (plain text — no highlighting
    is preferable to guessing wrong)."""
    import os
    ext = os.path.splitext(filename)[1].lower()
    lang = EXT_LANG.get(ext)
    if not lang:
        return None, None
    return SyntaxHighlighter(document, lang), lang


# ─── Gutter widget ─────────────────────────────────────────────
class _LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)


# ─── CodeEditor ──────────────────────────────────────────────
class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit with an integrated line-number gutter, a soft
    highlight on the current line, and Ctrl+scroll / Ctrl+=/- zoom —
    the baseline feature set people expect from a code editor."""

    MIN_PT, MAX_PT = 8, 28

    def __init__(self, base_point_size=12, parent=None):
        super().__init__(parent)
        self._base_pt = base_point_size
        self._pt      = base_point_size
        self.setFont(monospace_font(self._pt))
        self.setFrameStyle(0)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(' '))

        self._gutter = _LineNumberArea(self)
        self._search_selections = []  # ExtraSelections for find/replace matches
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)

        self._update_gutter_width()
        self._highlight_current_line()
        self.refresh_theme()

    # ── Gutter geometry / paint ─────────────────────────────
    def line_number_area_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance('9') * digits

    def _update_gutter_width(self, _=0):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(T["BG_PANEL"]))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        current_line = self.textCursor().blockNumber()
        gutter_w = self._gutter.width()
        fh = self.fontMetrics().height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                is_current = block_number == current_line
                painter.setPen(QColor(T["TEXT_PRIMARY"] if is_current else T["TEXT_MUTED"]))
                f = QFont(self.font())
                f.setBold(is_current)
                painter.setFont(f)
                painter.drawText(0, top, gutter_w - 8, fh, Qt.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    # ── Current-line highlight ──────────────────────────────
    def _highlight_current_line(self):
        selections = []
        if not self.isReadOnly():
            sel = QTextEdit_ExtraSelection()
            sel.format.setBackground(QColor(T["BG_ITEM"]))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        # Search-match highlights (find/replace) layer on top, non-
        # destructively — they never touch the document's actual char
        # format, so they can't clash with the syntax highlighter.
        selections.extend(self._search_selections)
        self.setExtraSelections(selections)
        self._gutter.update()

    def set_search_selections(self, selections):
        """Replace the current set of find/replace match highlights."""
        self._search_selections = selections
        self._highlight_current_line()

    # ── Zoom ──────────────────────────────────────────────────
    def zoom_in(self):
        self.set_point_size(self._pt + 1)

    def zoom_out(self):
        self.set_point_size(self._pt - 1)

    def zoom_reset(self):
        self.set_point_size(self._base_pt)

    def set_point_size(self, pt):
        pt = max(self.MIN_PT, min(self.MAX_PT, pt))
        if pt == self._pt:
            return
        self._pt = pt
        self.setFont(monospace_font(pt))
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(' '))
        self._update_gutter_width()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    # ── Theming ───────────────────────────────────────────────
    def refresh_theme(self):
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {T['BG_DARK']}; color: {T['TEXT_PRIMARY']}; "
            f"border: none; padding: 4px 0 4px 8px; "
            f"selection-background-color: {T['ACCENT']}; selection-color: #ffffff; }}"
        )
        self._highlight_current_line()
        self._gutter.update()


# QTextEdit.ExtraSelection lives on QTextEdit but works fine with
# QPlainTextEdit's setExtraSelections too — imported lazily under a local
# alias to keep the top-level import list tidy.
from PyQt5.QtWidgets import QTextEdit as _QTextEdit_ns
QTextEdit_ExtraSelection = _QTextEdit_ns.ExtraSelection