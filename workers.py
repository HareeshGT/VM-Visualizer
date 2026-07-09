"""workers.py — Background QThread workers for SSH commands and file transfers."""

import os
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class ConnectWorker(QThread):
    """Establishes an SSH connection (and opens SFTP) in a background thread
    so the UI thread — and any 'Connecting…' animation — keeps running
    smoothly instead of freezing for the duration of the handshake."""

    connected = pyqtSignal(object, object, str)   # ssh, sftp, home_dir
    error     = pyqtSignal(str)

    def __init__(self, host, port, user, pem, password):
        # type: (str, int, str, str, str) -> None
        super().__init__()
        self.host     = host
        self.port     = port
        self.user     = user
        self.pem      = pem
        self.password = password
        self.finished.connect(self.deleteLater)

    def run(self):
        import paramiko
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(hostname=self.host, port=self.port, username=self.user,
                      timeout=10, banner_timeout=10, auth_timeout=10)
            if self.host in ["127.0.0.1", "localhost"] or self.password:
                kw["password"] = self.password
            elif self.pem:
                kw["key_filename"] = self.pem
            ssh.connect(**kw)
            sftp = ssh.open_sftp()
            _, stdout, _ = ssh.exec_command("echo $HOME")
            home = stdout.read().decode().strip()
            self.connected.emit(ssh, sftp, home)
        except Exception as e:
            self.error.emit(str(e))


class CommandWorker(QThread):
    """Runs a single SSH command in a background thread."""

    done  = pyqtSignal(str)
    error = pyqtSignal(str)

    # Safety net: exec_command's stdout only closes once the remote command
    # actually exits. A script that runs something long-lived in the
    # foreground (e.g. a blocking 'kubectl port-forward' without '&') never
    # exits, so without a timeout this would hang forever with no feedback
    # at all — no output, no error, no next prompt.
    DEFAULT_TIMEOUT = 45  # seconds

    def __init__(self, ssh, cmd, cwd=None, sudo_user=None, timeout=None):
        # type: (object, str, Optional[str], Optional[str], Optional[int]) -> None
        super().__init__()
        self.ssh       = ssh
        self.cmd       = cmd
        self.cwd       = cwd
        self.sudo_user = sudo_user
        self.timeout   = timeout or self.DEFAULT_TIMEOUT
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            if self.sudo_user:
                _, stdout, _ = self.ssh.exec_command(
                    "sudo -u {} sh -c 'echo $HOME'".format(self.sudo_user)
                )
            else:
                _, stdout, _ = self.ssh.exec_command("echo $HOME")
            home = stdout.read().decode().strip()

            prefix = "cd {} 2>/dev/null; ".format(self.cwd) if self.cwd else ""
            inner = (
                "export PATH={h}:{h}/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH;".format(h=home)
                + prefix
                + self.cmd
            )

            if self.sudo_user:
                safe_inner = inner.replace("'", "'\\''")
                cmd = "sudo -u {} sh -c '{}'".format(self.sudo_user, safe_inner)
            else:
                cmd = inner

            _, stdout, stderr = self.ssh.exec_command(cmd)
            stdout.channel.settimeout(self.timeout)
            try:
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
            except Exception as read_err:
                # Most likely socket.timeout: the command is still running
                # and hasn't produced EOF within the timeout window.
                try:
                    stdout.channel.close()
                except Exception:
                    pass
                self.error.emit(
                    "Command is still running after {}s with no output — it looks like it's "
                    "blocking in the foreground rather than exiting (this terminal waits for a "
                    "command to fully finish before showing its result).\n"
                    "If it's meant to keep running (a server, a port-forward, 'tail -f', etc.), "
                    "start it backgrounded instead, e.g.:\n"
                    "    nohup {} > /tmp/out.log 2>&1 &\n"
                    "(raw error: {})".format(self.timeout, self.cmd, read_err)
                )
                return
            self.done.emit(out + ("\n[stderr]\n{}".format(err) if err else ""))
        except Exception as e:
            self.error.emit(str(e))


class _TransferWorker(QThread):
    """Runs a single SFTP get/put in a background thread and emits progress."""

    progress     = pyqtSignal(int, int)   # bytes_done, bytes_total
    finished_ok  = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, sftp, direction, local_path, remote_path):
        # type: (object, str, str, str) -> None
        super().__init__()
        self._sftp      = sftp           # SudoFS instance
        self._direction = direction      # "upload" | "download"
        self._local     = local_path
        self._remote    = remote_path
        self._cancelled = False
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._direction == "download":
                self._download()
            else:
                self._upload()
            if not self._cancelled:
                self.finished_ok.emit()
        except Exception as e:
            if not self._cancelled:
                self.finished_err.emit(str(e))

    # ── helpers ──────────────────────────────────────────────
    def _download(self):
        if self._sftp.sudo_user:
            # sudo path — stream via cat
            try:
                st    = self._sftp.stat(self._remote)
                total = st.st_size
            except Exception:
                total = 0

            out, _ = self._sftp._run(
                "{}cat {} 2>/dev/null".format(
                    self._sftp._sudo_prefix, self._sftp._sq(self._remote))
            )
            if self._cancelled:
                return
            data  = out.encode("utf-8", errors="replace")
            total = total or len(data)
            chunk = 65536
            done  = 0
            with open(self._local, "wb") as f:
                for i in range(0, len(data), chunk):
                    if self._cancelled:
                        return
                    f.write(data[i : i + chunk])
                    done = min(i + chunk, len(data))
                    self.progress.emit(done, total)
        else:
            # Direct SFTP with real progress
            st    = self._sftp._sftp.stat(self._remote)
            total = st.st_size
            done  = 0
            chunk = 65536
            with self._sftp._sftp.open(self._remote, "rb") as remote_f:
                with open(self._local, "wb") as local_f:
                    while True:
                        if self._cancelled:
                            return
                        buf = remote_f.read(chunk)
                        if not buf:
                            break
                        local_f.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)

    def _upload(self):
        total = os.path.getsize(self._local)
        chunk = 65536
        done  = 0
        if self._sftp.sudo_user:
            self.progress.emit(0, total)
            self._sftp.put(self._local, self._remote)
            self.progress.emit(total, total)
        else:
            with open(self._local, "rb") as local_f:
                with self._sftp._sftp.open(self._remote, "wb") as remote_f:
                    while True:
                        if self._cancelled:
                            return
                        buf = local_f.read(chunk)
                        if not buf:
                            break
                        remote_f.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)