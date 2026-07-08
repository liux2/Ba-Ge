"""Insert text at the cursor by pasting — fast and atomic.

Ba-Ge sets the clipboard to the transcript, sends the paste shortcut (Ctrl+V, or
Ctrl+Shift+V in terminals), and restores the previous clipboard — all coordinated
through the bundled clipboard manager (`ba_ge/clipboard.py`), so dictation never
pollutes your clipboard or its history, and it's instant (no per-key typing).

The paste keystroke is synthesised with **pynput** (XTEST) — X11 only. xdotool and
ydotool are retired.
"""

from __future__ import annotations

import logging

log = logging.getLogger("bage.inject")

# WM_CLASS substrings for terminals that paste with Ctrl+Shift+V (not Ctrl+V).
_TERMINAL_HINTS = ("terminal", "konsole", "xterm", "alacritty", "kitty",
                   "terminator", "tilix", "rxvt", "st-256color", "wezterm",
                   "foot", "ptyxis", "ghostty", "warp", "hyper", "tabby",
                   "urxvt", "termite", "sakura", "roxterm", "contour", "wave")


class InjectionError(Exception):
    pass


def resolve_backend(pref: str) -> str:
    return "paste"


class Injector:
    def __init__(self, config, clipboard=None):
        # clipboard is a ClipboardManager (Qt, main thread); app.py wires it in.
        self._clipboard = clipboard
        self.backend = "paste"

    def bind_clipboard(self, clipboard) -> None:
        self._clipboard = clipboard

    def type_text(self, text: str) -> None:
        if not text:
            return
        if self._clipboard is None:
            raise InjectionError("clipboard manager unavailable — cannot paste")
        terminal = self._active_is_terminal()
        self._clipboard.paste_text(text, lambda: self._send_paste_key(terminal))

    # ---- helpers ----

    def _active_is_terminal(self) -> bool:
        return any(h in _active_window_class() for h in _TERMINAL_HINTS)

    @staticmethod
    def _send_paste_key(terminal: bool) -> None:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        mods = (Key.ctrl, Key.shift) if terminal else (Key.ctrl,)
        for m in mods:
            kb.press(m)
        try:
            kb.press("v")
            kb.release("v")
        finally:
            for m in reversed(mods):
                kb.release(m)


def _active_window_class() -> str:
    """Lowercased WM_CLASS (instance + class) of the X11 active window; '' if n/a."""
    try:
        from Xlib import X, display
        d = display.Display()
        try:
            root = d.screen().root
            na = d.intern_atom("_NET_ACTIVE_WINDOW")
            prop = root.get_full_property(na, X.AnyPropertyType)
            if not prop or not prop.value:
                return ""
            win = d.create_resource_object("window", prop.value[0])
            cls = win.get_wm_class()  # (instance, class) or None
            return " ".join(cls).lower() if cls else ""
        finally:
            d.close()
    except Exception:
        return ""
