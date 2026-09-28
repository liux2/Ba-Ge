"""Global hold-to-talk hotkey on X11 via pynput.

Fires on_start on the first press of the target key and on_stop on its release.
X11 auto-repeat (synthetic release+press churn while held) is collapsed into a
single hold by HoldDebouncer, so on_start/on_stop each fire exactly once.
"""

from __future__ import annotations

import logging

from pynput import keyboard

from .debounce import HoldDebouncer

log = logging.getLogger("bage.hotkey")

# String aliases -> pynput special keys. Built defensively: pynput's per-OS
# backends don't all expose the same Key members (e.g. macOS lacks pause /
# scroll_lock / menu / insert), so a missing name is simply skipped rather than
# crashing at import. Function keys f1..f20 are added the same way.
_SPECIAL_NAMES = {
    "space": "space",
    "tab": "tab",
    "esc": "esc",
    "caps_lock": "caps_lock",
    "pause": "pause",
    "scroll_lock": "scroll_lock",
    "ctrl": "ctrl",
    "ctrl_l": "ctrl_l",
    "ctrl_r": "ctrl_r",
    "alt": "alt",
    "alt_l": "alt_l",
    "alt_r": "alt_r",
    "alt_gr": "alt_gr",
    "shift": "shift",
    "shift_l": "shift_l",
    "shift_r": "shift_r",
    "super": "cmd",
    "cmd": "cmd",
    "cmd_l": "cmd_l",
    "cmd_r": "cmd_r",
    "menu": "menu",
    "insert": "insert",
    "home": "home",
    "end": "end",
}
_SPECIAL = {}
for _alias, _member in _SPECIAL_NAMES.items():
    _key = getattr(keyboard.Key, _member, None)
    if _key is not None:
        _SPECIAL[_alias] = _key
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


def _vk_of(key):
    """Virtual keycode of a pynput Key/KeyCode, or None (e.g. from_char targets)."""
    return getattr(getattr(key, "value", key), "vk", None)


class HotkeyListener:
    def __init__(self, key_name, on_start, on_stop):
        self._target = resolve_key(key_name)
        self._debounce = HoldDebouncer(on_start, on_stop)
        self._listener = None
        self._vk = _vk_of(self._target)  # refined from the first real key event

    def _on_press(self, key):
        if _key_matches(key, self._target):
            vk = _vk_of(key)
            if vk is not None:
                self._vk = vk
            self._debounce.press()

    def _on_release(self, key):
        if _key_matches(key, self._target):
            self._debounce.release()

    def _on_tap_restored(self) -> None:
        """macOS re-enabled a disabled event tap; a key-up may have been lost.

        Without this a release that happened while the tap was off leaves the hold
        "in progress" forever: the recording never ends (mic lit) and every later
        press is swallowed as auto-repeat. Only acts on a definite "key is up".
        """
        if not self._debounce.held:
            return
        from . import platform
        if platform.key_is_down(self._vk) is False:
            log.warning("hotkey was released while the event tap was disabled — "
                        "ending the hold now")
            self._debounce.release()

    def start(self) -> None:
        # macOS: cache the keyboard layout on THIS (main) thread so pynput's
        # listener thread never calls the main-thread-only Text Input Source API
        # (restarting the listener mid-event-loop would otherwise SIGTRAP — see
        # platform.prewarm_macos_input_source). No-op off macOS.
        from . import platform
        platform.prewarm_macos_input_source()
        self._listener = platform.make_keyboard_listener(
            self._on_press, self._on_release, on_tap_restored=self._on_tap_restored)
        self._listener.start()

    def key_is_down(self) -> bool | None:
        """Whether the hotkey is physically held now (macOS); None if unknown."""
        from . import platform
        return platform.key_is_down(self._vk)

    def reset(self) -> None:
        """Forget a hold in progress (the app ended it without seeing the key-up)."""
        self._debounce.cancel()

    def stop(self) -> None:
        self._debounce.cancel()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
