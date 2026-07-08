"""Bundled Qt clipboard manager: history stack + paste coordination.

Ba-Ge owns both the paste and the clipboard, so it can make dictation *invisible*
to the board: when it sets the clipboard to inject a transcript, it flags the
change as its own — so it is **not** recorded into history and the previous
clipboard is restored right after the paste. Real copies still populate the stack.

Runs on the Qt main thread. `paste_text()` is thread-safe (worker threads call it);
it marshals the actual clipboard work onto the main thread via a queued signal.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from PySide6.QtCore import QMimeData, QObject, QTimer, Signal

from .inject import InjectionError

log = logging.getLogger("bage.clipboard")

_RESTORE_MS = 400   # wait for the target to read before restoring the old clipboard
_PASTE_TIMEOUT = 8  # worker-thread guard so a wedged main loop can't hang dictation


class ClipboardManager(QObject):
    _marshal = Signal(object)  # run a callable on the main (GUI) thread

    def __init__(self, app, history_size: int = 20):
        super().__init__()
        self._clip = app.clipboard()
        self._history: deque = deque(maxlen=history_size)
        self._suppress = 0  # ignore this many self-initiated dataChanged events
        self._clip.dataChanged.connect(self._on_change)
        # Connect to a bound method of THIS QObject so cross-thread emits are queued
        # onto the main (GUI) thread — a bare lambda would run on the emitting thread.
        self._marshal.connect(self._invoke)

    def _invoke(self, fn) -> None:
        fn()

    # ---- history ----

    def _on_change(self) -> None:
        if self._suppress > 0:
            self._suppress -= 1
            return
        text = self._clip.text()
        if text and (not self._history or self._history[0] != text):
            # de-dupe: move an existing entry to the front
            try:
                self._history.remove(text)
            except ValueError:
                pass
            self._history.appendleft(text)

    def history(self) -> list:
        return list(self._history)

    def set_clipboard(self, text: str) -> None:
        """Put a history entry back on the clipboard (from the tray menu)."""
        self._marshal.emit(lambda: self._clip.setText(text))

    # ---- coordinated paste (thread-safe) ----

    def paste_text(self, text: str, send_key) -> None:
        """Set the clipboard to `text`, fire `send_key()`, then restore.

        Called from a worker thread. The clipboard *write* is marshalled onto the
        main thread, but `send_key()` runs HERE (the worker) on purpose: while a Qt
        slot executes, the event loop can't answer the target's SelectionRequest, so
        a cross-process paste (a terminal) would read a stale/empty clipboard. Firing
        the keystroke off the main thread keeps the loop free to serve the paste.
        """
        done = threading.Event()
        box: dict = {}

        def prep():
            box["saved"] = self._clone(self._clip.mimeData())
            self._suppress += 1            # our set — don't record it
            self._clip.setText(text)
            done.set()

        self._marshal.emit(prep)
        if not done.wait(_PASTE_TIMEOUT):
            raise InjectionError("clipboard set timed out (Qt main loop unresponsive)")

        send_key()  # worker thread → main loop stays free to serve the paste request

        self._marshal.emit(
            lambda: QTimer.singleShot(_RESTORE_MS, lambda: self._restore(box.get("saved"))))

    def _restore(self, saved) -> None:
        self._suppress += 1            # our restore — don't record it either
        if saved is not None:
            self._clip.setMimeData(saved)
        else:
            self._clip.clear()

    @staticmethod
    def _clone(md) -> QMimeData | None:
        if md is None:
            return None
        copy = QMimeData()
        for fmt in md.formats():
            copy.setData(fmt, md.data(fmt))  # preserve every format (text, images, …)
        return copy
