"""Global hold-to-talk hotkey on X11 via pynput.

Fires on_start on the first press of the target key and on_stop on its release.
X11 auto-repeat (synthetic release+press churn while held) is collapsed into a
single hold by HoldDebouncer, so on_start/on_stop each fire exactly once.
"""

from __future__ import annotations

from pynput import keyboard

from .debounce import HoldDebouncer

# String aliases -> pynput special keys. Function keys f1..f20 added below.
_SPECIAL = {
    "pause": keyboard.Key.pause,
    "scroll_lock": keyboard.Key.scroll_lock,
    "ctrl_r": keyboard.Key.ctrl_r,
    "ctrl_l": keyboard.Key.ctrl_l,
    "alt_r": keyboard.Key.alt_r,
    "alt_gr": keyboard.Key.alt_gr,
    "shift_r": keyboard.Key.shift_r,
    "super": keyboard.Key.cmd,
    "cmd": keyboard.Key.cmd,
    "menu": keyboard.Key.menu,
    "insert": keyboard.Key.insert,
    "home": keyboard.Key.home,
    "end": keyboard.Key.end,
}
for _n in range(1, 21):
    _fkey = getattr(keyboard.Key, f"f{_n}", None)
    if _fkey is not None:
        _SPECIAL[f"f{_n}"] = _fkey


def resolve_key(name: str):
    n = name.strip().lower()
    if n in _SPECIAL:
        return _SPECIAL[n]
    if len(n) == 1:
        return keyboard.KeyCode.from_char(n)
    raise ValueError(f"Unknown hotkey: {name!r}")


def _key_matches(key, target) -> bool:
    if key == target:
        return True
    if isinstance(target, keyboard.KeyCode) and isinstance(key, keyboard.KeyCode):
        # Case-insensitive so e.g. "k" still matches when Shift is held.
        if key.char is None or target.char is None:
            return False
        return key.char.lower() == target.char.lower()
    return False


class HotkeyListener:
    def __init__(self, key_name, on_start, on_stop):
        self._target = resolve_key(key_name)
        self._debounce = HoldDebouncer(on_start, on_stop)
        self._listener = None

    def _on_press(self, key):
        if _key_matches(key, self._target):
            self._debounce.press()

    def _on_release(self, key):
        if _key_matches(key, self._target):
            self._debounce.release()

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        self._debounce.cancel()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
