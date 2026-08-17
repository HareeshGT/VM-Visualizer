"""workers.py — Background QThread workers for SSH commands and file transfers."""

import os
import re
import signal
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


def track_worker(pool: list, worker: QThread) -> QThread:
    """Register *worker* in *pool* and auto-remove it once it finishes.

    Every call site that fires off a background worker needs to keep a
    reference to it (so it isn't garbage-collected mid-run) and drop that
    reference again once it's done. That add/remove bookkeeping used to be
    hand-rolled at each call site (a local ``on_finished`` closure, or a
    one-off lambda) with slightly different implementations scattered
    across main_window.py, dialogs.py, and kubernetes_tab.py. Centralizing
    it here keeps the behavior identical everywhere and removes the
    duplication. Returns the worker so it can be used as
    ``worker = track_worker(self._workers, CommandWorker(...))``.
    """
    pool.append(worker)
    worker.finished.connect(lambda: pool.remove(worker) if worker in pool else None)
    return worker


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
            if self.password:
                # A real password was typed in — force straight password
                # auth. Left at paramiko's defaults, look_for_keys/
                # allow_agent are both True, so connect() tries every key
                # in ~/.ssh and every identity in a running ssh-agent
                # BEFORE ever trying this password — and if the server
                # hard-rejects one of those (a passphrase-locked key it
                # can't open, a key the server explicitly refuses, etc.),
                # paramiko raises AuthenticationException right there and
                # the password is never attempted. This is why "connect
                # with password" could fail even with the correct password
                # typed in. Forcing both off makes this a real password-
                # only attempt, exactly like ssh -o
                # PreferredAuthentications=password would.
                kw["password"]      = self.password
                kw["look_for_keys"] = False
                kw["allow_agent"]   = False
            elif self.pem:
                kw["key_filename"] = self.pem
            elif self.host in ["127.0.0.1", "localhost"]:
                # "Connect to Localhost" quick-button with no password/pem
                # entered — leave look_for_keys/allow_agent at their
                # defaults so a local ~/.ssh key or running ssh-agent can
                # still authenticate, same as a bare `ssh user@localhost`.
                pass
            ssh.connect(**kw)

            # Paramiko's default per-channel flow-control window is small
            # (~2MB), which caps throughput hard on a fast link — every
            # read/write ends up round-trip-bound rather than actually
            # using the available bandwidth. Widen it before any channel
            # (SFTP or exec) is opened on this transport, so every channel
            # opened afterwards inherits it.
            #
            # NOTE: this must be a large-but-sane value, not the literal
            # protocol maximum (paramiko.common.MAX_WINDOW_SIZE, 2**32-1).
            # A lot of real-world SSH servers, appliances, and firewalls
            # reject or mishandle a window advertisement that large and
            # just reset the connection outright — which showed up as
            # instant "Error: Socket is closed" upload/download failures.
            # 64MB is the same order of magnitude used by other
            # high-throughput SFTP clients and is safe in practice.
            try:
                transport = ssh.get_transport()
                transport.default_window_size = 64 * 1024 * 1024

                # Paramiko renegotiates the session keys (a "rekey") once a
                # certain amount of data has crossed the transport — by
                # default around 1GB (packetizer.REKEY_BYTES) or a large
                # packet count (REKEY_PACKETS), whichever comes first. While
                # that renegotiation is happening the *entire* transport is
                # paused — every channel on it, including whatever SFTP
                # transfer is mid-flight — until the handshake completes.
                #
                # Crucially, this counter is cumulative for the *whole life
                # of the transport*, not per-transfer: one SSH/SFTP
                # connection is kept open and shared across everything the
                # app does in a session (directory listings, previews,
                # terminal commands, k8s polling, earlier up/downloads, ...).
                # So even a modest file can be the transfer that happens to
                # tip the counter over, if earlier session activity already
                # used up most of the threshold — that's exactly the
                # "stalls even on a 170MB file" symptom. Raising the
                # threshold well past anything a normal session is likely to
                # accumulate means this basically never fires on its own;
                # a server-initiated rekey (if the remote side enforces one)
                # is unaffected by this and still happens normally.
                try:
                    transport.packetizer.REKEY_BYTES   = pow(2, 40)  # ~1TB
                    transport.packetizer.REKEY_PACKETS = pow(2, 40)
                except Exception:
                    pass

                # Paramiko never sends anything on its own while a session
                # sits idle. Some time after connecting — browsing around,
                # picking a big file to upload — the remote sshd's
                # ClientAliveInterval, a NAT gateway, a cloud load
                # balancer, or a firewall's idle-connection timeout can
                # silently drop the session. The socket then just sits
                # there looking "open" until the next real read/write,
                # which is exactly when an upload/download would hit an
                # instant "Socket is closed" at 0 bytes. A periodic
                # SSH-level keepalive (a lightweight global request every
                # 15s) keeps NAT/firewall mappings alive and lets a truly
                # dead connection be detected quickly instead of silently.
                transport.set_keepalive(15)
            except Exception:
                pass

            sftp = ssh.open_sftp()
            _, stdout, _ = ssh.exec_command("echo $HOME")
            home = stdout.read().decode().strip()
            self.connected.emit(ssh, sftp, home)
        except Exception as e:
            self.error.emit(str(e))


class ConnectionHealthWorker(QThread):
    """Watches the live SSH transport and warns the UI before the session
    is actually gone, instead of the app only finding out when the next
    real operation (file listing, command, tunnel restart...) fails.

    ConnectWorker already calls transport.set_keepalive(15), but that runs
    entirely inside paramiko's own background thread and gives the app no
    visible signal either way — it silently keeps the socket alive, or
    silently lets it die. This worker adds an observable heartbeat on top:
    every INTERVAL seconds it checks transport.is_active() and sends a
    harmless SSH-level "ignore" packet, on a background thread so it never
    blocks the UI. One missed heartbeat is reported as `at_risk` (an early
    warning — could be a transient blip); LOST_THRESHOLD consecutive
    misses, or transport.is_active() going False outright, is reported as
    `lost`.
    """

    at_risk   = pyqtSignal(str)   # first missed heartbeat — may be transient
    recovered = pyqtSignal()      # heartbeats resumed after being at_risk
    lost      = pyqtSignal(str)   # transport confirmed dead

    INTERVAL       = 10   # seconds between heartbeats
    LOST_THRESHOLD = 3    # consecutive missed heartbeats before declaring it lost

    def __init__(self, ssh):
        super().__init__()
        self.ssh = ssh
        self._stop = threading.Event()
        self.finished.connect(self.deleteLater)

    def stop(self):
        # Unblocks the wait() below immediately so the loop exits before
        # its next heartbeat, instead of firing one more check (and
        # possibly a stray `lost` signal) after the caller has already
        # torn down the connection on purpose.
        self._stop.set()

    def run(self):
        fail_count   = 0
        was_at_risk  = False
        while not self._stop.wait(self.INTERVAL):
            try:
                transport = self.ssh.get_transport()
            except Exception:
                transport = None

            if transport is None or not transport.is_active():
                self.lost.emit("SSH transport is no longer active.")
                return

            try:
                transport.send_ignore()
            except Exception as e:
                fail_count += 1
                if fail_count >= self.LOST_THRESHOLD:
                    self.lost.emit(str(e))
                    return
                if fail_count == 1:
                    self.at_risk.emit(str(e))
                    was_at_risk = True
            else:
                if was_at_risk:
                    self.recovered.emit()
                    was_at_risk = False
                fail_count = 0


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


class FileStreamReadWorker(QThread):
    """Reads a remote file in 64KB chunks on a background QThread.

    Opening/editing a large remote file used to do a single synchronous
    ``sftp.open(path).read(N)`` (or, worse, a sudo ``cat`` whose *entire*
    output was buffered by ``SudoFS._run()``) directly on the UI thread.
    For a big enough file that call could take long enough to look like a
    hang, and for the sudo path it could pull the whole file into memory
    before any size cap even applied — together, effectively a crash for
    large files. This worker moves that read off the UI thread entirely
    and reads incrementally, chunk by chunk, so the Qt event loop keeps
    running (progress can be shown, Cancel stays clickable) no matter how
    large the remote file is.

    Works for both the plain-SFTP path and the sudo path:
      - No sudo user: reads straight from the paramiko SFTP file handle,
        which gives real per-chunk progress against the file's actual size.
      - Sudo user active: streams a ``sudo -u <user> cat`` command's output
        over a raw paramiko channel (recv() polling), the same pattern
        used elsewhere in the app for live command streaming, rather than
        letting it block until the whole thing lands in one shot.
    """

    progress     = pyqtSignal(int, int)   # bytes_done, bytes_total (total==0 if unknown)
    chunk_ready  = pyqtSignal(bytes)      # each chunk, as it arrives — lets a caller render live
    finished_ok  = pyqtSignal(int)        # total bytes read, once the read completes (or hits max_bytes)
    finished_err = pyqtSignal(str)

    CHUNK_SIZE = 64 * 1024

    def __init__(self, sftp, ssh, remote_path, sudo_user=None, max_bytes=None):
        # type: (object, object, str, Optional[str], Optional[int]) -> None
        super().__init__()
        self._sftp      = sftp   # SudoFS instance
        self._ssh       = ssh
        self._remote     = remote_path
        self._sudo_user  = sudo_user
        self._max_bytes  = max_bytes
        self._cancelled  = False
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._sudo_user:
                self._run_sudo_stream()
            else:
                self._run_direct_stream()
        except Exception as e:
            self.finished_err.emit(str(e))

    # ── plain SFTP path — real chunked reads with real progress ──
    def _run_direct_stream(self):
        raw = getattr(self._sftp, "_sftp", self._sftp)
        try:
            total = raw.stat(self._remote).st_size
        except Exception:
            total = 0

        done   = 0
        with raw.open(self._remote, "rb") as f:
            try:
                f.MAX_REQUEST_SIZE = 256 * 1024
                f.prefetch(total or None)
            except Exception:
                pass
            while True:
                if self._cancelled:
                    return
                buf = f.read(self.CHUNK_SIZE)
                if not buf:
                    break
                done += len(buf)
                self.chunk_ready.emit(buf)
                self.progress.emit(done, total)
                if self._max_bytes and done >= self._max_bytes:
                    break
        self.finished_ok.emit(done)

    # ── sudo path — stream a "sudo -u <user> cat" over a raw channel ──
    def _run_sudo_stream(self):
        try:
            total = self._sftp.stat(self._remote).st_size
        except Exception:
            total = 0

        prefix = getattr(self._sftp, "_sudo_prefix", "")
        sq     = getattr(self._sftp, "_sq", lambda p: "'" + p.replace("'", "'\\''") + "'")
        cmd    = "{}cat {} 2>/dev/null".format(prefix, sq(self._remote))

        channel = self._ssh.get_transport().open_session()
        channel.settimeout(0.5)
        channel.exec_command(cmd)

        done   = 0
        while True:
            if self._cancelled:
                channel.close()
                return
            try:
                if channel.recv_ready():
                    buf = channel.recv(self.CHUNK_SIZE)
                    if buf:
                        done += len(buf)
                        self.chunk_ready.emit(buf)
                        self.progress.emit(done, total)
                        if self._max_bytes and done >= self._max_bytes:
                            channel.close()
                            break
                        continue
            except Exception:
                pass
            if channel.exit_status_ready() and not channel.recv_ready():
                break
            self.msleep(30)

        self.finished_ok.emit(done)


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
                msg = str(e)
                # A dead/idled-out SSH session (no keepalive reached it in
                # time, the server's ClientAliveInterval fired, a NAT/
                # firewall dropped it, ...) surfaces here as a low-level
                # socket error on the very first read/write — the raw
                # message ("Socket is closed", "Connection reset by
                # peer") isn't obviously actionable, so translate it.
                low = msg.lower()
                if "socket is closed" in low or "connection reset" in low or "broken pipe" in low:
                    msg = (
                        "Connection to the VM was lost (the SSH session "
                        "went idle and got dropped). Please reconnect and "
                        "try again."
                    )
                self.finished_err.emit(msg)

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
            # Direct SFTP with real progress.
            #
            # The naive version of this loop (a plain remote_f.read(chunk)
            # in a while loop) issues one SFTP request, waits for its
            # reply, *then* issues the next — every chunk pays a full
            # network round trip before the next one even starts. On a
            # fast/high-latency-ish link that caps throughput at a few
            # hundred KB/s no matter how much raw bandwidth is actually
            # available (exactly the "1400MiB/s on the box, KB/s over
            # SFTP" symptom). prefetch() queues many concurrent read
            # requests in the background so .read() calls are mostly
            # served from an already-filled buffer instead of blocking on
            # a round trip each time — this is the same technique
            # paramiko's own sftp.get() uses internally.
            REQUEST_SIZE = 256 * 1024
            st    = self._sftp._sftp.stat(self._remote)
            total = st.st_size
            done  = 0
            chunk = REQUEST_SIZE
            with self._sftp._sftp.open(self._remote, "rb") as remote_f:
                remote_f.MAX_REQUEST_SIZE = REQUEST_SIZE
                remote_f.prefetch(total)
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
            # Same round-trip problem as the download path, mirrored for
            # writes: set_pipelined(True) stops paramiko from waiting for
            # each write's server ack before sending the next chunk, so
            # writes queue up back-to-back instead of stalling on
            # latency. This is what paramiko's own sftp.put() does
            # internally, too.
            REQUEST_SIZE = 256 * 1024
            chunk = REQUEST_SIZE
            with open(self._local, "rb") as local_f:
                with self._sftp._sftp.open(self._remote, "wb") as remote_f:
                    remote_f.MAX_REQUEST_SIZE = REQUEST_SIZE
                    remote_f.set_pipelined(True)
                    while True:
                        if self._cancelled:
                            return
                        buf = local_f.read(chunk)
                        if not buf:
                            break
                        remote_f.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)

class _PtyProc:
    """Wraps an os.forkpty() child so the read loop below can treat it the
    same way it treats a subprocess.Popen object — .poll() / .wait() /
    .kill(), nothing loop-specific has to know which one it's holding."""

    def __init__(self, pid: int, master_fd: int):
        self.pid       = pid
        self.master_fd = master_fd
        self._status   = None

    def fileno(self) -> int:
        return self.master_fd

    def poll(self):
        """None while still running, else the exit code."""
        if self._status is not None:
            return self._status
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._status = 0
            return self._status
        if wpid == 0:
            return None
        self._status = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        return self._status

    def wait(self):
        if self._status is not None:
            return self._status
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            self._status = 0
            return self._status
        self._status = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        return self._status

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass


class ScpTransferWorker(QThread):
    """Runs a single upload/download as a real ``scp`` subprocess on the
    machine this app is running on, instead of shuttling bytes through
    paramiko's SFTP implementation.

    Why this exists: paramiko's SFTP — even with the prefetch/pipelining
    tricks in _TransferWorker above — tops out well short of what the same
    link does over a plain ``scp``: OpenSSH's own C implementation
    pipelines far more aggressively and isn't paying Python's per-packet
    overhead. Shelling out to the system ``scp`` binary gets the same
    throughput a user would get typing the command by hand, e.g.:

        scp -i key.pem ~/Downloads/4K.mp4 ec2-user@44.224.86.255:/home/ec2-user/gt/

    This is only used for the plain (non-sudo) case — a sudo-target
    upload/download still goes through SudoFS/_TransferWorker, since that
    path already needs a second hop (upload to a tmp path, then ``sudo mv``
    over the existing ssh session) that a single scp invocation can't
    express. Both key- and password-authenticated connections use scp:
    for a password, this worker answers scp's own "password:" prompt
    itself over the pty below, the same way a person typing it by hand
    would — no external helper (sshpass, etc.) needed.
    """

    progress     = pyqtSignal(int, int)   # bytes_done (estimated from scp's own % ), bytes_total
    finished_ok  = pyqtSignal()
    finished_err = pyqtSignal(str)

    _PASSWORD_PROMPT_RE = re.compile(rb"(?i)password:\s*$")

    def __init__(self, host, port, user, pem, password, direction, local_path, remote_path, total_size=None):
        # type: (str, int, str, str, str, str, str, str, Optional[int]) -> None
        super().__init__()
        self._host      = host
        self._port      = port or 22
        self._user      = user
        self._pem       = pem
        self._password  = password or None
        self._direction = direction        # "upload" | "download"
        self._local     = local_path
        self._remote    = remote_path
        self._total     = total_size or 0
        self._cancelled = False
        self._proc      = None
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ── helpers ──────────────────────────────────────────────
    def _build_cmd(self):
        remote_spec = "{}@{}:{}".format(self._user, self._host, self._remote)
        if self._direction == "upload":
            src, dst = self._local, remote_spec
        else:
            src, dst = remote_spec, self._local

        cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new"]
        # accept-new auto-trusts a host key we haven't seen before but
        # still refuses one that *changed*, same as ssh would warn about
        # interactively — either way we're not left hanging on that prompt.
        if self._pem:
            # Fully unattended: BatchMode disables every interactive
            # prompt (falling straight to an error instead), which is
            # safe here since a key is what's actually authenticating.
            cmd += ["-o", "BatchMode=yes", "-i", self._pem]
        elif self._password:
            # We're about to answer scp's password prompt ourselves over
            # the pty (see run()), so BatchMode must stay off here. Skip
            # local ~/.ssh keys and any running ssh-agent identity so a
            # hard rejection from one of those can't derail this into a
            # failure before the password prompt even shows up — the same
            # class of bug fixed on the paramiko side in ConnectWorker.
            # NumberOfPasswordPrompts=1 means a wrong password fails fast
            # instead of scp silently re-prompting for a password we're
            # not going to answer again.
            cmd += ["-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1"]
        if self._port and int(self._port) != 22:
            cmd += ["-P", str(self._port)]
        cmd += [src, dst]
        return cmd

    @staticmethod
    def _parse_pct(line: bytes):
        """Pull the percentage out of one line/segment of scp's own
        progress meter, e.g. '4Kfile.mp4    45%   45MB   10.2MB/s   00:02'."""
        text = line.decode(errors="replace")
        m = re.search(r"(\d{1,3})%", text)
        if not m:
            return None
        return max(0, min(100, int(m.group(1))))

    def run(self):
        import subprocess
        import select

        if not self._total and self._direction == "upload":
            try:
                self._total = os.path.getsize(self._local)
            except Exception:
                self._total = 0

        cmd = self._build_cmd()

        # scp's progress meter is gated on more than just "is stdout a tty":
        # OpenSSH also checks that the process is running in the
        # *foreground* of that terminal's session (tcgetpgrp), which needs
        # a real controlling terminal, not just a tty-typed file descriptor.
        # Handing a plain pty slave fd to subprocess.Popen (via
        # pty.openpty()) gives scp a tty it can isatty()-check, but the
        # child is never made a session leader or given that pty as its
        # controlling terminal — so the foreground check still fails and
        # the meter stays off, exactly like a plain pipe. os.forkpty() is
        # the version that actually does the setsid()-and-attach dance (the
        # same mechanism the `script` command and tools like pexpect use),
        # so scp behaves exactly as it does when a person runs it by hand.
        pid       = None
        master_fd = None
        try:
            pid, master_fd = os.forkpty()
        except (AttributeError, OSError):
            pid = None

        if pid == 0:
            # Child: this thread's Python state ends here — replace the
            # process image immediately with the real scp binary. cmd was
            # already fully built before the fork, so there's nothing left
            # to compute (allocate/lock) in the child beforehand.
            try:
                os.execvp(cmd[0], cmd)
            finally:
                os._exit(127)  # only reached if execvp itself failed

        if pid is not None:
            proc    = _PtyProc(pid, master_fd)
            read_fd = master_fd
        else:
            # forkpty() unavailable for some reason — fall back to a plain
            # pipe. No live progress meter from scp in this case, but the
            # transfer and the exit-detection logic below still work.
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                )
            except FileNotFoundError:
                self.finished_err.emit(
                    "The 'scp' command isn't available on this machine. Install "
                    "an OpenSSH client (or check your network/PATH settings)."
                )
                return
            except Exception as e:
                self.finished_err.emit(str(e))
                return
            read_fd = proc.stdout.fileno()

        self._proc = proc  # so cancel() can kill whichever kind this is

        buf           = b""
        last_pct      = -1
        password_sent = not bool(self._password)
        # Drive this off the process's own exit status (poll()), not off
        # read() returning EOF. ssh/scp can leave the pty/pipe's write end
        # referenced by a lingering child process even after the transfer
        # you actually care about has finished, so waiting for a real EOF
        # can hang indefinitely with the file already fully uploaded on the
        # other end. Polling for process exit alongside a short select()
        # timeout means we notice the moment scp itself is done, and only
        # keep reading in the meantime to surface live progress.
        while True:
            if self._cancelled:
                proc.kill()
                break

            exit_code = proc.poll()
            try:
                rlist, _, _ = select.select([read_fd], [], [], 0.2)
            except (OSError, ValueError):
                rlist = []

            got_data = False
            if rlist:
                try:
                    chunk = os.read(read_fd, 4096)
                except OSError:
                    # A pty raises EIO once the slave side is fully closed,
                    # instead of returning b"" like a pipe would — treat it
                    # the same as "nothing more to read right now".
                    chunk = b""
                if chunk:
                    got_data = True
                    buf += chunk

                    # scp's "ec2-user@1.2.3.4's password: " prompt has no
                    # trailing \r or \n — ssh just sits there waiting for
                    # input right after it — so the \r/\n line-splitting
                    # below would never see it; it'd sit in buf forever
                    # while we wait on a prompt nobody answers. Check for
                    # it directly against the raw buffer instead, and type
                    # the password back through the pty exactly like a
                    # person would (ssh disables local echo while reading
                    # it, so it never comes back through our own read()).
                    if not password_sent and self._PASSWORD_PROMPT_RE.search(buf[-64:]):
                        try:
                            os.write(read_fd, (self._password + "\n").encode())
                        except OSError:
                            pass
                        password_sent = True
                        buf = b""  # the prompt text itself has no % to parse

                    while b"\r" in buf or b"\n" in buf:
                        idx_r = buf.find(b"\r")
                        idx_n = buf.find(b"\n")
                        idx   = min(i for i in (idx_r, idx_n) if i != -1)
                        line, buf = buf[:idx], buf[idx + 1:]
                        pct = self._parse_pct(line)
                        if pct is not None and pct != last_pct:
                            last_pct = pct
                            done = int(self._total * pct / 100) if self._total else pct
                            self.progress.emit(done, self._total)

            if not got_data and exit_code is not None:
                break

        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass

        if self._cancelled:
            return

        ret = proc.wait()
        if ret != 0:
            tail = buf.decode(errors="replace").strip()
            self.finished_err.emit(tail or "scp exited with status {}".format(ret))
            return

        if self._total:
            self.progress.emit(self._total, self._total)
        self.finished_ok.emit()


# ─── Local streaming proxy for media playback ─────────────────
# QMediaPlayer plays best from a URL it can issue HTTP GET/Range requests
# against — that's how it drives seeking, buffering, and progressive
# playback. Since the only channel back to the box is SSH/SFTP, this spins
# up a tiny HTTP server bound to 127.0.0.1 that translates each incoming
# request into SFTP reads (or, if a sudo user is active and plain SFTP
# can't reach the file, a streamed "sudo cat") on demand. Nothing is ever
# written to local disk — every response streams bytes straight from the
# remote connection to the player as they're requested.
class _ChannelReader:
    """Minimal file-like wrapper around a raw paramiko Channel, for the
    sudo-cat streaming fallback (sequential-only, no seek)."""

    def __init__(self, channel):
        self._channel = channel
        self._channel.settimeout(0.5)
        self._eof = False

    def read(self, n):
        if self._eof:
            return b""
        while True:
            try:
                if self._channel.recv_ready():
                    buf = self._channel.recv(n)
                    if not buf:
                        self._eof = True
                    return buf
            except Exception:
                pass
            if self._channel.exit_status_ready() and not self._channel.recv_ready():
                self._eof = True
                return b""

    def close(self):
        try:
            self._channel.close()
        except Exception:
            pass


class _RangeHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # a request per byte-range chunk would otherwise spam stderr

    def do_HEAD(self):
        self.close_connection = True
        self._send_headers(0, max(self.server.file_size - 1, 0), ranged=False)

    def do_GET(self):
        self.close_connection = True
        size  = self.server.file_size
        start, end = 0, max(size - 1, 0)
        ranged = False
        rng = self.headers.get("Range")
        if rng and self.server.supports_range and size:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                ranged = True
        self._send_headers(start, end, ranged)
        if self.command != "HEAD":
            self._stream_range(start, end)

    def _send_headers(self, start, end, ranged):
        size   = self.server.file_size
        length = (end - start + 1) if size else None
        if ranged:
            self.send_response(206)
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Accept-Ranges", "bytes" if self.server.supports_range else "none")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()

    def _stream_range(self, start, end):
        try:
            reader = self.server.open_reader(start)
        except Exception:
            return
        size      = self.server.file_size
        remaining = (end - start + 1) if size else None
        chunk     = 65536
        try:
            while remaining is None or remaining > 0:
                want = chunk if remaining is None else min(chunk, remaining)
                buf = reader.read(want)
                if not buf:
                    break
                self.wfile.write(buf)
                if remaining is not None:
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                reader.close()
            except Exception:
                pass


class _MediaStreamHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MediaStreamServer:
    """Serves exactly one remote file over a loopback-only local HTTP
    server so QMediaPlayer can stream it directly — with real Range-based
    seeking when plain SFTP access is available — instead of the app
    downloading the whole file to local disk first.

    - No sudo user (or sudo user but the login account can still read the
      file directly): opens a dedicated SFTP channel and serves true
      byte-range reads straight from it, so seeking works.
    - Sudo user active and the file isn't reachable over plain SFTP: falls
      back to a single sequential ``sudo -u <user> cat`` stream over a raw
      channel. Still zero local disk usage, but no seeking mid-stream.
    """

    def __init__(self, ssh, sftp, remote_path, sudo_user=None):
        self._ssh        = ssh
        self._sftp       = sftp     # SudoFS instance
        self._remote      = remote_path
        self._sudo_user   = sudo_user
        self._httpd       = None
        self._thread      = None
        self._raw_sftp    = None
        self.url            = None
        self.supports_range = False

    def start(self) -> str:
        content_type = mimetypes.guess_type(self._remote)[0] or "application/octet-stream"

        size, supports_range = 0, False
        if not self._sudo_user:
            try:
                raw = self._ssh.open_sftp()
                size = raw.stat(self._remote).st_size
                self._raw_sftp = raw
                supports_range = True
            except Exception:
                self._raw_sftp = None
                supports_range = False

        if supports_range:
            raw_sftp = self._raw_sftp
            file_size = size

            def open_reader(start):
                f = raw_sftp.open(self._remote, "rb")
                f.MAX_REQUEST_SIZE = 256 * 1024
                if start:
                    f.seek(start)
                # prefetch() queues concurrent read-ahead requests from
                # the current position onward, so playback (and seeking,
                # which opens a fresh reader at the new offset) streams
                # smoothly instead of stalling on a round trip per chunk.
                try:
                    f.prefetch(file_size)
                except Exception:
                    pass
                return f
        else:
            prefix = getattr(self._sftp, "_sudo_prefix", "")
            sq     = getattr(self._sftp, "_sq", lambda p: "'" + p.replace("'", "'\\''") + "'")
            cmd    = "{}cat {} 2>/dev/null".format(prefix, sq(self._remote))
            ssh    = self._ssh

            def open_reader(_start):
                channel = ssh.get_transport().open_session()
                channel.exec_command(cmd)
                return _ChannelReader(channel)

            try:
                size = self._sftp.stat(self._remote).st_size
            except Exception:
                size = 0

        httpd = _MediaStreamHTTPServer(("127.0.0.1", 0), _RangeHTTPRequestHandler)
        httpd.file_size      = size
        httpd.content_type   = content_type
        httpd.supports_range = supports_range
        httpd.open_reader    = open_reader
        httpd.timeout        = 30

        self._httpd  = httpd
        self.supports_range = supports_range
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

        port = httpd.server_address[1]
        self.url = "http://127.0.0.1:{}/stream".format(port)
        return self.url

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._raw_sftp is not None:
            try:
                self._raw_sftp.close()
            except Exception:
                pass
            self._raw_sftp = None


class _StreamServerStartWorker(QThread):
    """Starts a MediaStreamServer off the UI thread (opening the dedicated
    SFTP channel it needs can take a moment on a slow link)."""

    ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, server: "MediaStreamServer"):
        super().__init__()
        self._server = server
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            url = self._server.start()
            self.ready.emit(url)
        except Exception as e:
            self.error.emit(str(e))