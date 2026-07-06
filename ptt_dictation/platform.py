"""Per-OS backend factory — the ONLY module that inspects ``sys.platform``.

Everything else imports interfaces/behaviour from here so `app.py` stays free of
platform branches (grep-enforced; see docs/PORTING.md). Backends are added here as
they land; today Linux is fully wired and macOS/Windows are best-effort/unverified.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("ptt.platform")

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"


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

def missing_permissions() -> list[str]:
    """Names of TCC grants the app still needs (macOS only). Best-effort."""
    if not IS_MAC:
        return []
    missing = []
    try:
        from ApplicationServices import AXIsProcessTrusted
        if not AXIsProcessTrusted():
            missing.append("Accessibility")
    except Exception:
        log.debug("could not query Accessibility trust", exc_info=True)
    return missing


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
