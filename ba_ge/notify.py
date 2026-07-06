"""Desktop notifications — one backend per OS, same ``notify(title, message, ...)``.

Linux: ``notify-send``. macOS: ``osascript`` (best-effort; reliable only from a
signed .app — see PORTING.md). Windows: Windows-Toasts if available, else no-op.
The macOS/Windows paths are UNVERIFIED in the dev environment.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("bage.notify")

_APP_NAME = "Ba-Ge"
_win_toaster = None  # lazily created Windows toaster


def notify(title: str, message: str = "", urgency: str = "normal",
           icon: str | None = None, expire_ms: int = 2500) -> None:
    try:
        if sys.platform == "linux":
            _notify_linux(title, message, urgency, icon, expire_ms)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        elif sys.platform == "win32":
            _notify_windows(title, message)
    except Exception:
        log.debug("notification failed", exc_info=True)


def _notify_linux(title, message, urgency, icon, expire_ms):
    cmd = ["notify-send", "-a", _APP_NAME, "-u", urgency, "-t", str(expire_ms)]
    if icon:
        cmd += ["-i", icon]
    cmd += [title, message]
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def _notify_macos(title, message):
    text = message.replace('"', '\\"')
    ttl = title.replace('"', '\\"')
    script = f'display notification "{text}" with title "{ttl}"'
    subprocess.run(["osascript", "-e", script], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _notify_windows(title, message):
    global _win_toaster
    try:
        from windows_toasts import Toast, WindowsToaster
        if _win_toaster is None:
            _win_toaster = WindowsToaster(_APP_NAME)
        toast = Toast()
        toast.text_fields = [title, message]
        _win_toaster.show_toast(toast)
    except Exception:
        log.debug("windows toast unavailable", exc_info=True)
