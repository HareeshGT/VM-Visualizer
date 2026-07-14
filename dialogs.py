"""dialogs.py — All modal dialogs: Connect, FileTransfer, LogViewer, Exec,
                FileEditor, FileExec, SearchDialog."""

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
    QTreeWidgetItem, QHeaderView, QShortcut,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt5.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QKeySequence

from themes import T, apply_qss_to
from utils import load_recent_instances, size_fmt, append_terminal_html, append_terminal_text, html_escape, monospace_font
from workers import CommandWorker, _TransferWorker


# ─── OS-style file-transfer dialog ───────────────────────────
class FileTransferDialog(QDialog):
    """OS-style file-transfer sheet with animated progress, speed and ETA."""

    def __init__(self, parent, sftp, direction: str, local_path: str, remote_path: str):
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
    def download(cls, parent, sftp, remote_path: str, local_path: str) -> bool:
        dlg = cls(parent, sftp, "download", local_path, remote_path)
        return dlg.exec_() == QDialog.Accepted

    @classmethod
    def upload(cls, parent, sftp, local_path: str, remote_path: str) -> bool:
        dlg = cls(parent, sftp, "upload", local_path, remote_path)
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
class LogViewerDialog(QDialog):
    """Live-tails pod logs (kubectl logs -f) instead of one-shot fetches.
    Uses _ExecStreamWorker (same streaming machinery as FileExecDialog) to
    keep pushing new lines into the view as they arrive over SSH, rather
    than requiring the user to click Refresh."""

    def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None):
        super().__init__(parent)
        self.ssh            = ssh
        self._pod           = pod
        self._ns            = namespace
        self._container     = container
        self._workers       = []
        self._stream_worker = None   # the currently-running _ExecStreamWorker, if any

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
            worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
            self._workers.append(worker)
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
        self._workers.append(worker)
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
        if self._stream_worker in self._workers:
            self._workers.remove(self._stream_worker)
        self._stream_worker = None
        if "error" not in self.status_lbl.text():
            self.status_lbl.setText("○ stream ended")
            self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")

    def closeEvent(self, event):
        # Stop the SSH channel/thread rather than leaking it once the dialog closes.
        if self._stream_worker is not None:
            self._stream_worker.request_stop()
        super().closeEvent(event)


# ─── K8s exec dialog ─────────────────────────────────────────
class ExecDialog(QDialog):
    def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None):
        super().__init__(parent)
        self.ssh        = ssh
        self._pod       = pod
        self._ns        = namespace
        self._container = container
        self._cwd       = None
        self._workers   = []

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
        layout.addLayout(inp_row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _cleanup_worker(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)

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
            worker.finished.connect(lambda: self._cleanup_worker(worker))
            self._workers.append(worker)
            worker.start()
            self.cmd_inp.clear()
            return

        safe_cmd = (f"cd '{self._cwd}' && {cmd}" if self._cwd else cmd).replace("'", "'\\''")
        full = f"kubectl exec -n {self._ns} {self._pod} {c} -- sh -c '{safe_cmd}' 2>&1"
        append_terminal_html(self.output, f"\n<span style='color:{T['ACCENT2']}'>$ {html_escape(cmd)}</span>")
        worker = CommandWorker(self.ssh, full)
        worker.done.connect(lambda r: append_terminal_text(self.output, r))
        worker.error.connect(lambda e: append_terminal_html(self.output, f"<span style='color:{T['DANGER']}'>[error] {html_escape(e)}</span>"))
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._workers.append(worker)
        worker.start()
        self.cmd_inp.clear()

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

    def __init__(self, parent, sftp, ssh, remote_path: str, content: str, sudo_user=None):
        super().__init__(parent)
        self._sftp      = sftp
        self._ssh       = ssh
        self._remote    = remote_path
        self._sudo_user = sudo_user
        self._original  = content
        self._highlights = []

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

        path_lbl = QLabel(remote_path)
        path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        tb.addWidget(path_lbl)
        tb.addStretch()

        self._modified_dot = QLabel("●")
        self._modified_dot.setStyleSheet(f"color: {T['WARNING']}; font-size: 16px;")
        self._modified_dot.setToolTip("Unsaved changes")
        self._modified_dot.hide()
        tb.addWidget(self._modified_dot)

        self._wrap_btn = QPushButton("⇌ Wrap")
        self._wrap_btn.setCheckable(True)
        self._wrap_btn.setChecked(True)
        self._wrap_btn.setFixedWidth(74)
        self._wrap_btn.toggled.connect(self._toggle_wrap)
        tb.addWidget(self._wrap_btn)

        self._find_btn = QPushButton("🔍 Find")
        self._find_btn.setCheckable(True)
        self._find_btn.setFixedWidth(74)
        self._find_btn.toggled.connect(self._toggle_find_bar)
        tb.addWidget(self._find_btn)

        save_btn = QPushButton("💾  Save")
        save_btn.setObjectName("primary")
        save_btn.setFixedWidth(90)
        save_btn.clicked.connect(self._save)
        tb.addWidget(save_btn)

        save_close_btn = QPushButton("Save & Close")
        save_close_btn.setFixedWidth(110)
        save_close_btn.clicked.connect(self._save_and_close)
        tb.addWidget(save_close_btn)

        discard_btn = QPushButton("Discard")
        discard_btn.setObjectName("danger")
        discard_btn.setFixedWidth(76)
        discard_btn.clicked.connect(self._confirm_close)
        tb.addWidget(discard_btn)
        lay.addWidget(tb_widget)

        # ── Find/Replace bar ──────────────────────────────────
        self._find_bar = QWidget()
        self._find_bar.setFixedHeight(40)
        self._find_bar.setStyleSheet(
            f"background: {T['BG_ITEM']}; border-bottom: 1px solid {T['BORDER']};"
        )
        fb = QHBoxLayout(self._find_bar)
        fb.setContentsMargins(10, 4, 10, 4)
        fb.setSpacing(6)

        self._find_inp = QLineEdit()
        self._find_inp.setPlaceholderText("Find…")
        self._find_inp.setFixedWidth(200)
        self._find_inp.textChanged.connect(self._do_highlight)
        self._find_inp.returnPressed.connect(self._find_next)
        fb.addWidget(self._find_inp)

        self._replace_inp = QLineEdit()
        self._replace_inp.setPlaceholderText("Replace…")
        self._replace_inp.setFixedWidth(200)
        fb.addWidget(self._replace_inp)

        for label, slot in [
            ("Prev",        self._find_prev),
            ("Next",        self._find_next),
            ("Replace",     self._replace_one),
            ("Replace All", self._replace_all),
        ]:
            b = QPushButton(label)
            b.setFixedWidth(86 if "All" in label else 60)
            b.clicked.connect(slot)
            fb.addWidget(b)

        self._match_lbl = QLabel("")
        self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
        fb.addWidget(self._match_lbl)
        fb.addStretch()

        self._case_chk = QCheckBox("Case")
        self._case_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
        self._case_chk.stateChanged.connect(self._do_highlight)
        fb.addWidget(self._case_chk)

        self._find_bar.hide()
        lay.addWidget(self._find_bar)

        # ── Editor area ───────────────────────────────────────
        editor_area = QWidget()
        editor_area.setStyleSheet(f"background: {T['BG_DARK']};")
        ea_lay = QHBoxLayout(editor_area)
        ea_lay.setContentsMargins(0, 0, 0, 0)
        ea_lay.setSpacing(0)

        self._line_nums = QTextEdit()
        self._line_nums.setReadOnly(True)
        self._line_nums.setFixedWidth(52)
        self._line_nums.setFont(monospace_font(12))
        self._line_nums.setStyleSheet(
            f"background: {T['BG_PANEL']}; color: {T['TEXT_MUTED']}; "
            f"border: none; border-right: 1px solid {T['BORDER']}; padding: 12px 4px;"
        )
        self._line_nums.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._line_nums.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ea_lay.addWidget(self._line_nums)

        self.editor = QTextEdit()
        self.editor.setFont(monospace_font(12))
        self.editor.setStyleSheet(
            f"background: {T['BG_DARK']}; color: {T['TEXT_PRIMARY']}; "
            f"border: none; padding: 12px;"
        )
        self.editor.setPlainText(content)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.verticalScrollBar().valueChanged.connect(self._sync_line_scroll)
        ea_lay.addWidget(self.editor, 1)
        lay.addWidget(editor_area, 1)

        self._update_line_numbers()

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
        lang_lbl = QLabel(ext.lstrip(".").upper() if ext else "TXT")
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

    # ── Line numbers ──────────────────────────────────────────
    def _update_line_numbers(self):
        n = self.editor.document().blockCount()
        self._line_nums.setPlainText("\n".join(str(i) for i in range(1, n + 1)))

    def _sync_line_scroll(self, val):
        self._line_nums.verticalScrollBar().setValue(val)

    # ── Helpers ───────────────────────────────────────────────
    def _toggle_wrap(self, on: bool):
        self.editor.setLineWrapMode(QTextEdit.WidgetWidth if on else QTextEdit.NoWrap)

    def _toggle_find_bar(self, on: bool):
        self._find_bar.setVisible(on)
        if on:
            self._find_inp.setFocus()
            self._find_inp.selectAll()
        else:
            self._clear_highlights()

    def _on_text_changed(self):
        self._modified_dot.setVisible(self.editor.toPlainText() != self._original)
        self._update_line_numbers()

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

    # ── Find / Replace ────────────────────────────────────────
    def _search_flags(self):
        from PyQt5.QtGui import QTextDocument
        flags = QTextDocument.FindFlags()
        if self._case_chk.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        return flags

    def _do_highlight(self):
        from PyQt5.QtGui import QTextDocument
        self._clear_highlights()
        term = self._find_inp.text()
        if not term:
            self._match_lbl.setText("")
            return
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(T['WARNING']))
        fmt.setForeground(QColor("#000000"))
        doc    = self.editor.document()
        cursor = QTextCursor(doc)
        count  = 0
        flags  = QTextDocument.FindCaseSensitively if self._case_chk.isChecked() else QTextDocument.FindFlags()
        while True:
            cursor = doc.find(term, cursor, flags)
            if cursor.isNull():
                break
            cursor.mergeCharFormat(fmt)
            self._highlights.append(cursor)
            count += 1
        self._match_lbl.setText(f"{count} match{'es' if count != 1 else ''}")

    def _clear_highlights(self):
        if not self._highlights:
            return
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(T['BG_DARK']))
        fmt.setForeground(QColor(T['TEXT_PRIMARY']))
        for c in self._highlights:
            c.mergeCharFormat(fmt)
        self._highlights = []

    def _find_next(self):
        from PyQt5.QtGui import QTextDocument
        term = self._find_inp.text()
        if not term:
            return
        flags = QTextDocument.FindCaseSensitively if self._case_chk.isChecked() else QTextDocument.FindFlags()
        found = self.editor.find(term, flags)
        if not found:
            cur = self.editor.textCursor()
            cur.movePosition(QTextCursor.Start)
            self.editor.setTextCursor(cur)
            self.editor.find(term, flags)

    def _find_prev(self):
        from PyQt5.QtGui import QTextDocument
        term = self._find_inp.text()
        if not term:
            return
        flags = QTextDocument.FindBackward
        if self._case_chk.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        found = self.editor.find(term, flags)
        if not found:
            cur = self.editor.textCursor()
            cur.movePosition(QTextCursor.End)
            self.editor.setTextCursor(cur)
            self.editor.find(term, flags)

    def _replace_one(self):
        cur = self.editor.textCursor()
        if cur.hasSelection():
            cur.insertText(self._replace_inp.text())
        self._find_next()
        self._do_highlight()

    def _replace_all(self):
        term    = self._find_inp.text()
        replace = self._replace_inp.text()
        if not term:
            return
        text  = self.editor.toPlainText()
        flags = re.IGNORECASE if not self._case_chk.isChecked() else 0
        new_text, n = re.subn(re.escape(term), replace, text, flags=flags)
        if n:
            self.editor.setPlainText(new_text)
            self._set_status(f"Replaced {n} occurrence{'s' if n != 1 else ''}", T['SUCCESS'])
        self._do_highlight()

    # ── Save ──────────────────────────────────────────────────
    def _save(self) -> bool:
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

    def _confirm_close(self):
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
        if self.editor.toPlainText() != self._original:
            r = QMessageBox.question(
                self, "Unsaved Changes", "Discard changes and close?",
                QMessageBox.Discard | QMessageBox.Cancel,
            )
            if r != QMessageBox.Discard:
                event.ignore()
                return
        event.accept()

    @classmethod
    def open_remote(cls, parent, sftp, ssh, remote_path: str, sudo_user=None):
        try:
            with sftp.open(remote_path, "r") as f:
                content = f.read(2 * 1024 * 1024).decode(errors="replace")
        except Exception as e:
            QMessageBox.critical(parent, "Cannot Open File", str(e))
            return None
        dlg = cls(parent, sftp, ssh, remote_path, content, sudo_user=sudo_user)
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