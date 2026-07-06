"""sounddevice audio backend (macOS/Windows).

Records raw S16_LE frames via PortAudio and serializes them to the SAME canonical
16-bit mono WAV bytes the ``arecord`` backend produces, so downstream code
(``peak_amplitude``, ``_wav_seconds``, ``transcribe``) is untouched.

UNVERIFIED on macOS/Windows — see docs/PORTING.md (mic TCC / privacy toggle both
yield silence with no error; ``peak_amplitude`` is the guard). No numpy dependency
(uses ``RawInputStream`` byte buffers).
"""

from __future__ import annotations

import io
import logging
import time
import wave

from .audio import _WAV_HEADER_BYTES, _wav_seconds, is_too_short

log = logging.getLogger("ptt.audio_sd")


class AudioError(Exception):
    pass


def _to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)  # S16_LE
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class SdRecorder:
    def __init__(self, config):
        self.config = config
        self._stream = None
        self._chunks: list[bytes] = []
        self._start = 0.0

    def _resolve_device(self):
        dev = self.config.audio_device
        return None if dev in ("", "default", "pipewire", "pulse") else dev

    def start(self) -> None:
        import sounddevice as sd

        self._chunks = []
        self._start = time.monotonic()

        def callback(indata, frames, time_info, status):
            self._chunks.append(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                device=self._resolve_device(),
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioError(f"could not open microphone: {exc}") from exc

    def stop(self) -> bytes | None:
        if self._stream is None:
            return None
        wall = time.monotonic() - self._start
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

        pcm = b"".join(self._chunks)
        self._chunks = []
        if not pcm:
            return None
        data = _to_wav(pcm, self.config.sample_rate, self.config.channels)
        if len(data) <= _WAV_HEADER_BYTES:
            return None
        seconds = _wav_seconds(data, self.config.sample_rate, self.config.channels) or wall
        if is_too_short(seconds, self.config.min_duration):
            return None
        return data
