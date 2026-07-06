"""Start-on-login — one backend per OS, same ``is_enabled`` / ``set_enabled``.

* Linux: XDG ``~/.config/autostart/ptt-dictation.desktop``.
* macOS: LaunchAgent plist in ``~/Library/LaunchAgents``.
* Windows: HKCU ``...\\Run`` registry value.

Only the Linux path is exercised in the dev environment; macOS/Windows are
UNVERIFIED (see docs/PORTING.md autostart TEST steps).
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---- Linux (XDG) ----

AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_PATH = AUTOSTART_DIR / "ptt-dictation.desktop"

_DESKTOP = """\
[Desktop Entry]
Type=Application
Name=PTT Dictation
Comment=Hold-to-talk voice dictation (ElevenLabs Scribe)
Exec={exec}
Icon=ptt-dictation
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def is_enabled(path: Path | None = None) -> bool:
    if sys.platform == "linux" or path is not None:
        return _linux_is_enabled(path)
    if sys.platform == "darwin":
        return _macos_is_enabled()
    if sys.platform == "win32":
        return _windows_is_enabled()
    return False


def set_enabled(enabled: bool, exec_cmd: str = "ptt-dictation",
                path: Path | None = None) -> None:
    if sys.platform == "linux" or path is not None:
        _linux_set_enabled(enabled, exec_cmd, path)
    elif sys.platform == "darwin":
        _macos_set_enabled(enabled, exec_cmd)
    elif sys.platform == "win32":
        _windows_set_enabled(enabled, exec_cmd)


def _linux_is_enabled(path: Path | None) -> bool:
    path = AUTOSTART_PATH if path is None else Path(path)
    if not path.exists():
        return False
    for line in path.read_text(errors="ignore").splitlines():
        if line.replace(" ", "").lower() == "x-gnome-autostart-enabled=false":
            return False
    return True


def _linux_set_enabled(enabled: bool, exec_cmd: str, path: Path | None) -> None:
    path = AUTOSTART_PATH if path is None else Path(path)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DESKTOP.format(exec=exec_cmd))
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ---- macOS (LaunchAgent) ----

_MAC_LABEL = "com.ptt-dictation.agent"
_MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""


def _macos_set_enabled(enabled: bool, exec_cmd: str) -> None:
    if enabled:
        _MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in exec_cmd.split())
        _MAC_PLIST.write_text(_PLIST.format(label=_MAC_LABEL, args=args))
    else:
        try:
            _MAC_PLIST.unlink()
        except FileNotFoundError:
            pass


def _macos_is_enabled() -> bool:
    return _MAC_PLIST.exists()


# ---- Windows (HKCU Run) ----

_WIN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_NAME = "PTTDictation"


def _windows_set_enabled(enabled: bool, exec_cmd: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _WIN_NAME, 0, winreg.REG_SZ, exec_cmd)
        else:
            try:
                winreg.DeleteValue(key, _WIN_NAME)
            except FileNotFoundError:
                pass


def _windows_is_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_KEY) as key:
            winreg.QueryValueEx(key, _WIN_NAME)
        return True
    except FileNotFoundError:
        return False
