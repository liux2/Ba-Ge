"""Application wiring: hotkey -> record -> transcribe -> inject, with a state machine.

Threading model:
  * main thread     : indicator main loop (GTK) or a blocking wait (notify mode)
  * hotkey thread   : pynput listener -> on_press / on_release
  * worker thread   : _process() (stop recording, transcribe, inject) off the UI thread
State transitions are guarded by a lock; indicator updates happen outside the lock.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from . import paths, platform, singleton
from .audio import AudioError, peak_amplitude
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

        try:
            self.recorder.start()
        except AudioError as exc:
            self._fail(str(exc))
            return
        self._indicate(State.RECORDING)

    def on_release(self) -> None:
        with self._lock:
            if self._state is not State.RECORDING:
                return
            self._state = State.BUSY
        self._indicate(State.BUSY)
        self._arm_busy_watchdog()

        if self.run_async:
            threading.Thread(target=self._process, daemon=True).start()
        else:
            self._process()

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

    # ---- worker ----

    def _process(self) -> None:
        try:
            wav = self.recorder.stop()
            if not wav:
                self._set_state(State.IDLE)
                return
            if peak_amplitude(wav) < _SILENCE_PEAK:
                # Skip the (billed) API call and tell the user what's wrong.
                self._set_state(State.IDLE)
                self.notifier(
                    "Ba-Ge — silent recording",
                    "No audio captured. Is the microphone muted or the wrong "
                    "device selected? (Settings → Microphone)",
                    urgency="critical")
                return
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
            hotkey_name=self.config.hotkey.upper())
        if platform.IS_LINUX:
            from .inject import ensure_ydotoold  # reliable typing needs ydotoold running
            ensure_ydotoold()
        self._start_hotkey()
        self._set_state(State.IDLE)

        missing = platform.missing_permissions()  # macOS TCC; [] elsewhere
        if missing:
            self.notifier(
                "Ba-Ge — permission needed",
                f"Grant {', '.join(missing)} and Input Monitoring in System Settings "
                "› Privacy & Security, then relaunch.", urgency="critical")

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

    def _open_transcribe(self) -> None:
        from .ui_files import choose_and_transcribe
        choose_and_transcribe(self.indicator.root, self.config)

    def reload_config(self) -> None:
        """Re-read config and rebuild config-derived components (live apply)."""
        if self.state is not State.IDLE:
            self.notifier("Ba-Ge", "Settings saved — will apply when idle.")
            return
        self.config = load_config()
        self.recorder = platform.make_recorder(self.config)
        self.injector = platform.make_injector(self.config)
        self.transcribe_fn = lambda wav: transcribe(wav, self.config)
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
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
            self._hotkey = None
        try:
            if self.state is State.RECORDING:
                self.recorder.stop()
        except Exception:
            pass


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


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
        notify("Ba-Ge", "Already running — hold F9 to dictate.")
        return

    ensure_config_file()
    config = load_config()
    DictationApp(config).run()


if __name__ == "__main__":
    main()
