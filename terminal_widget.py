"""terminal_widget.py — A single scrolling terminal surface.

Replaces the old "line-edit above a read-only output box" combo with one
widget: the prompt and the command you type live directly inside the same
scrolling history as the output, exactly like a real terminal. You type at
the prompt, press Enter, the output appears right below it, and a new
prompt appears for the next command.

This is NOT a full PTY/VT100 emulator (no cursor-addressing for curses
apps like vim/top/htop) — commands are still executed one at a time by the
owner (main_window.py keeps using CommandWorker/SSH exec_command under the
hood, including its sudo-user and cwd tracking). What changed is purely
that input and output now share one surface instead of two separate
widgets.
"""

from PyQt5.QtWidgets import QPlainTextEdit
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QTextCursor

from themes import T
from utils import monospace_font


class TerminalWidget(QPlainTextEdit):
    """Emits `command_entered(str)` when Enter is pressed at the live prompt.
    The owner calls `show_prompt(prompt)` to start a new input line, and
    `write_output(text)` to append a finished command's output before the
    next prompt appears."""

    command_entered      = pyqtSignal(str)
    interrupt_requested   = pyqtSignal()   # Ctrl+C while a command is running

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUndoRedoEnabled(False)
        self.setFont(monospace_font(11))
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setTabChangesFocus(False)

        self._input_start = 0     # document position where live input begins
        self._history      = []   # previously entered commands
        self._history_idx  = -1
        self._busy         = False  # True while a command is executing

    # ── Public API ──────────────────────────────────────────────
    def show_prompt(self, prompt: str):
        """Append a fresh prompt and mark where the next input begins."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        if self.toPlainText() and not self.toPlainText().endswith("\n"):
            cursor.insertText("\n")
        cursor.insertText(prompt)
        self.setTextCursor(cursor)
        self.moveCursor(QTextCursor.End)
        self._input_start = self.textCursor().position()
        self._busy = False
        self.ensureCursorVisible()

    def write_output(self, text: str):
        """Append command output (or any async message) above the next prompt."""
        if text is None:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        existing = self.toPlainText()
        if existing and not existing.endswith("\n"):
            cursor.insertText("\n")
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.moveCursor(QTextCursor.End)
        self._input_start = self.textCursor().position()
        self.ensureCursorVisible()

    def current_input(self) -> str:
        cursor = self.textCursor()
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        # Qt represents embedded newlines in selectedText() as U+2029.
        return cursor.selectedText().replace("\u2029", "\n")

    def set_busy(self, busy: bool):
        self._busy = busy

    # ── Internal helpers ────────────────────────────────────────
    def _replace_input(self, text: str):
        cursor = self.textCursor()
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # ── Key handling: history is read-only, only the live input is editable ──
    def keyPressEvent(self, event):
        ctrl = bool(event.modifiers() & Qt.ControlModifier)

        if self._busy and ctrl and event.key() == Qt.Key_C:
            self.interrupt_requested.emit()
            return
        if self._busy:
            # A command is running — don't buffer keystrokes ahead of it.
            return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            cmd = self.current_input()
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText("\n")
            self.setTextCursor(cursor)
            if cmd.strip():
                self._history.append(cmd)
            self._history_idx = len(self._history)
            self._busy = True
            self.command_entered.emit(cmd)
            return

        if event.key() == Qt.Key_Up:
            if self._history and self._history_idx > 0:
                self._history_idx -= 1
                self._replace_input(self._history[self._history_idx])
            return

        if event.key() == Qt.Key_Down:
            if self._history:
                if self._history_idx < len(self._history) - 1:
                    self._history_idx += 1
                    self._replace_input(self._history[self._history_idx])
                else:
                    self._history_idx = len(self._history)
                    self._replace_input("")
            return

        cursor = self.textCursor()

        if event.key() == Qt.Key_Home:
            cursor.setPosition(self._input_start)
            self.setTextCursor(cursor)
            return

        if event.key() == Qt.Key_Backspace and cursor.position() <= self._input_start:
            return  # nothing left to erase in the live input

        # Any other key: make sure we're not editing inside the read-only history.
        if cursor.position() < self._input_start or (
            cursor.hasSelection() and cursor.selectionStart() < self._input_start
        ):
            cursor.movePosition(QTextCursor.End)
            self.setTextCursor(cursor)

        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # Clicks are fine for selecting/copying old output, but typing
        # afterwards should resume at the live prompt, not mid-history.
        cursor = self.textCursor()
        if not cursor.hasSelection() and cursor.position() < self._input_start:
            cursor.movePosition(QTextCursor.End)
            self.setTextCursor(cursor)

    def contextMenuEvent(self, event):
        # Keep the standard copy/select-all context menu — just don't let
        # "Paste" or similar land text before the live input point.
        super().contextMenuEvent(event)