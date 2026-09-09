"""Microphone capture via the `arecord` subprocess.

Recording is a child `arecord` process writing a WAV file. On stop we send
SIGINT so arecord finalizes the WAV header (it seeks back and patches the size
fields on a clean close), then read the bytes back. If arecord has to be killed
(SIGINT didn't drain in time) the header keeps its ~2GB placeholder size, so we
defensively recompute the RIFF/data sizes from the actual file length.
"""

from __future__ import annotations

import array
import io
import logging
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from typing import NamedTuple

log = logging.getLogger("bage.audio")

# Canonical PCM WAV header is 44 bytes; anything <= that contains no audio frames.
_WAV_HEADER_BYTES = 44
_BYTES_PER_SAMPLE = 2  # S16_LE
# |sample| at or above this counts as pinned to the rail (~-0.02 dBFS).
_CLIP_FLOOR = 32700
_SILENT_DBFS = -999.0
# Never open more than this many capture channels (mirrors the mac/win backend).
_MAX_CAPTURE_CHANNELS = 8


class AudioError(Exception):
    pass


# Devices that already follow the system default through PipeWire.
_FOLLOW_DEFAULT = ("", "default", "pipewire", "pulse")
# Raw ALSA device syntaxes — passed straight to arecord -D (non-PipeWire / advanced).
_RAW_ALSA_PREFIXES = ("hw:", "plughw:", "sysdefault", "dmix", "dsnoop", "plug:",
                      "front:", "iec958")


def _is_pulse_source(dev: str) -> bool:
    """A PipeWire/PulseAudio source *name* (e.g. alsa_input.usb-...), not raw ALSA."""
    return dev not in _FOLLOW_DEFAULT and not dev.startswith(_RAW_ALSA_PREFIXES)


def _alsa_device(dev: str) -> str:
    if dev in _FOLLOW_DEFAULT:
        return dev or "default"
    if dev.startswith(_RAW_ALSA_PREFIXES):
        return dev
    return "pulse"  # named PipeWire source -> route via the pulse plugin (+ PULSE_SOURCE)


def build_arecord_cmd(config, path: str, channels: int | None = None) -> list[str]:
    ch = config.channels if channels is None else channels
    return [
        "arecord", "-q",
        "-D", _alsa_device(config.audio_device),
        "-f", "S16_LE",
        "-r", str(config.sample_rate),
        "-c", str(ch),
        "-t", "wav",
        path,
    ]


def _source_channels(device: str) -> int | None:
    """How many channels the named PipeWire/Pulse source exposes, via `pactl`.

    None if it can't be determined — not a pulse source, `pactl` absent, or the
    source isn't listed. `pactl list short sources` prints tab-separated rows whose
    4th column is the sample spec, e.g. ``s24le 2ch 48000Hz``.
    """
    if not _is_pulse_source(device):
        return None
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 4 and cols[1] == device:
            m = re.search(r"(\d+)ch", cols[3])
            if m:
                return int(m.group(1))
    return None


def _capture_channels(config) -> int:
    """How many channels arecord should open.

    A multi-mic wireless receiver (e.g. a DJI dual kit) puts each transmitter on its
    OWN channel of a 2-channel source. Capturing a single channel silently drops
    whichever mic the user switched to — the app records that channel's near-silence
    and Scribe returns nothing. So for a PipeWire/Pulse source we open every channel
    it exposes (probed via pactl; defaulting to 2 when the probe can't run) and let
    `stop()` keep the loudest — mirroring the macOS/Windows backend. Raw ALSA /
    default devices keep the configured count (we can't assume they support more).
    """
    if not _is_pulse_source(config.audio_device):
        return config.channels
    detected = _source_channels(config.audio_device) or 2
    return max(config.channels, min(detected, _MAX_CAPTURE_CHANNELS))


def arecord_env(config, base=None) -> dict:
    """Environment for arecord; targets a specific PipeWire source via PULSE_SOURCE."""
    env = dict(os.environ if base is None else base)
    if _is_pulse_source(config.audio_device):
        env["PULSE_SOURCE"] = config.audio_device
    return env


def is_too_short(duration: float, min_duration: float) -> bool:
    return duration < min_duration


def _patch_wav_sizes(data: bytes) -> bytes:
    """Recompute RIFF + data chunk sizes from the actual byte length.

    Idempotent when the header is already correct; repairs an unfinalized
    (killed-arecord) header. Only touches a standard 44-byte PCM layout.
    """
    if len(data) < _WAV_HEADER_BYTES or data[36:40] != b"data":
        return data
    buf = bytearray(data)
    struct.pack_into("<I", buf, 4, len(buf) - 8)       # RIFF chunk size
    struct.pack_into("<I", buf, 40, len(buf) - 44)     # data chunk size
    return bytes(buf)


def _samples(wav: bytes) -> array.array:
    """The S16_LE PCM payload as native-order signed shorts (empty if none)."""
    if len(wav) <= _WAV_HEADER_BYTES:
        return array.array("h")
    pcm = wav[_WAV_HEADER_BYTES:]
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def peak_amplitude(wav: bytes) -> int:
    """Peak |sample| of the S16_LE PCM payload; 0 for silence/empty.

    Used to catch a muted mic or dead device: a silent clip would otherwise
    sail through to a 200 from Scribe with an empty transcript.
    """
    samples = _samples(wav)
    if not samples:
        return 0
    return max(-min(samples), max(samples))


class LevelStats(NamedTuple):
    peak: int          # 0..32768
    rms_dbfs: float    # -inf..0; loudness, not peak
    clipped_pct: float # % of samples pinned to the rails


def level_stats(wav: bytes) -> LevelStats:
    """Input level of a clip, for telling the user *why* a transcript is wrong.

    A silence check alone is not enough. Audio driven into the rails passes it
    happily — the signal is loud, just destroyed — and the model returns confident
    nonsense ("page 4" -> "H4"), which reads as a broken app rather than a mic
    turned up too far. Peak alone is not enough either: normal speech has 12-18 dB
    of crest, so an occasional plosive touching full scale is fine while a *sustained*
    pin is not. Hence the clipped fraction, and RMS for the quiet end.
    """
    samples = _samples(wav)
    if not samples:
        return LevelStats(0, _SILENT_DBFS, 0.0)
    peak = max(-min(samples), max(samples))
    total = 0
    clipped = 0
    for v in samples:
        total += v * v
        if v >= _CLIP_FLOOR or v <= -_CLIP_FLOOR:
            clipped += 1
    rms = math.sqrt(total / len(samples))
    dbfs = 20 * math.log10(rms / 32768) if rms > 0 else _SILENT_DBFS
    return LevelStats(peak, dbfs, 100.0 * clipped / len(samples))


def _wav_seconds(data: bytes, sample_rate: int, channels: int) -> float | None:
    """Audio duration derived from PCM byte count (immune to spawn latency)."""
    frame_bytes = sample_rate * channels * _BYTES_PER_SAMPLE
    if frame_bytes <= 0 or len(data) <= _WAV_HEADER_BYTES:
        return None
    return (len(data) - _WAV_HEADER_BYTES) / frame_bytes


def _wav_format(data: bytes) -> tuple[int, int] | None:
    """(channels, sample_rate) from a canonical PCM WAV header; None if unparseable."""
    if len(data) < _WAV_HEADER_BYTES or data[:4] != b"RIFF" or data[12:16] != b"fmt ":
        return None
    channels = struct.unpack_from("<H", data, 22)[0]
    sample_rate = struct.unpack_from("<I", data, 24)[0]
    if channels <= 0 or sample_rate <= 0:
        return None
    return channels, sample_rate


def _mono_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(_BYTES_PER_SAMPLE)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _downmix_loudest(data: bytes) -> tuple[bytes, int]:
    """Collapse a multi-channel recording to its single loudest channel.

    Returns ``(mono_wav, chosen_index)``; ``(data, -1)`` unchanged for mono or
    unparseable input. Loudest — NOT summed (a dual-mono device would clip) and NOT
    averaged (−6 dB when only one mic is live, which is exactly the case this exists
    for: a wireless receiver with one active transmitter). See `_capture_channels`.
    """
    fmt = _wav_format(data)
    if fmt is None:
        return data, -1
    channels, sample_rate = fmt
    if channels <= 1 or len(data) <= _WAV_HEADER_BYTES:
        return data, -1
    pcm = data[_WAV_HEADER_BYTES:]
    frame = _BYTES_PER_SAMPLE * channels
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // frame * frame])
    if sys.byteorder == "big":
        samples.byteswap()
    peaks = []
    for c in range(channels):
        chan = samples[c::channels]
        peaks.append(max(max(chan), -min(chan)) if chan else 0)
    best = peaks.index(max(peaks)) if peaks else 0
    mono = samples[best::channels]
    if sys.byteorder == "big":
        mono.byteswap()
    return _mono_wav(mono.tobytes(), sample_rate), best


class Recorder:
    def __init__(self, config):
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._path: str | None = None
        self._start = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        # Probe channels OUTSIDE the lock (a pactl call); a multi-mic receiver needs
        # every channel opened so stop() can keep the loudest.
        channels = _capture_channels(self.config)
        with self._lock:
            if self._proc is not None:
                return
            fd, path = tempfile.mkstemp(prefix="bage-", suffix=".wav")
            os.close(fd)
            self._path = path
            self._start = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    build_arecord_cmd(self.config, path, channels=channels),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=arecord_env(self.config),
                )
            except FileNotFoundError as exc:
                self._path = None
                self._unlink(path)
                raise AudioError("arecord not found — install alsa-utils.") from exc

    def stop(self) -> bytes | None:
        """Stop recording. Returns WAV bytes, or None if too short / empty."""
        with self._lock:
            if self._proc is None:
                return None
            proc, path = self._proc, self._path
            wall = time.monotonic() - self._start
            self._proc = None
            self._path = None

        clean = True
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            clean = False
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

        data = None
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            log.warning("could not read recording: %s", exc)
        finally:
            self._unlink(path)

        if not data or len(data) <= _WAV_HEADER_BYTES:
            return None
        if not clean:
            log.warning("arecord did not stop cleanly; repairing WAV header")
        data = _patch_wav_sizes(data)

        # A multi-mic receiver puts each transmitter on its own channel; keep the one
        # actually being spoken into (loudest) so switching mics doesn't yield silence.
        data, chosen = _downmix_loudest(data)
        if chosen >= 0:
            log.info("multi-channel input: kept loudest channel %d", chosen)

        fmt = _wav_format(data)
        final_channels = fmt[0] if fmt else 1
        seconds = _wav_seconds(data, self.config.sample_rate, final_channels)
        if seconds is None:
            seconds = wall
        if is_too_short(seconds, self.config.min_duration):
            log.info("discarded %.2fs tap (< %.2fs)", seconds, self.config.min_duration)
            return None
        return data

    def _unlink(self, path: str | None = None) -> None:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
