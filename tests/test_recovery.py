"""Recovery from a dead audio layer and from lost hotkey events (see audio_sd.py).

The macOS failure these pin down: dictation works for a while, then a CoreAudio
close deadlocks (or an open fails after sleep). The abandoned stream keeps the OS
mic indicator lit, and every later press used to fail SILENTLY — the backend
raised its own ``AudioError`` class that app.py never caught, and the relaunch
only ran after a successful paste. Separately, a key-up lost while macOS had the
event tap disabled left the app recording forever.

Runs on any OS: PortAudio, pynput and Quartz are faked. Anything that would hang
if a bound regressed runs on a helper thread, so the test fails instead.
"""

import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import ba_ge
from ba_ge import app as _appmod
from ba_ge import audio, audio_sd, platform
from ba_ge.app import DictationApp
from ba_ge.audio_sd import SdRecorder
from ba_ge.inject import InjectionError
from ba_ge.state import State
from ba_ge.transcribe import TranscriptionError
from tests.test_audio_sd import _AudioStateTest, _cfg, _SdPatch, _wait_until

LOUD = b"\x10\x27" * 16000   # 1 s of loud 16 kHz mono
QUIET = b"\x00\x00" * 16000  # 1 s of digital silence
LOUD_WAV = b"\x00" * 44 + b"\x10\x27" * 8000
REPO = Path(__file__).resolve().parent.parent
_TEST_CLOSE_TIMEOUT = 0.3


def _open_without_checks(rec, sd, device=None):
    """What a successful _open_stream does, minus its up-front poison check — as if
    the flag landed after PortAudio had already been entered."""
    stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                               device=device, callback=rec._callback)
    stream.start()
    with audio_sd._open_lock:
        audio_sd._open_streams += 1
    rec._stream, rec._open_device = stream, device


def _bounded(testcase, fn, timeout=3.0):
    """Run fn on a helper thread; fail (don't hang) if it doesn't return in time."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    testcase.assertFalse(t.is_alive(), f"{getattr(fn, '__name__', fn)} is no longer bounded")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class _Seen:
    """What the app did: pastes, indicator states, notifications, relaunches."""

    def __init__(self, order):
        self.typed: list[str] = []
        self.states: list[State] = []
        self.notes: list[tuple] = []
        self.order = order  # shared with the relaunch stub: pins paste-before-relaunch

    def type_text(self, text):
        self.typed.append(text)
        self.order.append("paste")

    def set_state(self, state):
        self.states.append(state)


class _AppRecoveryBase(_AudioStateTest):
    """Real DictationApp + real SdRecorder over a fake sounddevice."""

    def setUp(self):
        super().setUp()
        _appmod._TRANSCRIPT_LOG = Path(tempfile.mkdtemp()) / "transcripts.log"
        self._saved_close = audio_sd._CLOSE_TIMEOUT
        # A "wedged" close gives up fast — but not so fast that a healthy fake
        # close loses the race on a loaded machine and finishes late in the NEXT
        # test, where it would un-count that test's stream.
        audio_sd._CLOSE_TIMEOUT = _TEST_CLOSE_TIMEOUT
        self.relaunches: list[str] = []
        self.order: list[str] = []
        self.last_relaunch = None
        for p in (
            mock.patch.object(platform, "relaunch_self", side_effect=self._relaunch),
            mock.patch.object(platform, "last_relaunch",
                              side_effect=lambda: self.last_relaunch),
            mock.patch.object(platform, "hotkey_can_lose_release", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        audio_sd._CLOSE_TIMEOUT = self._saved_close
        super().tearDown()

    def _relaunch(self, reason=""):
        self.relaunches.append(reason)
        self.order.append("relaunch")

    def make_app(self, recorder, *, transcribe_fn=None, run_async=False,
                 injector=None):
        seen = _Seen(self.order)
        app = DictationApp(
            recorder.config, recorder=recorder,
            injector=injector or types.SimpleNamespace(type_text=seen.type_text,
                                                       backend="fake"),
            indicator=types.SimpleNamespace(set_state=seen.set_state),
            transcribe_fn=transcribe_fn or (lambda wav: "hello world"),
            notifier=lambda *a, **k: seen.notes.append(a), run_async=run_async)
        return app, seen

    @staticmethod
    def sd_recorder(**cfg):
        return SdRecorder(_cfg(api_key="sk-real", **cfg))

    @staticmethod
    def reinit_ok():
        mod = sys.modules["sounddevice"]
        mod._terminate = lambda: None
        mod._initialize = lambda: None


class SingleErrorClassTest(unittest.TestCase):
    def test_backends_share_one_audio_error(self):
        # app.py catches ba_ge.audio.AudioError; the mac/win backend used to raise
        # a different class of the same name, so every failure bypassed recovery.
        self.assertIs(audio_sd.AudioError, audio.AudioError)
        self.assertIs(_appmod.AudioError, audio.AudioError)


# ---------------------------------------------------------------- audio_sd ----

class PoisonedProcessTest(_AudioStateTest):
    def setUp(self):
        super().setUp()
        self._saved_close = audio_sd._CLOSE_TIMEOUT
        audio_sd._CLOSE_TIMEOUT = _TEST_CLOSE_TIMEOUT
        self.addCleanup(lambda: setattr(audio_sd, "_CLOSE_TIMEOUT", self._saved_close))

    def _open_failing_with(self, exc, **cfg):
        with _SdPatch() as sd:
            _AppRecoveryBase.reinit_ok()

            def boom(**kw):
                raise exc
            sd.RawInputStream = boom
            rec = SdRecorder(_cfg(**cfg))
            with self.assertRaises(audio.AudioError) as ctx:
                rec.start()
        return rec, str(ctx.exception)

    def test_wedged_close_poisons_every_recorder_in_the_process(self):
        with _SdPatch(hang_on_stop=True) as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            sd.opened[0].feed(LOUD)
            self.assertIsNotNone(rec.stop())  # the audio still comes back
        self.assertTrue(rec.stalled)
        # Settings swaps in a fresh recorder: it must not forget the fault.
        self.assertTrue(SdRecorder(_cfg()).stalled)
        self.assertIn("close", rec.stall_reason)
        self.assertTrue(audio_sd.needs_hard_exit())

    def test_missing_device_is_an_error_not_a_poisoned_process(self):
        rec, msg = self._open_failing_with(
            ValueError("No input device matching 'DJI Mic'"), audio_device="DJI Mic")
        self.assertIn("Settings", msg)
        self.assertFalse(rec.stalled)  # a relaunch can't plug the mic back in
        self.assertFalse(audio_sd.needs_hard_exit())

    def test_invalid_device_code_is_not_poison(self):
        rec, _ = self._open_failing_with(RuntimeError("Invalid device [PaErrorCode -9996]"))
        self.assertFalse(rec.stalled)

    def test_structured_portaudio_code_is_read_from_args(self):
        class PortAudioError(Exception):
            pass
        rec, _ = self._open_failing_with(PortAudioError("Device unavailable", -9985))
        self.assertFalse(rec.stalled)

    def test_no_default_input_device_is_not_poison(self):
        # A desktop Mac with its only mic unplugged: sounddevice asks about -1.
        rec, msg = self._open_failing_with(RuntimeError("Error querying device -1"))
        self.assertFalse(rec.stalled)
        self.assertIn("No microphone found", msg)

    def test_format_mismatch_is_not_poison(self):
        rec, msg = self._open_failing_with(
            RuntimeError("Invalid number of channels [PaErrorCode -9998]"))
        self.assertFalse(rec.stalled)
        self.assertIn("format", msg)

    def test_internal_error_after_reinit_poisons(self):
        rec, _ = self._open_failing_with(
            RuntimeError("Internal PortAudio error [PaErrorCode -9986]"))
        self.assertTrue(rec.stalled)

    def test_reinit_that_never_returns_poisons(self):
        with _SdPatch() as sd:
            def broken(**kw):
                raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
            sd.RawInputStream = broken
            with mock.patch.object(platform, "call_with_timeout",
                                   return_value=(False, None)):
                rec = SdRecorder(_cfg())
                with self.assertRaises(audio.AudioError):
                    rec.start()
        self.assertTrue(rec.stalled)
        self.assertIn("re-init", rec.stall_reason)

    def test_reinit_that_raises_poisons(self):
        """Terminated but not re-initialised: PortAudio's state is unknown."""
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None

            def init_fails():
                raise RuntimeError("CoreAudio not running yet")
            mod._initialize = init_fails
            sd.RawInputStream = lambda **kw: (_ for _ in ()).throw(
                ValueError("No input device matching 'Mic'"))
            rec = SdRecorder(_cfg(audio_device="Mic"))
            with self.assertRaises(audio.AudioError):
                rec.start()
        self.assertTrue(rec.stalled)

    def test_open_is_refused_once_poisoned(self):
        audio_sd.mark_poisoned("Settings refresh stalled")
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            with self.assertRaises(audio.AudioError):
                rec.start()
        self.assertEqual(sd.opened, [])  # never touched PortAudio

    def test_open_queued_behind_a_stalled_refresh_never_reaches_portaudio(self):
        """Settings' re-enumeration stalls while a press waits for the lock."""
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            waiting = threading.Event()
            result = {}
            audio_sd._pa_lock.acquire()  # the refresh, on the Qt thread
            try:
                def press():
                    waiting.set()
                    try:
                        rec.start()
                    except audio.AudioError as exc:
                        result["error"] = exc
                t = threading.Thread(target=press, daemon=True)
                t.start()
                waiting.wait(2)
                time.sleep(0.05)
                audio_sd.mark_poisoned("PortAudio re-enumeration did not finish")
            finally:
                audio_sd._pa_lock.release()
            t.join(2)
        self.assertIn("error", result)
        self.assertEqual(sd.opened, [])

    def test_open_that_finishes_after_the_app_gave_up_releases_the_mic(self):
        """A slow open completing in a process flagged meanwhile must not arm."""
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())

            def open_then_flagged(device, retry=False):
                _open_without_checks(rec, sd, device)
                audio_sd.mark_poisoned("microphone open did not return within 10s")

            rec._open_stream = open_then_flagged
            with self.assertRaises(audio.AudioError):
                rec.start()
        self.assertFalse(rec._armed)
        self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])  # mic off
        self.assertFalse(audio_sd.capture_active())

    def test_recording_that_received_no_audio_says_so(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg(min_duration=0.3))
            rec.start()
            rec._start -= 2.0  # held two seconds, but no buffer ever arrived
            with self.assertRaises(audio.AudioError) as ctx:
                rec.stop()
        self.assertIn("No audio", str(ctx.exception))
        self.assertFalse(rec.stalled)

    def test_dead_stream_is_not_reused_even_with_a_grace_period(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg(min_duration=0.3, idle_release=30))
            rec.start()
            rec._start -= 2.0
            with self.assertRaises(audio.AudioError):
                rec.stop()
            self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])
            rec.start()  # the next press opens a fresh stream
            self.assertEqual(len(sd.opened), 2)
            rec.close()

    def test_short_holds_with_no_audio_are_just_taps(self):
        with _SdPatch():
            rec = SdRecorder(_cfg(min_duration=0.3))
            rec.start()
            rec._start -= 1.0  # e.g. Bluetooth's first buffer still on its way
            self.assertIsNone(rec.stop())

    def test_close_never_blocks_on_a_stalled_call(self):
        with _SdPatch():
            rec = SdRecorder(_cfg())
            rec._lock.acquire()  # a start()/stop() wedged inside PortAudio
            try:
                self.assertFalse(_bounded(self, rec.close, timeout=5))
            finally:
                rec._lock.release()
        self.assertTrue(audio_sd.needs_hard_exit())

    def test_half_open_stream_close_is_bounded(self):
        """A stream that opened but failed to start can wedge on close too."""
        class HalfOpen:
            def __init__(self, **kw):
                pass

            def start(self):
                raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")

            def close(self):
                threading.Event().wait(10)

        with _SdPatch() as sd:
            sd.RawInputStream = lambda **kw: HalfOpen(**kw)
            rec = SdRecorder(_cfg())
            with self.assertRaises(audio.AudioError):
                _bounded(self, rec.start, timeout=5)
        self.assertIn("half-open", audio_sd.poisoned_reason())

    def test_slow_close_is_uncounted_when_it_finally_returns(self):
        release = threading.Event()
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            sd.opened[0].stop = lambda: release.wait(5)  # slow, not dead
            sd.opened[0].feed(LOUD)
            rec.stop()
            self.assertTrue(audio_sd.capture_active())  # abandoned: still counted
            release.set()
            self.assertTrue(_wait_until(lambda: not audio_sd.capture_active()))

    def test_idle_release_wedge_reports_the_stall(self):
        """With a grace period, nobody's recording when the close wedges."""
        stalls = []
        with _SdPatch(hang_on_stop=True) as sd:
            rec = SdRecorder(_cfg(idle_release=0.05))
            rec.on_stall = lambda: stalls.append(1)
            rec.start()
            sd.opened[0].feed(LOUD)
            rec.stop()
            self.assertTrue(_wait_until(lambda: stalls == [1]))
        self.assertTrue(rec.stalled)


class MicEnumerationTest(_AudioStateTest):
    """Settings' mic list re-initialises PortAudio on the Qt thread."""

    def _fake_sd(self, terminate=None):
        calls = []
        mod = sys.modules["sounddevice"]
        mod._terminate = terminate or (lambda: calls.append("terminate"))
        mod._initialize = lambda: calls.append("initialize")
        mod.query_devices = lambda *a, **k: (
            [{"name": "Mic", "max_input_channels": 1}] if not a else
            {"max_input_channels": 1})
        return calls

    def test_lists_inputs_after_a_refresh(self):
        with _SdPatch():
            calls = self._fake_sd()
            self.assertEqual(platform._sounddevice_inputs(), [("Mic", "Mic")])
        self.assertEqual(calls, ["terminate", "initialize"])

    def test_skipped_while_the_mic_is_being_opened(self):
        with _SdPatch():
            calls = self._fake_sd()
            held = threading.Event()
            done = threading.Event()

            def opener():  # another thread inside _open_stream
                with audio_sd._pa_lock:
                    held.set()
                    done.wait(2)
            t = threading.Thread(target=opener, daemon=True)
            t.start()
            held.wait(2)
            try:
                self.assertEqual(_bounded(self, platform._sounddevice_inputs), [])
            finally:
                done.set()
                t.join(2)
        self.assertEqual(calls, [])  # never touched PortAudio under the open

    def test_a_stalled_refresh_poisons_and_skips_the_query(self):
        with _SdPatch():
            self._fake_sd()
            with mock.patch.object(platform, "call_with_timeout",
                                   return_value=(False, None)):
                self.assertEqual(platform._sounddevice_inputs(), [])
        self.assertIn("re-enumeration", audio_sd.poisoned_reason())
        self.assertTrue(audio_sd._pa_lock.acquire(blocking=False))  # released
        audio_sd._pa_lock.release()

    def test_a_poisoned_process_does_not_reinitialise(self):
        audio_sd.mark_poisoned("close wedged")
        with _SdPatch():
            calls = self._fake_sd()
            self.assertEqual(platform._sounddevice_inputs(), [])
        self.assertEqual(calls, [])

    def test_a_failing_terminate_still_releases_the_lock(self):
        def boom():
            raise RuntimeError("PortAudio not initialized [PaErrorCode -10000]")
        with _SdPatch():
            self._fake_sd(terminate=boom)
            self.assertEqual(platform._sounddevice_inputs(), [])
        self.assertIsNotNone(audio_sd.poisoned_reason())
        self.assertTrue(audio_sd._pa_lock.acquire(blocking=False))
        audio_sd._pa_lock.release()


# --------------------------------------------------------------------- app ----

class RelaunchOnEveryPathTest(_AppRecoveryBase):
    """A wedged close must trigger the relaunch however the utterance ends —
    and always AFTER the paste (the real relaunch never returns)."""

    def _dictate(self, feed, **app_kw):
        with _SdPatch(hang_on_stop=True) as sd:
            app, seen = self.make_app(self.sd_recorder(min_duration=0.3), **app_kw)
            app.on_press()
            self.assertIs(app.state, State.RECORDING)
            if feed:
                sd.opened[0].feed(feed)
            app.on_release()
        return app, seen

    def test_normal_clip_pastes_first_then_relaunches(self):
        app, seen = self._dictate(LOUD)
        self.assertEqual(self.order, ["paste", "relaunch"])
        self.assertEqual(self.relaunches, ["audio"])
        self.assertIs(app.state, State.IDLE)

    def test_short_tap(self):
        self._dictate(b"\x10\x27" * 1600)  # 0.1 s < min_duration
        self.assertEqual(self.relaunches, ["audio"])

    def test_silent_clip(self):
        app, seen = self._dictate(QUIET)
        self.assertEqual(seen.typed, [])
        self.assertEqual(self.relaunches, ["audio"])

    def test_transcription_error(self):
        def boom(wav):
            raise TranscriptionError("api down")
        app, seen = self._dictate(LOUD, transcribe_fn=boom)
        self.assertIn(State.ERROR, seen.states)
        self.assertEqual(self.relaunches, ["audio"])

    def test_injection_error(self):
        def paste_fails(text):
            raise InjectionError("paste keystroke failed")
        injector = types.SimpleNamespace(type_text=paste_fails, backend="fake")
        app, seen = self._dictate(LOUD, injector=injector)
        self.assertEqual(self.relaunches, ["audio"])


class RelaunchPolicyTest(_AppRecoveryBase):
    def _poisoned_press(self):
        audio_sd.mark_poisoned("close wedged earlier")
        with _SdPatch() as sd:
            app, seen = self.make_app(self.sd_recorder())
            app.on_press()
        return app, seen, sd

    def test_press_on_a_poisoned_process_relaunches_without_opening(self):
        app, seen, sd = self._poisoned_press()
        self.assertEqual(sd.opened, [])  # never touched the broken audio layer
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.relaunches, ["audio"])

    def test_no_relaunch_loop(self):
        """Poisoned again right after an audio relaunch: advise, don't loop."""
        self.last_relaunch = {"time": time.time() - 5, "reason": "audio", "pid": 1}
        app, seen, _ = self._poisoned_press()
        self.assertEqual(self.relaunches, [])
        self.assertTrue(any("stuck" in n[0] for n in seen.notes))
        self.assertIs(app.state, State.IDLE)

    def test_an_old_audio_relaunch_does_not_block(self):
        self.last_relaunch = {"time": time.time() - 120, "reason": "audio", "pid": 1}
        self._poisoned_press()
        self.assertEqual(self.relaunches, ["audio"])

    def test_a_permissions_restart_does_not_block(self):
        self.last_relaunch = {"time": time.time() - 5, "reason": "", "pid": 1}
        self._poisoned_press()
        self.assertEqual(self.relaunches, ["audio"])

    def test_our_own_relaunch_record_does_not_block(self):
        """Written by this process a moment ago, on its way out."""
        self.last_relaunch = {"time": time.time(), "reason": "audio", "pid": os.getpid()}
        self._poisoned_press()
        self.assertEqual(self.relaunches, ["audio"])

    def test_only_one_relaunch_however_many_threads_notice(self):
        app, seen, _ = self._poisoned_press()
        app._recover_if_audio_stalled()
        app._busy_timed_out(app._gen)
        self.assertEqual(self.relaunches, ["audio"])

    def test_settings_swap_recovers(self):
        with _SdPatch():
            app, seen = self.make_app(self.sd_recorder())
            audio_sd.mark_poisoned("the old recorder's close wedged")
            with mock.patch.object(_appmod, "load_config",
                                   return_value=_cfg(api_key="sk-real")), \
                    mock.patch.object(platform, "make_recorder",
                                      side_effect=lambda c: SdRecorder(c)), \
                    mock.patch.object(platform, "make_injector"):
                app.reload_config()
        self.assertEqual(self.relaunches, ["audio"])


class StartFailureTest(_AppRecoveryBase):
    def test_failed_open_is_reported_and_relaunches(self):
        """The regression: this used to escape as an uncaught exception."""
        with _SdPatch() as sd:
            self.reinit_ok()
            sd.RawInputStream = lambda **kw: (_ for _ in ()).throw(
                RuntimeError("Internal PortAudio error [PaErrorCode -9986]"))
            app, seen = self.make_app(self.sd_recorder())
            app.on_press()
        self.assertIs(app.state, State.IDLE)     # not stuck in RECORDING
        self.assertIn(State.ERROR, seen.states)  # the user sees it failed
        self.assertTrue(seen.notes)
        self.assertEqual(self.relaunches, ["audio"])

    def test_missing_mic_is_reported_without_a_relaunch(self):
        with _SdPatch() as sd:
            self.reinit_ok()
            sd.RawInputStream = lambda **kw: (_ for _ in ()).throw(
                ValueError("No input device matching 'DJI Mic'"))
            app, seen = self.make_app(self.sd_recorder(audio_device="DJI Mic"))
            app.on_press()
        self.assertIs(app.state, State.IDLE)
        self.assertIn(State.ERROR, seen.states)
        self.assertEqual(self.relaunches, [])

    def test_any_exception_from_start_resets_to_idle(self):
        starts = []

        class Exploding:
            config = _cfg(api_key="sk-real")

            def start(self):
                starts.append(1)
                raise OSError("device busy")

            def stop(self):
                return None

        app, seen = self.make_app(Exploding())
        app.on_press()
        self.assertIs(app.state, State.IDLE)
        app.on_press()  # and the next press is not swallowed
        self.assertEqual(len(starts), 2)
        self.assertEqual(seen.states.count(State.ERROR), 2)

    def test_open_that_never_returns_relaunches(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Hanging(SdRecorder):
            def _open_stream(self, device, retry=False):
                gate.wait(10)  # CoreAudio never answers (within the test)

        with _SdPatch(), mock.patch.object(_appmod, "_START_TIMEOUT", 0.2):
            app, seen = self.make_app(Hanging(_cfg(api_key="sk-real")))
            _bounded(self, app.on_press)
        self.assertIs(app.state, State.IDLE)
        self.assertTrue(audio_sd.poisoned_reason())
        self.assertEqual(self.relaunches, ["audio"])

    def test_open_finishing_late_under_the_loop_guard_releases_the_mic(self):
        """The guard keeps this process alive, so a late open must not stay armed."""
        self.last_relaunch = {"time": time.time() - 5, "reason": "audio", "pid": 1}
        finish = threading.Event()
        self.addCleanup(finish.set)
        with _SdPatch() as sd:
            class Slow(SdRecorder):
                def _open_stream(self, device, retry=False):
                    finish.wait(5)  # e.g. Bluetooth switching profile
                    _open_without_checks(self, sd, device)  # already inside PortAudio

            rec = Slow(_cfg(api_key="sk-real"))
            with mock.patch.object(_appmod, "_START_TIMEOUT", 0.2):
                app, seen = self.make_app(rec)
                app.on_press()
            self.assertEqual(self.relaunches, [])  # guarded
            finish.set()
            self.assertTrue(_wait_until(lambda: sd.opened and
                                        sd.opened[0].calls[-1:] == ["close"]))
        self.assertFalse(rec._armed)
        self.assertIs(app.state, State.IDLE)

    def test_stop_that_never_returns_marks_the_backend(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Recorder:
            config = _cfg(api_key="sk-real")
            stalled = False

            def start(self):
                pass

            def stop(self):
                gate.wait(10)

            def mark_stalled(self, reason):
                self.stalled = True

        rec = Recorder()
        with mock.patch.object(_appmod, "_STOP_TIMEOUT", 0.1):
            app, seen = self.make_app(rec)
            app.on_press()
            _bounded(self, app.on_release)
        self.assertTrue(rec.stalled)
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.relaunches, ["audio"])

    def test_backend_without_a_stall_flag_is_started_directly(self):
        """Linux's arecord: an abandoned start would leave arecord running unseen."""
        threads = []

        class Arecord:
            config = _cfg(api_key="sk-real")

            def start(self):
                threads.append(threading.current_thread())

            def stop(self):
                return LOUD_WAV

        app, seen = self.make_app(Arecord())
        app.on_press()
        self.assertEqual(threads, [threading.current_thread()])


class HotkeyWorkerTest(_AppRecoveryBase):
    """The pynput (event-tap) thread must never wait on the microphone."""

    def _slow_recorder(self, gate, calls):
        class Slow:
            config = _cfg(api_key="sk-real")

            def start(self):
                gate.wait(5)  # e.g. a Bluetooth headset switching profile
                calls.append("start")

            def stop(self):
                calls.append("stop")
                return LOUD_WAV
        return Slow()

    def test_press_returns_at_once_and_release_waits_for_the_start(self):
        gate, calls = threading.Event(), []
        self.addCleanup(gate.set)
        app, seen = self.make_app(self._slow_recorder(gate, calls), run_async=True)
        began = time.monotonic()
        app.on_press()
        app.on_release()  # arrives while the start is still in progress
        self.assertLess(time.monotonic() - began, 0.5)
        gate.set()
        self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))
        self.assertEqual(calls, ["start", "stop"])  # never out of order
        self.assertTrue(_wait_until(lambda: app.state is State.IDLE))

    def test_toggle_taps_stay_in_order(self):
        gate, calls = threading.Event(), []
        self.addCleanup(gate.set)
        rec = self._slow_recorder(gate, calls)
        rec.config = _cfg(api_key="sk-real", hotkey_mode="toggle")
        app, seen = self.make_app(rec, run_async=True)
        for _ in range(2):  # tap on, tap off — both during the slow start
            app.on_press()
            app.on_release()
        gate.set()
        self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))
        self.assertEqual(calls, ["start", "stop"])


def _shown_recording(seen):
    """How many recordings have been shown: the tray turns red only once the mic
    is actually open, whereas the state flips to RECORDING before the open."""
    return seen.states.count(State.RECORDING)


class BusyWatchdogTest(_AppRecoveryBase):
    """A slow transcript must survive the watchdog, and never land in (or clobber)
    the next dictation."""

    def test_watchdog_waits_for_the_transcript_before_relaunching(self):
        transcribing = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)

        def slow(wav):
            transcribing.set()
            finish.wait(5)
            return "the precious dictation"

        with _SdPatch(hang_on_stop=True) as sd, \
                mock.patch.object(_appmod, "_BUSY_TIMEOUT", 0.2):
            app, seen = self.make_app(self.sd_recorder(), transcribe_fn=slow,
                                      run_async=True)
            app.on_press()
            self.assertTrue(_wait_until(lambda: _shown_recording(seen) == 1))
            sd.opened[0].feed(LOUD)
            app.on_release()
            self.assertTrue(transcribing.wait(3))
            self.assertTrue(_wait_until(lambda: app.state is State.IDLE))  # watchdog
            time.sleep(0.1)
            self.assertEqual(self.relaunches, [])  # not while the text is in flight
            app.on_press()  # "Try again"
            time.sleep(0.2)
            self.assertEqual(self.relaunches, [])
            finish.set()
            self.assertTrue(_wait_until(lambda: self.relaunches == ["audio"]))
        self.assertEqual(seen.typed, ["the precious dictation"])
        self.assertEqual(self.order, ["paste", "relaunch"])

    def test_late_transcript_does_not_clobber_the_next_recording(self):
        finish = threading.Event()
        self.addCleanup(finish.set)
        texts = iter(["late A", "B text"])

        def transcribe(wav):
            text = next(texts)
            if text == "late A":
                finish.wait(5)
            return text

        with _SdPatch() as sd, mock.patch.object(_appmod, "_BUSY_TIMEOUT", 0.2):
            app, seen = self.make_app(self.sd_recorder(), transcribe_fn=transcribe,
                                      run_async=True)
            app.on_press()
            self.assertTrue(_wait_until(lambda: _shown_recording(seen) == 1))
            sd.opened[0].feed(LOUD)
            app.on_release()
            self.assertTrue(_wait_until(lambda: any(
                "timed out" in n[1] for n in seen.notes if len(n) > 1)))
            app.on_press()  # recording B starts while A is still out
            self.assertTrue(_wait_until(lambda: _shown_recording(seen) == 2))
            finish.set()  # A's transcript lands mid-B
            time.sleep(0.2)
            self.assertIs(app.state, State.RECORDING)  # B not clobbered
            self.assertEqual(seen.typed, [])            # A not pasted into B
            sd.opened[1].feed(LOUD)
            app.on_release()
            self.assertTrue(_wait_until(lambda: seen.typed == ["B text"]))
            self.assertTrue(_wait_until(lambda: app.state is State.IDLE))
        self.assertEqual(sd.opened[1].calls, ["start", "stop", "close"])  # mic off

    def test_late_transcript_still_pastes_if_nothing_else_started(self):
        finish = threading.Event()
        self.addCleanup(finish.set)

        def slow(wav):
            finish.wait(5)
            return "late but wanted"

        with _SdPatch() as sd, mock.patch.object(_appmod, "_BUSY_TIMEOUT", 0.2):
            app, seen = self.make_app(self.sd_recorder(), transcribe_fn=slow,
                                      run_async=True)
            app.on_press()
            self.assertTrue(_wait_until(lambda: _shown_recording(seen) == 1))
            sd.opened[0].feed(LOUD)
            app.on_release()
            self.assertTrue(_wait_until(lambda: app.state is State.IDLE))
            finish.set()
            self.assertTrue(_wait_until(lambda: seen.typed == ["late but wanted"]))


class RecordingLimitTest(_AppRecoveryBase):
    def _record(self, mode="hold", key_down=False, limit=0.2):
        hotkey = mock.Mock()
        hotkey.key_is_down.return_value = key_down
        self.enterContext(mock.patch.object(_appmod, "_MAX_RECORDING", limit))
        sd = self.enterContext(_SdPatch())
        app, seen = self.make_app(self.sd_recorder(hotkey_mode=mode))
        app._hotkey = hotkey
        app.on_press()
        sd.opened[0].feed(LOUD)
        return app, seen, sd, hotkey

    def test_a_hold_whose_key_up_never_arrives_is_ended(self):
        app, seen, sd, hotkey = self._record()
        self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))
        self.assertTrue(_wait_until(lambda: app.state is State.IDLE))
        hotkey.reset.assert_called_once()  # next press isn't swallowed
        self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])  # mic off

    def test_a_key_still_physically_held_keeps_recording(self):
        app, seen, sd, hotkey = self._record(key_down=True)
        time.sleep(0.5)  # two limit periods
        self.assertIs(app.state, State.RECORDING)
        hotkey.reset.assert_not_called()
        app.on_release()
        self.assertEqual(seen.typed, ["hello world"])

    def test_unknown_key_state_still_ends_it(self):
        app, seen, sd, hotkey = self._record(key_down=None)
        self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))

    def test_toggle_mode_has_no_limit(self):
        """Otherwise the user's "stop" tap would start a new, unattended recording."""
        app, seen, sd, hotkey = self._record(mode="toggle")
        time.sleep(0.4)
        self.assertIs(app.state, State.RECORDING)
        app.on_press()  # the stop tap stops
        self.assertEqual(seen.typed, ["hello world"])
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(len(sd.opened), 1)

    def test_no_limit_where_key_events_cant_be_lost(self):
        with mock.patch.object(platform, "hotkey_can_lose_release", return_value=False):
            app, seen, sd, hotkey = self._record()
            self.assertIsNone(app._record_timer)
            time.sleep(0.4)
            self.assertIs(app.state, State.RECORDING)

    def test_limit_is_cancelled_by_a_normal_release(self):
        app, seen, sd, hotkey = self._record(limit=0.3)
        time.sleep(0.15)
        app.on_release()
        self.assertIsNone(app._record_timer)
        app.on_press()  # a new recording: the FIRST deadline falls inside it...
        time.sleep(0.2)
        self.assertIs(app.state, State.RECORDING)  # ...and must not end it
        hotkey.reset.assert_not_called()


class ShutdownTest(_AppRecoveryBase):
    def test_shutdown_runs_once(self):
        with _SdPatch():
            rec = self.sd_recorder()
            app, seen = self.make_app(rec)
            with mock.patch.object(_appmod, "_close_recorder") as close:
                app._shutdown()  # Quit
                app._shutdown()  # run()'s finally
        close.assert_called_once_with(rec)


class MainExitTest(unittest.TestCase):
    """main() must not let interpreter teardown hang on a wedged stream."""

    def _main(self, wedged, raises=False):
        app = mock.Mock()
        if raises:
            app.run.side_effect = RuntimeError("boom")
        exits = []
        with mock.patch.object(sys, "argv", ["ba-ge"]), \
                mock.patch.object(_appmod, "_acquire_single_instance", return_value=True), \
                mock.patch.object(_appmod, "ensure_config_file"), \
                mock.patch.object(_appmod, "load_config", return_value=_cfg()), \
                mock.patch.object(_appmod, "_add_file_log"), \
                mock.patch.object(_appmod.logging, "basicConfig"), \
                mock.patch.object(_appmod, "_log_startup"), \
                mock.patch.object(_appmod, "_install_stack_dump"), \
                mock.patch.object(_appmod, "DictationApp", return_value=app), \
                mock.patch.object(platform, "audio_needs_hard_exit", return_value=wedged), \
                mock.patch.object(platform, "hard_exit", side_effect=exits.append), \
                mock.patch.object(platform, "arm_exit_watchdog") as watchdog, \
                mock.patch.object(platform, "IS_LINUX", False):
            if raises:
                with self.assertRaises(RuntimeError):
                    _appmod.main()
            else:
                _appmod.main()
        return exits, watchdog

    def test_wedged_audio_exits_hard(self):
        exits, _ = self._main(wedged=True)
        self.assertEqual(exits, [0])

    def test_healthy_audio_exits_normally_behind_a_watchdog(self):
        exits, watchdog = self._main(wedged=False)
        self.assertEqual(exits, [])
        watchdog.assert_called_once()

    def test_a_crash_is_not_reported_as_success(self):
        exits, _ = self._main(wedged=True, raises=True)
        self.assertEqual(exits, [1])

    def test_open_stream_alone_needs_a_hard_exit(self):
        with mock.patch.object(audio_sd, "_open_streams", 1):
            self.assertTrue(audio_sd.needs_hard_exit())


class ExitWatchdogTest(unittest.TestCase):
    def test_fires_during_a_hang_in_interpreter_teardown(self):
        code = ("import atexit, time\n"
                "from ba_ge import platform\n"
                "atexit.register(lambda: time.sleep(20))\n"
                "platform.arm_exit_watchdog(0.5)\n")
        began = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                              capture_output=True, timeout=15)
        self.assertLess(time.monotonic() - began, 10)
        self.assertNotEqual(proc.returncode, 0)


class SingleInstanceTest(unittest.TestCase):
    def test_a_relaunched_instance_waits_for_its_predecessor(self):
        answers = iter([False, False, True])
        with mock.patch.object(_appmod.singleton, "acquire",
                               side_effect=lambda name: next(answers)), \
                mock.patch.dict(os.environ, {"BAGE_RELAUNCHED_FROM": "123"}):
            self.assertTrue(_appmod._acquire_single_instance())
            self.assertNotIn("BAGE_RELAUNCHED_FROM", os.environ)

    def test_a_second_launch_does_not_wait(self):
        env = {k: v for k, v in os.environ.items() if k != "BAGE_RELAUNCHED_FROM"}
        with mock.patch.object(_appmod.singleton, "acquire", return_value=False), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(_appmod._acquire_single_instance())


# ---------------------------------------------------------------- platform ----

class RelaunchCommandTest(unittest.TestCase):
    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "relaunch.json"
        p = mock.patch.object(platform, "_relaunch_record_path",
                              return_value=self.record)
        p.start()
        self.addCleanup(p.stop)

    def _relaunch(self, executable, orig_argv, frozen=False, argv=None):
        popen = mock.Mock()
        exits = []
        with mock.patch("subprocess.Popen", popen), \
                mock.patch.object(platform, "hard_exit", side_effect=exits.append), \
                mock.patch.object(platform, "IS_WIN", False), \
                mock.patch.object(sys, "executable", executable), \
                mock.patch.object(sys, "argv", argv or orig_argv), \
                mock.patch.object(sys, "orig_argv", orig_argv, create=True), \
                mock.patch.object(sys, "frozen", frozen, create=True):
            platform.relaunch_self("audio")
        self.assertEqual(exits, [0])
        args, kwargs = popen.call_args
        self.assertEqual(args[0][:2], ["/bin/sh", "-c"])
        self.assertEqual(kwargs["env"]["BAGE_RELAUNCHED_FROM"], str(os.getpid()))
        return args[0][2]

    def test_source_launch_keeps_dash_m_and_waits_for_this_pid(self):
        script = self._relaunch("/venv/bin/python", ["python", "-m", "ba_ge"])
        self.assertIn(f"kill -0 {os.getpid()}", script)
        self.assertTrue(script.endswith("exec /venv/bin/python -m ba_ge"), script)

    def test_app_bundle_reopens_the_bundle(self):
        exe = "/Applications/Ba-Ge.app/Contents/MacOS/Ba-Ge"
        script = self._relaunch(exe, [exe], frozen=True)
        self.assertTrue(script.endswith("exec open -n /Applications/Ba-Ge.app"), script)

    def test_source_run_under_python_app_is_not_mistaken_for_the_bundle(self):
        exe = ("/Library/Frameworks/Python.framework/Versions/3.12/Resources/"
               "Python.app/Contents/MacOS/Python")
        script = self._relaunch(exe, ["python3", "-m", "ba_ge"])
        self.assertTrue(script.endswith("-m ba_ge"), script)
        self.assertNotIn("open -n", script)

    def test_frozen_binary_outside_a_bundle_keeps_its_args(self):
        script = self._relaunch("/opt/ba-ge/ba-ge", ["/opt/ba-ge/ba-ge"], frozen=True,
                                argv=["/opt/ba-ge/ba-ge", "--flag"])
        self.assertTrue(script.endswith("exec /opt/ba-ge/ba-ge --flag"), script)

    def test_relaunch_is_recorded_for_the_loop_guard(self):
        self._relaunch("/venv/bin/python", ["python", "-m", "ba_ge"])
        last = platform.last_relaunch()
        self.assertEqual(last["reason"], "audio")
        self.assertEqual(last["pid"], os.getpid())
        self.assertLess(time.time() - last["time"], 60)

    def test_no_record_reads_as_none(self):
        self.assertIsNone(platform.last_relaunch())


class HardExitTest(_AudioStateTest):
    def test_only_a_wedged_audio_layer_needs_a_hard_exit(self):
        self.assertFalse(platform.audio_needs_hard_exit())
        audio_sd.mark_poisoned("close wedged")
        self.assertTrue(platform.audio_needs_hard_exit())


class TapGuardTest(unittest.TestCase):
    """macOS disables a tap that was too slow and says so once; pynput ignores it."""

    def setUp(self):
        self.enabled = []
        self.invalidated = []
        quartz = types.ModuleType("Quartz")
        quartz.CGEventTapEnable = lambda tap, on: self.enabled.append((tap, on))
        quartz.CFMachPortInvalidate = lambda tap: self.invalidated.append(tap)
        for name, module in (("Quartz", quartz), ("CoreFoundation", None)):
            saved = sys.modules.get(name, None)
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
            self.addCleanup(self._restore, name, saved)

        class FakePynputListener:
            def __init__(self, on_press=None, on_release=None):
                self.handled = []

            def _create_event_tap(self):
                return "TAP"

            def _handler(self, proxy, event_type, event, refcon):
                self.handled.append(event_type)

            def _run(self):
                self._create_event_tap()

        self.cls = platform._tap_guarded(FakePynputListener)

    @staticmethod
    def _restore(name, saved):
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved

    def test_disabled_tap_is_reenabled_and_reported(self):
        restored = []
        listener = self.cls(on_press=None, on_release=None,
                            on_tap_restored=lambda: restored.append(1))
        listener._create_event_tap()
        for kind in (platform._TAP_DISABLED_BY_TIMEOUT,
                     platform._TAP_DISABLED_BY_USER_INPUT):
            self.assertIsNone(listener._handler(None, kind, None, None))
        self.assertEqual(self.enabled, [("TAP", True), ("TAP", True)])
        self.assertEqual(restored, [1, 1])
        self.assertEqual(listener.handled, [])  # never reached pynput's handler

    def test_key_events_pass_through(self):
        listener = self.cls(on_press=None, on_release=None)
        listener._create_event_tap()
        listener._handler(None, 10, object(), None)  # kCGEventKeyDown
        self.assertEqual(listener.handled, [10])
        self.assertEqual(self.enabled, [])

    def test_tap_is_released_when_the_listener_ends(self):
        listener = self.cls(on_press=None, on_release=None)
        listener._run()
        self.assertEqual(self.enabled, [("TAP", False)])
        self.assertEqual(self.invalidated, ["TAP"])
        self.assertIsNone(listener._bage_tap)

    def test_key_state_is_unknown_off_macos(self):
        with mock.patch.object(platform, "IS_MAC", False):
            self.assertIsNone(platform.key_is_down(0x65))
        self.assertIsNone(platform.key_is_down(None))


# ------------------------------------------------------------------ hotkey ----

_MISSING = object()


def _hotkey_module_with_stub_pynput(testcase):
    """Import ba_ge.hotkey against a stub pynput (the real one needs a display)."""
    class KeyCode:
        def __init__(self, char=None, vk=None):
            self.char, self.vk = char, vk

        @classmethod
        def from_char(cls, char):
            return cls(char=char)

        def __eq__(self, other):
            return isinstance(other, KeyCode) and (self.char, self.vk) == (
                other.char, other.vk)

        __hash__ = object.__hash__

    class Key:
        pass

    for n in range(1, 21):
        setattr(Key, f"f{n}", types.SimpleNamespace(value=KeyCode(vk=0x60 + n)))

    kb = types.ModuleType("pynput.keyboard")
    kb.Key, kb.KeyCode, kb.Listener = Key, KeyCode, object
    pyn = types.ModuleType("pynput")
    pyn.keyboard = kb
    names = ("pynput", "pynput.keyboard", "ba_ge.hotkey")
    saved = {n: sys.modules.get(n) for n in names}
    saved_attr = getattr(ba_ge, "hotkey", _MISSING)

    def restore():
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
        # import_module also set the package attribute; put that back too, or a
        # later `from ba_ge import hotkey` would get the stub-backed module.
        if saved_attr is _MISSING:
            if hasattr(ba_ge, "hotkey"):
                delattr(ba_ge, "hotkey")
        else:
            ba_ge.hotkey = saved_attr

    testcase.addCleanup(restore)
    sys.modules["pynput"], sys.modules["pynput.keyboard"] = pyn, kb
    sys.modules.pop("ba_ge.hotkey", None)
    return importlib.import_module("ba_ge.hotkey")


class LostKeyUpTest(unittest.TestCase):
    """After macOS re-enables the tap, a key-up that was lost must be recovered."""

    def setUp(self):
        self.hotkey = _hotkey_module_with_stub_pynput(self)
        self.stops = threading.Event()
        self.listener = self.hotkey.HotkeyListener(
            "f9", on_start=lambda: None, on_stop=self.stops.set)
        self.f9 = self.hotkey.resolve_key("f9")

    def test_key_physically_up_ends_the_hold(self):
        self.listener._on_press(self.f9)
        with mock.patch.object(platform, "key_is_down", return_value=False) as down:
            self.listener._on_tap_restored()
        down.assert_called_once_with(0x69)  # F9's keycode, from the press
        self.assertTrue(self.stops.wait(2))

    def test_key_still_down_keeps_recording(self):
        self.listener._on_press(self.f9)
        with mock.patch.object(platform, "key_is_down", return_value=True):
            self.listener._on_tap_restored()
        self.assertFalse(self.stops.wait(0.2))

    def test_unknown_key_state_changes_nothing(self):
        self.listener._on_press(self.f9)
        with mock.patch.object(platform, "key_is_down", return_value=None):
            self.listener._on_tap_restored()
        self.assertFalse(self.stops.wait(0.2))

    def test_reset_forgets_a_hold_so_the_next_press_starts(self):
        starts = []
        listener = self.hotkey.HotkeyListener(
            "f9", on_start=lambda: starts.append(1), on_stop=lambda: None)
        listener._on_press(self.f9)
        listener.reset()           # app ended the recording itself
        listener._on_press(self.f9)
        self.assertEqual(starts, [1, 1])

    def test_key_is_down_asks_about_the_hotkey(self):
        with mock.patch.object(platform, "key_is_down", return_value=True) as down:
            self.assertTrue(self.listener.key_is_down())
        down.assert_called_once_with(0x69)

    def test_listener_is_built_with_the_tap_restore_hook(self):
        fake = mock.Mock()
        with mock.patch.object(platform, "make_keyboard_listener",
                               return_value=fake) as make, \
                mock.patch.object(platform, "prewarm_macos_input_source"):
            self.listener.start()
        self.assertEqual(make.call_args.kwargs["on_tap_restored"],
                         self.listener._on_tap_restored)
        fake.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
