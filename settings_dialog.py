"""settings_dialog.py — App-wide Settings dialog: Security (app lock) and
Kubernetes tab visibility."""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QComboBox, QTabWidget, QWidget, QScrollArea, QDialogButtonBox, QFrame,
    QLineEdit,
)
from PyQt5.QtGui import QFont

from themes import T, apply_qss_to, load_settings, save_settings
import security
import ai_assist
from lock_screen import SetPinDialog

_AUTOLOCK_OPTIONS = [
    ("1 minute", 1), ("2 minutes", 2), ("5 minutes", 5),
    ("10 minutes", 10), ("15 minutes", 15), ("30 minutes", 30),
]


class SettingsDialog(QDialog):
    """`k8s_tab_titles` should be the exact tab-bar strings currently used
    by KubernetesTab's sub_tabs (see KubernetesTab.visible_tab_titles()),
    so the checkboxes shown here always match what's actually on screen."""

    def __init__(self, parent=None, k8s_tab_titles=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(460, 480)
        apply_qss_to(self)

        self._k8s_titles = list(k8s_tab_titles or [])
        self._hidden      = set(load_settings().get("k8s_hidden_tabs", []))
        self._lock         = security.get_lock_settings()
        self._pending_pin  = None  # set only if the user (re)sets the PIN this session

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        title = QLabel("⚙  Settings")
        title.setFont(QFont("Segoe UI", 15, QFont.Bold))
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        lay.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._build_security_tab(), "🔒  Security")
        tabs.addTab(self._build_k8s_tab(), "⎈  Kubernetes Tabs")
        tabs.addTab(self._build_ai_tab(), "🤖  AI")
        lay.addWidget(tabs, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Save")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ── Security tab ─────────────────────────────────────────
    def _build_security_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        v.setContentsMargins(4, 14, 4, 4)

        self.lock_chk = QCheckBox("Require a PIN to open this app")
        self.lock_chk.setChecked(self._lock["enabled"])
        self.lock_chk.toggled.connect(self._on_lock_toggled)
        v.addWidget(self.lock_chk)

        desc = QLabel("Locks the app on launch, and again after a period of inactivity.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)

        v.addSpacing(4)

        self.pin_btn = QPushButton("Change PIN…" if self._lock["pin_hash"] else "Set PIN…")
        self.pin_btn.clicked.connect(self._on_set_pin)
        v.addWidget(self.pin_btn)

        self.pin_status = QLabel("PIN is set." if self._lock["pin_hash"] else "No PIN set yet.")
        self.pin_status.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        v.addWidget(self.pin_status)

        v.addSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Auto-lock after"))
        self.autolock_combo = QComboBox()
        for label, mins in _AUTOLOCK_OPTIONS:
            self.autolock_combo.addItem(label, mins)
        idx = self.autolock_combo.findData(self._lock["autolock_minutes"])
        self.autolock_combo.setCurrentIndex(idx if idx >= 0 else 2)
        row.addWidget(self.autolock_combo)
        row.addStretch()
        v.addLayout(row)

        v.addStretch()
        self._update_security_enabled_state()
        return w

    def _on_lock_toggled(self, checked):
        if checked and not self._lock["pin_hash"] and not self._pending_pin:
            self._on_set_pin()
            if not self._pending_pin:
                # User backed out of PIN entry — nothing to unlock with,
                # so don't leave the checkbox on.
                self.lock_chk.blockSignals(True)
                self.lock_chk.setChecked(False)
                self.lock_chk.blockSignals(False)
        self._update_security_enabled_state()

    def _update_security_enabled_state(self):
        self.autolock_combo.setEnabled(self.lock_chk.isChecked())

    def _on_set_pin(self):
        dlg = SetPinDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self._pending_pin = dlg.pin_value()
            self.pin_status.setText("PIN is set.")
            self.pin_btn.setText("Change PIN…")
            if not self.lock_chk.isChecked():
                self.lock_chk.blockSignals(True)
                self.lock_chk.setChecked(True)
                self.lock_chk.blockSignals(False)
                self._update_security_enabled_state()

    # ── Kubernetes tab ────────────────────────────────────────
    def _build_k8s_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)
        v.setContentsMargins(4, 14, 4, 4)

        desc = QLabel("Choose which Kubernetes sub-tabs are visible.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setSpacing(4)
        iv.setContentsMargins(2, 6, 2, 6)

        self._k8s_checks = {}
        for title in self._k8s_titles:
            chk = QCheckBox(title.strip())
            chk.setChecked(title not in self._hidden)
            iv.addWidget(chk)
            self._k8s_checks[title] = chk
        iv.addStretch()
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        return w

    # ── AI tab ────────────────────────────────────────────────
    def _build_ai_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        v.setContentsMargins(4, 14, 4, 4)

        desc = QLabel(
            "Used by the ✨ Explain button in pod log/exec views to get an "
            "AI diagnosis of crashes and errors. Pick a provider, choose "
            "(or type) a model, and paste the matching API key — each is "
            "stored per-provider and sent only to that provider's own API."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)

        # Keys and model choices are kept per-provider so switching
        # providers in the combo box doesn't lose whatever was entered
        # for the others; only the current provider's values are shown
        # at once.
        ai_settings = ai_assist.get_ai_settings()
        self._ai_keys = dict(ai_settings["api_keys"])
        self._ai_models = dict(ai_settings["models"])
        self._current_provider_id = None

        v.addSpacing(4)
        v.addWidget(QLabel("Provider"))
        self.provider_combo = QComboBox()
        for pid, info in ai_assist.PROVIDERS.items():
            self.provider_combo.addItem(info["label"], pid)
        v.addWidget(self.provider_combo)

        v.addWidget(QLabel("Model"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setToolTip(
            "Pick a sample or type any other model id this provider supports."
        )
        v.addWidget(self.model_combo)

        v.addWidget(QLabel("API key"))
        row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        row.addWidget(self.api_key_edit, 1)

        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(60)
        self.show_key_btn.toggled.connect(self._on_toggle_show_key)
        row.addWidget(self.show_key_btn)
        v.addLayout(row)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        idx = self.provider_combo.findData(ai_settings["provider"])
        # setCurrentIndex fires currentIndexChanged, which populates the
        # model/key fields for whichever provider ends up selected —
        # including the idx < 0 fallback (an unrecognized/removed provider id).
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)

        v.addStretch()
        return w

    def _on_provider_changed(self, _index):
        # Stash whatever's currently entered under the provider we're
        # leaving, then load the fields with what's already saved (or the
        # provider's default model) for the newly-selected one.
        if self._current_provider_id is not None:
            self._ai_keys[self._current_provider_id] = self.api_key_edit.text()
            self._ai_models[self._current_provider_id] = self.model_combo.currentText()

        pid = self.provider_combo.currentData()
        info = ai_assist.PROVIDERS[pid]
        self._current_provider_id = pid

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(info["model_samples"])
        current_model = self._ai_models.get(pid) or info["default_model"]
        model_idx = self.model_combo.findText(current_model)
        if model_idx >= 0:
            self.model_combo.setCurrentIndex(model_idx)
        else:
            self.model_combo.setEditText(current_model)
        self.model_combo.blockSignals(False)

        self.api_key_edit.setText(self._ai_keys.get(pid, ""))
        self.api_key_edit.setPlaceholderText(info["key_placeholder"])

    def _on_toggle_show_key(self, checked):
        self.api_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_key_btn.setText("Hide" if checked else "Show")

    # ── Save ──────────────────────────────────────────────────
    def _on_save(self):
        # Security
        if self.lock_chk.isChecked():
            if self._pending_pin:
                security.set_pin(self._pending_pin, self.autolock_combo.currentData())
            elif self._lock["pin_hash"]:
                security.save_lock_settings(
                    enabled=True, autolock_minutes=self.autolock_combo.currentData()
                )
            else:
                # Checked, but somehow no PIN ended up set — don't leave
                # the app in a "locked with no way to unlock it" state.
                security.disable_lock()
        else:
            security.save_lock_settings(enabled=False)

        # Kubernetes tab visibility
        save_settings(k8s_hidden_tabs=self.hidden_k8s_tabs())

        # AI
        self._ai_keys[self._current_provider_id] = self.api_key_edit.text()
        self._ai_models[self._current_provider_id] = self.model_combo.currentText()
        ai_assist.save_ai_settings(
            self.provider_combo.currentData(), self._ai_keys, self._ai_models
        )

        self.accept()

    def hidden_k8s_tabs(self):
        return [t for t, chk in self._k8s_checks.items() if not chk.isChecked()]