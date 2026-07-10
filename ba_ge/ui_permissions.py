"""Qt permissions panel (macOS) — show each required TCC grant with live status
and a one-click action to grant it.

macOS needs three separate grants for dictation, all of which fail *silently*:
Input Monitoring (detect the hotkey), Accessibility (paste the text), and
Microphone (record). This window lists them, polls their status live (so toggling
one in System Settings reflects here within a second), and gives each row a button
that either shows the OS prompt or opens the right Settings pane.

No-op surface off macOS: :func:`open_permissions_window` and
:func:`maybe_prompt_permissions` return without doing anything when there is
nothing to grant.
"""

from __future__ import annotations

import logging

from . import platform

log = logging.getLogger("bage.ui.permissions")

_DOT = {"granted": "#2ecc71", "denied": "#e74c3c", "unknown": "#f1c40f"}
_STATUS_TEXT = {
    "granted": "Granted",
    "denied": "Not allowed — turn it on in Settings",
    "unknown": "Not requested yet",
}
_WHY = {
    "Input Monitoring": "Detect when you hold the hotkey.",
    "Accessibility": "Paste the transcribed text at your cursor.",
    "Microphone": "Record your voice to transcribe.",
}

_current: dict = {"win": None}


def open_permissions_window(on_restart=None):
    """Show (or raise) the permissions window. Call on the Qt main thread."""
    if not platform.IS_MAC:
        return None
    win = _current.get("win")
    if win is not None:
        try:
            win.refresh()
            win.show()
            win.raise_()
            win.activateWindow()
            return win
        except RuntimeError:
            pass  # previous window destroyed
    win = PermissionsWindow(on_restart)
    _current["win"] = win
    win.show()
    return win


def maybe_prompt_permissions(on_restart=None):
    """Open the window only if something still needs granting (macOS)."""
    if not platform.IS_MAC:
        return None
    if platform.missing_permissions():
        return open_permissions_window(on_restart)
    return None


class PermissionsWindow:
    def __init__(self, on_restart):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (
            QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

        self._on_restart = on_restart
        self._rows: dict[str, tuple] = {}

        self.w = QWidget()
        self.w.setWindowTitle("Ba-Ge — Permissions")
        self.w.setMinimumWidth(460)

        outer = QVBoxLayout(self.w)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)

        intro = QLabel("Ba-Ge needs these macOS permissions to dictate. Each one is "
                       "separate and silent until granted.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        for name in platform.REQUIRED_PERMISSIONS:
            row = QFrame()
            row.setFrameShape(QFrame.StyledPanel)
            h = QHBoxLayout(row)
            h.setContentsMargins(12, 10, 12, 10)
            h.setSpacing(12)

            dot = QLabel("●")
            dot.setFixedWidth(16)

            texts = QVBoxLayout()
            texts.setSpacing(1)
            title = QLabel(f"<b>{name}</b>")
            why = QLabel(_WHY.get(name, ""))
            why.setStyleSheet("color:#9a9a9a")
            status = QLabel("")
            status.setStyleSheet("color:#9a9a9a")
            texts.addWidget(title)
            texts.addWidget(why)
            texts.addWidget(status)

            btn = QPushButton("Grant")
            btn.setFixedWidth(140)
            btn.clicked.connect(lambda _=False, n=name: self._grant(n))

            h.addWidget(dot, 0, Qt.AlignTop)
            h.addLayout(texts, 1)
            h.addWidget(btn, 0, Qt.AlignVCenter)
            outer.addWidget(row)
            self._rows[name] = (dot, status, btn)

        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color:#9a9a9a")
        outer.addWidget(self._note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._restart_btn = QPushButton("Quit && Relaunch")
        self._restart_btn.clicked.connect(self._restart)
        close = QPushButton("Close")
        close.clicked.connect(self.w.close)
        buttons.addWidget(self._restart_btn)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        # Poll status so toggles made in System Settings show up here promptly.
        self._timer = QTimer(self.w)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1200)
        self.refresh()

    def _grant(self, name: str) -> None:
        platform.request_permission(name)
        self.refresh()

    def _restart(self) -> None:
        if self._on_restart:
            self._on_restart()
        else:
            _relaunch_self()

    def refresh(self) -> None:
        status = platform.permission_status()
        need_restart = False
        for name, (dot, label, btn) in self._rows.items():
            st = status.get(name, "unknown")
            dot.setStyleSheet(f"color:{_DOT.get(st, '#f1c40f')}")
            label.setText(_STATUS_TEXT.get(st, st))
            if st == "granted":
                btn.setText("Granted")
                btn.setEnabled(False)
            else:
                btn.setEnabled(True)
                btn.setText("Open Settings" if st == "denied" else "Grant")
                if name in ("Input Monitoring", "Accessibility"):
                    need_restart = True
        if not status or all(v == "granted" for v in status.values()):
            self._note.setText("All set. Hold your hotkey to dictate.")
            self._restart_btn.setEnabled(False)
        elif need_restart:
            self._note.setText("After granting Input Monitoring or Accessibility, "
                               "Ba-Ge must be relaunched for it to take effect.")
            self._restart_btn.setEnabled(True)
        else:
            self._note.setText("")

    def show(self):
        self.w.show()

    def raise_(self):
        self.w.raise_()

    def activateWindow(self):
        self.w.activateWindow()


def _relaunch_self() -> None:
    """Quit and relaunch the app bundle (macOS) so new grants take effect.

    The single-instance lock is held until THIS process dies, so the relaunch
    waits for our PID to exit before reopening — otherwise the new instance would
    hit the lock and immediately quit."""
    import os
    import shlex
    import subprocess
    import sys

    pid = os.getpid()
    marker = ".app/Contents/MacOS/"
    try:
        if marker in sys.executable:
            app_path = sys.executable.split(marker)[0] + ".app"
            cmd = (f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; "
                   f"open -n {shlex.quote(app_path)}")
            subprocess.Popen(["/bin/sh", "-c", cmd])
        else:
            subprocess.Popen([sys.executable] + sys.argv)
    except Exception:
        log.warning("relaunch failed", exc_info=True)
    finally:
        os._exit(0)
