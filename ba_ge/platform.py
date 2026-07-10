"""Per-OS backend factory — the ONLY module that inspects ``sys.platform``.

Everything else imports interfaces/behaviour from here so `app.py` stays free of
platform branches (grep-enforced; see docs/PORTING.md). Backends are added here as
they land; today Linux is fully wired and macOS/Windows are best-effort/unverified.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("bage.platform")

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"


# --- Qt platform-plugin visibility (macOS hidden-flag workaround) ---

def ensure_qt_plugins() -> None:
    """Make Qt's platform plugin discoverable on macOS. No-op on other OSes.

    PySide6's macOS wheel ships its plugin directories AND the plugin dylibs with
    the ``UF_HIDDEN`` BSD file flag set. Qt enumerates plugin dirs with
    ``getattrlistbulk``, which skips hidden entries, so ``QDir::entryList`` comes
    back empty, the plugin factory finds no ``cocoa`` platform plugin, and the app
    aborts at startup ("Could not find the Qt platform plugin ... in ''") — even
    though the dylibs load fine by explicit path. Clearing the hidden flag in place
    makes Qt find them (verified on 6.8.1, arm64). Must run BEFORE any
    ``QApplication`` is constructed.

    No-op when the platform dir already enumerates (e.g. a packaged app whose plugin
    files aren't hidden), and best-effort — any failure (e.g. a read-only install)
    is swallowed so this never blocks startup on its own.
    """
    if not IS_MAC:
        return
    try:
        import os

        from PySide6.QtCore import QDir, QLibraryInfo

        plugins = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
        platforms = os.path.join(plugins, "platforms")
        if QDir(platforms).entryList(QDir.Filter.Files):
            return  # Qt can already enumerate the plugins — nothing to do.
        if not os.path.isdir(platforms) or not os.listdir(platforms):
            return  # dir genuinely empty/absent (not the hidden-flag case).

        _clear_hidden_flag(plugins)
        log.info("cleared UF_HIDDEN on Qt plugins under %s", plugins)
    except Exception:
        log.debug("ensure_qt_plugins failed", exc_info=True)


def _clear_hidden_flag(root: str) -> None:
    """Recursively clear the macOS ``UF_HIDDEN`` BSD flag on ``root`` and every
    directory AND file under it, so Qt's ``getattrlistbulk`` enumeration (which
    skips hidden entries) can see the plugin dirs and dylibs."""
    import os
    import stat

    def unhide(path: str) -> None:
        try:
            flags = os.stat(path, follow_symlinks=False).st_flags
            if flags & stat.UF_HIDDEN:
                os.chflags(path, flags & ~stat.UF_HIDDEN, follow_symlinks=False)
        except OSError:
            pass

    unhide(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            unhide(os.path.join(dirpath, name))


# --- audio + injection backends ---

def make_recorder(config):
    if IS_LINUX:
        from .audio import Recorder
        return Recorder(config)
    from .audio_sd import SdRecorder
    return SdRecorder(config)


def make_injector(config):
    if IS_LINUX:
        from .inject import Injector
        return Injector(config)
    from .inject_pynput import PynputInjector
    return PynputInjector(config)


# --- ffmpeg (system on Linux; bundled/imageio elsewhere) ---

def ffmpeg_exe() -> str:
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# --- permissions (macOS TCC; no-op elsewhere) ---

# The three grants Ba-Ge needs, in the order the user should tackle them (hotkey
# first, then paste, then mic). Each maps to its System Settings › Privacy pane id.
REQUIRED_PERMISSIONS = ("Input Monitoring", "Accessibility", "Microphone")
_PRIVACY_PANES = {
    "Input Monitoring": "Privacy_ListenEvent",
    "Accessibility": "Privacy_Accessibility",
    "Microphone": "Privacy_Microphone",
}
_AV_MEDIA_AUDIO = b"soun"  # AVMediaTypeAudio


def permission_status() -> dict[str, str]:
    """{grant_name: 'granted'|'denied'|'unknown'} for every required grant (macOS).

    ``unknown`` means "not decided yet" — the OS will show a prompt when asked
    (see :func:`request_permission`); ``denied`` means the user must toggle it in
    System Settings. Empty dict off macOS. Best-effort: probe failures read
    ``unknown`` rather than raising.
    """
    if not IS_MAC:
        return {}
    return {
        "Input Monitoring": _input_monitoring_status(),
        "Accessibility": _accessibility_status(),
        "Microphone": _microphone_status(),
    }


def missing_permissions() -> list[str]:
    """Required grants that are not yet ``granted`` (macOS only)."""
    return [name for name, st in permission_status().items() if st != "granted"]


def _input_monitoring_status() -> str:
    # IOKit IOHIDCheckAccess(kIOHIDRequestTypeListenEvent=1) -> 0 granted/1 denied/2 unknown
    try:
        import ctypes

        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        return {0: "granted", 1: "denied", 2: "unknown"}.get(
            iokit.IOHIDCheckAccess(1), "unknown")
    except Exception:
        log.debug("IOHIDCheckAccess failed", exc_info=True)
        return "unknown"


def _accessibility_status() -> str:
    try:
        from ApplicationServices import AXIsProcessTrusted
        return "granted" if AXIsProcessTrusted() else "denied"
    except Exception:
        log.debug("AXIsProcessTrusted failed", exc_info=True)
        return "unknown"


def _microphone_status() -> str:
    # AVCaptureDevice authorizationStatusForMediaType: 0 notDetermined/1 restricted/
    # 2 denied/3 authorized. Reached via the Obj-C runtime (no AVFoundation wheel).
    try:
        import ctypes
        import objc

        ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        av = objc.lookUpClass("AVCaptureDevice")
        return {0: "unknown", 1: "denied", 2: "denied", 3: "granted"}.get(
            av.authorizationStatusForMediaType_("soun"), "unknown")
    except Exception:
        log.debug("mic authorization status failed", exc_info=True)
        return "unknown"


def request_permission(name: str) -> None:
    """Nudge the user toward granting ``name`` (macOS). If the grant is still
    undecided, show the OS prompt; otherwise open its System Settings pane so the
    user can toggle it. Requesting Microphone also registers Ba-Ge in the mic list.
    Safe to call from the main thread; no-op off macOS."""
    if not IS_MAC:
        return
    status = permission_status().get(name, "unknown")
    try:
        if name == "Accessibility":
            # Apple's own dialog with an "Open System Settings" button.
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            return
        if name == "Input Monitoring" and status == "unknown":
            import ctypes
            iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            iokit.IOHIDRequestAccess.restype = ctypes.c_bool
            iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
            iokit.IOHIDRequestAccess(1)
            return
        if name == "Microphone" and status == "unknown":
            import ctypes
            import objc
            ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
            av = objc.lookUpClass("AVCaptureDevice")
            av.requestAccessForMediaType_completionHandler_("soun", lambda _granted: None)
            return
    except Exception:
        log.debug("request_permission(%s) prompt failed; opening pane", name,
                  exc_info=True)
    open_privacy_pane(name)


def open_privacy_pane(name: str) -> None:
    """Open the System Settings › Privacy pane for ``name`` (macOS; no-op else)."""
    if not IS_MAC:
        return
    pane = _PRIVACY_PANES.get(name)
    if not pane:
        return
    try:
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
            check=False, timeout=5)
    except Exception:
        log.debug("could not open privacy pane for %s", name, exc_info=True)


# --- macOS: keep pynput's Text Input Source calls on the main thread ---

_macos_input_source_cached = False


def prewarm_macos_input_source() -> None:
    """Stop pynput from calling a main-thread-only macOS API off the main thread.

    pynput's keyboard ``Listener._run()`` runs on a background thread and, before
    setting up its event tap, does ``with keycode_context()`` — which calls
    ``TISCopyCurrentKeyboardInputSource`` / ``TISGetInputSourceProperty``. macOS 14+
    restrict those Text Input Source APIs to the main thread and abort the process
    with SIGTRAP (``dispatch_assert_queue``) when they run off it, once HIToolbox's
    TSM has been claimed by the main thread (i.e. after a window has been shown).
    So restarting the hotkey listener while the Qt event loop is running — e.g. on
    Settings *Save* — crashes the whole app.

    Fix: build pynput's keycode context ONCE here and rebind ``keycode_context`` to
    reuse it, so the background thread never touches TIS. The cached value is
    ``(keyboard_type: int, layout_data: bytes)`` — plain data with no live
    CoreFoundation handles (``keycode_context`` copies the layout to ``bytes``
    before yielding), so sharing it across threads is safe.

    MUST be called on the main thread; no-op off macOS and after the first call.
    """
    global _macos_input_source_cached
    if _macos_input_source_cached or not IS_MAC:
        return
    _macos_input_source_cached = True  # even on failure: don't re-call TIS repeatedly
    try:
        import contextlib

        from pynput._util import darwin as _pd

        with _pd.keycode_context() as ctx:  # the real TIS call — on the main thread
            cached = ctx

        @contextlib.contextmanager
        def _cached_keycode_context(_ctx=cached):
            yield _ctx

        _pd.keycode_context = _cached_keycode_context
        # keyboard/_darwin.py imported the name directly, so rebind it there too.
        from pynput.keyboard import _darwin as _kd
        _kd.keycode_context = _cached_keycode_context
    except Exception:
        log.warning("could not pre-cache macOS keyboard layout; hotkey restart may "
                    "be unstable", exc_info=True)


# --- microphone enumeration (matched to the active audio backend) ---

def list_input_devices() -> list[tuple[str, str]]:
    """[(device_id, label)] for the settings mic dropdown."""
    devices = [("default", "System default")]
    if IS_LINUX:
        devices += _pactl_sources()
    else:
        devices += _sounddevice_inputs()
    return devices


def _pactl_sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        desc = _source_descriptions()
        res = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in res.splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and not cols[1].endswith(".monitor"):
                out.append((cols[1], desc.get(cols[1], cols[1])))
    except Exception as exc:
        log.info("could not list PulseAudio sources: %s", exc)
    return out


def _source_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        res = subprocess.run(["pactl", "list", "sources"], capture_output=True,
                             text=True, timeout=3).stdout
        name = None
        for line in res.splitlines():
            t = line.strip()
            if t.startswith("Name:"):
                name = t[len("Name:"):].strip()
            elif t.startswith("Description:") and name:
                out[name] = t[len("Description:"):].strip()
    except Exception:
        pass
    return out


def _sounddevice_inputs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        import sounddevice as sd
        for dev in sd.query_devices():
            if dev.get("max_input_channels", 0) > 0:
                out.append((dev["name"], dev["name"]))
    except Exception as exc:
        log.info("could not enumerate sounddevice inputs: %s", exc)
    return out
