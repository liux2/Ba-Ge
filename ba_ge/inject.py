"""Insert text at the cursor by pasting — fast and atomic.

Ba-Ge sets the clipboard (via the bundled Qt clipboard manager) and sends the
paste shortcut (Ctrl+V, or Ctrl+Shift+V in terminals). The keystroke is sent via
**uinput** (evdev): kernel-level events that behave like real hardware, so GTK
terminals such as Ghostty honour their paste *keybind* for them. Synthetic X
(XTEST/pynput) events do NOT trigger those keybinds — hence uinput.

`/dev/uinput` is reachable without root via the systemd-logind `uaccess` ACL (no
input-group membership needed). If uinput is unavailable we fall back to
pynput/XTEST, which still works for ordinary GUI apps.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("bage.inject")

# WM_CLASS substrings for terminals that paste with Ctrl+Shift+V (not Ctrl+V).
_TERMINAL_HINTS = ("terminal", "konsole", "xterm", "alacritty", "kitty",
                   "terminator", "tilix", "rxvt", "st-256color", "wezterm",
                   "foot", "ptyxis", "ghostty", "warp", "hyper", "tabby",
                   "urxvt", "termite", "sakura", "roxterm", "contour", "wave")

_REGISTER_DELAY = 0.4   # one-time: let the compositor notice the new uinput device
_KEY_HOLD = 0.02        # hold V briefly so the keypress registers
_MASK_SHIFT = 1         # X11 modifier bits (from query_pointer().mask)
_MASK_CTRL = 4
_MOD_POLL_TIMEOUT = 0.4 # max wait for the modifier chord to actually register before V
_TYPE_GAP = 0.010       # per-character gap when typing terminals (reliability > speed)


class InjectionError(Exception):
    pass


def resolve_backend(pref: str) -> str:
    return "paste"


# ---- uinput keystroke (persistent virtual keyboard) ----

_uinput_dev = None
_uinput_failed = False
_KEYMAP = None


def _keymap() -> dict:
    """char -> (evdev keycode, needs_shift) for printable US-ASCII. Cached."""
    global _KEYMAP
    if _KEYMAP is not None:
        return _KEYMAP
    import string
    from evdev import ecodes as e
    m: dict = {}
    for c in string.ascii_lowercase:
        m[c] = (getattr(e, "KEY_" + c.upper()), False)
    for c in string.ascii_uppercase:
        m[c] = (getattr(e, "KEY_" + c), True)
    for i, c in enumerate("0123456789"):
        m[c] = (getattr(e, "KEY_" + c), False)
        m[")!@#$%^&*("[i]] = (getattr(e, "KEY_" + c), True)
    for ch, pair in {
        " ": (e.KEY_SPACE, False), "\t": (e.KEY_TAB, False), "\n": (e.KEY_ENTER, False),
        "-": (e.KEY_MINUS, False), "_": (e.KEY_MINUS, True),
        "=": (e.KEY_EQUAL, False), "+": (e.KEY_EQUAL, True),
        "[": (e.KEY_LEFTBRACE, False), "{": (e.KEY_LEFTBRACE, True),
        "]": (e.KEY_RIGHTBRACE, False), "}": (e.KEY_RIGHTBRACE, True),
        "\\": (e.KEY_BACKSLASH, False), "|": (e.KEY_BACKSLASH, True),
        ";": (e.KEY_SEMICOLON, False), ":": (e.KEY_SEMICOLON, True),
        "'": (e.KEY_APOSTROPHE, False), '"': (e.KEY_APOSTROPHE, True),
        "`": (e.KEY_GRAVE, False), "~": (e.KEY_GRAVE, True),
        ",": (e.KEY_COMMA, False), "<": (e.KEY_COMMA, True),
        ".": (e.KEY_DOT, False), ">": (e.KEY_DOT, True),
        "/": (e.KEY_SLASH, False), "?": (e.KEY_SLASH, True),
    }.items():
        m[ch] = pair
    _KEYMAP = m
    return m


def _can_type(text: str) -> bool:
    """True if every character maps to a key (ASCII); CJK etc. must be pasted."""
    km = _keymap()
    return all(c in km for c in text)


def _get_uinput():
    """Lazily create ONE persistent uinput device; None if unavailable."""
    global _uinput_dev, _uinput_failed
    if _uinput_dev is not None:
        return _uinput_dev
    if _uinput_failed:
        return None
    try:
        from evdev import UInput, ecodes as e
        keys = sorted({code for code, _ in _keymap().values()}
                      | {e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V})
        dev = UInput({e.EV_KEY: keys}, name="ba-ge-virtual-kbd")
        time.sleep(_REGISTER_DELAY)  # once, so the first keystroke isn't dropped
        _uinput_dev = dev
        log.info("uinput keyboard device ready")
        return dev
    except Exception:
        _uinput_failed = True
        log.warning("uinput unavailable; falling back to XTEST (paste keybinds may "
                    "not fire in GTK terminals). Check /dev/uinput access.",
                    exc_info=True)
        return None


def ensure_device() -> None:
    """Pre-create the uinput device at startup so the first paste is instant."""
    _get_uinput()


def close_device() -> None:
    global _uinput_dev
    if _uinput_dev is not None:
        try:
            _uinput_dev.close()
        except Exception:
            pass
        _uinput_dev = None


def _wait_mods(want_bits: int, timeout: float = _MOD_POLL_TIMEOUT) -> bool:
    """Poll the X server until the Ctrl/Shift modifier state equals want_bits.

    Deterministic replacement for a fixed sleep: uinput modifier events take a
    variable moment to land in the input state (worse under load), and pressing V
    before they register makes GTK terminals miss the paste keybind (V leaks through
    as a CSI-u key sequence). We wait until the server actually reports the chord.
    """
    try:
        from Xlib import display
        d = display.Display()
        try:
            root = d.screen().root
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if (root.query_pointer().mask & (_MASK_CTRL | _MASK_SHIFT)) == want_bits:
                    return True
                time.sleep(0.004)
        finally:
            d.close()
    except Exception:
        time.sleep(0.03)  # fallback if X can't be queried
    return False


def _send_via_uinput(terminal: bool) -> bool:
    from evdev import ecodes as e
    dev = _get_uinput()
    if dev is None:
        return False
    mods = [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT] if terminal else [e.KEY_LEFTCTRL]
    want = _MASK_CTRL | (_MASK_SHIFT if terminal else 0)

    def ev(code, value):
        dev.write(e.EV_KEY, code, value)
        dev.syn()

    for m in mods:        # press Ctrl (, Shift)
        ev(m, 1)
    _wait_mods(want)      # block until the server confirms the chord is held
    ev(e.KEY_V, 1)        # V — modifiers are now guaranteed active
    time.sleep(_KEY_HOLD)
    ev(e.KEY_V, 0)
    for m in reversed(mods):  # release Shift, then Ctrl
        ev(m, 0)
    _wait_mods(0)         # ensure nothing is left stuck (would corrupt later input)
    return True


def _send_via_pynput(terminal: bool) -> None:
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


def _type_via_uinput(text: str) -> bool:
    """Type `text` character-by-character via uinput. False if the device is n/a.

    Characters reach the app's input directly (no clipboard, no paste keybind), so
    this is reliable in GTK terminals where the Ctrl+Shift+V keybind is flaky.
    """
    from evdev import ecodes as e
    dev = _get_uinput()
    if dev is None:
        return False
    km = _keymap()

    def ev(code, value):
        dev.write(e.EV_KEY, code, value)
        dev.syn()

    for ch in text:
        entry = km.get(ch)
        if entry is None:
            return False  # non-typeable char (shouldn't happen; caller checked)
        code, shift = entry
        if shift:
            ev(e.KEY_LEFTSHIFT, 1)
        ev(code, 1)
        ev(code, 0)
        if shift:
            ev(e.KEY_LEFTSHIFT, 0)
        time.sleep(_TYPE_GAP)
    return True


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
        terminal = self._active_is_terminal()
        # Terminals: TYPE the text — characters reach the PTY reliably, whereas the
        # Ctrl+Shift+V paste keybind is flaky in GTK terminals (Ghostty). GUI apps
        # and non-typeable text (CJK — Scribe is bilingual) PASTE (fast, atomic).
        if terminal and _can_type(text) and _type_via_uinput(text):
            return
        if self._clipboard is None:
            raise InjectionError("clipboard manager unavailable — cannot paste")
        self._clipboard.paste_text(text, lambda: self._send_paste_key(terminal))

    # ---- helpers ----

    def _active_is_terminal(self) -> bool:
        return any(h in _active_window_class() for h in _TERMINAL_HINTS)

    @staticmethod
    def _send_paste_key(terminal: bool) -> None:
        if not _send_via_uinput(terminal):  # kernel-level: works in GTK terminals
            _send_via_pynput(terminal)      # fallback: GUI apps only


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
