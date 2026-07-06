"""Cross-platform UI runtime — the app's tray host, built on PySide6 (Qt).

Qt is self-contained (its wheels bundle their own libraries — no system Tk/GTK),
and QSystemTrayIcon speaks StatusNotifier natively, so the tray works on GNOME
without PyGObject. One codebase serves Linux, macOS, and Windows.

THE event-loop rule (see docs/PORTING.md):
  * a QApplication owns the MAIN thread (`app.exec()`);
  * the tray icon and every widget live on the main thread;
  * pynput / worker threads NEVER touch Qt directly — they emit a Qt Signal on a
    QObject bridge, which Qt delivers (queued) on the main thread. This is the
    portable form of the old GLib.idle_add / tk `after` marshalling.
"""

from __future__ import annotations

import logging
import signal
import sys

from .state import State

log = logging.getLogger("ptt.ui")

_COLORS = {
    State.IDLE: "#cdcdcd",
    State.RECORDING: "#e74c3c",
    State.BUSY: "#f1c40f",
    State.ERROR: "#e67e22",
}
_TIP = {
    State.IDLE: "PTT Dictation — idle (hold {hk})",
    State.RECORDING: "Recording…",
    State.BUSY: "Transcribing…",
    State.ERROR: "PTT Dictation — error",
}


class UiRuntime:
    owns_main_loop = True

    def __init__(self, on_quit, on_settings=None, on_transcribe=None, hotkey_name="F9"):
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
        from PySide6.QtCore import QObject, Signal

        from . import theme

        self._on_quit = on_quit
        self._hk = hotkey_name

        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)  # closing a window must not quit the app
        theme.apply(self._app)
        self.root = None  # Qt windows are parentless top-levels; kept for the app.py contract

        class _Bridge(QObject):
            state = Signal(object)
            call = Signal(object)

        self._bridge = _Bridge()
        self._bridge.state.connect(self._apply_state)  # delivered on the main thread
        self._bridge.call.connect(lambda fn: fn())

        self._tray = QSystemTrayIcon(self._icon_for(State.IDLE), self._app)
        menu = QMenu()
        if on_transcribe is not None:
            menu.addAction("Transcribe file…", on_transcribe)
        if on_settings is not None:
            menu.addAction("Settings…", on_settings)
        menu.addAction("Quit", self.quit)
        self._menu = menu  # keep a reference
        self._tray.setContextMenu(menu)
        self._tray.setToolTip(_TIP[State.IDLE].format(hk=self._hk))
        self._tray.show()

    # ---- tray icon ----

    def _icon_for(self, state: State):
        from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
        from PySide6.QtCore import Qt

        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_COLORS[state]))
        p.drawEllipse(8, 8, 48, 48)
        p.end()
        return QIcon(pm)

    def _apply_state(self, state: State) -> None:
        try:
            self._tray.setIcon(self._icon_for(state))
            self._tray.setToolTip(_TIP[state].format(hk=self._hk))
        except Exception:
            log.debug("tray update failed", exc_info=True)

    # ---- Indicator interface ----

    def set_state(self, state: State) -> None:
        self._bridge.state.emit(state)  # safe from any thread (queued to main)

    def run_on_ui(self, fn) -> None:
        """Run fn on the Qt (main) thread. Safe from any thread."""
        self._bridge.call.emit(fn)

    def run_main_loop(self) -> None:
        from PySide6.QtCore import QTimer

        # A no-op timer keeps Python running periodically so Unix signals
        # (Ctrl-C / SIGTERM) are delivered while Qt's C++ loop blocks.
        self._keepalive = QTimer()
        self._keepalive.timeout.connect(lambda: None)
        self._keepalive.start(200)

        def handler(*_):
            self.quit()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not on the main thread / unsupported

        self._app.exec()

    def quit(self) -> None:
        try:
            if self._on_quit:
                self._on_quit()
        finally:
            try:
                self._tray.hide()
            except Exception:
                pass
            self._app.quit()


def make_indicator(on_quit, on_settings=None, on_transcribe=None, hotkey_name="F9"):
    return UiRuntime(on_quit, on_settings, on_transcribe, hotkey_name)
