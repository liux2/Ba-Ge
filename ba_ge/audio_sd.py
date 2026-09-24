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
  ``paInternalError``. That is unrecoverable in-process, so it is recorded
  process-wide (``mark_poisoned``; every recorder's ``stalled`` then reads True)
  and ``app.py`` relaunches (see ``platform.relaunch_self``) rather than leaving a
  half-dead app behind. The abandoned stream keeps the OS mic indicator lit until
  this process exits, which is one more reason the relaunch must happen on EVERY
  path, not just after a successful paste.

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
import re
import threading
import time
import wave

from .audio import _WAV_HEADER_BYTES, AudioError, _wav_seconds, is_too_short

log = logging.getLogger("bage.audio_sd")

# How long to wait for a PortAudio stop/close before abandoning it. Hit once per
# utterance now that the mic is released eagerly; see the module docstring.
_CLOSE_TIMEOUT = 2.0

# A recording at least this long that received no buffer at all means the device
# stopped delivering (callbacks normally start within tens of milliseconds).
_NO_AUDIO_AFTER = 0.5

# Multi-input devices put the mic on whichever channel they feel like: a 2-channel
# wireless receiver can move it between slots when a transmitter is re-paired, and
# capturing mono then silently grabs channel 1 and yields DIGITAL SILENCE while the
# OS meter (which watches every channel) looks perfectly healthy. So capture every
# channel the device offers and pick the live one. Capped so a pro interface with
# dozens of inputs doesn't make us haul useless audio around.
_MAX_CAPTURE_CHANNELS = 8

# PortAudio errors that mean "that microphone isn't there right now" (unplugged,
# renamed, taken by another app) rather than "this process's CoreAudio is broken".
# A relaunch can't fix these, so they must not trigger one.
_MISSING_DEVICE_CODES = {-9996, -9985}  # paInvalidDevice, paDeviceUnavailable
_PA_CODE = re.compile(r"PaErrorCode (-?\d+)")

# Set once this process's PortAudio/CoreAudio is known to be unusable (a wedged
# stop/close, a stalled re-init, an open that never returned): only a relaunch
# clears it. Process-wide on purpose — it is the audio layer that is broken, not
# one recorder, so swapping recorders in Settings must not forget it.
_poisoned: str | None = None

# Serialises PortAudio open/re-init against the Settings mic re-enumeration
# (platform._refresh_portaudio), which runs Pa_Terminate on another thread.
# Never held across a stop/close — those can wedge.
_pa_lock = threading.RLock()


def mark_poisoned(reason: str) -> None:
    """Record that this process's audio layer is unusable (first reason wins)."""
    global _poisoned
    if _poisoned is None:
        _poisoned = reason
        log.error("audio is unusable in this process (%s) — it needs a relaunch", reason)


def poisoned_reason() -> str | None:
    return _poisoned


def needs_hard_exit() -> bool:
    """True if normal interpreter teardown could hang in PortAudio.

    sounddevice registers an atexit handler that stops/closes the last stream and
    runs Pa_Terminate; against a wedged stream that blocks forever (quit then
    needs SIGKILL). Callers skip it with ``os._exit`` when this is True.
    """
    return _poisoned is not None or capture_active()


def _is_missing_device(exc: BaseException) -> bool:
    """The open failed because the device is absent/unavailable, not poisoned."""
    if isinstance(exc, ValueError):  # sounddevice: "No input device matching ..."
        return True
    match = _PA_CODE.search(str(exc))
    return bool(match) and int(match.group(1)) in _MISSING_DEVICE_CODES


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

    # ---- poisoned audio layer (app.py relaunches when this reads True) ----

    @property
    def stalled(self) -> bool:
        """True once this process's audio layer needs a relaunch (process-wide)."""
        return _poisoned is not None

    @stalled.setter
    def stalled(self, value: bool) -> None:
        if value:
            mark_poisoned("flagged by caller")

    @property
    def stall_reason(self) -> str | None:
        return _poisoned

    def mark_stalled(self, reason: str) -> None:
        """Called by app.py when a start()/stop() it bounded never came back."""
        mark_poisoned(reason)

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

    # ---- stream lifecycle (one stream per recording unless idle_release > 0) ----

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

        self._open_stream(device)

    def _reinit_portaudio(self) -> bool:
        """Rebuild PortAudio's state after the OS pulled CoreAudio out from under it.

        Bounded, because terminate/initialise reaches into the same CoreAudio layer
        that can deadlock. Skipped if any capture stream is open — tearing one down
        mid-recording would be worse than the failure we are recovering from.
        """
        if capture_active():
            return False
        try:
            import sounddevice as sd

            from . import platform
            finished, _ = platform.call_with_timeout(
                lambda: (sd._terminate(), sd._initialize()), 3.0)
        except Exception:
            log.debug("PortAudio re-init failed", exc_info=True)
            return False
        if not finished:
            # Abandoned half-way through Pa_Terminate/Pa_Initialize: nothing in this
            # process can use PortAudio safely any more.
            mark_poisoned("PortAudio re-init did not finish within 3s")
        return bool(finished)

    def _open_stream(self, device, retry: bool = False) -> None:
        import sounddevice as sd

        with _pa_lock:
            channels = self._capture_channels(device)
            began = time.monotonic()
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
                if not retry and self._reinit_portaudio():
                    # Sleep/wake tears CoreAudio down underneath PortAudio, whose
                    # cached HAL state then fails every open with paInternalError
                    # even though a fresh process opens the same device fine.
                    # Re-initialising clears it (and refreshes the device list, so a
                    # replugged mic is found again), so the user never sees it.
                    log.warning("microphone open failed (%s) — re-initialised "
                                "PortAudio, retrying", exc)
                    return self._open_stream(device, retry=True)
                if _poisoned is None and _is_missing_device(exc):
                    # Unplugged / renamed / claimed by another app. A relaunch can't
                    # fix that, so say what can instead of restarting on every press.
                    name = "the default microphone" if device is None else repr(device)
                    raise AudioError(
                        f"Microphone unavailable ({name}): {exc}. Plug it in, or "
                        "choose another one in Settings → Microphone.") from exc
                mark_poisoned(f"microphone open failed: {exc}")
                raise AudioError(f"could not open microphone: {exc}") from exc
            global _open_streams
            with _open_lock:
                _open_streams += 1
            self._stream = stream
            self._open_device = device
            self._capture_ch = channels
            log.info("capture stream open in %.0f ms (device=%r, channels=%d)",
                     (time.monotonic() - began) * 1000, device, channels)

    def _close_stream(self) -> bool:
        """Stop+close the live stream, bounded by ``_CLOSE_TIMEOUT``.

        Returns False if PortAudio did not return in time; the worker is a daemon
        thread and is deliberately abandoned rather than joined, so a wedged audio
        layer can never block the caller. The process is then poisoned: app.py
        relaunches, and the exit path skips sounddevice's atexit Pa_Terminate, which
        would otherwise hang forever on this same stream.
        """
        stream, self._stream, self._open_device = self._stream, None, None
        if stream is None:
            return True

        state = {"abandoned": False}
        began = time.monotonic()

        def shut():
            global _open_streams
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("stream close failed", exc_info=True)
            if state["abandoned"]:
                # Slow rather than dead. Uncount it so a device refresh isn't
                # blocked forever; the process stays flagged — it already relaunched
                # or will.
                with _open_lock:
                    _open_streams = max(0, _open_streams - 1)
                log.warning("abandoned PortAudio close finished late, after %.1fs",
                            time.monotonic() - began)

        global _open_streams
        worker = threading.Thread(target=shut, name="bage-audio-close", daemon=True)
        worker.start()
        worker.join(_CLOSE_TIMEOUT)
        if worker.is_alive():
            # NOT decremented: PortAudio still holds the device, so a device-list
            # refresh must keep treating this process as busy.
            state["abandoned"] = True
            log.error("PortAudio did not close within %.1fs — abandoning the stream",
                      _CLOSE_TIMEOUT)
            mark_poisoned("PortAudio stream close wedged")
            return False
        with _open_lock:
            _open_streams = max(0, _open_streams - 1)
        log.info("capture stream closed in %.0f ms", (time.monotonic() - began) * 1000)
        return True

    def close(self) -> bool:
        """Release the microphone. Safe to call more than once.

        Never blocks indefinitely: if a wedged start()/stop() still holds the lock
        (quit during an audio stall), give up and report the process as poisoned so
        the exit path skips PortAudio's teardown.
        """
        if not self._lock.acquire(timeout=_CLOSE_TIMEOUT + 1.0):
            mark_poisoned("recorder lock held by a stalled audio call at close")
            return False
        try:
            self._cancel_idle_release()
            self._armed = False
            self._chunks = []
            return self._close_stream()
        finally:
            self._lock.release()

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

    # ---- recording ----

    def start(self) -> None:
        with self._lock:
            # Cancel first: an idle release firing mid-dictation would be worse than
            # a late one. Holding the lock also means a release already in progress
            # finishes (~100ms) before we reopen, rather than racing it.
            self._cancel_idle_release()
            self._ensure_stream()  # opens a stream unless idle_release kept one
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
            # Held long enough to expect audio, yet PortAudio never delivered a
            # single buffer: the device died under an open stream. Returning None
            # would make this look like an ignored tap and the app like it simply
            # stopped working, so say why.
            if wall >= max(self.config.min_duration, _NO_AUDIO_AFTER):
                log.warning("no audio buffers delivered in %.1fs of recording", wall)
                raise AudioError(
                    "No audio from the microphone — it may be unplugged or asleep. "
                    "Replug it or choose another in Settings → Microphone.")
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
