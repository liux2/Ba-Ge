"""Recovery from a dead audio layer and from lost hotkey events (see audio_sd.py).

The macOS failure these pin down: dictation works for a while, then a CoreAudio
close deadlocks (or an open fails after sleep). The abandoned stream keeps the OS
mic indicator lit, and every later press used to fail SILENTLY — the backend
raised its own ``AudioError`` class that app.py never caught, and the relaunch
only ran after a successful paste. Separately, a key-up lost while macOS had the
event tap disabled left the app recording forever.

Runs on any OS: PortAudio, pynput and Quartz are faked.
"""

import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from ba_ge import app as _appmod
from ba_ge import audio, audio_sd, platform
from ba_ge.app import DictationApp
from ba_ge.audio_sd import SdRecorder
from ba_ge.state import State
from ba_ge.transcribe import TranscriptionError
from tests.test_audio_sd import _AudioStateTest, _cfg, _SdPatch, _wait_until

LOUD = b"\x10\x27" * 16000   # 1 s of loud 16 kHz mono
QUIET = b"\x00\x00" * 16000  # 1 s of digital silence


class _Recorder:
    """Records what the app does with the transcript/indicator/notifications."""

    def __init__(self):
        self.typed: list[str] = []
        self.states: list[State] = []
        self.notes: list[tuple] = []
        self.relaunches: list[str] = []
        self.order: list[str] = []

    def type_text(self, text):
        self.typed.append(text)
        self.order.append("paste")

    def set_state(self, state):
        self.states.append(state)


def _make_app(recorder, *, transcribe_fn=None, run_async=False, config=None):
    seen = _Recorder()
    injector = types.SimpleNamespace(type_text=seen.type_text, backend="fake")
    indicator = types.SimpleNamespace(set_state=seen.set_state)
    app = DictationApp(
        config or recorder.config, recorder=recorder, injector=injector,
        indicator=indicator,
        transcribe_fn=transcribe_fn or (lambda wav: "hello world"),
        notifier=lambda *a, **k: seen.notes.append(a), run_async=run_async)
    return app, seen


class _AppRecoveryBase(_AudioStateTest):
    """Real DictationApp + real SdRecorder over a fake sounddevice."""

    def setUp(self):
        super().setUp()
        _appmod._TRANSCRIPT_LOG = Path(tempfile.mkdtemp()) / "transcripts.log"
        self._saved_close = audio_sd._CLOSE_TIMEOUT
        audio_sd._CLOSE_TIMEOUT = 0.1  # a "wedged" close gives up fast in tests
        self.relaunches: list[str] = []
        self.order: list[str] = []
        patches = [
            mock.patch.object(platform, "relaunch_self", side_effect=self._relaunch),
            mock.patch.object(platform, "last_relaunch", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        audio_sd._CLOSE_TIMEOUT = self._saved_close
        super().tearDown()

    def _relaunch(self, reason=""):
        self.relaunches.append(reason)
        self.order.append("relaunch")


class SingleErrorClassTest(unittest.TestCase):
    def test_backends_share_one_audio_error(self):
        # app.py catches ba_ge.audio.AudioError; the mac/win backend used to raise
        # a different class of the same name, so every failure bypassed recovery.
        self.assertIs(audio_sd.AudioError, audio.AudioError)
        self.assertIs(_appmod.AudioError, audio.AudioError)


class PoisonedProcessTest(_AudioStateTest):
    def test_wedged_close_poisons_every_recorder_in_the_process(self):
        saved, audio_sd._CLOSE_TIMEOUT = audio_sd._CLOSE_TIMEOUT, 0.1
        try:
            with _SdPatch(hang_on_stop=True) as sd:
                rec = SdRecorder(_cfg())
                rec.start()
                sd.opened[0].feed(LOUD)
                self.assertIsNotNone(rec.stop())  # the audio still comes back
        finally:
            audio_sd._CLOSE_TIMEOUT = saved
        self.assertTrue(rec.stalled)
        # Settings swaps in a fresh recorder: it must not forget the fault.
        self.assertTrue(SdRecorder(_cfg()).stalled)
        self.assertIn("close", rec.stall_reason)
        self.assertTrue(audio_sd.needs_hard_exit())

    def test_missing_device_is_an_error_not_a_poisoned_process(self):
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None
            mod._initialize = lambda: None

            def gone(**kw):
                raise ValueError("No input device matching 'DJI Mic'")
            sd.RawInputStream = gone
            rec = SdRecorder(_cfg(audio_device="DJI Mic"))
            with self.assertRaises(audio.AudioError) as ctx:
                rec.start()
        self.assertIn("Settings", str(ctx.exception))
        self.assertFalse(rec.stalled)  # a relaunch can't plug the mic back in
        self.assertFalse(audio_sd.needs_hard_exit())

    def test_invalid_device_code_is_not_poison_either(self):
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None
            mod._initialize = lambda: None

            def invalid(**kw):
                raise RuntimeError("Invalid device [PaErrorCode -9996]")
            sd.RawInputStream = invalid
            rec = SdRecorder(_cfg())
            with self.assertRaises(audio.AudioError):
                rec.start()
        self.assertFalse(rec.stalled)

    def test_internal_error_after_reinit_poisons(self):
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None
            mod._initialize = lambda: None

            def broken(**kw):
                raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
            sd.RawInputStream = broken
            rec = SdRecorder(_cfg())
            with self.assertRaises(audio.AudioError):
                rec.start()
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

    def test_recording_that_received_no_audio_says_so(self):
        with _SdPatch():
            rec = SdRecorder(_cfg(min_duration=0.3))
            rec.start()
            rec._start -= 1.0  # held a full second, but no buffer ever arrived
            with self.assertRaises(audio.AudioError) as ctx:
                rec.stop()
        self.assertIn("No audio", str(ctx.exception))
        self.assertFalse(rec.stalled)

    def test_quick_tap_with_no_audio_is_still_just_a_tap(self):
        with _SdPatch():
            rec = SdRecorder(_cfg(min_duration=0.3))
            rec.start()
            self.assertIsNone(rec.stop())

    def test_close_never_blocks_on_a_stalled_call(self):
        saved, audio_sd._CLOSE_TIMEOUT = audio_sd._CLOSE_TIMEOUT, 0.1
        try:
            with _SdPatch():
                rec = SdRecorder(_cfg())
                rec._lock.acquire()  # a start()/stop() wedged inside PortAudio
                try:
                    began = time.monotonic()
                    self.assertFalse(rec.close())
                    self.assertLess(time.monotonic() - began, 2.0)
                finally:
                    rec._lock.release()
        finally:
            audio_sd._CLOSE_TIMEOUT = saved
        self.assertTrue(audio_sd.needs_hard_exit())

    def test_slow_close_is_uncounted_when_it_finally_returns(self):
        release = threading.Event()
        saved, audio_sd._CLOSE_TIMEOUT = audio_sd._CLOSE_TIMEOUT, 0.1
        try:
            with _SdPatch() as sd:
                rec = SdRecorder(_cfg())
                rec.start()
                stream = sd.opened[0]
                stream.stop = lambda: release.wait(5)  # slow, not dead
                sd.opened[0].feed(LOUD)
                rec.stop()
                self.assertTrue(audio_sd.capture_active())  # abandoned: still counted
                release.set()
                self.assertTrue(_wait_until(lambda: not audio_sd.capture_active()))
        finally:
            audio_sd._CLOSE_TIMEOUT = saved


class RelaunchOnEveryPathTest(_AppRecoveryBase):
    """A wedged close must trigger the relaunch however the utterance ends."""

    def _dictate(self, feed, **app_kw):
        with _SdPatch(hang_on_stop=True) as sd:
            rec = SdRecorder(_cfg(api_key="sk-real", min_duration=0.3))
            app, seen = _make_app(rec, **app_kw)
            app.on_press()
            self.assertIs(app.state, State.RECORDING)
            if feed:
                sd.opened[0].feed(feed)
            app.on_release()
        return app, seen

    def test_normal_clip_pastes_first_then_relaunches(self):
        app, seen = self._dictate(LOUD)
        self.assertEqual(seen.typed, ["hello world"])
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


class StartFailureTest(_AppRecoveryBase):
    def test_failed_open_is_reported_and_relaunches(self):
        """The regression: this used to escape as an uncaught exception."""
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None
            mod._initialize = lambda: None

            def broken(**kw):
                raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
            sd.RawInputStream = broken
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real")))
            app.on_press()
        self.assertIs(app.state, State.IDLE)     # not stuck in RECORDING
        self.assertIn(State.ERROR, seen.states)  # the user sees it failed
        self.assertTrue(seen.notes)
        self.assertEqual(self.relaunches, ["audio"])

    def test_missing_mic_is_reported_without_a_relaunch(self):
        with _SdPatch() as sd:
            mod = sys.modules["sounddevice"]
            mod._terminate = lambda: None
            mod._initialize = lambda: None

            def gone(**kw):
                raise ValueError("No input device matching 'DJI Mic'")
            sd.RawInputStream = gone
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real",
                                                  audio_device="DJI Mic")))
            app.on_press()
        self.assertIs(app.state, State.IDLE)
        self.assertIn(State.ERROR, seen.states)
        self.assertEqual(self.relaunches, [])

    def test_any_exception_from_start_resets_to_idle(self):
        class Exploding:
            config = _cfg(api_key="sk-real")

            def start(self):
                raise OSError("device busy")

            def stop(self):
                return None

        app, seen = _make_app(Exploding())
        app.on_press()
        self.assertIs(app.state, State.IDLE)
        self.assertIn(State.ERROR, seen.states)
        app.on_press()  # and the next press is not swallowed
        self.assertIn(State.ERROR, seen.states)

    def test_open_that_never_returns_relaunches(self):
        started = threading.Event()

        class Hanging(SdRecorder):
            def _open_stream(self, device, retry=False):
                started.set()
                threading.Event().wait()  # CoreAudio never answers

        with _SdPatch(), mock.patch.object(_appmod, "_START_TIMEOUT", 0.2):
            app, seen = _make_app(Hanging(_cfg(api_key="sk-real")))
            began = time.monotonic()
            app.on_press()
            self.assertLess(time.monotonic() - began, 3.0)
        self.assertTrue(started.is_set())
        self.assertIs(app.state, State.IDLE)
        self.assertTrue(audio_sd.poisoned_reason())
        self.assertEqual(self.relaunches, ["audio"])

    def test_press_on_a_poisoned_process_relaunches_without_opening(self):
        audio_sd.mark_poisoned("close wedged earlier")
        with _SdPatch() as sd:
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real")))
            app.on_press()
        self.assertEqual(sd.opened, [])  # never touched the broken audio layer
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.relaunches, ["audio"])

    def test_no_relaunch_loop(self):
        """Poisoned again right after an audio relaunch: advise, don't loop."""
        audio_sd.mark_poisoned("close wedged earlier")
        recent = {"time": time.time() - 5, "reason": "audio", "pid": 1}
        with _SdPatch(), mock.patch.object(platform, "last_relaunch",
                                           return_value=recent):
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real")))
            app.on_press()
        self.assertEqual(self.relaunches, [])
        self.assertTrue(any("stuck" in n[0] for n in seen.notes))
        self.assertIs(app.state, State.IDLE)

    def test_stop_that_never_returns_marks_the_backend(self):
        class Recorder:
            config = _cfg(api_key="sk-real")
            stalled = False

            def start(self):
                pass

            def stop(self):
                threading.Event().wait()

            def mark_stalled(self, reason):
                self.stalled = True

        rec = Recorder()
        with mock.patch.object(_appmod, "_STOP_TIMEOUT", 0.1):
            app, seen = _make_app(rec)
            app.on_press()
            app.on_release()
        self.assertTrue(rec.stalled)
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.relaunches, ["audio"])


class HotkeyWorkerTest(_AppRecoveryBase):
    """The pynput (event-tap) thread must never wait on the microphone."""

    def test_press_returns_at_once_and_release_waits_for_the_start(self):
        gate = threading.Event()
        calls = []

        class Slow:
            config = _cfg(api_key="sk-real")

            def start(self):
                gate.wait(5)  # e.g. a Bluetooth headset switching profile
                calls.append("start")

            def stop(self):
                calls.append("stop")
                return b"\x00" * 44 + b"\x10\x27" * 8000

        app, seen = _make_app(Slow(), run_async=True)
        began = time.monotonic()
        app.on_press()
        app.on_release()  # arrives while the start is still in progress
        self.assertLess(time.monotonic() - began, 0.5)
        gate.set()
        self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))
        self.assertEqual(calls, ["start", "stop"])  # never out of order
        self.assertTrue(_wait_until(lambda: app.state is State.IDLE))


class RecordingLimitTest(_AppRecoveryBase):
    def test_a_recording_whose_key_up_never_arrives_is_ended(self):
        with _SdPatch() as sd, mock.patch.object(_appmod, "_MAX_RECORDING", 0.2):
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real")))
            hotkey = mock.Mock()
            app._hotkey = hotkey
            app.on_press()
            sd.opened[0].feed(LOUD)
            # no on_release: the key-up was lost
            self.assertTrue(_wait_until(lambda: seen.typed == ["hello world"]))
            self.assertTrue(_wait_until(lambda: app.state is State.IDLE))
        hotkey.reset.assert_called_once()  # next press isn't swallowed
        self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])  # mic off

    def test_limit_is_cancelled_by_a_normal_release(self):
        with _SdPatch() as sd, mock.patch.object(_appmod, "_MAX_RECORDING", 0.2):
            app, seen = _make_app(SdRecorder(_cfg(api_key="sk-real")))
            app.on_press()
            sd.opened[0].feed(LOUD)
            app.on_release()
            time.sleep(0.4)
        self.assertEqual(seen.typed, ["hello world"])  # exactly once


class RelaunchCommandTest(unittest.TestCase):
    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "relaunch.json"
        p = mock.patch.object(platform, "_relaunch_record_path",
                              return_value=self.record)
        p.start()
        self.addCleanup(p.stop)

    def _relaunch(self, executable, orig_argv, frozen=False):
        popen = mock.Mock()
        exits = []
        with mock.patch("subprocess.Popen", popen), \
                mock.patch.object(platform, "hard_exit", side_effect=exits.append), \
                mock.patch.object(platform, "IS_WIN", False), \
                mock.patch.object(sys, "executable", executable), \
                mock.patch.object(sys, "orig_argv", orig_argv, create=True), \
                mock.patch.object(sys, "frozen", frozen, create=True):
            platform.relaunch_self("audio")
        self.assertEqual(exits, [0])
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["/bin/sh", "-c"])
        return args[2]

    def test_source_launch_keeps_dash_m_and_waits_for_this_pid(self):
        script = self._relaunch("/venv/bin/python", ["python", "-m", "ba_ge"])
        self.assertIn(f"kill -0 {os.getpid()}", script)
        self.assertTrue(script.endswith("exec /venv/bin/python -m ba_ge"), script)

    def test_app_bundle_reopens_the_bundle(self):
        script = self._relaunch("/Applications/Ba-Ge.app/Contents/MacOS/Ba-Ge",
                                ["/Applications/Ba-Ge.app/Contents/MacOS/Ba-Ge"],
                                frozen=True)
        self.assertTrue(script.endswith("exec open -n /Applications/Ba-Ge.app"), script)

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
        quartz = types.ModuleType("Quartz")
        quartz.CGEventTapEnable = lambda tap, on: self.enabled.append((tap, on))
        saved = sys.modules.get("Quartz")
        sys.modules["Quartz"] = quartz
        self.addCleanup(lambda: sys.modules.__setitem__("Quartz", saved)
                        if saved is not None else sys.modules.pop("Quartz", None))

        class FakePynputListener:
            def __init__(self, on_press=None, on_release=None):
                self.handled = []

            def _create_event_tap(self):
                return "TAP"

            def _handler(self, proxy, event_type, event, refcon):
                self.handled.append(event_type)

        self.cls = platform._tap_guarded(FakePynputListener)

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

    def test_key_state_is_unknown_off_macos(self):
        with mock.patch.object(platform, "IS_MAC", False):
            self.assertIsNone(platform.key_is_down(0x65))
        self.assertIsNone(platform.key_is_down(None))


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

    def restore():
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m

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
