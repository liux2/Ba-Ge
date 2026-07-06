"""Injection backend for macOS/Windows via pynput + clipboard.

Default is **clipboard-paste** (Cmd+V on macOS, Ctrl+V on Windows): layout-
independent, atomic, and immune to the dropped-character bug that plagued
per-keystroke typing on Linux. ``backend="type"`` forces per-key synthesis.

UNVERIFIED on macOS/Windows — on macOS this ALSO needs Accessibility permission
(silent no-op without it; see docs/PORTING.md). Controller + clipboard are
injectable for tests.
"""

from __future__ import annotations

import logging
import sys
import time

from .inject import InjectionError

log = logging.getLogger("ptt.inject_pynput")


class PynputInjector:
    def __init__(self, config, controller=None, clipboard=None):
        pref = getattr(config, "inject_backend", "auto")
        self.backend = "type" if pref == "type" else "paste"
        self.key_delay_ms = getattr(config, "key_delay_ms", 20)
        self._paste_restore_delay = 0.2

        if controller is None:
            from pynput.keyboard import Controller
            controller = Controller()
        self._kb = controller

        if clipboard is None:
            import pyperclip
            clipboard = pyperclip
        self._clip = clipboard

    def type_text(self, text: str) -> None:
        if not text:
            return
        if self.backend == "type":
            self._type(text)
        else:
            self._paste(text)

    def _type(self, text: str) -> None:
        try:
            self._kb.type(text)
        except Exception as exc:
            raise InjectionError(f"pynput typing failed: {exc}") from exc

    def _paste(self, text: str) -> None:
        from pynput.keyboard import Key

        try:
            saved = self._clip.paste()
        except Exception:
            saved = None
        try:
            self._clip.copy(text)
        except Exception as exc:
            raise InjectionError(f"clipboard set failed: {exc}") from exc

        modifier = Key.cmd if sys.platform == "darwin" else Key.ctrl
        try:
            with self._kb.pressed(modifier):
                self._kb.press("v")
                self._kb.release("v")
        except Exception as exc:
            raise InjectionError(f"paste keystroke failed: {exc}") from exc

        if saved is not None:
            time.sleep(self._paste_restore_delay)
            try:
                self._clip.copy(saved)
            except Exception:
                pass
