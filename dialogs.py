"""dialogs.py — All modal dialogs: Connect, FileTransfer, LogViewer, Exec,
                FileEditor, FileExec, SearchDialog."""

import base64
import codecs
import io
import os
import re
import shlex
import time
import threading

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QDialogButtonBox, QListWidget, QListWidgetItem,
    QLineEdit, QFrame, QTextEdit, QFileDialog, QSpinBox, QCheckBox,
    QApplication, QComboBox, QMessageBox, QSplitter, QWidget,
    QPlainTextEdit, QTextBrowser, QAbstractItemView, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QShortcut, QSlider,
    QScrollArea, QStackedWidget, QCompleter,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QUrl
from PyQt5.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QKeySequence, QIntValidator

from themes import T, apply_qss_to
from utils import load_recent_instances, size_fmt, append_terminal_html, append_terminal_text, html_escape, monospace_font
from workers import CommandWorker, _TransferWorker, ScpTransferWorker, track_worker, FileStreamReadWorker, MediaStreamServer, _StreamServerStartWorker
from editor_widgets import CodeEditor, make_highlighter, LANG_LABEL
import ai_assist

# QtMultimedia is an optional Qt component — most PyQt5 installs on macOS
# and Linux ship it, but guard the import so a system missing the
# multimedia plugin doesn't break the whole app, just the media player.
try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
    from PyQt5.QtMultimediaWidgets import QVideoWidget
    _MULTIMEDIA_AVAILABLE = True
except Exception:
    _MULTIMEDIA_AVAILABLE = False


# ─── OS-style file-transfer dialog ───────────────────────────
class FileTransferDialog(QDialog):
    """OS-style file-transfer sheet with animated progress, speed and ETA."""

    def __init__(self, parent, sftp, direction: str, local_path: str, remote_path: str,
                 host: str = None, port: int = 22, user: str = None, pem: str = None,
                 password: str = None, sudo_user: str = None):
        super().__init__(parent)
        self._direction  = direction
        self._local      = local_path
        self._remote     = remote_path
        self._speed_buf  = []
        self._done       = False

        verb  = "Uploading" if direction == "upload" else "Downloading"
        fname = os.path.basename(local_path)
        self.setWindowTitle(f"{verb} — {fname}")
        self.setFixedWidth(480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(14)

        icon_row = QHBoxLayout()
        icon_row.setSpacing(14)
        icon_lbl = QLabel("⬇" if direction == "download" else "⬆")
        icon_lbl.setFont(QFont("Segoe UI Emoji", 28))
        icon_lbl.setStyleSheet(f"color: {T['ACCENT']}; background: transparent;")
        icon_lbl.setFixedWidth(46)
        icon_row.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        action_lbl = QLabel(f"{verb}…")
        action_lbl.setFont(QFont("Segoe UI", 13, QFont.Bold))
        action_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; background: transparent;")
        text_col.addWidget(action_lbl)
        self.file_lbl = QLabel(fname)
        self.file_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px; background: transparent;")
        self.file_lbl.setWordWrap(True)
        text_col.addWidget(self.file_lbl)
        icon_row.addLayout(text_col, 1)
        lay.addLayout(icon_row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.bar.setStyleSheet(f"""
            QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {T['ACCENT']}, stop:1 {T['ACCENT2']});
                border-radius: 3px;
            }}
        """)
        lay.addWidget(self.bar)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(0)
        self.xfer_lbl  = QLabel("0 B / —")
        self.xfer_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
        stats_row.addWidget(self.xfer_lbl)
        stats_row.addStretch()
        self.speed_lbl = QLabel("")
        self.speed_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
        stats_row.addWidget(self.speed_lbl)
        stats_row.addSpacing(16)
        self.eta_lbl   = QLabel("")
        self.eta_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
        stats_row.addWidget(self.eta_lbl)
        lay.addLayout(stats_row)

        dest = remote_path if direction == "upload" else local_path
        dest_lbl = QLabel(f"To:  {dest}")
        dest_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px; background: transparent;")
        dest_lbl.setWordWrap(True)
        lay.addWidget(dest_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setFixedWidth(90)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._update_speed)

        self._t0 = time.monotonic()

        # Use real `scp` on the app's host machine when we can: a plain
        # (non-sudo) connection, whether key- or password-authenticated —
        # ScpTransferWorker answers scp's password prompt itself over the
        # pty it already opens for the progress meter. A sudo-target
        # transfer still needs the SudoFS two-hop dance (upload to a tmp
        # path, then `sudo mv` over ssh), which a single scp invocation
        # can't express, so that keeps using the SFTP path.
        use_scp = bool(host) and bool(user) and not sudo_user and (bool(pem) or bool(password))
        total_size = None
        if use_scp:
            try:
                if direction == "upload":
                    total_size = os.path.getsize(local_path)
                else:
                    total_size = sftp.stat(remote_path).st_size
            except Exception:
                total_size = None

        if use_scp:
            self._worker = ScpTransferWorker(
                host, port, user, pem, password, direction, local_path, remote_path, total_size
            )
        else:
            self._worker = _TransferWorker(sftp, direction, local_path, remote_path)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_success)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()
        self._timer.start()

    def _on_progress(self, done: int, total: int):
        elapsed = max(time.monotonic() - self._t0, 0.001)
        speed   = done / elapsed
        if total > 0:
            self.bar.setValue(int(done / total * 1000))
            self.xfer_lbl.setText(f"{size_fmt(done)} / {size_fmt(total)}")
            if speed > 0:
                self.eta_lbl.setText(f"ETA {self._fmt_eta((total - done) / speed)}")
        else:
            self.bar.setRange(0, 0)
            self.xfer_lbl.setText(f"{size_fmt(done)} / —")
        self._speed_buf.append((time.monotonic(), done))
        cutoff = time.monotonic() - 3.0
        self._speed_buf = [(t, b) for t, b in self._speed_buf if t >= cutoff]

    def _update_speed(self):
        if len(self._speed_buf) >= 2:
            t0, b0 = self._speed_buf[0]
            t1, b1 = self._speed_buf[-1]
            dt = t1 - t0
            if dt > 0:
                self.speed_lbl.setText(f"{size_fmt(int((b1 - b0) / dt))}/s")

    def _on_success(self):
        self._done = True
        self._timer.stop()
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        self.bar.setStyleSheet(f"""
            QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
            QProgressBar::chunk {{ background: {T['SUCCESS']}; border-radius: 3px; }}
        """)
        self.speed_lbl.setText("Done")
        self.eta_lbl.setText("")
        self.cancel_btn.setText("Close")
        self.cancel_btn.setObjectName("success")
        self.cancel_btn.setStyleSheet(
            f"background: transparent; border: 1px solid {T['SUCCESS']}; "
            f"color: {T['SUCCESS']}; border-radius: 7px; padding: 7px 16px;"
        )
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.accept)
        QTimer.singleShot(1200, self.accept)

    def _on_error(self, msg: str):
        self._done = True
        self._timer.stop()
        self.bar.setRange(0, 1000)
        self.bar.setStyleSheet(f"""
            QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
            QProgressBar::chunk {{ background: {T['DANGER']}; border-radius: 3px; }}
        """)
        self.speed_lbl.setText("Failed")
        self.eta_lbl.setText("")
        self.file_lbl.setText(f"Error: {msg}")
        self.file_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px; background: transparent;")
        self.cancel_btn.setText("Close")
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.reject)

    def _on_cancel(self):
        if not self._done:
            self._worker.cancel()
            self._timer.stop()
        self.reject()

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"

    @classmethod
    def download(cls, parent, sftp, remote_path: str, local_path: str,
                 host: str = None, port: int = 22, user: str = None, pem: str = None,
                 password: str = None, sudo_user: str = None) -> bool:
        dlg = cls(parent, sftp, "download", local_path, remote_path,
                  host=host, port=port, user=user, pem=pem, password=password,
                  sudo_user=sudo_user)
        return dlg.exec_() == QDialog.Accepted

    @classmethod
    def upload(cls, parent, sftp, local_path: str, remote_path: str,
               host: str = None, port: int = 22, user: str = None, pem: str = None,
               password: str = None, sudo_user: str = None) -> bool:
        dlg = cls(parent, sftp, "upload", local_path, remote_path,
                  host=host, port=port, user=user, pem=pem, password=password,
                  sudo_user=sudo_user)
        return dlg.exec_() == QDialog.Accepted


# ─── Connect dialog ───────────────────────────────────────────
class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Server")
        self.setFixedWidth(500)
        apply_qss_to(self)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("SSH Connection")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; margin-bottom: 4px;")
        layout.addWidget(title)

        # ── Recent instances ──────────────────────────────────
        recent = load_recent_instances()
        if recent:
            recent_lbl = QLabel("RECENT INSTANCES")
            recent_lbl.setStyleSheet(
                f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 700; "
                f"letter-spacing: 1px; padding: 4px 0 2px 0;"
            )
            layout.addWidget(recent_lbl)
            self.recent_list = QListWidget()
            self.recent_list.setObjectName("recent_list")
            self.recent_list.setMaximumHeight(130)
            self.recent_list.setFocusPolicy(Qt.NoFocus)
            for inst in recent:
                # Show alias if set, otherwise fall back to user@host:port
                alias = inst.get("alias", "").strip()
                if alias:
                    label = f"  🖥  {alias}  —  {inst['user']}@{inst['host']}:{inst['port']}"
                else:
                    label = f"  🖥  {inst['user']}@{inst['host']}:{inst['port']}"
                if inst.get("pem"):
                    label += "  🔑"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, inst)
                self.recent_list.addItem(item)
            self.recent_list.itemClicked.connect(self._fill_from_recent)
            self.recent_list.itemDoubleClicked.connect(self._fill_and_accept)
            layout.addWidget(self.recent_list)
            div = QFrame()
            div.setFrameShape(QFrame.HLine)
            div.setStyleSheet(f"color: {T['BORDER']}; margin: 4px 0;")
            layout.addWidget(div)
        else:
            self.recent_list = None

        fields_lbl = QLabel("CONNECTION DETAILS")
        fields_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 700; "
            f"letter-spacing: 1px; padding: 2px 0;"
        )
        layout.addWidget(fields_lbl)

        self.host_input  = self._field("ec2-xx-xx-xx-xx.compute.amazonaws.com")
        self.port_input  = self._field("22")
        import getpass
        self.user_input  = self._field(getpass.getuser())
        self.pem_input   = self._field("/home/user/.ssh/key.pem")
        self.password    = self._field("password", password=True)
        self.alias_input = self._field("e.g. prod-web, staging-db  (optional)")

        pem_row = QHBoxLayout()
        pem_row.setSpacing(6)
        pem_row.addWidget(self.pem_input)
        browse = QPushButton("Browse")
        browse.setFixedWidth(70)
        browse.clicked.connect(self._browse_pem)
        pem_row.addWidget(browse)

        for label, field in [
            ("Host / IP",  self.host_input),
            ("Port",       self.port_input),
            ("Username",   self.user_input),
            ("Password",   self.password),
        ]:
            layout.addWidget(QLabel(label))
            layout.addWidget(field)

        layout.addWidget(QLabel("PEM Key  (leave blank for password / agent auth)"))
        layout.addLayout(pem_row)

        layout.addWidget(QLabel("Alias  (optional — shown in recent list and status bar)"))
        layout.addWidget(self.alias_input)

        layout.addSpacing(6)

        localhost_btn = QPushButton("⚡  Connect to Localhost")
        localhost_btn.setObjectName("success")
        localhost_btn.clicked.connect(self._fill_localhost)
        layout.addWidget(localhost_btn)
        layout.addSpacing(4)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Connect")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _field(self, hint: str = "", password: bool = False) -> QLineEdit:
        w = QLineEdit()
        w.setPlaceholderText(hint)
        if password:
            w.setEchoMode(QLineEdit.Password)
        return w

    def _browse_pem(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select PEM Key", "",
            "Private Key Files (*.pem *.privkey);;All Files (*)"
        )
        if path:
            self.pem_input.setText(path)

    def _fill_from_recent(self, item):
        inst = item.data(Qt.UserRole)
        self.host_input.setText(inst.get("host", ""))
        self.port_input.setText(inst.get("port", "22"))
        self.user_input.setText(inst.get("user", ""))
        self.pem_input.setText(inst.get("pem", ""))
        self.alias_input.setText(inst.get("alias", ""))

    def _fill_and_accept(self, item):
        self._fill_from_recent(item)
        self.accept()

    def _fill_localhost(self):
        import getpass
        self.host_input.setText("127.0.0.1")
        self.port_input.setText("22")
        self.user_input.setText(getpass.getuser())
        self.pem_input.clear()
        self.alias_input.clear()

    def values(self) -> tuple:
        """Returns (host, port, user, pem, password, alias)."""
        return (
            self.host_input.text().strip(),
            int(self.port_input.text().strip() or "22"),
            self.user_input.text().strip(),
            self.pem_input.text().strip(),
            self.password.text().strip(),
            self.alias_input.text().strip(),
        )


# ─── "Connecting…" status dialog ───────────────────────────────
class ConnectingDialog(QDialog):
    """Small status dialog shown while an SSH connection attempt is in
    progress. Has no Cancel/Close button — it is dismissed programmatically
    by the caller once the attempt succeeds or fails."""

    def __init__(self, parent, host: str):
        super().__init__(parent)
        self.setWindowTitle("Connecting")
        self.setFixedSize(340, 150)
        self.setModal(True)
        # No close ("X") button — this dialog is closed programmatically.
        self.setWindowFlags(
            Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint
        )
        apply_qss_to(self)
        self.setStyleSheet(self.styleSheet() + f"QDialog {{ background: {T['BG_PANEL']}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(14)

        icon_lbl = QLabel("🔌")
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setFont(QFont("Segoe UI Emoji", 28))
        icon_lbl.setStyleSheet("background: transparent;")
        layout.addWidget(icon_lbl)

        self.text_lbl = QLabel(f"Connecting to {host}…")
        self.text_lbl.setAlignment(Qt.AlignCenter)
        self.text_lbl.setWordWrap(True)
        self.text_lbl.setStyleSheet(
            f"background: transparent; color: {T['TEXT_PRIMARY']}; "
            f"font-size: 13px; font-weight: 600;"
        )
        layout.addWidget(self.text_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate / "busy" style
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        layout.addWidget(self.bar)

        sub_lbl = QLabel("This may take a few seconds…")
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setStyleSheet(f"background: transparent; color: {T['TEXT_DIM']}; font-size: 13px;")
        layout.addWidget(sub_lbl)

    def set_status(self, text: str):
        self.text_lbl.setText(text)

    def closeEvent(self, event):
        # Prevent the user from dismissing it manually (e.g. Alt+F4);
        # the caller controls its lifecycle.
        event.ignore()


# ─── K8s log viewer ───────────────────────────────────────────
# ─── AI diagnosis result ─────────────────────────────────────
class AIExplainDialog(QDialog):
    """Interactive AI diagnosis workspace.

    The first response is the original one-shot diagnosis.  After it arrives,
    the same dialog becomes a small conversation so the user can ask follow-up
    questions without losing the pod/command evidence that produced the
    diagnosis.
    """

    def __init__(self, parent, title: str, source_context: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 620)
        apply_qss_to(self)

        self._source_context = source_context or ""
        self._diagnosis = ""
        self._conversation = []
        self._followup_worker = None
        self._loading_followup = False
        self._followup_enabled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header = QLabel("✨  AI troubleshooting")
        header.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 15px; font-weight: 700;"
        )
        header_row.addWidget(header)
        header_row.addStretch()
        self.context_lbl = QLabel("Interactive diagnosis")
        self.context_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 11px;"
        )
        header_row.addWidget(self.context_lbl)
        layout.addLayout(header_row)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet(
            f"background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 12px;"
        )
        self.body.document().setDefaultStyleSheet(
            "h1, h2, h3 { margin-top: 14px; margin-bottom: 6px; }"
            "p, ul, ol { margin-top: 4px; margin-bottom: 4px; }"
        )
        layout.addWidget(self.body, 1)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        for label, prompt in (
            ("Why?", "Why do you think this is the likely cause?"),
            ("Explain evidence", "Explain the evidence behind the diagnosis."),
            ("What should I check?", "What should I check next?"),
        ):
            btn = QPushButton(label)
            btn.setFixedHeight(30)
            btn.clicked.connect(lambda _=False, p=prompt: self.ask(p))
            quick_row.addWidget(btn)
        quick_row.addStretch()
        layout.addLayout(quick_row)

        ask_row = QHBoxLayout()
        ask_row.setSpacing(8)
        self.question_input = QLineEdit()
        self.question_input.setPlaceholderText(
            "Ask a follow-up about this diagnosis…"
        )
        self.question_input.returnPressed.connect(self._send_followup)
        self.question_input.setEnabled(False)
        ask_row.addWidget(self.question_input, 1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.setFixedHeight(34)
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._send_followup)
        ask_row.addWidget(self.send_btn)
        layout.addLayout(ask_row)

        btn_row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy conversation")
        self.copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.body.toPlainText())
        )
        btn_row.addWidget(self.copy_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._loading_timer = QTimer(self)
        self._loading_timer.timeout.connect(self._update_loading_text)
        self._loading_dots = 0

        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._type_next_chunk)
        self._response_text = ""
        self._typed_position = 0

        # Separate timer for follow-up responses so their animation does not
        # interfere with the initial diagnosis typing state.
        self._followup_typing_timer = QTimer(self)
        self._followup_typing_timer.timeout.connect(self._type_followup_chunk)
        self._followup_response_text = ""
        self._followup_typed_position = 0

    def set_source_context(self, text: str):
        self._source_context = (text or "").strip()

    def set_loading(self):
        self._typing_timer.stop()
        self._loading_timer.stop()
        self._loading_dots = 0
        self._followup_enabled = False
        self.question_input.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.body.setPlainText("Thinking")
        self._loading_timer.start(400)

    def _update_loading_text(self):
        self._loading_dots = (self._loading_dots + 1) % 4
        self.body.setPlainText(f"Thinking{'.' * self._loading_dots}")

    def stop_loading(self):
        self._loading_timer.stop()

    def set_markdown(self, text: str):
        self.stop_loading()
        self._response_text = text or ""
        self._typed_position = 0
        self.body.clear()
        if not self._response_text:
            self._enable_followups()
            return
        self._typing_timer.start(25)

    def _type_next_chunk(self):
        if self._typed_position >= len(self._response_text):
            self._typing_timer.stop()
            self.body.setMarkdown(self._response_text)
            self._diagnosis = self._response_text
            self._conversation = [{"role": "assistant", "content": self._response_text}]
            self._enable_followups()
            return

        next_position = min(self._typed_position + 3, len(self._response_text))
        self.body.setPlainText(self._response_text[:next_position])
        self._typed_position = next_position
        scrollbar = self.body.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _enable_followups(self):
        self._followup_enabled = True
        self.question_input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.question_input.setFocus()

    def ask(self, question: str):
        if not self._followup_enabled or self._loading_followup:
            return
        self.question_input.setText(question)
        self._send_followup()

    def _send_followup(self):
        if not self._followup_enabled or self._loading_followup:
            return
        question = self.question_input.text().strip()
        if not question:
            return
        self.question_input.clear()
        self._loading_followup = True
        self.question_input.setEnabled(False)
        self.send_btn.setEnabled(False)
        self._conversation.append({"role": "user", "content": question})
        self._render_conversation()
        self._append_thinking()

        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            self.append_followup_error(
                "No AI API key is configured. Add one in Settings → 🤖 AI."
            )
            return

        worker = ai_assist.AIConversationWorker(
            provider,
            api_key,
            ai_assist.get_model(provider),
            self._source_context,
            self.conversation_payload()[:-1],
            question,
        )
        worker.done.connect(
            lambda answer, q=question: self.append_followup(q, answer)
        )
        worker.error.connect(self.append_followup_error)
        worker.finished.connect(self._followup_finished)
        self._followup_worker = worker
        worker.start()

    def _append_user(self, text: str):
        safe = html_escape(text).replace("\n", "<br>")
        self.body.append(
            f'<br><div style="margin-top:8px;">'
            f'<span style="color:{T["ACCENT"]}; font-weight:700;">You</span><br>'
            f'<span style="color:{T["TEXT_PRIMARY"]};">{safe}</span></div>'
        )

    def _append_thinking(self):
        self.body.append(
            f'<br><span style="color:{T["TEXT_MUTED"]};">'
            f'🤖 Thinking about that…</span>'
        )
        self.body.verticalScrollBar().setValue(self.body.verticalScrollBar().maximum())

    def _render_conversation(self):
        parts = []
        for item in self._conversation:
            role = item.get("role")
            content = str(item.get("content", ""))
            if role == "user":
                parts.append(f"### 👤 You\n\n{content}")
            else:
                parts.append(f"### 🤖 AI\n\n{content}")
        self.body.setMarkdown("\n\n---\n\n".join(parts))
        self.body.verticalScrollBar().setValue(self.body.verticalScrollBar().maximum())

    def append_followup(self, question: str, answer: str):
        """Animate a completed follow-up answer into the conversation."""
        self._followup_typing_timer.stop()
        self._followup_response_text = answer or ""
        self._followup_typed_position = 0

        # Add an empty assistant message; it will be filled progressively.
        self._conversation.append({"role": "assistant", "content": ""})
        self._render_conversation()

        if not self._followup_response_text:
            self._finish_followup_typing()
            return

        self._followup_typing_timer.start(25)

    def _type_followup_chunk(self):
        if not self._conversation:
            self._finish_followup_typing()
            return

        if self._followup_typed_position >= len(self._followup_response_text):
            self._finish_followup_typing()
            return

        next_position = min(self._followup_typed_position + 3,
                            len(self._followup_response_text))
        self._followup_typed_position = next_position
        self._conversation[-1]["content"] = self._followup_response_text[:next_position]

        # Plain text during animation avoids expensive Markdown reflow on every tick.
        self.body.setPlainText(self._conversation_to_plain_text())
        scrollbar = self.body.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _conversation_to_plain_text(self):
        parts = []
        for item in self._conversation:
            label = "👤 You" if item.get("role") == "user" else "🤖 AI"
            parts.append(f"{label}\n\n{item.get('content', '')}")
        return "\n\n────────────────────────\n\n".join(parts)

    def _finish_followup_typing(self):
        self._followup_typing_timer.stop()
        self._render_conversation()
        self._followup_response_text = ""
        self._followup_typed_position = 0
        self._loading_followup = False
        self.question_input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.question_input.setFocus()

    def _followup_finished(self):
        """Release the dialog-local worker reference after a follow-up ends."""
        self._followup_worker = None

    def append_followup_error(self, message: str):
        # Keep the conversation intact and show the error as a transient final
        # message; the next question can be asked normally.
        self._render_conversation()
        safe = html_escape(message or "Unknown AI error").replace("\n", "<br>")
        self.body.append(
            f'<br><span style="color:{T["DANGER"]};">⚠ {safe}</span>'
        )
        self._loading_followup = False
        self.question_input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.question_input.setFocus()

    def conversation_payload(self):
        return list(self._conversation)

    def source_context(self):
        return self._source_context

    def set_error(self, message: str):
        self.stop_loading()
        self._typing_timer.stop()
        self._response_text = ""
        self._typed_position = 0
        self.body.setPlainText(f"⚠ {message}")
        self._followup_enabled = False
        self.question_input.setEnabled(False)
        self.send_btn.setEnabled(False)

    def closeEvent(self, event):
        self._loading_timer.stop()
        self._typing_timer.stop()
        self._followup_typing_timer.stop()
        if self._followup_worker is not None:
            try:
                self._followup_worker.quit()
            except Exception:
                pass
        super().closeEvent(event)


class LogViewerDialog(QDialog):
    """Live-tails pod logs (kubectl logs -f) instead of one-shot fetches.
    Uses _ExecStreamWorker (same streaming machinery as FileExecDialog) to
    keep pushing new lines into the view as they arrive over SSH, rather
    than requiring the user to click Refresh."""

    def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None, context=None):
        super().__init__(parent)
        self.ssh            = ssh
        self._pod           = pod
        self._ns            = namespace
        self._container     = container
        self._context       = context
        self._workers       = []
        self._stream_worker = None   # the currently-running _ExecStreamWorker, if any
        self._ai_worker     = None   # the currently-running AIExplainWorker, if any
        self._ai_dialog     = None

        self.setWindowTitle(f"Logs — {pod}")
        self.resize(900, 600)
        apply_qss_to(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Lines:"))
        self.lines_spin = QSpinBox()
        self.lines_spin.setRange(10, 5000)
        self.lines_spin.setValue(200)
        self.lines_spin.setFixedWidth(80)
        ctrl.addWidget(self.lines_spin)

        self.prev_chk = QCheckBox("Previous container")
        self.prev_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        ctrl.addWidget(self.prev_chk)

        self.autoscroll_chk = QCheckBox("Autoscroll")
        self.autoscroll_chk.setChecked(True)
        self.autoscroll_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        ctrl.addWidget(self.autoscroll_chk)

        ctrl.addStretch()

        self.status_lbl = QLabel("○ connecting…")
        self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")
        ctrl.addWidget(self.status_lbl)

        restart_btn = QPushButton("↺  Restart")
        restart_btn.setToolTip("Restart the live stream (e.g. after changing Lines / Previous container)")
        restart_btn.clicked.connect(self._load)
        ctrl.addWidget(restart_btn)

        clear_btn = QPushButton("🧹  Clear")
        clear_btn.setToolTip("Clear the screen — the live stream keeps running in the background")
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        ctrl.addWidget(clear_btn)

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
        ctrl.addWidget(copy_btn)

        self.explain_btn = QPushButton("✨  Analyze with AI")
        self.explain_btn.setToolTip("Ask AI to diagnose this log output")
        self.explain_btn.clicked.connect(self._on_explain)
        ctrl.addWidget(self.explain_btn)
        layout.addLayout(ctrl)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(monospace_font(11))
        self.log_view.setStyleSheet(f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;")
        layout.addWidget(self.log_view)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        self._load()

    def _build_log_cmd(self, follow: bool) -> str:
        n    = self.lines_spin.value()
        prev = "--previous" if self.prev_chk.isChecked() else ""
        c    = f"-c {self._container}" if self._container else ""
        f    = "-f" if follow else ""
        inner = f"kubectl logs --tail={n} -n {self._ns} {prev} {c} {f} {self._pod} 2>&1"
        # exec_command() opens a non-login shell, which skips /etc/profile —
        # exactly where kubectl's PATH entry usually lives (Homebrew, snap,
        # etc.). "bash -lc" forces a login shell so those get sourced.
        return f"bash -lc {shlex.quote(inner)}"

    def _load(self):
        # Kill whatever stream is already running before starting a fresh one
        # (e.g. user changed Lines / Previous container and hit Restart).
        if self._stream_worker is not None:
            self._stream_worker.request_stop()
            self._stream_worker = None

        # -f and --previous don't mix meaningfully (a terminated container's
        # logs can't be followed), so fall back to a one-shot fetch for that case.
        follow = not self.prev_chk.isChecked()
        cmd = self._build_log_cmd(follow=follow)

        if not follow:
            self.log_view.setPlainText("Loading…")
            self.status_lbl.setText("○ static (previous container)")
            self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")
            worker = CommandWorker(self.ssh, cmd)
            worker.done.connect(self.log_view.setPlainText)
            worker.error.connect(lambda e: self.log_view.setPlainText(f"[error] {e}"))
            track_worker(self._workers, worker)
            worker.start()
            return

        self.log_view.clear()
        self.status_lbl.setText("● live")
        self.status_lbl.setStyleSheet(f"color: {T['SUCCESS']};")

        worker = _ExecStreamWorker(self.ssh, cmd)
        worker.line.connect(self._on_stream_line)
        worker.error.connect(self._on_stream_error)
        worker.finished.connect(self._on_stream_finished)
        self._stream_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _on_stream_line(self, text: str):
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4   # checked BEFORE inserting new text
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(text)
        if self.autoscroll_chk.isChecked() and at_bottom:
            sb.setValue(sb.maximum())

    def _on_stream_error(self, err: str):
        self.status_lbl.setText("● error")
        self.status_lbl.setStyleSheet(f"color: {T['DANGER']};")
        self._on_stream_line(f"\n[stream error] {err}\n")

    def _on_stream_finished(self, code: int):
        # track_worker() already removes the worker from self._workers once
        # its finished signal fires; only the dialog-local reference needs
        # clearing here.
        self._stream_worker = None
        if "error" not in self.status_lbl.text():
            self.status_lbl.setText("○ stream ended")
            self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")

    # ── AI diagnosis ──────────────────────────────────────────
    def _on_explain(self):
        if self._ai_worker is not None:
            return  # already running — button is disabled while it is, but be defensive

        log_text = self.log_view.toPlainText().strip()
        if not log_text or log_text == "Loading…":
            QMessageBox.information(self, "Nothing to explain", "There's no log output yet.")
            return

        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
            QMessageBox.information(
                self, "No API key set",
                f"Add a {label} API key in Settings → 🤖 AI to use this feature."
            )
            return

        self._ai_dialog = AIExplainDialog(self, f"AI diagnosis — {self._pod}")
        self._ai_dialog.set_source_context(
            f"Pod: {self._pod}\nNamespace: {self._ns}\n"
            f"Container: {self._container or 'default'}\n\nLogs/evidence:\n{log_text}"
        )
        self._ai_dialog.set_loading()
        self._ai_dialog.show()

        self.explain_btn.setEnabled(False)
        self.explain_btn.setText("✨  Thinking…")

        worker = ai_assist.AIExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            self._pod, self._ns, self._container, log_text
        )
        worker.done.connect(self._on_explain_done)
        worker.error.connect(self._on_explain_error)
        self._ai_worker = worker
        worker.finished.connect(self._on_explain_finished)
        worker.start()

    def _on_explain_done(self, text: str):
        if self._ai_dialog is not None:
            self._ai_dialog.set_markdown(text)

    def _on_explain_error(self, message: str):
        if self._ai_dialog is not None:
            self._ai_dialog.set_error(message)

    def _on_explain_finished(self):
        self._ai_worker = None
        self.explain_btn.setEnabled(True)
        self.explain_btn.setText("✨  Analyze with AI")

    def closeEvent(self, event):
        # Stop the SSH channel/thread rather than leaking it once the dialog closes.
        if self._stream_worker is not None:
            self._stream_worker.request_stop()
        if self._ai_worker is not None:
            self._ai_worker.quit()
        super().closeEvent(event)


# ─── K8s exec dialog ─────────────────────────────────────────
class ContainerPickerDialog(QDialog):
    """Shown before opening ExecDialog when the target pod has more than
    one container. kubectl exec silently targets the pod's first
    container when -c isn't given — harmless for a single-container pod,
    but easy to exec into the wrong place once a pod has a sidecar
    (istio-proxy, log shippers, init containers, etc.), so ask instead of
    guessing whenever there's an actual choice."""

    def __init__(self, parent, pod: str, containers: list):
        super().__init__(parent)
        self.setWindowTitle(f"Select Container — {pod}")
        apply_qss_to(self)
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        info = QLabel(f'"{pod}" has {len(containers)} containers — pick one to exec into:')
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        layout.addWidget(info)

        self.combo = QComboBox()
        self.combo.addItems(containers)
        layout.addWidget(self.combo)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = bb.button(QDialogButtonBox.Ok)
        ok_btn.setObjectName("primary")
        ok_btn.setText("Exec")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def selected_container(self) -> str:
        return self.combo.currentText()


class ExecDialog(QDialog):
    def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None):
        super().__init__(parent)
        self.ssh        = ssh
        self._pod       = pod
        self._ns        = namespace
        self._container = container
        self._cwd       = None
        self._workers   = []

        # State for the "Analyze with AI" AI feature — the last command run
        # (not counting `cd`, which has its own distinct failure message
        # already) plus its exit code and raw stdout/stderr, so a click on
        # the button doesn't need to re-derive any of that from the
        # combined text already sitting in self.output.
        self._last_cmd        = None
        self._last_exit_code  = None
        self._last_stdout     = ""
        self._last_stderr     = ""
        self._ai_worker       = None
        self._ai_dialog       = None

        self.setWindowTitle(f"Exec — {pod}")
        self.resize(860, 500)
        apply_qss_to(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(f"Running commands inside <b>{pod}</b>  (namespace: {namespace})")
        info.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        layout.addWidget(info)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(monospace_font(11))
        self.output.setStyleSheet(f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;")
        layout.addWidget(self.output)

        inp_row = QHBoxLayout()
        self.cmd_inp = QLineEdit()
        self.cmd_inp.setPlaceholderText("$ command inside pod (cd, $VAR, pipes all work)…")
        self.cmd_inp.returnPressed.connect(self._run)
        inp_row.addWidget(self.cmd_inp)

        run_btn = QPushButton("Run")
        run_btn.setObjectName("primary")
        run_btn.clicked.connect(self._run)
        inp_row.addWidget(run_btn)

        self.explain_btn = QPushButton("✨  Analyze with AI")
        self.explain_btn.setToolTip("Ask AI to diagnose the last failed command")
        self.explain_btn.setEnabled(False)
        self.explain_btn.clicked.connect(self._on_explain)
        inp_row.addWidget(self.explain_btn)
        layout.addLayout(inp_row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _run(self):
        cmd = self.cmd_inp.text().strip()
        if not cmd:
            return
        c = f"-c {self._container}" if self._container else ""

        cd_match = re.match(r'^\s*cd\s*(.*?)\s*$', cmd)
        if cd_match:
            target = cd_match.group(1) or ''
            if target in ('', '~'):
                resolve_cmd = 'echo $HOME'
            elif target.startswith('/'):
                resolve_cmd = f"cd '{target}' 2>/dev/null && pwd || echo __FAIL__"
            else:
                prefix = f"cd '{self._cwd}' && " if self._cwd else ""
                resolve_cmd = f"{prefix}cd '{target}' 2>/dev/null && pwd || echo __FAIL__"
            safe = resolve_cmd.replace("'", "'\\''")
            full = f"kubectl exec -n {self._ns} {self._pod} {c} -- sh -c '{safe}' 2>&1"
            append_terminal_html(self.output, f"\n<span style='color:{T['ACCENT2']}'>$ {html_escape(cmd)}</span>")
            worker = CommandWorker(self.ssh, full)
            worker.done.connect(self._handle_cd_result)
            worker.error.connect(lambda e: append_terminal_html(self.output, f"<span style='color:{T['DANGER']}'>[error] {html_escape(e)}</span>"))
            track_worker(self._workers, worker)
            worker.start()
            self.cmd_inp.clear()
            return

        safe_cmd = (f"cd '{self._cwd}' && {cmd}" if self._cwd else cmd).replace("'", "'\\''")
        full = f"kubectl exec -n {self._ns} {self._pod} {c} -- sh -c '{safe_cmd}' 2>&1"
        append_terminal_html(self.output, f"\n<span style='color:{T['ACCENT2']}'>$ {html_escape(cmd)}</span>")
        worker = CommandWorker(self.ssh, full)
        worker.result.connect(lambda out, err, code, cmd=cmd: self._on_cmd_result(cmd, out, err, code))
        worker.done.connect(lambda r: append_terminal_text(self.output, r))
        worker.error.connect(lambda e: append_terminal_html(self.output, f"<span style='color:{T['DANGER']}'>[error] {html_escape(e)}</span>"))
        track_worker(self._workers, worker)
        worker.start()
        self.cmd_inp.clear()

    def _on_cmd_result(self, cmd: str, out: str, err: str, exit_code: int):
        """Remembers the last command's real exit code/stdout/stderr (the
        `2>&1` baked into `full` above means `err` from CommandWorker
        itself is usually empty — the actual error text lives in `out` —
        so the Explain button/prompt fall back to `out` when `err` is
        blank; see ai_assist._build_command_prompt)."""
        self._last_cmd       = cmd
        self._last_stdout    = out
        self._last_stderr    = err
        self._last_exit_code = exit_code
        failed = exit_code != 0 or bool((err or "").strip())
        self.explain_btn.setEnabled(failed)

    # ── AI diagnosis of the last failed command ────────────────
    def _on_explain(self):
        if self._ai_worker is not None or self._last_cmd is None:
            return  # already running, or nothing to explain yet

        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
            QMessageBox.information(
                self, "No API key set",
                f"Add a {label} API key in Settings → 🤖 AI to use this feature."
            )
            return

        self._ai_dialog = AIExplainDialog(self, f"AI diagnosis — {self._last_cmd}")
        self._ai_dialog.set_source_context(
            f"Command: {self._last_cmd}\n"
            f"Exit code: {self._last_exit_code}\n\n"
            f"stderr:\n{self._last_stderr or '(none)'}\n\n"
            f"stdout:\n{self._last_stdout or '(none)'}"
        )
        self._ai_dialog.set_loading()
        self._ai_dialog.show()

        self.explain_btn.setEnabled(False)
        self.explain_btn.setText("✨  Thinking…")

        worker = ai_assist.AICommandExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            self._last_cmd, self._last_exit_code, self._last_stderr, self._last_stdout,
        )
        worker.done.connect(self._on_explain_done)
        worker.error.connect(self._on_explain_error)
        worker.finished.connect(self._on_explain_finished)
        self._ai_worker = worker
        worker.start()

    def _on_explain_done(self, text: str):
        if self._ai_dialog is not None:
            self._ai_dialog.set_markdown(text)

    def _on_explain_error(self, message: str):
        if self._ai_dialog is not None:
            self._ai_dialog.set_error(message)

    def _on_explain_finished(self):
        self._ai_worker = None
        self.explain_btn.setEnabled(True)
        self.explain_btn.setText("✨  Analyze with AI")

    def closeEvent(self, event):
        if self._ai_worker is not None:
            self._ai_worker.quit()
        super().closeEvent(event)

    def _handle_cd_result(self, result: str):
        result = result.strip()
        if result and not result.startswith('__FAIL__') and result.startswith('/'):
            self._cwd = result
            append_terminal_html(self.output, f"<span style='color:{T['TEXT_DIM']}'>{html_escape(result)}</span>")
            self.setWindowTitle(f"Exec — {self._pod}  [{result}]")
        else:
            append_terminal_html(self.output, f"<span style='color:{T['DANGER']}'>cd: no such directory</span>")


# ─── File Editor ──────────────────────────────────────────────
class FileEditorDialog(QDialog):
    """Remote file editor with find/replace, line numbers, and save-back."""

    def __init__(self, parent, sftp, ssh, remote_path: str, content=None, sudo_user=None):
        super().__init__(parent)
        self._sftp      = sftp
        self._ssh       = ssh
        self._remote    = remote_path
        self._sudo_user = sudo_user
        self._original  = content or ""
        self._matches    = []   # [(start, end), ...] offsets of current find matches
        self._match_idx  = -1
        self._highlighter = None

        # When content is None, the editor opens immediately and streams
        # the file in live in the background (see _start_live_load) rather
        # than blocking behind a separate "downloading" dialog first.
        self._loading           = content is None
        self._load_worker       = None
        self._decoder           = None
        self._chunks_since_sync = 0

        fname = os.path.basename(remote_path)
        self.setWindowTitle(f"Edit — {fname}")
        self.resize(960, 700)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Toolbar ───────────────────────────────────────────
        tb_widget = QWidget()
        tb_widget.setFixedHeight(44)
        tb_widget.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        tb = QHBoxLayout(tb_widget)
        tb.setContentsMargins(10, 0, 10, 0)
        tb.setSpacing(6)

        # ── Filename chip ─────────────────────────────────────
        # File identity (icon + name + unsaved state) reads as one unit
        # instead of an icon, a full path label, and a separate warning
        # label competing for attention across the same row.
        chip = QFrame()
        chip.setObjectName("file_chip")
        chip_lay = QHBoxLayout(chip)
        chip_lay.setContentsMargins(10, 4, 10, 4)
        chip_lay.setSpacing(6)
        icon_lbl = QLabel("📝")
        icon_lbl.setStyleSheet("font-size: 14px; background: transparent;")
        chip_lay.addWidget(icon_lbl)
        path_lbl = QLabel(fname)
        path_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 13px; font-weight: 600; background: transparent;")
        path_lbl.setToolTip(remote_path)
        chip_lay.addWidget(path_lbl)
        self._modified_dot = QLabel("unsaved")
        self._modified_dot.setStyleSheet(
            f"background: {T['WARNING']}; color: #1a1a1a; font-size: 10px; font-weight: 700; "
            f"border-radius: 8px; padding: 1px 8px;"
        )
        self._modified_dot.hide()
        chip_lay.addWidget(self._modified_dot)
        chip.setStyleSheet(f"QFrame#file_chip {{ background: {T['BG_ITEM']}; border-radius: 14px; }}")
        tb.addWidget(chip)
        tb.addStretch()

        # ── View controls: segmented zoom + icon toggles ──────
        zoom_frame = QFrame()
        zoom_frame.setObjectName("zoom_group")
        zf = QHBoxLayout(zoom_frame)
        zf.setContentsMargins(2, 2, 2, 2)
        zf.setSpacing(0)

        def _flat_btn(text, tip, size=(28, 26)):
            b = QPushButton(text)
            b.setFixedSize(*size)
            b.setToolTip(tip)
            b.setFlat(True)
            b.setStyleSheet("border: none; background: transparent;")
            return b

        zoom_out_btn = _flat_btn("−", "Zoom out (Ctrl+-)")
        zoom_out_btn.clicked.connect(lambda: (self.editor.zoom_out(), self._update_zoom_label()))
        zf.addWidget(zoom_out_btn)

        self._zoom_lbl = QLabel("100%")
        self._zoom_lbl.setFixedWidth(38)
        self._zoom_lbl.setAlignment(Qt.AlignCenter)
        self._zoom_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px; background: transparent;")
        self._zoom_lbl.setToolTip("Reset zoom (Ctrl+0)")
        zf.addWidget(self._zoom_lbl)

        zoom_in_btn = _flat_btn("+", "Zoom in (Ctrl++)")
        zoom_in_btn.clicked.connect(lambda: (self.editor.zoom_in(), self._update_zoom_label()))
        zf.addWidget(zoom_in_btn)

        zoom_frame.setStyleSheet(f"QFrame#zoom_group {{ background: {T['BG_ITEM']}; border-radius: 7px; }}")
        tb.addWidget(zoom_frame)

        tb.addSpacing(6)

        self._wrap_btn = QPushButton("⇌")
        self._wrap_btn.setCheckable(True)
        self._wrap_btn.setChecked(True)
        self._wrap_btn.setFixedSize(30, 30)
        self._wrap_btn.setToolTip("Wrap lines")
        self._wrap_btn.toggled.connect(self._toggle_wrap)
        tb.addWidget(self._wrap_btn)

        self._find_btn = QPushButton("🔍")
        self._find_btn.setCheckable(True)
        self._find_btn.setFixedSize(30, 30)
        self._find_btn.setToolTip("Find / Replace (Ctrl+F)")
        self._find_btn.toggled.connect(self._toggle_find_bar)
        tb.addWidget(self._find_btn)

        tb.addSpacing(10)

        # ── Actions: one primary CTA, save+close folded into a checkbox
        # next to it rather than a third competing button ─────
        self._save_close_btn = QCheckBox("Close after save")
        self._save_close_btn.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        tb.addWidget(self._save_close_btn)

        discard_btn = QPushButton("Discard")
        discard_btn.setObjectName("danger")
        discard_btn.setFixedHeight(30)
        discard_btn.clicked.connect(self._confirm_close)
        tb.addWidget(discard_btn)

        save_btn = QPushButton("💾  Save")
        save_btn.setObjectName("primary")
        save_btn.setFixedSize(96, 30)
        save_btn.clicked.connect(
            lambda: self._save_and_close() if self._save_close_btn.isChecked() else self._save()
        )
        tb.addWidget(save_btn)
        self._save_btn = save_btn

        lay.addWidget(tb_widget)

        # Thin indeterminate/progress strip shown only while a file is
        # streaming in live (see _start_live_load) — hidden the rest of
        # the time.
        self._load_bar = QProgressBar()
        self._load_bar.setFixedHeight(3)
        self._load_bar.setTextVisible(False)
        self._load_bar.setStyleSheet(
            "QProgressBar {{ border: none; background: {bg}; }}"
            "QProgressBar::chunk {{ background: {ac}; }}".format(
                bg=T['BG_ITEM'], ac=T['ACCENT'])
        )
        self._load_bar.hide()
        lay.addWidget(self._load_bar)

        # ── Find/Replace bar ──────────────────────────────────
        self._find_bar = QWidget()
        self._find_bar.setFixedHeight(44)
        self._find_bar.setStyleSheet(
            f"background: {T['BG_ITEM']}; border-bottom: 1px solid {T['BORDER']};"
        )
        fb = QHBoxLayout(self._find_bar)
        fb.setContentsMargins(10, 6, 10, 6)
        fb.setSpacing(6)

        input_style = (
            f"QLineEdit {{ background: {T['BG_DARK']}; border: 1px solid {T['BORDER']}; "
            f"border-radius: 7px; padding: 5px 10px; }}"
            f"QLineEdit:focus {{ border-color: {T['ACCENT']}; }}"
        )

        self._find_inp = QLineEdit()
        self._find_inp.setPlaceholderText("Find…")
        self._find_inp.setFixedWidth(220)
        self._find_inp.setStyleSheet(input_style)
        self._find_inp.textChanged.connect(self._do_highlight)
        self._find_inp.returnPressed.connect(self._find_next)
        fb.addWidget(self._find_inp)

        self._match_lbl = QLabel("")
        self._match_lbl.setFixedWidth(64)
        self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        fb.addWidget(self._match_lbl)

        for text, tip, slot in [
            ("↑", "Previous match (Shift+Enter)", self._find_prev),
            ("↓", "Next match (Enter)",            self._find_next),
        ]:
            b = QPushButton(text)
            b.setFixedSize(30, 30)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            fb.addWidget(b)

        fb.addSpacing(6)
        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet(f"color: {T['BORDER']};")
        fb.addWidget(divider)
        fb.addSpacing(6)

        self._replace_inp = QLineEdit()
        self._replace_inp.setPlaceholderText("Replace…")
        self._replace_inp.setFixedWidth(220)
        self._replace_inp.setStyleSheet(input_style)
        fb.addWidget(self._replace_inp)

        for label, slot in [
            ("Replace",     self._replace_one),
            ("Replace All", self._replace_all),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(30)
            b.setFixedWidth(96 if "All" in label else 70)
            b.clicked.connect(slot)
            fb.addWidget(b)

        fb.addStretch()

        self._case_chk = QCheckBox("Aa")
        self._case_chk.setToolTip("Match case")
        self._case_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        self._case_chk.stateChanged.connect(self._do_highlight)
        fb.addWidget(self._case_chk)

        self._regex_chk = QCheckBox(".*")
        self._regex_chk.setToolTip("Regular expression")
        self._regex_chk.setStyleSheet(f"color: {T['TEXT_DIM']}; padding-left: 6px;")
        self._regex_chk.stateChanged.connect(self._do_highlight)
        fb.addWidget(self._regex_chk)

        close_find_btn = QPushButton("✕")
        close_find_btn.setFixedSize(26, 26)
        close_find_btn.setToolTip("Close (Esc)")
        close_find_btn.clicked.connect(lambda: self._find_btn.setChecked(False))
        fb.addWidget(close_find_btn)

        self._find_bar.hide()
        lay.addWidget(self._find_bar)

        # ── Editor area ───────────────────────────────────────
        # CodeEditor paints its own line-number gutter directly into the
        # QPlainTextEdit's viewport margin (see editor_widgets.py) — no
        # second widget to keep scrolled in sync, and it comes with a
        # soft current-line highlight and Ctrl+scroll zoom built in.
        self.editor = CodeEditor(base_point_size=12)
        self.editor.setPlainText(content or "")
        self.editor.textChanged.connect(self._on_text_changed)
        lay.addWidget(self.editor, 1)

        # Syntax highlighting — picked from the file extension; falls back
        # to plain text (no highlighter) for unrecognised extensions.
        self._highlighter, self._lang = make_highlighter(self.editor.document(), fname)

        # ── Status bar ────────────────────────────────────────
        sb_widget = QWidget()
        sb_widget.setFixedHeight(26)
        sb_widget.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
        )
        sb = QHBoxLayout(sb_widget)
        sb.setContentsMargins(12, 0, 12, 0)
        sb.setSpacing(16)

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
        sb.addWidget(self._status_lbl)
        sb.addStretch()

        self._cursor_lbl = QLabel("Ln 1, Col 1")
        self._cursor_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        sb.addWidget(self._cursor_lbl)

        ext = os.path.splitext(fname)[1].lower()
        lang_lbl = QLabel(LANG_LABEL.get(self._lang, ext.lstrip(".").upper() if ext else "Plain Text"))
        lang_lbl.setStyleSheet(f"color: {T['ACCENT2']}; font-size: 13px; font-weight: 600;")
        sb.addWidget(lang_lbl)
        lay.addWidget(sb_widget)

        self.editor.cursorPositionChanged.connect(self._update_cursor_pos)

        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._save)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: (self._find_btn.setChecked(True), self._find_inp.setFocus())
        )
        QShortcut(QKeySequence("Escape"), self._find_bar).activated.connect(
            lambda: self._find_btn.setChecked(False)
        )
        QShortcut(QKeySequence("Shift+Return"), self._find_inp).activated.connect(self._find_prev)
        QShortcut(QKeySequence("Ctrl++"), self).activated.connect(
            lambda: (self.editor.zoom_in(), self._update_zoom_label()))
        QShortcut(QKeySequence("Ctrl+="), self).activated.connect(
            lambda: (self.editor.zoom_in(), self._update_zoom_label()))
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(
            lambda: (self.editor.zoom_out(), self._update_zoom_label()))
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(
            lambda: (self.editor.zoom_reset(), self._update_zoom_label()))

        if self._loading:
            self._start_live_load()

    # ── Helpers ───────────────────────────────────────────────
    # Line numbers are painted directly into CodeEditor's own gutter (see
    # editor_widgets.py) and repaint themselves automatically on every
    # block-count/scroll change — nothing to keep in sync here anymore.
    def _update_zoom_label(self):
        pct = round(self.editor._pt / self.editor._base_pt * 100)
        self._zoom_lbl.setText(f"{pct}%")

    def _toggle_wrap(self, on: bool):
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)

    def _toggle_find_bar(self, on: bool):
        self._find_bar.setVisible(on)
        if on:
            self._find_inp.setFocus()
            self._find_inp.selectAll()
            if self._find_inp.text():
                self._do_highlight()
        else:
            self._clear_highlights()

    def _on_text_changed(self):
        self._modified_dot.setVisible(self.editor.toPlainText() != self._original)

    def _update_cursor_pos(self):
        cur = self.editor.textCursor()
        ln  = cur.blockNumber() + 1
        col = cur.columnNumber() + 1
        self._cursor_lbl.setText(f"Ln {ln}, Col {col}")

    def _set_status(self, msg, color=None):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(
            f"color: {color or T['TEXT_MUTED']}; font-size: 13px;"
        )

    # ── Live streaming load ─────────────────────────────────────
    # The editor opens immediately (empty) and content is appended as it
    # streams in from FileStreamReadWorker, instead of blocking behind a
    # separate "downloading" dialog first — you can start reading/
    # scrolling the file as soon as the first chunks land. Save stays
    # disabled until the whole file has arrived, so there's no risk of
    # saving back a partially-loaded file.
    def _start_live_load(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._chunks_since_sync = 0
        self._save_btn.setEnabled(False)
        self._save_close_btn.setEnabled(False)
        self._load_bar.show()
        self._load_bar.setRange(0, 0)
        self._set_status("Loading…", T['WARNING'])

        # Undo/redo would otherwise record every one of the ~240 chunk
        # inserts that make up a big file's load as its own undo command —
        # each holding a copy of the text it inserted, so the undo stack
        # alone would double memory use for as long as the dialog stays
        # open. Off during the bulk load, back on once it's real editing.
        self.editor.setUndoRedoEnabled(False)

        # The modified-dot recompute (toPlainText() == original comparison)
        # is O(n) — doing it on every single 64KB chunk of a big file would
        # add up fast, so it's skipped entirely during the load and only
        # reinstated once the whole file has arrived.
        try:
            self.editor.textChanged.disconnect(self._on_text_changed)
        except Exception:
            pass

        self._load_worker = FileStreamReadWorker(
            self._sftp, self._ssh, self._remote,
            sudo_user=self._sudo_user, max_bytes=self.MAX_EDIT_BYTES,
        )
        self._load_worker.chunk_ready.connect(self._on_load_chunk)
        self._load_worker.progress.connect(self._on_load_progress)
        self._load_worker.finished_ok.connect(self._on_load_finished)
        self._load_worker.finished_err.connect(self._on_load_error)
        self._load_worker.start()

    def _on_load_chunk(self, chunk: bytes):
        try:
            text = self._decoder.decode(chunk)
        except RuntimeError:
            return  # dialog is mid-teardown; drop the chunk
        if not text:
            return
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self._chunks_since_sync += 1

    def _on_load_progress(self, done, total):
        try:
            if total > 0:
                self._load_bar.setRange(0, 1000)
                self._load_bar.setValue(int(done / total * 1000))
                self._set_status(
                    "Loading… {} / {}".format(size_fmt(done), size_fmt(total)), T['WARNING'])
            else:
                self._load_bar.setRange(0, 0)
                self._set_status("Loading… {}".format(size_fmt(done)), T['WARNING'])
        except RuntimeError:
            pass

    def _on_load_finished(self, total_bytes: int):
        try:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                cur = self.editor.textCursor()
                cur.movePosition(QTextCursor.End)
                cur.insertText(tail)
            if total_bytes >= self.MAX_EDIT_BYTES:
                cur = self.editor.textCursor()
                cur.movePosition(QTextCursor.End)
                cur.insertText(
                    "\n\n[... file truncated at {} -- too large to fully load "
                    "into the editor. Download it instead to view the whole "
                    "thing.]".format(size_fmt(self.MAX_EDIT_BYTES))
                )
            self._loading = False
            self._load_bar.hide()
            self.editor.setUndoRedoEnabled(True)
            self._save_btn.setEnabled(True)
            self._save_close_btn.setEnabled(True)
            self._original = self.editor.toPlainText()
            self._modified_dot.hide()
            self._set_status("Ready")
            self.editor.textChanged.connect(self._on_text_changed)
        except RuntimeError:
            pass
        finally:
            # Drop the worker/decoder now that loading is done rather than
            # holding them (and whatever buffers paramiko still has open)
            # alive for the rest of the dialog's lifetime.
            self._load_worker = None
            self._decoder     = None

    def _on_load_error(self, msg):
        try:
            self._loading = False
            self._load_bar.hide()
            self.editor.setUndoRedoEnabled(True)
            self._set_status("Failed to load: {}".format(msg), T['DANGER'])
            self.editor.textChanged.connect(self._on_text_changed)
        except RuntimeError:
            pass
        finally:
            self._load_worker = None
            self._decoder     = None
        QMessageBox.critical(self, "Cannot Open File", msg)

    # ── Find / Replace ────────────────────────────────────────
    # Matches are tracked as a plain list of (start, end) character offsets
    # computed with Python's `re` over the document's full text — this
    # gives regex support and an accurate "3 of 17" counter/navigation for
    # free, and (unlike QTextDocument.find + mergeCharFormat) the matches
    # are painted purely as ExtraSelections, so they never touch the
    # document's real character formatting and can't clash with the
    # syntax highlighter's colours.
    def _compiled_pattern(self):
        term = self._find_inp.text()
        if not term:
            return None
        flags = 0 if self._case_chk.isChecked() else re.IGNORECASE
        try:
            if self._regex_chk.isChecked():
                return re.compile(term, flags)
            return re.compile(re.escape(term), flags)
        except re.error:
            return None

    def _do_highlight(self):
        pattern = self._compiled_pattern()
        self._matches   = []
        self._match_idx = -1
        if pattern is None:
            self.editor.set_search_selections([])
            if self._find_inp.text() and self._regex_chk.isChecked():
                self._match_lbl.setText("bad regex")
                self._match_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px;")
            else:
                self._match_lbl.setText("")
                self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
            return

        text = self.editor.toPlainText()
        self._matches = [(m.start(), m.end()) for m in pattern.finditer(text)]

        # Keep navigation anchored near the cursor rather than always
        # snapping back to match #1 on every keystroke.
        cur_pos = self.editor.textCursor().position()
        self._match_idx = next(
            (i for i, (s, _e) in enumerate(self._matches) if s >= cur_pos), 0
        ) if self._matches else -1

        self._apply_match_selections()
        self._update_match_label()

    def _apply_match_selections(self):
        doc = self.editor.document()
        selections = []
        normal_fmt = QTextCharFormat()
        normal_fmt.setBackground(QColor(T['WARNING']))
        normal_fmt.setForeground(QColor("#1a1a1a"))
        current_fmt = QTextCharFormat()
        current_fmt.setBackground(QColor(T['ACCENT']))
        current_fmt.setForeground(QColor("#ffffff"))

        for i, (start, end) in enumerate(self._matches):
            sel = QTextEdit.ExtraSelection()
            sel.format = current_fmt if i == self._match_idx else normal_fmt
            c = QTextCursor(doc)
            c.setPosition(start)
            c.setPosition(end, QTextCursor.KeepAnchor)
            sel.cursor = c
            selections.append(sel)
        self.editor.set_search_selections(selections)

    def _update_match_label(self):
        if not self._matches:
            self._match_lbl.setText("No results" if self._find_inp.text() else "")
        else:
            self._match_lbl.setText(f"{self._match_idx + 1} of {len(self._matches)}")
        self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")

    def _goto_match(self, idx):
        if not self._matches:
            return
        self._match_idx = idx % len(self._matches)
        start, end = self._matches[self._match_idx]
        c = self.editor.textCursor()
        c.setPosition(start)
        c.setPosition(end, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(c)
        self.editor.ensureCursorVisible()
        self._apply_match_selections()
        self._update_match_label()

    def _find_next(self):
        if not self._matches:
            self._do_highlight()
        if self._matches:
            self._goto_match(self._match_idx + 1 if self._match_idx >= 0 else 0)

    def _find_prev(self):
        if not self._matches:
            self._do_highlight()
        if self._matches:
            self._goto_match(self._match_idx - 1 if self._match_idx >= 0 else -1)

    def _clear_highlights(self):
        self._matches   = []
        self._match_idx = -1
        self.editor.set_search_selections([])
        self._match_lbl.setText("")

    def _replace_one(self):
        if not self._matches:
            self._do_highlight()
        if not self._matches or self._match_idx < 0:
            return
        start, end = self._matches[self._match_idx]
        c = self.editor.textCursor()
        c.setPosition(start)
        c.setPosition(end, QTextCursor.KeepAnchor)
        c.insertText(self._replace_inp.text())
        self._do_highlight()

    def _replace_all(self):
        pattern = self._compiled_pattern()
        replace = self._replace_inp.text()
        if pattern is None:
            return
        text = self.editor.toPlainText()
        try:
            new_text, n = pattern.subn(
                replace if self._regex_chk.isChecked() else replace.replace("\\", "\\\\"),
                text,
            )
        except re.error as e:
            self._set_status(f"Replace failed: {e}", T['DANGER'])
            return
        if n:
            cur = self.editor.textCursor()
            pos = cur.position()
            self.editor.setPlainText(new_text)
            cur = self.editor.textCursor()
            cur.setPosition(min(pos, len(new_text)))
            self.editor.setTextCursor(cur)
            self._set_status(f"Replaced {n} occurrence{'s' if n != 1 else ''}", T['SUCCESS'])
            QTimer.singleShot(2000, lambda: self._set_status("Ready"))
        self._do_highlight()

    # ── Save ──────────────────────────────────────────────────
    def _save(self) -> bool:
        if self._loading:
            self._set_status("Still loading — please wait", T['WARNING'])
            return False
        content = self.editor.toPlainText()
        self._set_status("Saving…", T['WARNING'])
        try:
            data = content.encode("utf-8")
            buf  = io.BytesIO(data)
            if self._sudo_user:
                tmp = f"/tmp/.ec2mgr_edit_{os.getpid()}"
                self._sftp._sftp.putfo(io.BytesIO(data), tmp)
                _, err = self._sftp._run(
                    f"sudo mv {self._sftp._sq(tmp)} {self._sftp._sq(self._remote)} "
                    f"&& sudo chown {self._sudo_user} {self._sftp._sq(self._remote)}"
                )
                if err.strip():
                    raise PermissionError(err.strip())
            else:
                self._sftp._sftp.putfo(buf, self._remote)
            self._original = content
            self._modified_dot.hide()
            self._set_status("Saved ✓", T['SUCCESS'])
            QTimer.singleShot(2000, lambda: self._set_status("Ready"))
            return True
        except Exception as e:
            self._set_status(f"Save failed: {e}", T['DANGER'])
            QMessageBox.critical(self, "Save Failed", str(e))
            return False

    def _save_and_close(self):
        if self._save():
            self.accept()

    def _cancel_live_load(self):
        """Stops a still-running load worker and disconnects its signals
        so a late chunk/finished/error can't touch a dialog that's mid-
        close (same pattern used for the media player's teardown)."""
        if self._load_worker:
            for sig, slot in (
                (self._load_worker.chunk_ready,  self._on_load_chunk),
                (self._load_worker.progress,     self._on_load_progress),
                (self._load_worker.finished_ok,  self._on_load_finished),
                (self._load_worker.finished_err, self._on_load_error),
            ):
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
            self._load_worker.cancel()

    def _confirm_close(self):
        if self._loading:
            self._cancel_live_load()
            self.reject()
            return
        if self.editor.toPlainText() != self._original:
            r = QMessageBox.question(
                self, "Discard Changes",
                "You have unsaved changes. Discard and close?",
                QMessageBox.Discard | QMessageBox.Cancel,
            )
            if r != QMessageBox.Discard:
                return
        self.reject()

    def closeEvent(self, event):
        if self._loading:
            self._cancel_live_load()
            event.accept()
            return
        if self.editor.toPlainText() != self._original:
            r = QMessageBox.question(
                self, "Unsaved Changes", "Discard changes and close?",
                QMessageBox.Discard | QMessageBox.Cancel,
            )
            if r != QMessageBox.Discard:
                event.ignore()
                return
        event.accept()

    MAX_EDIT_BYTES = 8 * 1024 * 1024

    @classmethod
    def open_remote(cls, parent, sftp, ssh, remote_path: str, sudo_user=None):
        """Opens *remote_path* for editing immediately — the editor window
        appears right away and the file's content streams in live from a
        background FileStreamReadWorker (see _start_live_load), instead of
        blocking behind a separate "downloading" dialog first. You can
        start reading/scrolling as soon as the first chunks land; Save
        stays disabled until the whole file has arrived.
        """
        dlg = cls(parent, sftp, ssh, remote_path, content=None, sudo_user=sudo_user)
        dlg.exec_()
        return dlg


# ─── Media player dialog ───────────────────────────────────────
class MediaPlayerDialog(QDialog):
    """Plays a remote video/audio file (mp4, mov, mp3, wav, ...) with
    standard media-player transport controls (play/pause, stop, skip
    +/-10s, seek bar, volume/mute).

    Nothing is downloaded to local disk. A tiny loopback-only HTTP server
    (MediaStreamServer, in workers.py) is started on a background thread;
    it translates each HTTP request QMediaPlayer makes into SFTP reads (or
    a streamed "sudo cat" if a sudo user is active and plain SFTP can't
    reach the file) on demand, straight from the SSH connection. Playback
    starts as soon as the server is up — QMediaPlayer pulls bytes as it
    plays, the same way it would stream from any web server.
    """

    def __init__(self, parent, sftp, ssh, remote_path: str, kind: str, sudo_user=None):
        super().__init__(parent)
        self._sftp        = sftp
        self._ssh         = ssh
        self._remote      = remote_path
        self._kind        = kind          # "video" or "audio"
        self._sudo_user   = sudo_user
        self._stream_server = None
        self._start_worker  = None
        self._seeking      = False

        fname = os.path.basename(remote_path)
        self.setWindowTitle("Play - {}".format(fname))
        self.resize(720, 480 if kind == "video" else 220)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ──────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(40)
        header.setStyleSheet(
            "background: {bg}; border-bottom: 1px solid {bd};".format(
                bg=T['BG_PANEL'], bd=T['BORDER'])
        )
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 0, 12, 0)
        name_lbl = QLabel(fname)
        name_lbl.setStyleSheet(
            "color: {}; font-size: 13px; font-weight: 600;".format(T['TEXT_PRIMARY']))
        h.addWidget(name_lbl)
        h.addStretch()
        lay.addWidget(header)

        # ── Body: video surface, or an icon placeholder for audio ──
        body = QWidget()
        body.setStyleSheet("background: #000000;" if kind == "video"
                            else "background: {};".format(T['BG_DARK']))
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(0, 0, 0, 0)

        self._video_widget = None
        if _MULTIMEDIA_AVAILABLE and kind == "video":
            self._video_widget = QVideoWidget()
            b_lay.addWidget(self._video_widget, 1)
        else:
            icon_lbl = QLabel("🎵" if kind == "audio" else "⚠")
            icon_lbl.setAlignment(Qt.AlignCenter)
            icon_lbl.setFont(QFont("Segoe UI Emoji", 64))
            b_lay.addWidget(icon_lbl, 1)
        lay.addWidget(body, 1)

        # ── Status / buffering progress ─────────────────────
        self._status_lbl = QLabel("Preparing...")
        self._status_lbl.setStyleSheet(
            "color: {}; font-size: 12px; padding: 6px 12px;".format(T['TEXT_DIM']))
        lay.addWidget(self._status_lbl)

        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)
        self._dl_bar.setFixedHeight(4)
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)

        # ── Transport controls ──────────────────────────────
        controls = QWidget()
        controls.setFixedHeight(72)
        controls.setStyleSheet(
            "background: {bg}; border-top: 1px solid {bd};".format(
                bg=T['BG_PANEL'], bd=T['BORDER'])
        )
        c = QVBoxLayout(controls)
        c.setContentsMargins(12, 6, 12, 6)
        c.setSpacing(4)

        seek_row = QHBoxLayout()
        self._pos_lbl = QLabel("0:00")
        self._pos_lbl.setFixedWidth(46)
        self._pos_lbl.setStyleSheet("color: {}; font-size: 11px;".format(T['TEXT_DIM']))
        seek_row.addWidget(self._pos_lbl)

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._on_seek_start)
        self._seek_slider.sliderReleased.connect(self._on_seek_end)
        seek_row.addWidget(self._seek_slider, 1)

        self._dur_lbl = QLabel("0:00")
        self._dur_lbl.setFixedWidth(46)
        self._dur_lbl.setAlignment(Qt.AlignRight)
        self._dur_lbl.setStyleSheet("color: {}; font-size: 11px;".format(T['TEXT_DIM']))
        seek_row.addWidget(self._dur_lbl)
        c.addLayout(seek_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        def _mkbtn(text, tooltip, width=36):
            b = QPushButton(text)
            b.setFixedSize(width, 32)
            b.setToolTip(tooltip)
            return b

        self._back_btn = _mkbtn("⏮", "Back 10s")
        self._back_btn.clicked.connect(lambda: self._skip(-10000))
        btn_row.addWidget(self._back_btn)

        self._play_btn = _mkbtn("▶", "Play / Pause", 46)
        self._play_btn.setObjectName("primary")
        self._play_btn.clicked.connect(self._toggle_play)
        btn_row.addWidget(self._play_btn)

        self._fwd_btn = _mkbtn("⏭", "Forward 10s")
        self._fwd_btn.clicked.connect(lambda: self._skip(10000))
        btn_row.addWidget(self._fwd_btn)

        self._stop_btn = _mkbtn("⏹", "Stop")
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self._stop_btn)

        btn_row.addSpacing(14)

        self._mute_btn = _mkbtn("🔊", "Mute")
        self._mute_btn.clicked.connect(self._toggle_mute)
        btn_row.addWidget(self._mute_btn)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setFixedWidth(90)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        btn_row.addWidget(self._vol_slider)

        btn_row.addStretch()
        c.addLayout(btn_row)
        lay.addWidget(controls)

        self._player = None
        if _MULTIMEDIA_AVAILABLE:
            self._player = QMediaPlayer(self)
            if self._video_widget is not None:
                self._player.setVideoOutput(self._video_widget)
            self._player.setVolume(80)
            self._player.positionChanged.connect(self._on_position_changed)
            self._player.durationChanged.connect(self._on_duration_changed)
            self._player.stateChanged.connect(self._on_state_changed)
            self._player.bufferStatusChanged.connect(self._on_buffer_status)
            self._player.error.connect(self._on_player_error)
            self._vol_slider.valueChanged.connect(self._player.setVolume)
        else:
            self._status_lbl.setText(
                "Media playback isn't available - this Qt install is missing "
                "QtMultimedia. You can still download the file instead."
            )
            self._status_lbl.setStyleSheet(
                "color: {}; font-size: 12px; padding: 6px 12px;".format(T['WARNING']))
            self._dl_bar.hide()

        self._set_controls_enabled(False)

        if _MULTIMEDIA_AVAILABLE:
            self._start_stream()

    # ── background stream startup ─────────────────────────────
    def _start_stream(self):
        self._status_lbl.setText("Connecting...")
        self._stream_server = MediaStreamServer(
            self._ssh, self._sftp, self._remote, sudo_user=self._sudo_user
        )
        self._start_worker = _StreamServerStartWorker(self._stream_server)
        self._start_worker.ready.connect(self._on_stream_ready)
        self._start_worker.error.connect(self._on_stream_error)
        self._start_worker.start()

    def _on_stream_ready(self, url):
        # Defensive: if this arrives in the brief window between the
        # worker finishing and closeEvent's disconnect calls running, the
        # dialog's C++ widgets may already be gone — don't touch them.
        try:
            if self._stream_server.supports_range:
                self._status_lbl.setText("Streaming")
            else:
                self._status_lbl.setText("Streaming (seek limited under sudo)")
            self._set_controls_enabled(True)
            self._player.setMedia(QMediaContent(QUrl(url)))
            self._player.play()
        except RuntimeError:
            pass

    def _on_stream_error(self, msg):
        try:
            self._dl_bar.hide()
            self._status_lbl.setText("Streaming failed: {}".format(msg))
            self._status_lbl.setStyleSheet(
                "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
        except RuntimeError:
            pass

    def _on_buffer_status(self, percent):
        try:
            if percent < 100:
                self._dl_bar.show()
                self._dl_bar.setRange(0, 100)
                self._dl_bar.setValue(percent)
                self._status_lbl.setText("Buffering... {}%".format(percent))
            else:
                self._dl_bar.hide()
                if self._stream_server and self._stream_server.supports_range:
                    self._status_lbl.setText("Streaming")
        except RuntimeError:
            pass

    # ── transport controls ──────────────────────────────────
    def _set_controls_enabled(self, enabled: bool):
        for w in (self._back_btn, self._play_btn, self._fwd_btn,
                  self._stop_btn, self._mute_btn, self._vol_slider, self._seek_slider):
            w.setEnabled(enabled)

    def _toggle_play(self):
        if not self._player:
            return
        if self._player.state() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _stop(self):
        if self._player:
            self._player.stop()

    def _skip(self, delta_ms: int):
        if not self._player:
            return
        new_pos = max(0, min(self._player.duration(), self._player.position() + delta_ms))
        self._player.setPosition(new_pos)

    def _toggle_mute(self):
        if not self._player:
            return
        muted = not self._player.isMuted()
        self._player.setMuted(muted)
        self._mute_btn.setText("🔇" if muted else "🔊")

    def _on_seek_start(self):
        self._seeking = True

    def _on_seek_end(self):
        if self._player:
            self._player.setPosition(self._seek_slider.value())
        self._seeking = False

    def _on_position_changed(self, pos):
        if not self._seeking:
            self._seek_slider.setValue(pos)
        self._pos_lbl.setText(self._fmt_time(pos))

    def _on_duration_changed(self, dur):
        self._seek_slider.setRange(0, dur)
        self._dur_lbl.setText(self._fmt_time(dur))

    def _on_state_changed(self, state):
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_player_error(self, _err):
        if not self._player:
            return
        msg = self._player.errorString()
        if msg:
            self._status_lbl.setText("Playback error: {}".format(msg))
            self._status_lbl.setStyleSheet(
                "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
            self._status_lbl.show()

    @staticmethod
    def _fmt_time(ms: int) -> str:
        s = ms // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return "{}:{:02d}:{:02d}".format(h, m, s) if h else "{}:{:02d}".format(m, s)

    # ── cleanup ──────────────────────────────────────────────
    def closeEvent(self, event):
        # Disconnect the player's own signals first so nothing it fires
        # while winding down (position/duration/buffer/error updates)
        # can land on a dialog that's mid-close.
        if self._player:
            for sig, slot in (
                (self._player.positionChanged,     self._on_position_changed),
                (self._player.durationChanged,     self._on_duration_changed),
                (self._player.stateChanged,        self._on_state_changed),
                (self._player.bufferStatusChanged, self._on_buffer_status),
                (self._player.error,               self._on_player_error),
            ):
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
            self._player.stop()
            # Releases the network source cleanly instead of leaving the
            # backend holding a socket into a proxy we're about to kill.
            self._player.setMedia(QMediaContent())

        # QThread.terminate() forcibly kills the thread at an arbitrary
        # point — if it's in the middle of a paramiko/SSL call (opening
        # the SFTP channel, exec'ing "cat") that can crash the whole
        # process, not just this dialog. Instead: stop listening for its
        # result so a late ready/error can't touch a closing dialog, then
        # let it finish naturally (it cleans itself up via
        # finished -> deleteLater). Waiting briefly here avoids racing
        # MediaStreamServer.stop() against the worker's own start().
        if self._start_worker:
            try:
                self._start_worker.ready.disconnect(self._on_stream_ready)
                self._start_worker.error.disconnect(self._on_stream_error)
            except Exception:
                pass
            if self._start_worker.isRunning():
                self._start_worker.wait(1500)

        if self._stream_server:
            self._stream_server.stop()

        event.accept()

    @classmethod
    def open_remote(cls, parent, sftp, ssh, remote_path: str, kind: str, sudo_user=None):
        dlg = cls(parent, sftp, ssh, remote_path, kind, sudo_user=sudo_user)
        dlg.exec_()
        return dlg


# ─── File Execution dialog ────────────────────────────────────
from PyQt5.QtGui import QTextDocument


def _exec_cmd_for(ext: str, quoted: str, dirpath: str) -> tuple:
    q = quoted
    d = f"'{dirpath}'"
    _map = {
        ".py":    ("Python 3",    f"cd {d} && python3 -u {q}"),
        ".sh":    ("Bash",        f"cd {d} && bash {q}"),
        ".bash":  ("Bash",        f"cd {d} && bash {q}"),
        ".rb":    ("Ruby",        f"cd {d} && ruby {q}"),
        ".js":    ("Node.js",     f"cd {d} && node {q}"),
        ".ts":    ("ts-node",     f"cd {d} && ts-node {q}"),
        ".php":   ("PHP",         f"cd {d} && php {q}"),
        ".pl":    ("Perl",        f"cd {d} && perl {q}"),
        ".lua":   ("Lua",         f"cd {d} && lua {q}"),
        ".r":     ("Rscript",     f"cd {d} && Rscript {q}"),
        ".go":    ("Go run",      f"cd {d} && go run {q}"),
        ".java":  ("javac+java",  f"cd {d} && javac {q} && java $(basename {q} .java)"),
        ".kt":    ("kotlinc",     f"cd {d} && kotlinc {q} -include-runtime -d /tmp/_kt_out.jar && java -jar /tmp/_kt_out.jar"),
        ".rs":    ("rustc",       f"cd {d} && rustc {q} -o /tmp/_rs_out && /tmp/_rs_out"),
        ".c":     ("gcc",         f"cd {d} && gcc {q} -o /tmp/_c_out && /tmp/_c_out"),
        ".cpp":   ("g++",         f"cd {d} && g++ {q} -o /tmp/_cpp_out && /tmp/_cpp_out"),
        ".swift": ("swift",       f"cd {d} && swift {q}"),
    }
    return _map.get(ext, (None, None))


class _ExecStreamWorker(QThread):
    """Streams SSH command output line by line via signals."""
    line     = pyqtSignal(str)
    error    = pyqtSignal(str)
    finished = pyqtSignal(int)   # exit code

    def __init__(self, ssh, cmd: str):
        super().__init__()
        self._ssh     = ssh
        self._cmd     = cmd
        self._stop    = False
        self._channel = None   # set once the session is opened; used by send_input()
        self.finished.connect(self.deleteLater)

    def request_stop(self):
        self._stop = True

    def send_input(self, text: str):
        """Write text to the running remote process's stdin (via the PTY).
        Safe to call from the UI thread — Paramiko channels serialize
        sends internally. No-ops silently if there's no live channel yet
        or the command has already finished."""
        if self._channel is None:
            return
        try:
            self._channel.send(text)
        except Exception:
            pass

    def run(self):
        try:
            # A PTY is allocated below (channel.get_pty()), which means the
            # remote process's stdout is a real tty as far as it's concerned.
            # bash, python3, and virtually everything else auto-switch to
            # line-buffered stdout the moment isatty(stdout) is true, so no
            # extra buffering trick is needed for normal output.
            #
            # (Previously this wrapped the command in
            # "stdbuf -oL -eL {cmd} || unbuffer {cmd} || {cmd}", but stdbuf
            # tries to exec {cmd} as a single program — it chokes instantly
            # on anything starting with a shell builtin like "cd ... &&
            # bash script.sh" ("failed to run command 'cd'"), and unbuffer
            # isn't installed on most hosts. Both fallbacks silently leaked
            # their error text into the output before finally landing on
            # the last, unbuffered variant — which is what made output look
            # delayed/batched in the first place.)
            channel = self._ssh.get_transport().open_session()
            channel.get_pty()
            channel.settimeout(0.5)
            channel.exec_command(self._cmd)
            # PTY is allocated up front, so stdin is writable as soon as the
            # channel exists — expose it immediately rather than waiting for
            # the command to finish setting up.
            self._channel = channel

            buf = b""

            while True:
                if self._stop:
                    channel.close()
                    break

                try:
                    if channel.recv_ready():
                        chunk = channel.recv(4096)
                        if chunk:
                            self.line.emit(chunk.decode("utf-8", errors="replace"))
                        continue
                except Exception:
                    pass

                if channel.exit_status_ready() and not channel.recv_ready():
                    break

                self.msleep(50)

            if buf:
                self.line.emit(buf.decode("utf-8", errors="replace"))

            code = channel.recv_exit_status() if not self._stop else -1
            self.finished.emit(code)
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit(-1)


class FileExecDialog(QDialog):
    """Run a remote script and stream its output line by line."""

    def __init__(self, parent, ssh, remote_path: str, sudo_user=None):
        super().__init__(parent)
        self._ssh       = ssh
        self._remote    = remote_path
        self._sudo_user = sudo_user
        self._worker    = None

        fname = os.path.basename(remote_path)
        ext   = os.path.splitext(fname)[1].lower()
        self.setWindowTitle(f"Run — {fname}")
        self.resize(880, 580)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Toolbar ───────────────────────────────────────────
        tb_widget = QWidget()
        tb_widget.setFixedHeight(48)
        tb_widget.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        tb = QHBoxLayout(tb_widget)
        tb.setContentsMargins(12, 0, 12, 0)
        tb.setSpacing(10)

        tb.addWidget(QLabel("Interpreter:"))
        self._interp_combo = QComboBox()
        self._interp_combo.setMinimumWidth(130)
        tb.addWidget(self._interp_combo)

        tb.addWidget(QLabel("Args:"))
        self._args_inp = QLineEdit()
        self._args_inp.setPlaceholderText("optional arguments…")
        self._args_inp.setMinimumWidth(220)
        self._args_inp.returnPressed.connect(self._run)
        tb.addWidget(self._args_inp, 1)

        self._run_btn = QPushButton("▶  Run")
        self._run_btn.setObjectName("primary")
        self._run_btn.setFixedWidth(90)
        self._run_btn.clicked.connect(self._run)
        tb.addWidget(self._run_btn)

        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setFixedWidth(90)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        tb.addWidget(self._stop_btn)

        # Declare _output before clr_btn so the lambda works
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(monospace_font(11))
        self._output.setStyleSheet(
            f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 10px;"
        )

        clr_btn = QPushButton("Clear")
        clr_btn.setFixedWidth(60)
        clr_btn.clicked.connect(self._output.clear)
        tb.addWidget(clr_btn)
        lay.addWidget(tb_widget)

        lay.addWidget(self._output)

        # ── Runtime input bar ──────────────────────────────────
        # For scripts that block on input() / read / a sudo password prompt
        # etc. The remote process runs under a PTY (see _ExecStreamWorker),
        # so whatever is typed here is written straight to its stdin — the
        # script keeps running instead of hanging forever waiting on a
        # terminal that was never actually connected to anything.
        in_widget = QWidget()
        in_widget.setFixedHeight(44)
        in_widget.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
        )
        in_lay = QHBoxLayout(in_widget)
        in_lay.setContentsMargins(12, 6, 12, 6)
        in_lay.setSpacing(8)

        in_lay.addWidget(QLabel("Input:"))
        self._input_inp = QLineEdit()
        self._input_inp.setPlaceholderText(
            "Type input for the running script and press Enter…"
        )
        self._input_inp.setEnabled(False)
        self._input_inp.returnPressed.connect(self._send_input)
        in_lay.addWidget(self._input_inp, 1)

        self._input_mask_btn = QPushButton("👁")
        self._input_mask_btn.setCheckable(True)
        self._input_mask_btn.setFixedWidth(32)
        self._input_mask_btn.setToolTip("Mask input (for passwords/secrets)")
        self._input_mask_btn.toggled.connect(self._toggle_input_echo)
        in_lay.addWidget(self._input_mask_btn)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(70)
        self._send_btn.setEnabled(False)
        self._send_btn.clicked.connect(self._send_input)
        in_lay.addWidget(self._send_btn)

        lay.addWidget(in_widget)

        # ── Status bar ────────────────────────────────────────
        sb_widget = QWidget()
        sb_widget.setFixedHeight(26)
        sb_widget.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
        )
        sb = QHBoxLayout(sb_widget)
        sb.setContentsMargins(12, 0, 12, 0)
        sb.setSpacing(0)

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
        sb.addWidget(self._status_lbl)
        sb.addStretch()

        copy_btn = QPushButton("Copy output")
        copy_btn.setFixedWidth(100)
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._output.toPlainText())
        )
        sb.addWidget(copy_btn)
        lay.addWidget(sb_widget)

        self._populate_interpreters(ext)

    # ── Interpreter combo ─────────────────────────────────────
    def _populate_interpreters(self, ext: str):
        label, _ = _exec_cmd_for(ext, "", "")
        options  = []
        if label:
            options.append(label)
        if ext in (".sh", ".bash") and "Bash" not in options:
            options.append("Bash")
        if ext == ".py" and "Python 3" not in options:
            options.append("Python 3")
        options += ["sh (raw)", "Custom…"]
        self._interp_combo.addItems(options)
        self._interp_combo.currentTextChanged.connect(self._on_interp_change)

    def _on_interp_change(self, text: str):
        if text == "Custom…":
            self._args_inp.setPlaceholderText("interpreter command (e.g. python3.11 -u)")
        else:
            self._args_inp.setPlaceholderText("optional arguments…")

    # ── Build shell command ───────────────────────────────────
    def _quoted(self, p: str) -> str:
        return "'" + p.replace("'", "'\\''") + "'"

    def _build_command(self) -> str:
        ext     = os.path.splitext(self._remote)[1].lower()
        dirpath = os.path.dirname(self._remote) or "/"
        qpath   = self._quoted(self._remote)
        qdir    = self._quoted(dirpath)
        args    = self._args_inp.text().strip()
        choice  = self._interp_combo.currentText()

        if choice == "Custom…":
            interp = args or "bash"
            return f"cd {qdir} && {interp} {qpath}"
        if choice == "sh (raw)":
            return (f"cd {qdir} && chmod +x {qpath} && {qpath} {args}").rstrip()

        _, cmd = _exec_cmd_for(ext, qpath, dirpath)
        if cmd:
            return (f"{cmd} {args}").rstrip() if args else cmd

        return (f"cd {qdir} && {choice} {qpath} {args}").rstrip()

    # ── Run / stop ────────────────────────────────────────────
    def _run(self):
        if self._worker and self._worker.isRunning():
            return

        cmd = self._build_command()
        if self._sudo_user:
            safe = cmd.replace("'", "'\\''")
            cmd  = f"sudo -u {self._sudo_user} sh -c '{safe}'"

        append_terminal_html(self._output, f"<span style='color:{T['ACCENT2']}'>$ {html_escape(cmd)}</span>")
        self._set_status("Running…", T['WARNING'])
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._input_inp.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._input_inp.setFocus()

        self._worker = _ExecStreamWorker(self._ssh, cmd)
        self._worker.line.connect(self._on_line)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
        append_terminal_html(self._output, f"<span style='color:{T['WARNING']}'>[stopped by user]</span>")
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._input_inp.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._set_status("Stopped", T['WARNING'])

    def _send_input(self):
        """Send whatever's in the input box to the running process's stdin,
        followed by Enter — same as typing it at a real terminal prompt.

        The remote PTY normally echoes typed input back into the output
        stream itself (that's how a real terminal works), so for plain
        input we don't print it a second time here — it'll show up via
        _on_line once the remote's line discipline reflects it back.
        For masked entries (passwords), the remote side almost always
        disables echo before reading, so nothing would appear on screen
        at all — we print a masked marker ourselves so there's still
        visible confirmation that something was sent."""
        if not (self._worker and self._worker.isRunning()):
            return
        text = self._input_inp.text()
        if self._input_mask_btn.isChecked():
            append_terminal_html(
                self._output,
                f"<span style='color:{T['INFO']}'>&gt; {'*' * len(text)}</span>",
            )
        self._worker.send_input(text + "\n")
        self._input_inp.clear()

    def _toggle_input_echo(self, checked: bool):
        self._input_inp.setEchoMode(QLineEdit.Password if checked else QLineEdit.Normal)

    def _on_line(self, line: str):
        append_terminal_text(self._output, line + "\n")
        sb = self._output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_error(self, err: str):
        append_terminal_html(self._output, f"<span style='color:{T['DANGER']}'>[error] {html_escape(err)}</span>")

    def _on_finished(self, code: int):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._input_inp.setEnabled(False)
        self._send_btn.setEnabled(False)
        color = T['SUCCESS'] if code == 0 else T['DANGER']
        append_terminal_html(self._output, f"<span style='color:{color}'>[exit code {code}]</span>")
        self._set_status(f"Finished (exit {code})", color)

    def _set_status(self, msg, color=None):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(
            f"color: {color or T['TEXT_MUTED']}; font-size: 13px;"
        )


# ─── Remote Search dialog ─────────────────────────────────────
class SearchDialog(QDialog):
    """Search file contents (grep) and filenames (find) on the remote host."""

    navigate = pyqtSignal(str)

    def __init__(self, parent, ssh, start_path: str = "/"):
        super().__init__(parent)
        self._ssh        = ssh
        self._start_path = start_path
        self._worker     = None

        self.setWindowTitle("Search Remote")
        self.resize(820, 560)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Search bar ────────────────────────────────────────
        top = QWidget()
        top.setFixedHeight(52)
        top.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
        )
        tb = QHBoxLayout(top)
        tb.setContentsMargins(12, 0, 12, 0)
        tb.setSpacing(8)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Content (grep)", "Filename (find)"])
        self._mode_combo.setFixedWidth(150)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_change)
        tb.addWidget(self._mode_combo)

        self._query_inp = QLineEdit()
        self._query_inp.setPlaceholderText("Search pattern…")
        self._query_inp.setFont(monospace_font(12))
        self._query_inp.returnPressed.connect(self._search)
        tb.addWidget(self._query_inp, 1)

        tb.addWidget(QLabel("In:"))
        self._path_inp = QLineEdit(start_path)
        self._path_inp.setFixedWidth(200)
        tb.addWidget(self._path_inp)

        self._case_chk = QCheckBox("Case")
        self._case_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        tb.addWidget(self._case_chk)

        self._regex_chk = QCheckBox("Regex")
        self._regex_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        tb.addWidget(self._regex_chk)

        self._search_btn = QPushButton("🔍  Search")
        self._search_btn.setObjectName("primary")
        self._search_btn.setFixedWidth(100)
        self._search_btn.clicked.connect(self._search)
        tb.addWidget(self._search_btn)

        self._stop_btn = QPushButton("■ Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setFixedWidth(80)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        tb.addWidget(self._stop_btn)
        lay.addWidget(top)

        # ── Options row ───────────────────────────────────────
        self._opts_row = QWidget()
        self._opts_row.setFixedHeight(36)
        self._opts_row.setStyleSheet(
            f"background: {T['BG_DARK']}; border-bottom: 1px solid {T['BORDER']};"
        )
        or_lay = QHBoxLayout(self._opts_row)
        or_lay.setContentsMargins(12, 0, 12, 0)
        or_lay.setSpacing(12)

        or_lay.addWidget(QLabel("File filter:"))
        self._include_inp = QLineEdit("*")
        self._include_inp.setFixedWidth(120)
        self._include_inp.setToolTip("e.g. *.py  *.js  *.txt")
        or_lay.addWidget(self._include_inp)

        self._recursive_chk = QCheckBox("Recursive")
        self._recursive_chk.setChecked(True)
        self._recursive_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        or_lay.addWidget(self._recursive_chk)

        self._hidden_chk = QCheckBox("Include hidden")
        self._hidden_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        or_lay.addWidget(self._hidden_chk)

        or_lay.addStretch()
        self._result_count_lbl = QLabel("")
        self._result_count_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        or_lay.addWidget(self._result_count_lbl)
        lay.addWidget(self._opts_row)

        # ── Results tree ──────────────────────────────────────
        self._results = QTreeWidget()
        self._results.setRootIsDecorated(True)
        self._results.setColumnCount(2)
        self._results.setHeaderLabels(["File / Match", "Line"])
        self._results.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._results.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._results.setAlternatingRowColors(True)
        self._results.setFont(monospace_font(11))
        self._results.itemDoubleClicked.connect(self._on_result_dblclick)
        lay.addWidget(self._results, 1)

        # ── Status / progress ─────────────────────────────────
        sb = QWidget()
        sb.setFixedHeight(26)
        sb.setStyleSheet(
            f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
        )
        sb_lay = QHBoxLayout(sb)
        sb_lay.setContentsMargins(12, 0, 12, 0)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
        sb_lay.addWidget(self._status_lbl)
        sb_lay.addStretch()
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setFixedWidth(120)
        self._progress.setFixedHeight(10)
        self._progress.hide()
        sb_lay.addWidget(self._progress)
        lay.addWidget(sb)

        self._query_inp.setFocus()

    def _on_mode_change(self, idx: int):
        is_grep = idx == 0
        self._opts_row.setVisible(is_grep)
        self._query_inp.setPlaceholderText(
            "Search pattern…" if is_grep else "Filename pattern (e.g. *.log)"
        )

    def _build_grep_cmd(self) -> str:
        q       = self._query_inp.text().strip()
        path    = self._path_inp.text().strip() or "/"
        incl    = self._include_inp.text().strip() or "*"
        flags   = []
        if not self._case_chk.isChecked():
            flags.append("-i")
        if not self._regex_chk.isChecked():
            flags.append("-F")
        if self._recursive_chk.isChecked():
            flags.append("-r")
        flags += ["-n", "--include=" + incl]
        if not self._hidden_chk.isChecked():
            flags.append("--exclude-dir='.*'")

        flag_str = " ".join(flags)
        sq_q     = "'" + q.replace("'", "'\\''") + "'"
        sq_path  = "'" + path.replace("'", "'\\''") + "'"
        return f"grep {flag_str} {sq_q} {sq_path} 2>/dev/null | head -500"

    def _build_find_cmd(self) -> str:
        q    = self._query_inp.text().strip()
        path = self._path_inp.text().strip() or "/"
        sq_q    = "'" + q.replace("'", "'\\''") + "'"
        sq_path = "'" + path.replace("'", "'\\''") + "'"
        hidden = "" if self._hidden_chk.isChecked() else r" ! -path '*/.*'"
        return f"find {sq_path}{hidden} -name {sq_q} 2>/dev/null | head -500"

    def _search(self):
        q = self._query_inp.text().strip()
        if not q:
            return
        if self._worker and self._worker.isRunning():
            return

        self._results.clear()
        self._result_count_lbl.setText("")
        self._progress.show()
        self._search_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("Searching…")

        if self._mode_combo.currentIndex() == 0:
            cmd = self._build_grep_cmd()
            self._worker = _ExecStreamWorker(self._ssh, cmd)
            self._worker.line.connect(self._on_grep_line)
        else:
            cmd = self._build_find_cmd()
            self._worker = _ExecStreamWorker(self._ssh, cmd)
            self._worker.line.connect(self._on_find_line)

        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_search_done)
        self._worker.start()

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()

    def _on_grep_line(self, line: str):
        parts = line.split(":", 2)
        if len(parts) < 3:
            return
        fpath, lineno, content = parts[0], parts[1], parts[2]

        parent = None
        for i in range(self._results.topLevelItemCount()):
            item = self._results.topLevelItem(i)
            if item.text(0) == fpath:
                parent = item
                break
        if parent is None:
            parent = QTreeWidgetItem([fpath, ""])
            parent.setForeground(0, QColor(T['ACCENT2']))
            parent.setFont(0, monospace_font(11, bold=True))
            parent.setData(0, Qt.UserRole, {"path": fpath})
            self._results.addTopLevelItem(parent)
            parent.setExpanded(True)

        child = QTreeWidgetItem([content.strip(), lineno])
        child.setData(0, Qt.UserRole, {"path": fpath, "line": lineno})
        child.setForeground(1, QColor(T['TEXT_MUTED']))
        parent.addChild(child)
        self._update_count()

    def _on_find_line(self, line: str):
        line = line.strip()
        if not line:
            return
        item = QTreeWidgetItem([line, ""])
        item.setData(0, Qt.UserRole, {"path": line})
        item.setForeground(0, QColor(T['ACCENT2']))
        self._results.addTopLevelItem(item)
        self._update_count()

    def _update_count(self):
        n = self._results.topLevelItemCount()
        self._result_count_lbl.setText(f"{n} file{'s' if n != 1 else ''}")

    def _on_error(self, err: str):
        self._status_lbl.setText(f"Error: {err}")

    def _on_search_done(self, code: int):
        self._progress.hide()
        self._search_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        n = self._results.topLevelItemCount()
        self._status_lbl.setText(f"Done — {n} result{'s' if n != 1 else ''}")

    def _on_result_dblclick(self, item, col):
        data = item.data(0, Qt.UserRole)
        if data and "path" in data:
            self.navigate.emit(os.path.dirname(data["path"]) or "/")

# ─── Tunnel service management (card-based add/edit UI) ───────
class _IconButton(QPushButton):
    """A small flat, round icon-only button used inside the service cards.
    Deliberately not routed through KubernetesTab._toolbar_btn — these live
    inside a QFrame card, not a toolbar, and need to be small and circular
    rather than pill-shaped."""

    def __init__(self, glyph: str, tooltip: str = "", danger: bool = False, parent=None):
        super().__init__(glyph, parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self._danger = danger
        self._apply_style()

    def _apply_style(self):
        hover_bg = f"rgba(248,113,113,0.15)" if self._danger else T["BG_HOVER"]
        border_hover = T["DANGER"] if self._danger else T["ACCENT"]
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {T['TEXT_DIM']};
                border: 1px solid {T['BORDER']}; border-radius: 15px;
                font-size: 13px; padding: 0;
            }}
            QPushButton:hover {{
                background: {hover_bg}; color: {T['TEXT_PRIMARY']};
                border-color: {border_hover};
            }}
        """)

    def refresh_theme(self):
        self._apply_style()


class _ServiceCard(QFrame):
    """A single tunnel service rendered as a friendly card (name, namespace
    badge, port mapping) with edit/delete actions — the row-level building
    block of ManageTunnelServicesDialog's list, styled after the
    NodeCard/badge visual language used elsewhere in the app."""

    edit_clicked   = pyqtSignal(dict)
    delete_clicked = pyqtSignal(dict)

    def __init__(self, svc: dict, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setObjectName("tunnel_service_card")

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 12, 12)
        row.setSpacing(12)

        icon_lbl = QLabel("🔀")
        icon_lbl.setStyleSheet("font-size: 18px;")
        icon_lbl.setFixedWidth(26)
        row.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.name_lbl = QLabel(svc["name"])
        name_row.addWidget(self.name_lbl)
        self.ns_badge = QLabel(f"ns/{svc.get('namespace') or 'default'}")
        name_row.addWidget(self.ns_badge)
        name_row.addStretch(1)
        text_col.addLayout(name_row)

        self.port_lbl = QLabel(
            f"container : {svc['container_port']}   →   local : {svc['port']}"
        )
        text_col.addWidget(self.port_lbl)

        row.addLayout(text_col, 1)

        self.edit_btn = _IconButton("✏️", "Edit this service")
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self.svc))
        row.addWidget(self.edit_btn)

        self.del_btn = _IconButton("🗑", "Remove this service", danger=True)
        self.del_btn.clicked.connect(lambda: self.delete_clicked.emit(self.svc))
        row.addWidget(self.del_btn)

        self._apply_styles()

    def _apply_styles(self):
        self.setStyleSheet(
            f"QFrame#tunnel_service_card {{ background: {T['BG_ITEM']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 12px; }} "
            f"QFrame#tunnel_service_card:hover {{ border: 1px solid {T['ACCENT']}; }}"
        )
        self.name_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700;"
        )
        self.ns_badge.setStyleSheet(
            f"background: {T['BG_ITEM_SEL']}; color: {T['TEXT_PRIMARY']}; "
            f"border-radius: 8px; padding: 1px 9px; font-size: 11px; font-weight: 600;"
        )
        self.port_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        self.edit_btn.refresh_theme()
        self.del_btn.refresh_theme()

    def refresh_theme(self):
        self._apply_styles()


def _contains_completer(combo: QComboBox) -> QCompleter:
    """Attach a "contains, not just starts-with" popup completer to an
    editable QComboBox, sharing the combo's own model so it stays in sync
    whenever the combo's items are cleared/reloaded (e.g. after a
    dependent-dropdown query) without needing to be rebuilt.

    Qt's default combo-box completer only matches from the start of the
    string and mostly just auto-fills inline, so typing "api" against
    ["auth-api", "payments-api", "api-gateway"] would show nothing (or at
    best "api-gateway") instead of all three. This switches to
    case-insensitive substring matching with a real dropdown popup of
    every match, which is what you want when picking a k8s service name
    out of a list you didn't choose.
    """
    completer = QCompleter(combo.model(), combo)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    combo.setCompleter(completer)
    return completer


class _ServiceFormPanel(QFrame):
    """Inline add/edit form. Shown in place of the card list (via a
    QStackedWidget in the owning dialog) rather than as its own popup
    dialog — keeps the whole flow feeling like one continuous window
    instead of a stack of nested modals.

    Namespace, Service Name, and Container Port are editable QComboBoxes
    rather than plain text fields: Namespace is pre-filled from the
    namespaces KubernetesTab already has cached; picking one queries the
    connected VM for that namespace's services (`kubectl get svc -n ...`)
    to fill Service Name; picking a service queries its actual container
    ports (`kubectl get svc <name> -n <ns>`) to fill Container Port. All
    three stay editable (QComboBox.setEditable) so a service that doesn't
    exist yet can still be typed in by hand — this is convenience
    autocomplete, not a hard constraint tied to what's live on the cluster.
    """

    save_clicked   = pyqtSignal(dict, object)   # (new_data, original_svc_or_None)
    cancel_clicked = pyqtSignal()

    def __init__(self, ssh=None, namespaces=None, parent=None):
        super().__init__(parent)
        self.setObjectName("tunnel_form_panel")
        self._editing    = None
        self.ssh         = ssh
        self._namespaces = list(namespaces or [])
        self._workers    = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(10)

        self.heading = QLabel("Add a new service")
        outer.addWidget(self.heading)

        # Namespace first — Service Name's choices depend on it.
        self.ns_input = QComboBox()
        self.ns_input.setEditable(True)
        self.ns_input.setInsertPolicy(QComboBox.NoInsert)
        self.ns_input.addItems(self._namespaces)
        self.ns_input.setCurrentText("default")
        self.ns_input.activated[str].connect(self._on_ns_picked)
        _contains_completer(self.ns_input)

        self.name_input = QComboBox()
        self.name_input.setEditable(True)
        self.name_input.setInsertPolicy(QComboBox.NoInsert)
        self.name_input.lineEdit().setPlaceholderText("e.g. auth-service")
        self.name_input.activated[str].connect(self._on_svc_picked)
        _contains_completer(self.name_input)

        ports_row = QHBoxLayout()
        ports_row.setSpacing(14)

        local_col = QVBoxLayout()
        local_col.setSpacing(4)
        local_lbl = QLabel("LOCAL PORT")
        local_col.addWidget(local_lbl)
        self.local_port_input = QLineEdit()
        self.local_port_input.setPlaceholderText("8080")
        self.local_port_input.setValidator(QIntValidator(1, 65535, self))
        local_col.addWidget(self.local_port_input)
        ports_row.addLayout(local_col)

        container_col = QVBoxLayout()
        container_col.setSpacing(4)
        container_lbl = QLabel("CONTAINER PORT")
        container_col.addWidget(container_lbl)
        self.container_port_input = QComboBox()
        self.container_port_input.setEditable(True)
        self.container_port_input.setInsertPolicy(QComboBox.NoInsert)
        self.container_port_input.lineEdit().setPlaceholderText("defaults to local port")
        self.container_port_input.lineEdit().setValidator(QIntValidator(1, 65535, self))
        _contains_completer(self.container_port_input)
        container_col.addWidget(self.container_port_input)
        ports_row.addLayout(container_col)

        self._field_labels = []
        for label_text, field in [
            ("NAMESPACE",    self.ns_input),
            ("SERVICE NAME", self.name_input),
        ]:
            lbl = QLabel(label_text)
            self._field_labels.append(lbl)
            outer.addWidget(lbl)
            outer.addWidget(field)

        outer.addLayout(ports_row)
        self._field_labels += [local_lbl, container_lbl]

        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.hide()
        outer.addWidget(self.error_lbl)

        outer.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_clicked.emit)
        btn_row.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("Add Service")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)
        outer.addLayout(btn_row)

        self._apply_styles()

    def _apply_styles(self):
        self.setStyleSheet(
            f"QFrame#tunnel_form_panel {{ background: {T['BG_PANEL']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 14px; }}"
        )
        self.heading.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 15px; font-weight: 700; "
            f"background: transparent;"
        )
        for lbl in self._field_labels:
            lbl.setStyleSheet(
                f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 700; "
                f"letter-spacing: 0.8px; background: transparent;"
            )
        self.error_lbl.setStyleSheet(
            f"color: {T['DANGER']}; font-size: 12px; background: transparent;"
        )

    # ── Context from the owning dialog ──────────────────────────
    def set_context(self, ssh, namespaces: list):
        """Called by ManageTunnelServicesDialog whenever it (re)opens, so
        the form always has the current SSH connection and namespace list
        rather than whatever was passed in at construction time."""
        self.ssh = ssh
        self._namespaces = list(namespaces or [])
        current = self.ns_input.currentText()
        self.ns_input.blockSignals(True)
        self.ns_input.clear()
        self.ns_input.addItems(self._namespaces)
        self.ns_input.setCurrentText(current or "default")
        self.ns_input.blockSignals(False)

    # ── Dependent-dropdown queries ───────────────────────────────
    def _run_query(self, cmd: str, callback):
        if not self.ssh:
            return
        worker = CommandWorker(self.ssh, cmd)
        worker.done.connect(callback)
        worker.error.connect(lambda _e: None)  # best-effort autocomplete only
        track_worker(self._workers, worker)
        worker.start()

    def _on_ns_picked(self, ns: str):
        self._reload_services(ns.strip())

    def _reload_services(self, ns: str):
        if not ns:
            return
        self._run_query(
            f"kubectl get svc -n {shlex.quote(ns)} "
            f"-o jsonpath='{{.items[*].metadata.name}}' 2>&1",
            self._populate_services,
        )

    def _populate_services(self, out: str):
        names = out.strip().strip("'").split()
        current = self.name_input.currentText()
        self.name_input.blockSignals(True)
        self.name_input.clear()
        self.name_input.addItems(names)
        self.name_input.setCurrentText(current)
        self.name_input.blockSignals(False)

    def _on_svc_picked(self, svc: str):
        svc = svc.strip()
        ns  = self.ns_input.currentText().strip()
        if not svc or not ns:
            return
        self._run_query(
            f"kubectl get svc {shlex.quote(svc)} -n {shlex.quote(ns)} "
            f"-o jsonpath='{{.spec.ports[*].port}}' 2>&1",
            self._populate_container_ports,
        )

    def _populate_container_ports(self, out: str):
        ports = out.strip().strip("'").split()
        current = self.container_port_input.currentText()
        self.container_port_input.blockSignals(True)
        self.container_port_input.clear()
        self.container_port_input.addItems(ports)
        if current:
            self.container_port_input.setCurrentText(current)
        elif len(ports) == 1:
            self.container_port_input.setCurrentText(ports[0])
            if not self.local_port_input.text().strip():
                self.local_port_input.setText(ports[0])
        self.container_port_input.blockSignals(False)

    # ── Loading state ────────────────────────────────────────
    def load_for_add(self):
        self._editing = None
        self.heading.setText("➕  Add a new service")
        self.save_btn.setText("Add Service")
        self.name_input.clearEditText()
        self.name_input.clear()
        self.ns_input.setCurrentText("default")
        self.local_port_input.clear()
        self.container_port_input.clearEditText()
        self.container_port_input.clear()
        self.error_lbl.hide()
        self.name_input.setFocus()
        self._reload_services(self.ns_input.currentText().strip())

    def load_for_edit(self, svc: dict):
        self._editing = svc
        self.heading.setText(f"✏️  Edit “{svc['name']}”")
        self.save_btn.setText("Save Changes")
        ns = svc.get("namespace", "") or "default"
        self.ns_input.setCurrentText(ns)
        self.name_input.clear()
        self.name_input.setCurrentText(svc["name"])
        self.local_port_input.setText(str(svc["port"]))
        self.container_port_input.clear()
        self.container_port_input.setCurrentText(str(svc["container_port"]))
        self.error_lbl.hide()
        self.name_input.setFocus()
        self._reload_services(ns)

    def show_error(self, msg: str):
        self.error_lbl.setText("⚠️  " + msg)
        self.error_lbl.show()

    def _on_save(self):
        name = self.name_input.currentText().strip()
        ns   = self.ns_input.currentText().strip()
        local_s     = self.local_port_input.text().strip()
        container_s = self.container_port_input.currentText().strip() or local_s

        if not name:
            self.show_error("Service name is required.")
            return
        if not local_s.isdigit():
            self.show_error("Local port must be a number.")
            return
        if not container_s.isdigit():
            self.show_error("Container port must be a number.")
            return

        data = {
            "name": name,
            "namespace": ns,
            "port": int(local_s),
            "container_port": int(container_s),
        }
        self.save_clicked.emit(data, self._editing)



class ManageTunnelServicesDialog(QDialog):
    """Friendly, card-based editor for the tunnel-services CSV that lives on
    the connected VM (see utils.load_tunnel_services / REMOTE_TUNNEL_CSV_PATH).

    Lets the user add new services and edit or remove existing ones without
    ever seeing a raw CSV — changes are staged locally and only written back
    to the VM (as CSV, base64-piped over the existing SSH connection to
    avoid any shell-quoting issues with service names) when "Save to VM" is
    pressed. Emits services_saved(list) with the new full list after a
    successful write so the caller (KubernetesTab) can refresh its own
    checklist from the same data instead of re-reading the file.
    """

    services_saved = pyqtSignal(list)

    def __init__(self, ssh, services: list, csv_path: str, namespaces: list = None, parent=None):
        super().__init__(parent)
        self.ssh = ssh
        self.csv_path = csv_path
        self._services = [dict(s) for s in services]
        self._namespaces = list(namespaces or [])
        self._dirty = False
        self._worker = None

        self.setWindowTitle("Manage Tunnel Services")
        self.setMinimumSize(580, 640)
        apply_qss_to(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(12)

        title = QLabel("🔀  Tunnel Services")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        outer.addWidget(title)

        self.subtitle = QLabel(f"Read from {csv_path} on the connected VM.")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍  Filter services…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_cards)
        top_row.addWidget(self.search, 1)

        self.add_btn = QPushButton("➕  Add Service")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self._show_add_form)
        top_row.addWidget(self.add_btn)
        outer.addLayout(top_row)

        self.stack = QStackedWidget()

        # ── List page ────────────────────────────────────────
        list_page = QWidget()
        list_lay = QVBoxLayout(list_page)
        list_lay.setContentsMargins(0, 0, 0, 0)
        list_lay.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll_body = QWidget()
        self.scroll_body.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.scroll_body)
        self.cards_layout.setContentsMargins(2, 2, 10, 2)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_body)
        list_lay.addWidget(self.scroll)

        self.empty_lbl = QLabel("No tunnel services yet — click “Add Service” above to create one.")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.hide()
        list_lay.addWidget(self.empty_lbl)

        self.stack.addWidget(list_page)

        # ── Form page ────────────────────────────────────────
        self.form_panel = _ServiceFormPanel(ssh=self.ssh, namespaces=self._namespaces)
        self.form_panel.save_clicked.connect(self._on_form_save)
        self.form_panel.cancel_clicked.connect(self._show_list)
        self.stack.addWidget(self.form_panel)

        outer.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status_lbl = QLabel("")
        footer.addWidget(self.status_lbl, 1)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        footer.addWidget(self.close_btn)

        self.save_all_btn = QPushButton("💾  Save to VM")
        self.save_all_btn.setObjectName("success")
        self.save_all_btn.clicked.connect(self._save_to_vm)
        footer.addWidget(self.save_all_btn)
        outer.addLayout(footer)

        self._apply_styles()
        self._rebuild_cards()

    def _apply_styles(self):
        self.subtitle.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        self.empty_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px; padding: 40px;")
        self.status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")

    # ── Card list management ─────────────────────────────────
    def _rebuild_cards(self):
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for svc in self._services:
            card = _ServiceCard(svc)
            card.edit_clicked.connect(self._show_edit_form)
            card.delete_clicked.connect(self._delete_service)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        self.empty_lbl.setVisible(not self._services)
        self.scroll.setVisible(bool(self._services))
        self._filter_cards(self.search.text())

    def _filter_cards(self, text):
        text = (text or "").strip().lower()
        for i in range(self.cards_layout.count() - 1):
            card = self.cards_layout.itemAt(i).widget()
            if not isinstance(card, _ServiceCard):
                continue
            svc = card.svc
            haystack = (
                f"{svc['name']} {svc.get('namespace','')} "
                f"{svc['port']} {svc['container_port']}"
            ).lower()
            card.setVisible(text in haystack)

    # ── Add / edit flow ──────────────────────────────────────
    def _show_add_form(self):
        self.form_panel.load_for_add()
        self.stack.setCurrentWidget(self.form_panel)

    def _show_edit_form(self, svc):
        self.form_panel.load_for_edit(svc)
        self.stack.setCurrentWidget(self.form_panel)

    def _show_list(self):
        self.stack.setCurrentIndex(0)

    def _on_form_save(self, data: dict, editing):
        others = [s for s in self._services if s is not editing]

        # Check for an identical service (all fields match)
        if any(s == data for s in others):
            self.form_panel.show_error("This service is already present.")
            return

        # Check for duplicate local port
        if any(s["port"] == data["port"] for s in others):
            self.form_panel.show_error(
                f"Local port {data['port']} is already used by another service."
            )
            return

        if editing is None:
            self._services.append(data)
            self.status_lbl.setText(
                f"Added “{data['name']}” — click Save to VM to apply."
            )
        else:
            idx = self._services.index(editing)
            self._services[idx] = data
            self.status_lbl.setText(
                f"Updated “{data['name']}” — click Save to VM to apply."
            )

        self._dirty = True
        self._rebuild_cards()
        self._show_list()

    def _delete_service(self, svc: dict):
        resp = QMessageBox.question(
            self, "Remove service",
            f"Remove “{svc['name']}” from the tunnel list?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._services = [s for s in self._services if s is not svc]
        self._dirty = True
        self.status_lbl.setText(f"Removed “{svc['name']}” — click Save to VM to apply.")
        self._rebuild_cards()

    # ── Persistence ───────────────────────────────────────────
    @staticmethod
    def _services_to_csv(services: list) -> str:
        lines = ["service_name,local_port,container_port,namespace"]
        for s in services:
            lines.append(f"{s['name']},{s['port']},{s['container_port']},{s.get('namespace','')}")
        return "\n".join(lines) + "\n"

    def _save_to_vm(self):
        if not self.ssh:
            QMessageBox.warning(self, "Not connected", "Connect to a VM before saving.")
            return
        if not self._services:
            resp = QMessageBox.question(
                self, "Save empty list?",
                "This will clear all tunnel services on the VM. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return

        csv_text = self._services_to_csv(self._services)
        b64 = base64.b64encode(csv_text.encode("utf-8")).decode("ascii")

        # csv_path (REMOTE_TUNNEL_CSV_PATH) starts with '~' by default. Shell
        # tilde-expansion only happens on an *unquoted* leading '~' — once
        # shlex.quote() wraps the path in single quotes (needed so
        # service/namespace text can't break out of the command), '~' is no
        # longer expanded and bash creates/writes a literal directory named
        # "~" inside the SSH session's cwd instead of $HOME. That silently
        # succeeded (no error), so the dialog reported "Saved" while the
        # real ~/.tunnel/tunnel_services.csv (the file load_tunnel_services
        # reads, unquoted, so it *does* expand '~') was never touched —
        # every newly added service looked like it hadn't been saved.
        # Fix: resolve '~' to the actual remote $HOME first, then quote the
        # already-expanded absolute path.
        csv_path = self.csv_path
        if csv_path.startswith("~"):
            _, _home_out, _ = self.ssh.exec_command("echo $HOME")
            home = _home_out.read().decode(errors="replace").strip()
            if home:
                csv_path = home + csv_path[1:]

        remote_dir = os.path.dirname(csv_path) or "."
        cmd = "mkdir -p {d} 2>/dev/null; echo {b64} | base64 -d > {p}".format(
            d=shlex.quote(remote_dir), b64=shlex.quote(b64), p=shlex.quote(csv_path),
        )

        self.save_all_btn.setEnabled(False)
        self.save_all_btn.setText("Saving…")
        self.status_lbl.setText("Writing changes to the VM…")

        worker = CommandWorker(self.ssh, cmd)

        def on_done(_out):
            self.save_all_btn.setEnabled(True)
            self.save_all_btn.setText("💾  Save to VM")
            self._dirty = False
            self.status_lbl.setText("✓ Saved to VM.")
            self.services_saved.emit(self._services)

        def on_error(err):
            self.save_all_btn.setEnabled(True)
            self.save_all_btn.setText("💾  Save to VM")
            self.status_lbl.setText("")
            QMessageBox.critical(self, "Save failed", str(err))

        worker.done.connect(on_done)
        worker.error.connect(on_error)
        self._worker = worker
        worker.start()

    # ── Close handling ────────────────────────────────────────
    def closeEvent(self, event):
        if self._dirty:
            resp = QMessageBox.question(
                self, "Unsaved changes",
                "You have changes that haven't been saved to the VM yet. Close anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)

    def reject(self):
        if self._dirty:
            resp = QMessageBox.question(
                self, "Unsaved changes",
                "You have changes that haven't been saved to the VM yet. Close anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        super().reject()