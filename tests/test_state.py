import tempfile
import unittest
from pathlib import Path

from ba_ge import app as _appmod
from ba_ge.app import DictationApp
from ba_ge.config import Config
from ba_ge.state import State
from ba_ge.transcribe import TranscriptionError


# 44-byte header + loud (0x2710) S16 samples so peak_amplitude() sees signal.
SIGNAL_WAV = b"\x00" * 44 + b"\x10\x27" * 400
SILENT_WAV = b"\x00" * 44 + b"\x00" * 400


class FakeRecorder:
    def __init__(self, wav=SIGNAL_WAV):
        self.wav = wav
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self.wav


class FakeInjector:
    def __init__(self):
        self.typed = []

    def type_text(self, text):
        self.typed.append(text)


class FakeIndicator:
    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


def make_app(**over):
    cfg = over.pop("config", Config(api_key="sk-real"))
    rec = over.pop("recorder", FakeRecorder())
    inj = over.pop("injector", FakeInjector())
    ind = over.pop("indicator", FakeIndicator())
    tfn = over.pop("transcribe_fn", lambda wav: "transcribed text")
    notes = []
    app = DictationApp(
        cfg, recorder=rec, injector=inj, indicator=ind, transcribe_fn=tfn,
        notifier=lambda *a, **k: notes.append((a, k)), run_async=False)
    app._notes = notes
    return app, rec, inj, ind


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        # Redirect the debug transcript log so tests never touch the real cache.
        _appmod._TRANSCRIPT_LOG = Path(tempfile.mkdtemp()) / "transcripts.log"

    def test_happy_path_types_text(self):
        app, rec, inj, ind = make_app()
        app.on_press()
        self.assertIs(app.state, State.RECORDING)
        self.assertTrue(rec.started)
        app.on_release()  # synchronous (run_async=False)
        self.assertEqual(inj.typed, ["transcribed text"])
        self.assertIs(app.state, State.IDLE)
        self.assertIn(State.RECORDING, ind.states)
        self.assertIn(State.BUSY, ind.states)

    def test_press_without_api_key_does_not_record(self):
        app, rec, inj, ind = make_app(config=Config())  # placeholder key
        app.on_press()
        self.assertIs(app.state, State.IDLE)
        self.assertFalse(rec.started)
        self.assertIn(State.ERROR, ind.states)
        self.assertTrue(app._notes)

    def test_release_without_press_is_ignored(self):
        app, rec, inj, ind = make_app()
        app.on_release()
        self.assertEqual(inj.typed, [])
        self.assertIs(app.state, State.IDLE)

    def test_double_press_ignored_while_recording(self):
        app, rec, inj, ind = make_app()
        app.on_press()
        app.on_press()  # second press must be a no-op
        self.assertIs(app.state, State.RECORDING)

    def test_toggle_mode_taps_start_then_stop(self):
        app, rec, inj, ind = make_app(config=Config(api_key="sk-real", hotkey_mode="toggle"))
        app.on_press()  # first tap: start
        self.assertIs(app.state, State.RECORDING)
        self.assertTrue(rec.started)
        app.on_release()  # tap release must NOT stop in toggle mode
        self.assertIs(app.state, State.RECORDING)
        self.assertEqual(inj.typed, [])
        app.on_press()  # second tap: stop + transcribe
        self.assertEqual(inj.typed, ["transcribed text"])
        self.assertIs(app.state, State.IDLE)

    def test_too_short_recording_types_nothing(self):
        app, rec, inj, ind = make_app(recorder=FakeRecorder(wav=None))
        app.on_press()
        app.on_release()
        self.assertEqual(inj.typed, [])
        self.assertIs(app.state, State.IDLE)

    def test_empty_transcript_types_nothing(self):
        app, rec, inj, ind = make_app(transcribe_fn=lambda wav: "   ")
        app.on_press()
        app.on_release()
        self.assertEqual(inj.typed, [])
        self.assertIs(app.state, State.IDLE)

    def test_silent_recording_notifies_and_skips_api(self):
        called = {"n": 0}
        app, rec, inj, ind = make_app(
            recorder=FakeRecorder(wav=SILENT_WAV),
            transcribe_fn=lambda wav: called.__setitem__("n", called["n"] + 1) or "x")
        app.on_press()
        app.on_release()
        self.assertEqual(called["n"], 0)       # no (billed) API call on silence
        self.assertEqual(inj.typed, [])        # nothing typed
        self.assertIs(app.state, State.IDLE)
        self.assertTrue(app._notes)            # user was told it was silent

    def test_transcription_error_recovers_to_idle(self):
        def boom(wav):
            raise TranscriptionError("api down")

        app, rec, inj, ind = make_app(transcribe_fn=boom)
        app.on_press()
        app.on_release()
        self.assertEqual(inj.typed, [])
        self.assertIs(app.state, State.IDLE)
        self.assertIn(State.ERROR, ind.states)
        self.assertTrue(app._notes)


if __name__ == "__main__":
    unittest.main()
