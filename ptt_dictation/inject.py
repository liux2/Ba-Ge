"""Insert text at the cursor.

Backends (config `inject.backend`):
  * paste   — set the clipboard and send Paste (Ctrl+V / Ctrl+Shift+V in terminals).
              Inserts the whole string atomically, so it CANNOT drop characters —
              the most reliable option. Needs xclip + xdotool (X11).
  * xdotool — XTEST keystroke synthesis (X11). Reliable-ish but, like any
              per-keystroke method, can drop keys on some apps.
  * ydotool — uinput keystroke synthesis. Works on Wayland too; daemonless it
              drops fast keystrokes unless the delay is high.
  * auto    — xdotool on X11, else ydotool. (Paste is opt-in; it touches the
              clipboard and the terminal paste shortcut differs.)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("ptt.inject")

# WM_CLASS substrings for terminals that paste with Ctrl+Shift+V (not Ctrl+V).
_TERMINAL_HINTS = ("terminal", "konsole", "xterm", "alacritty", "kitty",
                   "terminator", "tilix", "rxvt", "st-256color", "wezterm",
                   "foot", "ptyxis", "ghostty", "warp", "hyper", "tabby",
                   "urxvt", "termite", "sakura", "roxterm", "contour", "wave")


class InjectionError(Exception):
    pass


def _ydotool_env() -> dict:
    env = os.environ.copy()
    if "YDOTOOL_SOCKET" not in env:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        env["YDOTOOL_SOCKET"] = f"{runtime}/.ydotool_socket"
    return env


def resolve_backend(pref: str) -> str:
    if pref in ("xdotool", "ydotool", "paste"):
        return pref
    # auto: xdotool (XTEST) on X11 is reliable; else ydotool (uinput).
    wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    if not wayland and shutil.which("xdotool"):
        return "xdotool"
    return "ydotool"


class Injector:
    def __init__(self, config, runner=subprocess.run):
        self.key_delay_ms = config.key_delay_ms
        self.socket = getattr(config, "ydotool_socket", "")
        self._runner = runner
        self.backend = resolve_backend(getattr(config, "inject_backend", "auto"))
        self._warned_degraded = False
        self._paste_restore = True        # put the previous clipboard back after pasting
        self._paste_restore_delay = 1.0   # generous — runs on a worker thread, no UX cost
        self._paste_verify_timeout = 2.0  # poll until the clipboard actually holds our text

    def type_text(self, text: str) -> None:
        if not text:
            return
        if self.backend == "paste":
            self._type_paste(text)
        elif self.backend == "xdotool":
            self._type_xdotool(text)
        else:
            self._type_ydotool(text)

    # ---- helpers ----

    def _run(self, cmd, text=None, env=None, timeout=30, capture=True):
        # capture=False (DEVNULL) is required for processes that fork and keep
        # running (xclip holds the X selection); a captured PIPE would make
        # subprocess.run block forever waiting for the inherited fd to close.
        if capture:
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        else:
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        kwargs["timeout"] = timeout
        if text is not None:
            kwargs["input"] = text if isinstance(text, bytes) else text.encode("utf-8")
        if env is not None:
            kwargs["env"] = env
        try:
            return self._runner(cmd, **kwargs)
        except FileNotFoundError as exc:
            raise InjectionError(f"{cmd[0]} not found.") from exc
        except subprocess.TimeoutExpired as exc:
            raise InjectionError(f"{cmd[0]} timed out after {timeout}s.") from exc

    @staticmethod
    def _rc(result) -> int:
        return getattr(result, "returncode", 1)

    @staticmethod
    def _err(result) -> str:
        return (getattr(result, "stderr", b"") or b"").decode("utf-8", "replace").strip()

    # ---- paste backend ----

    def _paste_key(self) -> str:
        cls = self._active_window_class()
        return "ctrl+shift+v" if any(h in cls for h in _TERMINAL_HINTS) else "ctrl+v"

    def _active_window_class(self) -> str:
        """Lowercased WM_CLASS of the active window; '' if it can't be read.

        xdotool has no working class getter here, so grab the window id with
        xdotool and read WM_CLASS with xprop (both instance and class parts).
        """
        try:
            wid = self._run(["xdotool", "getactivewindow"])
            wid_s = (getattr(wid, "stdout", b"") or b"").decode("utf-8", "replace").strip()
            if not wid_s:
                return ""
            r = self._run(["xprop", "-id", wid_s, "WM_CLASS"])
            return (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace").lower()
        except Exception:
            return ""

    def _type_paste(self, text: str) -> None:
        data = text.encode("utf-8") if isinstance(text, str) else text
        saved = self._run(["xclip", "-selection", "clipboard", "-o"], timeout=5)
        saved_data = getattr(saved, "stdout", None) if self._rc(saved) == 0 else None

        # capture=False so the forking xclip can't block us (the "stuck" bug)
        self._run(["xclip", "-selection", "clipboard", "-i"], data, timeout=5, capture=False)
        # Deterministically wait until the clipboard actually serves our text before
        # pasting. A fixed sleep races xclip taking X selection ownership — the cause
        # of stale/empty pastes. If it never confirms, type the text instead.
        if not self._wait_clipboard(data):
            log.warning("clipboard did not confirm our text; falling back to xdotool type")
            self._type_xdotool(text)
            return

        key = self._paste_key()
        res = self._run(["xdotool", "key", "--clearmodifiers", key], timeout=10)
        if self._rc(res) != 0:
            raise InjectionError(f"paste (xdotool key {key}) failed: {self._err(res)}")

        if self._paste_restore and saved_data is not None:
            # Let the target read the selection before we restore the old clipboard.
            # This runs on the worker thread after the paste, so it costs no latency.
            time.sleep(self._paste_restore_delay)
            self._run(["xclip", "-selection", "clipboard", "-i"], saved_data,
                      timeout=5, capture=False)

    def _wait_clipboard(self, expected: bytes, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout or self._paste_verify_timeout)
        while time.monotonic() < deadline:
            r = self._run(["xclip", "-selection", "clipboard", "-o"], timeout=5)
            if self._rc(r) == 0 and (getattr(r, "stdout", b"") or b"") == expected:
                return True
            time.sleep(0.02)
        return False

    # ---- keystroke backends ----

    def _type_xdotool(self, text: str) -> None:
        cmd = ["xdotool", "type", "--clearmodifiers",
               "--delay", str(self.key_delay_ms), "--file", "/dev/stdin"]
        res = self._run(cmd, text)
        if self._rc(res) != 0:
            raise InjectionError(f"xdotool failed (exit {self._rc(res)}). {self._err(res)}")

    def _type_ydotool(self, text: str) -> None:
        env = _ydotool_env()
        if self.socket:
            env["YDOTOOL_SOCKET"] = self.socket
        cmd = ["ydotool", "type", "--key-delay", str(self.key_delay_ms), "--file", "-"]
        res = self._run(cmd, text, env=env)
        stderr = self._err(res)
        if self._rc(res) != 0:
            raise InjectionError(
                f"ydotool failed (exit {self._rc(res)}). {stderr} "
                "Is /dev/uinput accessible (ACL / input group)?")
        if not self._warned_degraded and "ydotoold backend unavailable" in stderr:
            self._warned_degraded = True
            log.warning("ydotoold not running — typing via /dev/uinput directly, which "
                        "can drop keystrokes. Use backend=paste or xdotool for reliability.")
