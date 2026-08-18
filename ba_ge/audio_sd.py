"""sounddevice audio backend (macOS/Windows).

Records raw S16_LE frames via PortAudio and serializes them to the SAME canonical
16-bit mono WAV bytes the ``arecord`` backend produces, so downstream code
(``peak_amplitude``, ``_wav_seconds``, ``transcribe``) is untouched.

Calling ``stream.stop()`` intermittently wedges the whole app on macOS (observed
twice in ~6h of real use), because PortAudio's CoreAudio backend inverts two locks:

* the stopping thread holds the AudioUnit lock inside ``Pa_StopStream`` ->
  ``AudioOutputUnitStop`` and waits on the CoreAudio IO-context mutex
  (``HALC_ProxyIOContext::StopIOProc`` -> ``HALB_Mutex::Lock``);
* the CoreAudio IO thread holds that IO-context mutex while running PortAudio's
  ``startStopCallback``, which calls ``AudioUnitGetProperty`` and waits on the
  AudioUnit lock.

Neither side can progress, and the recorder thread parks forever inside
``recorder.stop()`` — the app's state watchdog resets the indicator to idle, so the
tray looks healthy while dictation is permanently dead and only SIGKILL clears it.
Holding one stream open for the whole process would remove that window entirely,
and this module used to do exactly that. It no longer does:

**The microphone is released as soon as a recording ends** (``config.idle_release``
= 0, the default), so the OS indicator is lit only while you are actually dictating.
Holding the stream open between utterances would dodge the deadlock completely, but
an always-live mic reads as surveillance and is not an acceptable price for it.

So the stop still happens per utterance, and the deadlock is still *possible*. What
changed is that it is no longer fatal:

* ``_close_stream`` runs the stop/close on a daemon thread bounded by
  ``_CLOSE_TIMEOUT`` and abandons it if it wedges, so nothing ever blocks forever —
  and because the audio is already copied out of the buffer before the close, a
  deadlock does not even cost you the utterance in progress.
* A wedged close poisons this process's CoreAudio: every later open fails with
  ``paInternalError``. That is unrecoverable in-process, so ``stalled`` is set and
  ``app.py`` relaunches (see ``platform.relaunch_self``) rather than leaving a
  half-dead app behind.

``config.idle_release`` > 0 keeps the stream alive for that many idle seconds as a
grace period, trading indicator time for fewer stops. Set it only if the deadlock
actually bites you in practice.

Silence still has to be detected by signal, not by exception — see
``peak_amplitude`` and docs/PORTING.md (mic TCC / the Windows privacy toggle both
yield silence with no error). No numpy dependency (uses ``RawInputStream`` byte
buffers).
"""

from __future__ import annotations

import array
import io
import logging
import threading
import time
import wave

from .audio import _WAV_HEADER_BYTES, _wav_seconds, is_too_short

log = logging.getLogger("bage.audio_sd")

# How long to wait for a PortAudio stop/close before abandoning it. Hit once per
# utterance now that the mic is released eagerly; see the module docstring.
_CLOSE_TIMEOUT = 2.0

# Multi-input devices put the mic on whichever channel they feel like: a 2-channel
# wireless receiver can move it between slots when a transmitter is re-paired, and
# capturing mono then silently grabs channel 1 and yields DIGITAL SILENCE while the
# OS meter (which watches every channel) looks perfectly healthy. So capture every
# channel the device offers and pick the live one. Capped so a pro interface with
# dozens of inputs doesn't make us haul useless audio around.
_MAX_CAPTURE_CHANNELS = 8


class AudioError(Exception):
    pass


# Number of capture streams this process currently holds open. Enumerating devices
# re-initialises PortAudio, which would tear an open stream down mid-recording, so
# `platform` consults this first.
_open_streams = 0
_open_lock = threading.Lock()


def capture_active() -> bool:
    """True while a capture stream is open (a recording, or a wedged close)."""
    with _open_lock:
        return _open_streams > 0


def _pick_loudest_channel(pcm: bytes, in_channels: int) -> tuple[bytes, int, list[int]]:
    """Deinterleave ``pcm`` and return (mono_bytes, chosen_index, per-channel peaks).

    Loudest rather than summed on purpose: summing doubles a dual-mono device into
    clipping, and averaging costs 6 dB when only one channel is live — which is the
    case this exists for.
    """
    if in_channels <= 1:
        return pcm, 0, []
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // (2 * in_channels) * 2 * in_channels])
    peaks = []
    for c in range(in_channels):
        chan = samples[c::in_channels]
        peaks.append(max(max(chan), -min(chan)) if chan else 0)
    best = peaks.index(max(peaks)) if peaks else 0
    return samples[best::in_channels].tobytes(), best, peaks


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
        self._open_device = None  # device the live stream was actually opened with
        self._chunks: list[bytes] = []
        self._armed = False
        self._start = 0.0
        self._idle_timer: threading.Timer | None = None
        self._capture_ch = config.channels  # channels the live stream was opened with
        self._lock = threading.Lock()
        # Set when a stop/close wedged: this process's CoreAudio is then poisoned
        # and only a relaunch clears it. app.py checks this after every recording.
        self.stalled = False

    def _resolve_device(self):
        dev = self.config.audio_device
        return None if dev in ("", "default", "pipewire", "pulse") else dev

    def _capture_channels(self, device) -> int:
        """How many channels to open: every input the device has, within reason."""
        wanted = self.config.channels
        try:
            import sounddevice as sd
            info = sd.query_devices(sd.default.device[0] if device is None else device)
            max_in = int(info.get("max_input_channels", wanted))
        except Exception:
            log.debug("could not probe channel count for %r", device, exc_info=True)
            return wanted
        return max(wanted, min(max_in, _MAX_CAPTURE_CHANNELS))

    # ---- stream lifecycle (opened once, then left running) ----

    def _callback(self, indata, frames, time_info, status):
        """PortAudio thread. Buffers only while armed.

        Deliberately lock-free: ``list.append`` is atomic under the GIL, and taking
        ``self._lock`` here would let a slow caller stall a realtime audio callback.
        Racing a start/stop can only misplace a single buffer at the boundary —
        ``start`` installs the fresh list *before* arming, and ``stop`` disarms
        *before* taking the list away, so a late append lands in the recording being
        returned rather than bleeding into the next one.
        """
        if self._armed:
            self._chunks.append(bytes(indata))

    def _ensure_stream(self) -> None:
        """Open the capture stream if it isn't already running on the right device."""
        device = self._resolve_device()
        stream = self._stream
        if stream is not None:
            if self._open_device == device and stream.active:
                return
            # Device switched in Settings, or the stream died (mic unplugged).
            self._close_stream()

        import sounddevice as sd

        channels = self._capture_channels(device)
        stream = None
        try:
            stream = sd.RawInputStream(
                samplerate=self.config.sample_rate,
                channels=channels,
                dtype="int16",
                device=device,
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            self._stream = None
            self._open_device = None
            if stream is not None:  # opened but failed to start — don't leak it
                try:
                    stream.close()
                except Exception:
                    log.debug("discarding half-open stream failed", exc_info=True)
            raise AudioError(f"could not open microphone: {exc}") from exc
        global _open_streams
        with _open_lock:
            _open_streams += 1
        self._stream = stream
        self._open_device = device
        self._capture_ch = channels
        log.info("capture stream open (device=%r, channels=%d)", device, channels)

    def _close_stream(self) -> bool:
        """Stop+close the live stream, bounded by ``_CLOSE_TIMEOUT``.

        Returns False if PortAudio did not return in time; the worker is a daemon
        thread and is deliberately abandoned rather than joined, so a wedged audio
        layer can never keep the process alive (that is what made the old hang
        ignore SIGTERM and need SIGKILL).
        """
        stream, self._stream, self._open_device = self._stream, None, None
        if stream is None:
            return True

        def shut():
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("stream close failed", exc_info=True)

        global _open_streams
        worker = threading.Thread(target=shut, name="bage-audio-close", daemon=True)
        worker.start()
        worker.join(_CLOSE_TIMEOUT)
        if worker.is_alive():
            # NOT decremented: PortAudio still holds the device, so a device-list
            # refresh must keep treating this process as busy.
            log.error("PortAudio did not close within %.1fs — abandoning the stream; "
                      "this process's audio is now unusable and needs a relaunch",
                      _CLOSE_TIMEOUT)
            self.stalled = True
            return False
        with _open_lock:
            _open_streams = max(0, _open_streams - 1)
        return True

    def close(self) -> bool:
        """Release the microphone. Safe to call more than once."""
        with self._lock:
            self._cancel_idle_release()
            self._armed = False
            self._chunks = []
            return self._close_stream()

    # ---- idle release (so the OS mic indicator isn't permanent) ----

    def _cancel_idle_release(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_release(self) -> None:
        delay = float(getattr(self.config, "idle_release", 0) or 0)
        if delay <= 0:
            return  # opted out: hold the mic until quit
        self._idle_timer = threading.Timer(delay, self._release_if_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _release_if_idle(self) -> None:
        with self._lock:
            self._idle_timer = None
            if self._armed or self._stream is None:
                return  # a recording started in the meantime
            log.info("idle — releasing the microphone (indicator goes out)")
            self._close_stream()

    # ---- recording (no PortAudio calls on this path) ----

    def start(self) -> None:
        with self._lock:
            # Cancel first: an idle release firing mid-dictation would be worse than
            # a late one. Holding the lock also means a release already in progress
            # finishes (~100ms) before we reopen, rather than racing it.
            self._cancel_idle_release()
            self._ensure_stream()
            self._chunks = []  # fresh buffer BEFORE arming, never the reverse
            self._start = time.monotonic()
            self._armed = True

    def stop(self) -> bytes | None:
        with self._lock:
            if not self._armed:
                return None
            self._armed = False
            wall = time.monotonic() - self._start
            chunks, self._chunks = self._chunks, []
            # Audio is already out of the buffer, so releasing here costs nothing
            # even if the close wedges — the utterance still transcribes.
            self._cancel_idle_release()
            if float(getattr(self.config, "idle_release", 0) or 0) <= 0:
                self._close_stream()  # mic off now, not "eventually"
            else:
                self._schedule_idle_release()

        pcm = b"".join(chunks)
        if not pcm:
            return None
        if self._capture_ch > self.config.channels:
            pcm, chosen, peaks = _pick_loudest_channel(pcm, self._capture_ch)
            if peaks and sorted(peaks)[-1] > 0 and chosen != 0:
                # Worth saying out loud: capturing mono would have returned silence.
                log.info("using input channel %d of %d (peaks=%s)",
                         chosen + 1, self._capture_ch, peaks)
        data = _to_wav(pcm, self.config.sample_rate, self.config.channels)
        if len(data) <= _WAV_HEADER_BYTES:
            return None
        seconds = _wav_seconds(data, self.config.sample_rate, self.config.channels) or wall
        if is_too_short(seconds, self.config.min_duration):
            log.info("discarded %.2fs tap (< %.2fs)", seconds, self.config.min_duration)
            return None
        return data
