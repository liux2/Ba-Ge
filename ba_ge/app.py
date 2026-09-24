"""Application wiring: hotkey -> record -> transcribe -> inject, with a state machine.

Threading model:
  * main thread     : Qt event loop (tray, windows)
  * hotkey thread   : pynput listener -> on_press / on_release, which only enqueue
  * hotkey worker   : one serial thread running press/release actions in order —
                      including opening the mic, which must never run on the
                      listener thread (on macOS that is the event-tap thread, and a
                      slow callback there gets the tap disabled by the OS)
  * process thread  : _process() (stop recording, transcribe, inject) per utterance
State transitions are guarded by a lock; indicator updates happen outside the lock.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from pathlib import Path

from . import paths, platform, singleton
from .audio import AudioError, level_stats
from .config import CONFIG_PATH, DEPRECATED_MODELS, ensure_config_file, load_config
from .ui import make_indicator
from .inject import InjectionError
from .notify import notify
from .state import State
from .transcribe import TranscriptionError, transcribe

log = logging.getLogger("bage.app")

_ERROR_RESET_DELAY = 2.0  # seconds before the ERROR indicator reverts to IDLE
_SILENCE_PEAK = 64        # peak below this (of 32767) means the clip is silent
_BUSY_TIMEOUT = 90.0      # force-recover if processing hangs this long (never stay stuck)
_STOP_TIMEOUT = 5.0       # audio backend must hand back the clip within this
_START_TIMEOUT = 10.0     # opening the mic must finish within this (Bluetooth is slow)
# Longest single recording. Nothing else ends a recording whose key-up was lost, and
# until it ends the mic stays open (OS indicator lit) and the hotkey stays deaf.
_MAX_RECORDING = 300.0
# An audio relaunch that recurs this soon means the fault is outside this process
# (the device or the OS audio server); relaunching again would only loop.
_RELAUNCH_GUARD = 60.0

# Input-level guards. A clip driven into the rails is destroyed but LOUD, so the
# silence floor never catches it — the model just returns confident nonsense and the
# app looks broken. Warn on both ends of the range instead, and say what to change.
_CLIP_PCT = 0.05          # % of samples pinned to the rail before we call it clipping
_QUIET_DBFS = -45.0       # RMS below this is too faint to transcribe reliably
_LEVEL_WARN_INTERVAL = 90.0  # don't nag on every utterance of a bad setup


def _close_recorder(recorder) -> None:
    """Release a recorder's device, if its backend holds one (macOS/Windows)."""
    close = getattr(recorder, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        log.debug("recorder close failed", exc_info=True)


# TEMPORARY debug aid: append the raw transcript Scribe returns (before typing)
# so the exact text — spaces and all — can be inspected. Removed after we diagnose
# the dropped-spaces issue. Works for icon launches (no env/shell needed).
_TRANSCRIPT_LOG = paths.transcript_log()


def _log_transcript(text: str, backend: str = "") -> None:
    try:
        _TRANSCRIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRANSCRIPT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{backend}] {text!r}\n")
    except OSError:
        pass


class DictationApp:
    def __init__(self, config, *, recorder=None, transcribe_fn=None, injector=None,
                 indicator=None, notifier=notify, run_async=True):
        self.config = config
        self.recorder = recorder if recorder is not None else platform.make_recorder(config)
        self.transcribe_fn = (
            transcribe_fn if transcribe_fn is not None else lambda wav: transcribe(wav, config)
        )
        self.injector = injector if injector is not None else platform.make_injector(config)
        self.indicator = indicator
        self.notifier = notifier
        self.run_async = run_async

        self._state = State.IDLE
        self._lock = threading.RLock()
        self._hotkey = None
        self._error_timer = None
        self._busy_watchdog = None
        self._record_timer = None
        self._last_level_warn = 0.0
        self._actions: queue.SimpleQueue = queue.SimpleQueue()
        self._action_worker = None

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def _indicate(self, state: State) -> None:
        if self.indicator is not None:
            self.indicator.set_state(state)

    def _set_state(self, state: State) -> None:
        with self._lock:
            self._state = state
        self._indicate(state)

    # ---- hotkey callbacks (run on the pynput listener thread) ----

    def on_press(self) -> None:
        self._submit(self._handle_press)

    def on_release(self) -> None:
        self._submit(self._handle_release)

    def _submit(self, action) -> None:
        """Run ``action`` on the serial hotkey worker (inline when not async).

        The listener thread must return at once: on macOS it is the event-tap
        thread, and opening the mic there (~80 ms normally, seconds after a wake or
        on Bluetooth) is how a key-up gets lost for good. One serial worker keeps
        press/release order, so a release that arrives during a slow start ends the
        recording right after the start instead of being dropped.
        """
        if not self.run_async:
            action()
            return
        self._actions.put(action)
        with self._lock:
            if self._action_worker is None:
                self._action_worker = threading.Thread(
                    target=self._run_actions, name="bage-hotkey-worker", daemon=True)
                self._action_worker.start()

    def _run_actions(self) -> None:
        while True:
            action = self._actions.get()
            try:
                action()
            except Exception:  # pragma: no cover - defensive; never kill the worker
                log.exception("hotkey action failed")

    def _handle_press(self) -> None:
        # In toggle mode the hotkey's leading edge flips recording on/off; the
        # trailing edge (on_release) is ignored. In hold mode press starts.
        if self.config.hotkey_mode == "toggle":
            with self._lock:
                recording = self._state is State.RECORDING
            if recording:
                self._end_recording()
            else:
                self._begin_recording()
            return
        self._begin_recording()

    def _handle_release(self) -> None:
        if self.config.hotkey_mode == "toggle":
            return  # a tap-release must not stop a toggle recording
        self._end_recording()

    def _begin_recording(self) -> None:
        with self._lock:
            if self._state is not State.IDLE:
                return
            if not self.config.api_key_valid:
                started = False
            else:
                self._state = State.RECORDING
                started = True

        if not started:
            self._indicate(State.ERROR)
            self.notifier(
                "Ba-Ge",
                "No ElevenLabs API key configured. Edit ~/.config/ba-ge/config.toml",
                urgency="critical",
            )
            self._schedule_idle_reset()
            return

        if getattr(self.recorder, "stalled", False):
            # Audio already known dead (e.g. a close wedged after the last clip):
            # opening would fail or hang, and the stuck stream keeps the mic lit.
            with self._lock:
                self._state = State.IDLE
            self._recover_if_audio_stalled()
            return

        began = time.monotonic()
        try:
            finished, _ = platform.call_with_timeout(self.recorder.start, _START_TIMEOUT)
            if not finished:
                self._mark_audio_stalled(
                    f"microphone open did not return within {_START_TIMEOUT:.0f}s")
                raise AudioError("The microphone did not open — the audio system "
                                 "is stuck.")
        except Exception as exc:
            # Any exception, not only AudioError: one escaping here would leave the
            # state RECORDING with no stream behind it and no error shown.
            if not isinstance(exc, AudioError):
                log.error("could not start recording", exc_info=True)
            self._fail(str(exc) or type(exc).__name__)
            # A failed OPEN poisons the process just as a wedged close does — most
            # often after sleep/wake. Recover rather than sit here looking healthy.
            self._recover_if_audio_stalled()
            return
        log.info("recording (mic ready in %.0f ms)", (time.monotonic() - began) * 1000)
        self._indicate(State.RECORDING)
        self._arm_record_limit()

    def _end_recording(self) -> None:
        with self._lock:
            if self._state is not State.RECORDING:
                return
            self._state = State.BUSY
        self._cancel_record_limit()
        self._indicate(State.BUSY)
        self._arm_busy_watchdog()

        if self.run_async:
            threading.Thread(target=self._process, daemon=True).start()
        else:
            self._process()

    # ---- recording limit: a lost key-up must not hold the mic forever ----

    def _arm_record_limit(self) -> None:
        with self._lock:
            if self._record_timer is not None:
                self._record_timer.cancel()
            self._record_timer = threading.Timer(
                _MAX_RECORDING, lambda: self._submit(self._record_limit_reached))
            self._record_timer.daemon = True
            self._record_timer.start()

    def _cancel_record_limit(self) -> None:
        with self._lock:
            if self._record_timer is not None:
                self._record_timer.cancel()
                self._record_timer = None

    def _record_limit_reached(self) -> None:
        if self.state is not State.RECORDING:
            return
        log.warning("recording reached the %.0fs limit — ending it", _MAX_RECORDING)
        self.notifier("Ba-Ge",
                      f"Recording stopped at the {_MAX_RECORDING / 60:.0f}-minute limit.",
                      urgency="normal")
        if self._hotkey is not None:
            self._hotkey.reset()  # the key-up may never come; don't swallow the next press
        self._end_recording()

    # ---- watchdog: never stay stuck in BUSY ----

    def _arm_busy_watchdog(self) -> None:
        with self._lock:
            if self._busy_watchdog is not None:
                self._busy_watchdog.cancel()
            self._busy_watchdog = threading.Timer(_BUSY_TIMEOUT, self._busy_timed_out)
            self._busy_watchdog.daemon = True
            self._busy_watchdog.start()

    def _disarm_busy_watchdog(self) -> None:
        with self._lock:
            if self._busy_watchdog is not None:
                self._busy_watchdog.cancel()
                self._busy_watchdog = None

    def _busy_timed_out(self) -> None:
        if self.state is State.BUSY:
            log.error("processing exceeded %ss — force-resetting to idle", _BUSY_TIMEOUT)
            self._fail("Processing timed out — reset. Try again.")
            self._recover_if_audio_stalled()

    # ---- worker ----

    def _process(self) -> None:
        try:
            finished, wav = platform.call_with_timeout(self.recorder.stop, _STOP_TIMEOUT)
            if not finished:
                self._mark_audio_stalled(
                    f"ending the recording did not return within {_STOP_TIMEOUT:.0f}s")
                raise AudioError(
                    f"Audio backend stalled (>{_STOP_TIMEOUT:.0f}s) ending the "
                    "recording. Recording was lost; try again.")
            if not wav:
                self._set_state(State.IDLE)
                return
            levels = level_stats(wav)
            # Logged for every clip: when a transcript comes out wrong, this is the
            # first thing worth looking at, and it costs one line.
            log.info("input level: peak=%d rms=%.1f dBFS clipped=%.2f%%",
                     levels.peak, levels.rms_dbfs, levels.clipped_pct)
            if levels.peak < _SILENCE_PEAK:
                # Skip the (billed) API call and tell the user what's wrong.
                self._set_state(State.IDLE)
                self.notifier(
                    "Ba-Ge — silent recording",
                    "No audio captured. Is the microphone muted or the wrong "
                    "device selected? (Settings → Microphone)",
                    urgency="critical")
                return
            # Still transcribe: the words are usually partly recoverable, and the
            # user wants their text. The warning explains why it may be wrong.
            self._warn_about_levels(levels)
            text = (self.transcribe_fn(wav) or "").strip()
            log.info("transcript: %r", text)  # raw text from Scribe, before typing
            _log_transcript(text, getattr(self.injector, "backend", ""))
            if text:
                self.injector.type_text(text)
            self._set_state(State.IDLE)
        except (TranscriptionError, InjectionError, AudioError) as exc:
            self._fail(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unexpected error during processing")
            self._fail(f"Unexpected error: {exc}")
        finally:
            self._disarm_busy_watchdog()
            # On EVERY exit, not only after a paste: a close that wedged on a short
            # tap, a silent clip or a failed transcription otherwise leaves the
            # abandoned stream holding the mic (indicator lit) and every later open
            # failing. Still runs after the paste, so recovery never eats text.
            self._recover_if_audio_stalled()

    def _warn_about_levels(self, levels) -> None:
        """Tell the user when the *input* is why a transcript will be wrong.

        Rate-limited: a badly set-up mic would otherwise fire on every utterance,
        and a notification per sentence is its own kind of broken.
        """
        if levels.clipped_pct >= _CLIP_PCT:
            title = "Ba-Ge — microphone too loud"
            body = (f"Audio is clipping ({levels.clipped_pct:.1f}% at maximum), so "
                    "words may come out wrong. Turn the microphone's gain down.")
        elif levels.rms_dbfs < _QUIET_DBFS:
            title = "Ba-Ge — microphone very quiet"
            body = (f"Input is only {levels.rms_dbfs:.0f} dBFS, which is too faint to "
                    "transcribe reliably. Move closer, or raise the mic's gain.")
        else:
            return

        now = time.monotonic()
        if now - self._last_level_warn < _LEVEL_WARN_INTERVAL:
            return  # already told them recently; the log line still records it
        self._last_level_warn = now
        log.warning("%s: %s", title, body)
        self.notifier(title, body, urgency="critical")

    def _mark_audio_stalled(self, reason: str) -> None:
        """A bounded audio call never came back: flag the backend for relaunch."""
        mark = getattr(self.recorder, "mark_stalled", None)
        if mark is not None:
            mark(reason)
        else:
            log.error("audio backend stalled: %s", reason)

    def _recover_if_audio_stalled(self) -> None:
        """Relaunch if the audio backend is unusable.

        A deadlocked PortAudio stop poisons this process's CoreAudio — every later
        open fails with paInternalError — and nothing in-process can clear it. The
        old behaviour was to keep running as a dead app that still looked healthy
        (with the abandoned stream keeping the OS mic indicator lit); restarting
        costs a second and keeps dictation working. Callers invoke this only after
        any transcript has been injected, so recovery never eats text.
        """
        if not getattr(self.recorder, "stalled", False):
            return
        reason = getattr(self.recorder, "stall_reason", None) or "unknown"
        last = platform.last_relaunch()
        if last and last.get("reason") == "audio":
            since = time.time() - float(last.get("time", 0) or 0)
            if 0 <= since < _RELAUNCH_GUARD:
                log.error("audio unusable again %.0fs after an audio relaunch (%s) — "
                          "not relaunching again", since, reason)
                self.notifier(
                    "Ba-Ge — audio system stuck",
                    "Restarting Ba-Ge didn't bring the microphone back. Unplug and "
                    "replug it (or restart the computer), then reopen Ba-Ge.",
                    urgency="critical")
                return
        log.error("audio backend unusable (%s) — relaunching", reason)
        self.notifier("Ba-Ge", "Audio glitch — restarting to recover.",
                      urgency="critical")
        platform.relaunch_self("audio")  # does not return

    def _fail(self, message: str) -> None:
        log.error(message)
        with self._lock:
            self._state = State.IDLE
        self._indicate(State.ERROR)
        self.notifier("Ba-Ge — error", message, urgency="critical")
        self._schedule_idle_reset()

    def _schedule_idle_reset(self, delay: float = _ERROR_RESET_DELAY) -> None:
        def reset():
            if self.state is State.IDLE:
                self._indicate(State.IDLE)

        with self._lock:
            if self._error_timer is not None:
                self._error_timer.cancel()
            self._error_timer = threading.Timer(delay, reset)
            self._error_timer.daemon = True
            self._error_timer.start()

    # ---- lifecycle ----

    def run(self) -> None:
        self.indicator = make_indicator(
            self._request_quit, self._open_settings, self._open_transcribe,
            hotkey_name=self.config.hotkey.upper(),
            on_permissions=self._open_permissions)
        self._bind_clipboard()
        if platform.IS_LINUX:
            from .inject import ensure_device  # pre-create the uinput paste device
            ensure_device()
        self._start_hotkey()
        self._set_state(State.IDLE)

        missing = platform.missing_permissions()  # macOS TCC; [] elsewhere
        if missing:
            self.notifier(
                "Ba-Ge — permission needed",
                f"Grant {', '.join(missing)} in System Settings › Privacy & Security, "
                "then relaunch.", urgency="critical")
            # A guided panel beats a one-shot notification: live status + fix buttons.
            self.indicator.run_on_ui(self._open_permissions)

        if self.config.model_id in DEPRECATED_MODELS:
            log.warning('model_id %r is deprecated; set model_id = "scribe_v2" in %s',
                        self.config.model_id, CONFIG_PATH)
            self.notifier("Ba-Ge",
                          f"Model '{self.config.model_id}' is deprecated — update to scribe_v2.",
                          urgency="normal")

        if self.config.api_key_valid:
            self.notifier("Ba-Ge",
                          f"Ready — hold {self.config.hotkey.upper()} to dictate.")
        else:
            self.notifier("Ba-Ge",
                          f"Running, but no API key set. Edit {ensure_config_file()}",
                          urgency="normal")

        try:
            self.indicator.run_main_loop()
        finally:
            self._shutdown()

    def _start_hotkey(self) -> None:
        from .hotkey import HotkeyListener  # lazy: requires pynput
        self._hotkey = HotkeyListener(self.config.hotkey, self.on_press, self.on_release)
        self._hotkey.start()

    def _open_settings(self) -> None:
        # Called on the tk thread (the runtime marshals menu clicks there).
        from .ui_settings import open_settings
        open_settings(self.indicator.root, on_saved=self.reload_config)

    def _open_permissions(self) -> None:
        # macOS TCC helper window (no-op elsewhere). On the Qt main thread.
        # on_restart=None → the window relaunches the .app bundle itself.
        from .ui_permissions import open_permissions_window
        open_permissions_window()

    def _open_transcribe(self) -> None:
        from .ui_files import choose_and_transcribe
        choose_and_transcribe(self.indicator.root, self.config)

    def _bind_clipboard(self) -> None:
        """Give the Linux paste injector the Qt clipboard manager (no-op elsewhere)."""
        clip = getattr(self.indicator, "clipboard", None)
        if clip is not None and hasattr(self.injector, "bind_clipboard"):
            self.injector.bind_clipboard(clip)

    def reload_config(self) -> None:
        """Re-read config and rebuild config-derived components (live apply)."""
        if self.state is not State.IDLE:
            self.notifier("Ba-Ge", "Settings saved — will apply when idle.")
            return
        self.config = load_config()
        previous = self.recorder
        self.recorder = platform.make_recorder(self.config)
        # The old backend may hold the mic open (see ba_ge/audio_sd.py); dropping the
        # reference alone would leak the device and leave the OS indicator lit.
        _close_recorder(previous)
        self.injector = platform.make_injector(self.config)
        self._bind_clipboard()
        self.transcribe_fn = lambda wav: transcribe(wav, self.config)
        self._recover_if_audio_stalled()  # the old recorder's close may have wedged
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
            self._start_hotkey()
        self.notifier("Ba-Ge",
                      f"Settings applied — hold {self.config.hotkey.upper()} to dictate.")

    def _request_quit(self) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        self._disarm_busy_watchdog()
        self._cancel_record_limit()
        if platform.IS_LINUX:
            from .inject import close_device
            close_device()
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
            self._hotkey = None
        try:
            if self.state is State.RECORDING:
                platform.call_with_timeout(self.recorder.stop, _STOP_TIMEOUT)
        except Exception:
            pass
        # Hand the mic back. Bounded inside the backend, so a wedged audio layer
        # can't stop the app from quitting (that is what forced SIGKILL before).
        _close_recorder(self.recorder)


def _transcribe_file_cli(rest) -> None:
    """Headless file transcription: writes <name>.txt beside the source, prints the path."""
    from .filejob import FileJobError, default_txt_path, transcribe_file

    if not rest:
        sys.stderr.write("usage: ba-ge --transcribe <audio-file>\n")
        sys.exit(2)
    path = rest[0]
    config = load_config()
    try:
        text, _payload = transcribe_file(
            path, config,
            progress=lambda m: sys.stderr.write(f"[transcribe] {m}\n"))
    except FileJobError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(1)

    out = default_txt_path(path)
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"error: could not write {out}: {exc}\n")
        sys.exit(1)
    print(str(out))


def _type_test(rest) -> None:
    """Type a known string via the active backend — isolates typing from STT.

    If spaces survive here, the typing layer is fine and dropped spaces in
    dictation come from the transcript (ElevenLabs). If they drop here too, it's
    the typing/injection path.
    """
    from .inject import Injector

    text = rest[0] if rest else (
        "Now we are testing on long conversations to see if there are "
        "still random removal of whitespaces")
    cfg = load_config()
    inj = Injector(cfg)
    sys.stderr.write(
        f"[type-test] backend={inj.backend} key_delay={cfg.key_delay_ms}ms\n"
        f"[type-test] EXPECTED: {text!r}\n"
        "[type-test] focus a text field — typing in 3 seconds...\n")
    sys.stderr.flush()
    time.sleep(3)
    inj.type_text(text)
    sys.stderr.write("[type-test] done. Compare typed output with EXPECTED above.\n")


def _log_startup(config) -> None:
    """One line that says what is running, so a bug report's log is self-describing."""
    import platform as _stdplatform

    log.info("Ba-Ge starting: python %s on %s %s, frozen=%s, argv=%s, hotkey=%s (%s), "
             "device=%r, idle_release=%s",
             _stdplatform.python_version(), sys.platform,
             _stdplatform.mac_ver()[0] or _stdplatform.release(),
             bool(getattr(sys, "frozen", False)),
             list(getattr(sys, "orig_argv", sys.argv)), config.hotkey,
             config.hotkey_mode, config.audio_device, config.idle_release)
    last = platform.last_relaunch()
    if last:
        since = time.time() - float(last.get("time", 0) or 0)
        if 0 <= since < 600:
            log.info("relaunched %.0fs ago by pid %s (reason: %s)",
                     since, last.get("pid"), last.get("reason") or "requested")


_stack_dump_file = None  # kept open for faulthandler


def _install_stack_dump() -> None:
    """``kill -USR1 <pid>`` writes every thread's stack to ba-ge-stacks.log.

    The failures that matter here are hangs (an audio call that never returns),
    which leave no log line; a stack dump shows exactly which call is parked.
    POSIX only, best-effort.
    """
    global _stack_dump_file
    try:
        import faulthandler
        import signal

        if not hasattr(faulthandler, "register") or not hasattr(signal, "SIGUSR1"):
            return
        path = paths.cache_dir() / "ba-ge-stacks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        _stack_dump_file = open(path, "a", encoding="utf-8")
        faulthandler.register(signal.SIGUSR1, file=_stack_dump_file, all_threads=True)
    except Exception:
        log.debug("could not install the stack-dump handler", exc_info=True)


def _add_file_log() -> None:
    """Mirror logs to a rotating file so the (console-less) packaged app is
    diagnosable — on macOS/Windows stderr is /dev/null, so failures are otherwise
    silent. Best-effort: never let logging setup break startup."""
    try:
        from logging.handlers import RotatingFileHandler
        log_path = paths.cache_dir() / "ba-ge.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=512 * 1024, backupCount=2)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            root.addHandler(fh)
        log.info("log file: %s", log_path)
    except Exception:
        log.debug("could not set up file logging", exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _add_file_log()  # packaged app has no console (stderr -> /dev/null); keep a log
    args = sys.argv[1:]

    if args and args[0] == "--transcribe":
        _transcribe_file_cli(args[1:])
        return

    if args and args[0] == "--type-test":
        _type_test(args[1:])
        return

    if "--settings" in args:
        from .ui_settings import run_settings
        ensure_config_file()
        run_settings()
        return

    if not singleton.acquire("ba-ge"):
        log.info("another instance is already running — exiting")
        notify("Ba-Ge", "Already running — hold F9 to dictate.")
        return

    ensure_config_file()
    config = load_config()
    _log_startup(config)
    _install_stack_dump()
    try:
        DictationApp(config).run()
    except Exception:
        log.exception("fatal error")  # hard_exit below would otherwise swallow it
        raise
    finally:
        # A wedged audio stream makes sounddevice's atexit Pa_Terminate block
        # forever (quit then needs SIGKILL, and the mic stays lit meanwhile).
        if platform.audio_needs_hard_exit():
            log.warning("audio layer is wedged — exiting without PortAudio teardown")
            platform.hard_exit(0)
        if not platform.IS_LINUX:
            platform.arm_exit_watchdog(5.0)  # any other teardown hang


if __name__ == "__main__":
    main()
