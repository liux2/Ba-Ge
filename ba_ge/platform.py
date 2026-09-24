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


# --- bounded calls (audio backends can wedge; nothing may block forever) ---

def call_with_timeout(fn, timeout: float):
    """Run ``fn()`` on a daemon thread; return ``(finished, result)``.

    Used wherever a call can enter PortAudio/CoreAudio, which on macOS can deadlock
    outright (see ba_ge/audio_sd.py). A timed-out worker is abandoned, never joined:
    it is a daemon so it cannot hold the process open, and waiting on it would just
    relocate the hang. Exceptions are re-raised on the calling thread.
    """
    import threading

    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    worker = threading.Thread(target=run, name="bage-bounded-call", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


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


# --- self-relaunch (recovery from an unrecoverable backend state) ---

def _relaunch_record_path():
    from . import paths
    return paths.cache_dir() / "relaunch.json"


def last_relaunch() -> dict | None:
    """The most recent self-relaunch as {"time", "reason", "pid"}, or None."""
    import json

    try:
        data = json.loads(_relaunch_record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "time" in data else None
    except (OSError, ValueError):
        return None


def _record_relaunch(reason: str, pid: int) -> None:
    import json
    import time

    try:
        path = _relaunch_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"time": time.time(), "reason": reason, "pid": pid}),
                        encoding="utf-8")
    except OSError:
        log.debug("could not record relaunch", exc_info=True)


def _relaunch_argv() -> list[str]:
    """The command that started this process, runnable again from the same cwd.

    ``sys.argv`` is wrong for ``python -m ba_ge``: it holds the path of
    ``__main__.py``, and running that as a script fails on its relative import.
    ``sys.orig_argv`` keeps the interpreter options and ``-m ba_ge`` intact.
    """
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller/Nuitka binary outside a .app
        return [sys.executable] + sys.argv[1:]
    orig = list(getattr(sys, "orig_argv", None) or [])
    if orig:
        return [sys.executable] + orig[1:]
    return [sys.executable] + sys.argv


def relaunch_self(reason: str = "") -> None:
    """Quit and relaunch this app. Does not return.

    Used for states no in-process recovery can clear — new TCC grants, or a
    PortAudio/CoreAudio stop that deadlocked and poisoned this process's audio
    (every later open then fails with paInternalError; see ba_ge/audio_sd.py).

    The single-instance lock is held until THIS process dies, so the relaunch waits
    for our PID to exit before reopening — otherwise the new instance would hit the
    lock and immediately quit. Exits with ``os._exit``: normal interpreter teardown
    runs sounddevice's atexit Pa_Terminate, which hangs forever on a wedged stream.
    """
    import os
    import shlex
    import subprocess
    import sys

    pid = os.getpid()
    _record_relaunch(reason, pid)
    marker = ".app/Contents/MacOS/"
    try:
        if marker in sys.executable:
            cmd = ["open", "-n", sys.executable.split(marker)[0] + ".app"]
        else:
            cmd = _relaunch_argv()
        log.warning("relaunching (%s): %s", reason or "requested", shlex.join(cmd))
        if IS_WIN:
            subprocess.Popen(cmd)  # UNVERIFIED on Windows (no PID wait)
        else:
            script = (f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; "
                      f"exec {shlex.join(cmd)}")
            subprocess.Popen(["/bin/sh", "-c", script], stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:
        log.warning("relaunch failed", exc_info=True)
    finally:
        hard_exit(0)


def hard_exit(code: int = 0) -> None:
    """Exit immediately, skipping atexit handlers. Does not return.

    The atexit path is not safe once the audio layer has wedged: sounddevice's
    handler stops/closes the last stream and runs Pa_Terminate, which blocks
    forever on the wedged unit (that is what made quitting need SIGKILL). Logs are
    flushed first so the reason survives.
    """
    import logging
    import os

    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:
            pass
    os._exit(code)


def audio_needs_hard_exit() -> bool:
    """True if interpreter teardown could hang in PortAudio (see ``hard_exit``)."""
    import sys

    mod = sys.modules.get(f"{__package__}.audio_sd")  # never imported -> no PortAudio
    try:
        return bool(mod is not None and mod.needs_hard_exit())
    except Exception:
        return True


def arm_exit_watchdog(seconds: float) -> None:
    """Force the process out if normal teardown hasn't finished in ``seconds``."""
    import threading

    def fire():
        log.error("shutdown did not finish within %.0fs — forcing exit", seconds)
        hard_exit(0)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()


# --- hotkey listener (macOS: survive the OS disabling the event tap) ---

# Out-of-band CGEventTap "event types": macOS stops delivering to a tap whose
# thread was too slow, or on certain user input, and tells the tap once via its
# callback. pynput 1.8 ignores both and never re-enables the tap, so the hotkey
# goes dead for the rest of the process — and if it happened mid-hold, the key-up
# is lost and the app sits in RECORDING with the mic lit.
_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF
_darwin_listener_cls = None


def make_keyboard_listener(on_press, on_release, on_tap_restored=None):
    """A pynput keyboard listener; on macOS one that re-enables a disabled tap.

    ``on_tap_restored`` (macOS only) runs on the listener thread after macOS had
    disabled the tap and it was re-enabled — key events may have been lost while
    it was off, so the caller should reconcile its held-key state.
    """
    from pynput import keyboard

    if not IS_MAC:
        return keyboard.Listener(on_press=on_press, on_release=on_release)
    return _darwin_listener_class()(on_press=on_press, on_release=on_release,
                                    on_tap_restored=on_tap_restored)


def _darwin_listener_class():
    global _darwin_listener_cls
    if _darwin_listener_cls is None:
        from pynput import keyboard
        _darwin_listener_cls = _tap_guarded(keyboard.Listener)
    return _darwin_listener_cls


def _tap_guarded(base):
    """Subclass pynput's macOS keyboard listener to re-enable a disabled tap."""

    class TapGuardedListener(base):
        def __init__(self, *args, on_tap_restored=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._bage_tap = None
            self._bage_on_tap_restored = on_tap_restored

        def _create_event_tap(self):
            tap = super()._create_event_tap()
            self._bage_tap = tap
            return tap

        def _handler(self, proxy, event_type, event, refcon):
            # Checked before pynput touches ``event``: for these out-of-band types
            # it is not a keyboard event, and an exception inside pynput's handler
            # would stop the listener outright.
            if event_type in (_TAP_DISABLED_BY_TIMEOUT, _TAP_DISABLED_BY_USER_INPUT):
                _reenable_tap(self._bage_tap, event_type, self._bage_on_tap_restored)
                return event
            return super()._handler(proxy, event_type, event, refcon)

    return TapGuardedListener


def _reenable_tap(tap, event_type, on_restored) -> None:
    why = "timeout" if event_type == _TAP_DISABLED_BY_TIMEOUT else "user input"
    try:
        from Quartz import CGEventTapEnable

        if tap is None:
            log.error("macOS disabled the hotkey event tap (%s), and it could not be "
                      "re-enabled (no tap handle)", why)
            return
        CGEventTapEnable(tap, True)
        log.warning("macOS disabled the hotkey event tap (%s) — re-enabled it", why)
    except Exception:
        log.error("could not re-enable the hotkey event tap", exc_info=True)
        return
    if on_restored is not None:
        try:
            on_restored()
        except Exception:
            log.exception("hotkey tap-restored callback failed")


def key_is_down(vk) -> bool | None:
    """Whether the key with virtual keycode ``vk`` is physically held right now.

    macOS only (reads the HID system state, independent of event delivery), so it
    can tell whether a key-up was lost while the event tap was disabled. None when
    unknown: off macOS, no keycode, or the query failed.
    """
    if not IS_MAC or vk is None:
        return None
    try:
        import Quartz

        return bool(Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateHIDSystemState, int(vk)))
    except Exception:
        log.debug("CGEventSourceKeyState failed", exc_info=True)
        return None


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


def _refresh_portaudio(sd) -> None:
    """Rebuild PortAudio's device snapshot so newly attached mics appear.

    PortAudio enumerates devices once, inside ``Pa_Initialize`` — which sounddevice
    runs at import — and ``query_devices()`` only reads that snapshot. So a mic
    plugged in after the app started never shows up, no matter how often the list is
    re-queried; only a terminate/initialise cycle rebuilds it.

    Skipped while a capture stream is open or being opened, because
    ``Pa_Terminate`` would tear that stream down — and on macOS that is precisely
    the stop path that can deadlock (see ba_ge/audio_sd.py). Bounded for the same
    reason: this runs on the Qt main thread when Settings opens, so a wedge here
    would freeze the UI.
    """
    from . import audio_sd

    # Non-blocking: a hotkey press opening the mic on another thread holds this, and
    # waiting on it from the UI thread would freeze Settings behind a slow open.
    if not audio_sd._pa_lock.acquire(blocking=False):
        log.info("skipping mic re-enumeration: the microphone is being opened")
        return
    try:
        if audio_sd.capture_active():
            log.info("skipping mic re-enumeration: capture is active")
            return

        def cycle():
            sd._terminate()
            sd._initialize()

        finished, _ = call_with_timeout(cycle, 3.0)
        if not finished:
            log.error("PortAudio re-init stalled — mic list may be stale")
            audio_sd.mark_poisoned("PortAudio re-enumeration did not finish within 3s")
    finally:
        audio_sd._pa_lock.release()


def _sounddevice_inputs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        import sounddevice as sd
        _refresh_portaudio(sd)
        for dev in sd.query_devices():
            if dev.get("max_input_channels", 0) > 0:
                out.append((dev["name"], dev["name"]))
    except Exception as exc:
        log.info("could not enumerate sounddevice inputs: %s", exc)
    return out
