"""Qt settings window — modern cross-platform settings panel."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget,
)

from . import autostart, platform
from .config import (
    Config, load_config, parse_keyterms, save_config, validate_keyterms,
)

log = logging.getLogger("bage.ui.settings")

_MODELS = ["scribe_v2", "scribe_v1"]
_HOTKEYS = ["f9", "f8", "f10", "f7", "space", "cmd_r", "ctrl_r", "caps_lock",
            "pause", "scroll_lock"]

_current: dict = {"win": None}

# macOS virtual keycodes for keys Qt can't name precisely (esp. left/right
# modifiers). Maps to the pynput alias names that hotkey.resolve_key understands.
_MAC_VK = {
    54: "cmd_r", 55: "cmd_l", 56: "shift_l", 60: "shift_r", 58: "alt_l",
    61: "alt_r", 59: "ctrl_l", 62: "ctrl_r", 57: "caps_lock", 63: "",  # fn: unusable
    49: "space", 48: "tab", 53: "esc",
}


def _qt_event_to_hotkey_name(event):
    """Translate a captured Qt key event to a Ba-Ge hotkey name, or None if the key
    isn't usable. Returns names that ``hotkey.resolve_key`` accepts."""
    from PySide6.QtCore import Qt

    from . import hotkey
    if platform.IS_MAC:
        name = _MAC_VK.get(event.nativeVirtualKey())
        if name == "":
            return None  # explicitly unusable (fn)
        if name:
            return name
    key = event.key()
    if Qt.Key_F1 <= key <= Qt.Key_F35:
        name = f"f{key - Qt.Key_F1 + 1}"
    else:
        named = {
            Qt.Key_Space: "space", Qt.Key_Tab: "tab", Qt.Key_Escape: "esc",
            Qt.Key_CapsLock: "caps_lock", Qt.Key_Home: "home", Qt.Key_End: "end",
            Qt.Key_Insert: "insert", Qt.Key_Pause: "pause",
        }
        if key in named:
            name = named[key]
        else:
            text = event.text().strip().lower()
            name = text if (len(text) == 1 and text.isalnum()) else None
    if not name:
        return None
    try:
        hotkey.resolve_key(name)  # validate it round-trips
        return name
    except Exception:
        return None


def open_settings(root=None, exec_cmd: str = "ba-ge", on_saved=None) -> None:
    """Show (or raise) the settings window. Call on the Qt main thread."""
    win = _current.get("win")
    if win is not None:
        try:
            win.show()
            win.raise_()
            win.activateWindow()
            return
        except RuntimeError:
            pass  # previous window was destroyed
    win = SettingsWindow(exec_cmd, on_saved)
    _current["win"] = win
    win.show()


def run_settings(exec_cmd: str = "ba-ge") -> None:
    """Standalone settings window with its own event loop (for `--settings`)."""
    from . import platform, theme
    platform.ensure_qt_plugins()  # macOS: stage plugins out of protected folders
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    win = SettingsWindow(exec_cmd, on_saved=None)
    win.show()
    app.exec()


class SettingsWindow(QWidget):
    def __init__(self, exec_cmd, on_saved):
        super().__init__()
        self.exec_cmd = exec_cmd
        self.on_saved = on_saved
        self.cfg = load_config()
        self._capturing = False
        self.setWindowTitle("Ba-Ge — Settings")

        grid = QGridLayout(self)
        grid.setContentsMargins(22, 20, 22, 18)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        row = 0

        def add(label, widget, span=1, top=False):
            nonlocal row
            lab = QLabel(label)
            lab.setAlignment(Qt.AlignRight | (Qt.AlignTop if top else Qt.AlignVCenter))
            grid.addWidget(lab, row, 0)
            grid.addWidget(widget, row, 1, 1, span)
            row += 1

        # macOS: a banner when TCC grants are still missing (dictation silently
        # won't work without them). Click opens the guided permissions panel.
        missing = platform.missing_permissions()
        if missing:
            banner = QPushButton("⚠  Grant macOS permissions to dictate "
                                 f"({', '.join(missing)}) →")
            banner.setStyleSheet(
                "text-align:left; padding:8px 12px; color:#f0c040;"
                "border:1px solid #f0c040; border-radius:6px;")
            banner.clicked.connect(self._open_permissions)
            grid.addWidget(banner, row, 0, 1, 2)
            row += 1

        # API key + show toggle
        self.api = QLineEdit(self.cfg.api_key)
        self.api.setEchoMode(QLineEdit.Password)
        self.api.setMinimumWidth(320)
        show = QCheckBox("show")
        show.toggled.connect(
            lambda on: self.api.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        keyrow = QHBoxLayout()
        keyrow.addWidget(self.api, 1)
        keyrow.addWidget(show)
        grid.addWidget(QLabel("ElevenLabs API key"), row, 0, Qt.AlignRight)
        grid.addLayout(keyrow, row, 1)
        row += 1

        self.model = QComboBox()
        self.model.addItems(_MODELS)
        if self.cfg.model_id in _MODELS:
            self.model.setCurrentText(self.cfg.model_id)
        add("Model", self.model)

        self.lang = QLineEdit(self.cfg.language or "")
        self.lang.setPlaceholderText("auto-detect")
        add("Language", self.lang)

        self.keyterms = QPlainTextEdit("\n".join(self.cfg.keyterms))
        self.keyterms.setFixedHeight(96)
        add("Custom vocabulary\n(one per line or comma)", self.keyterms, top=True)
        hint = QLabel("Biases names/jargon · ≤1000 terms, ≤50 chars & ≤5 words · ~20% cost")
        hint.setStyleSheet("color:#8a8a8a")
        grid.addWidget(hint, row, 1)
        row += 1

        self.hotkey = QComboBox()
        self.hotkey.setEditable(True)
        self.hotkey.addItems(_HOTKEYS)
        self.hotkey.setCurrentText(self.cfg.hotkey)
        self._record_btn = QPushButton("⌨ Record")
        self._record_btn.setToolTip("Click, then press the key you want to use")
        self._record_btn.clicked.connect(self._toggle_record_hotkey)
        hkrow = QHBoxLayout()
        hkrow.addWidget(self.hotkey, 1)
        hkrow.addWidget(self._record_btn)
        grid.addWidget(QLabel("Hotkey"), row, 0, Qt.AlignRight)
        grid.addLayout(hkrow, row, 1)
        row += 1

        self.mode = QComboBox()
        self.mode.addItem("Hold to talk (push-to-talk)", "hold")
        self.mode.addItem("Toggle (tap to start, tap to stop)", "toggle")
        self.mode.setCurrentIndex(1 if self.cfg.hotkey_mode == "toggle" else 0)
        add("Recording mode", self.mode)
        mode_hint = QLabel("Toggle avoids holding the key — good for normal keys like Space.")
        mode_hint.setStyleSheet("color:#8a8a8a")
        grid.addWidget(mode_hint, row, 1)
        row += 1

        self.mic = QComboBox()
        self._mic_devs = []
        for dev, label in platform.list_input_devices():
            self.mic.addItem(label, dev)
            self._mic_devs.append(dev)
        if self.cfg.audio_device not in self._mic_devs:
            self.mic.addItem(self.cfg.audio_device, self.cfg.audio_device)
            self._mic_devs.append(self.cfg.audio_device)
        self.mic.setCurrentIndex(self._mic_devs.index(self.cfg.audio_device))
        add("Microphone", self.mic)

        self.min_dur = QDoubleSpinBox()
        self.min_dur.setRange(0.0, 5.0)
        self.min_dur.setSingleStep(0.05)
        self.min_dur.setValue(self.cfg.min_duration)
        add("Ignore taps shorter than (s)", self.min_dur)

        self.autostart = QCheckBox("Start automatically on login")
        self.autostart.setChecked(autostart.is_enabled())
        grid.addWidget(self.autostart, row, 1)
        row += 1

        self.status = QLabel("")
        self.status.setStyleSheet("color:#8a8a8a")
        self.status.setWordWrap(True)
        grid.addWidget(self.status, row, 0, 1, 2)
        row += 1

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(close)
        buttons.addWidget(save)
        grid.addLayout(buttons, row, 0, 1, 2)

        self.resize(560, self.sizeHint().height())

    def _open_permissions(self) -> None:
        from .ui_permissions import open_permissions_window
        open_permissions_window()

    # ---- record-a-hotkey capture ----

    def _toggle_record_hotkey(self) -> None:
        if self._capturing:
            self._end_capture()
        else:
            self._capturing = True
            self._record_btn.setText("Press a key…")
            self.status.setText("Press the key you want to hold/tap to dictate "
                                "(Esc-key still selectable).")
            self.grabKeyboard()

    def _end_capture(self) -> None:
        self._capturing = False
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        self._record_btn.setText("⌨ Record")

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._capturing:
            name = _qt_event_to_hotkey_name(event)
            if name:
                self.hotkey.setCurrentText(name)
                self._end_capture()
                self.status.setText(f"Hotkey set to “{name}”. Click Save to apply.")
            event.accept()
            return
        super().keyPressEvent(event)

    def _collect(self) -> Config:
        cfg = Config()
        cfg.api_key = self.api.text().strip()
        cfg.model_id = self.model.currentText() or _MODELS[0]
        cfg.language = self.lang.text().strip() or None
        cfg.keyterms = parse_keyterms(self.keyterms.toPlainText())
        cfg.hotkey = (self.hotkey.currentText().strip() or "f9").lower()
        cfg.hotkey_mode = self.mode.currentData() or "hold"
        cfg.audio_device = self.mic.currentData() or "default"
        cfg.min_duration = round(float(self.min_dur.value()), 2)
        cfg.key_delay_ms = self.cfg.key_delay_ms  # preserved for round-trip; paste ignores it
        cfg.inject_backend = self.cfg.inject_backend or "paste"
        cfg.ui_scale = self.cfg.ui_scale
        return cfg

    def _save(self) -> None:
        cfg = self._collect()
        try:
            path = save_config(cfg)
            autostart.set_enabled(self.autostart.isChecked(), self.exec_cmd)
        except OSError as exc:
            self.status.setText(f"Could not save: {exc}")
            return
        self.cfg = cfg
        _, warnings = validate_keyterms(cfg.keyterms)
        if warnings:
            self.status.setText("Saved — some key terms won't be sent: " + "; ".join(warnings))
        else:
            self.status.setText(f"Saved to {path}")
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                log.exception("on_saved callback failed")
