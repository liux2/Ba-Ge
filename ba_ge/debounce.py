"""Collapse X11 auto-repeat into a single sustained hold.

On X11 the server emits a synthetic KeyRelease after every auto-repeat
KeyPress (unless a client enables DetectableAutoRepeat, which pynput's XRecord
listener does not). So holding a key produces:

    press, release, press, release, ...   (~30 Hz after the initial delay)

A naive "fire on_stop on release" would stop ~0.5 s into a hold. HoldDebouncer
fixes this generically: a release schedules on_stop after a short delay, and an
auto-repeat press that arrives before the timer fires cancels it. Only a genuine
final release (with no press chasing it) lets the timer through to on_stop.

This module has no GUI/pynput dependency so the state logic is unit-testable.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("bage.hotkey")


class HoldDebouncer:
    def __init__(self, on_start, on_stop, delay: float = 0.06,
                 timer_factory=threading.Timer):
        self._on_start = on_start
        self._on_stop = on_stop
        self._delay = delay
        self._timer_factory = timer_factory
        self._held = False
        self._pending = None  # pending stop timer, or None
        self._lock = threading.Lock()

    def press(self) -> None:
        fire = False
        with self._lock:
            if self._pending is not None:
                # Auto-repeat resume: a release was pending — cancel it, stay held.
                self._pending.cancel()
                self._pending = None
                return
            if self._held:
                return
            self._held = True
            fire = True
        if fire:
            self._safe(self._on_start)

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            if self._pending is not None:
                self._pending.cancel()
            self._pending = self._timer_factory(self._delay, self._fire_stop)
            timer = self._pending
        timer.start()

    def _fire_stop(self) -> None:
        fire = False
        with self._lock:
            self._pending = None
            if self._held:
                self._held = False
                fire = True
        if fire:
            self._safe(self._on_stop)

    def cancel(self) -> None:
        """Drop any pending stop without firing (used on shutdown)."""
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None
            self._held = False

    @staticmethod
    def _safe(callback) -> None:
        # A raising callback must never kill the listener / timer thread.
        try:
            callback()
        except Exception:
            log.exception("hotkey callback failed")
