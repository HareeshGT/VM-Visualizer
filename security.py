"""security.py — App-lock security: PIN hashing, persisted lock settings,
and an inactivity watcher that triggers auto-lock.

The PIN is never stored in plain text — only a salted PBKDF2-SHA256 hash
(see hash_pin/verify_pin) lives in settings.json, alongside the salt used
to produce it.
"""

import hashlib
import os

from PyQt5.QtCore import QObject, QEvent, QTimer, pyqtSignal

from themes import load_settings, save_settings

_PBKDF2_ITERATIONS = 200_000


# ─── PIN hashing ───────────────────────────────────────────────
def generate_salt() -> str:
    return os.urandom(16).hex()


def hash_pin(pin: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()


def verify_pin(pin: str, salt_hex: str, expected_hash: str) -> bool:
    if not salt_hex or not expected_hash or not pin:
        return False
    try:
        return hash_pin(pin, salt_hex) == expected_hash
    except Exception:
        return False


# ─── Persisted lock settings ────────────────────────────────────
# Stored under settings.json's "security" key so it merges cleanly with
# theme / tunnel-path / k8s-tab-visibility settings already saved there
# (see themes.save_settings, which merges rather than overwrites).
_DEFAULTS = {
    "enabled": False,
    "salt": "",
    "pin_hash": "",
    "autolock_minutes": 5,
}


def get_lock_settings() -> dict:
    stored = load_settings().get("security") or {}
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in _DEFAULTS})
    return merged


def save_lock_settings(**kwargs):
    current = get_lock_settings()
    current.update(kwargs)
    save_settings(security=current)


def set_pin(pin: str, autolock_minutes: int = None):
    """Enable the lock and (re)set the PIN — generates a fresh salt so a
    changed PIN also invalidates the old hash entirely."""
    salt = generate_salt()
    kwargs = {
        "enabled": True,
        "salt": salt,
        "pin_hash": hash_pin(pin, salt),
    }
    if autolock_minutes is not None:
        kwargs["autolock_minutes"] = autolock_minutes
    save_lock_settings(**kwargs)


def disable_lock():
    save_lock_settings(enabled=False, salt="", pin_hash="")


# ─── Inactivity watcher ──────────────────────────────────────────
# Installed as an application-wide event filter (see main_window.py) so it
# sees user input across every tab/dialog, not just one widget.
_ACTIVITY_EVENTS = {
    QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.KeyPress,
    QEvent.Wheel, QEvent.TouchBegin, QEvent.TouchUpdate,
}


class InactivityWatcher(QObject):
    """Emits `idle_timeout` once no qualifying user-input event has been
    observed for `minutes` minutes. The countdown restarts on every
    matching event; call `suspend()` while a lock screen (or anything
    else that should pause the clock) is on screen, and `resume()`
    afterwards. `set_minutes()` updates the timeout live, e.g. right
    after the user changes it in Settings."""

    idle_timeout = pyqtSignal()

    def __init__(self, minutes: int, parent=None):
        super().__init__(parent)
        self._minutes    = 5
        self._suspended  = True  # starts suspended; caller resumes explicitly
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.idle_timeout.emit)
        self.set_minutes(minutes)

    def set_minutes(self, minutes: int):
        self._minutes = max(1, int(minutes or 1))
        if not self._suspended:
            self._timer.start(self._minutes * 60_000)

    def suspend(self):
        self._suspended = True
        self._timer.stop()

    def resume(self):
        self._suspended = False
        self._timer.start(self._minutes * 60_000)

    def eventFilter(self, obj, event):
        if not self._suspended and event.type() in _ACTIVITY_EVENTS:
            self._timer.start(self._minutes * 60_000)
        return False  # never actually consume the event