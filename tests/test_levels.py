"""Input-level guards: tell the user when the MIC is why a transcript is wrong.

The silence floor alone let a clipping mic through — loud but destroyed audio —
so Scribe returned confident nonsense and the app looked broken. These pin the
behaviour at both ends of the range.
"""

import array
import unittest

from ba_ge.app import DictationApp, _CLIP_PCT, _QUIET_DBFS
from ba_ge.audio import level_stats
from ba_ge.config import Config


def _wav(samples) -> bytes:
    return b"\x00" * 44 + array.array("h", samples).tobytes()


SPEECH = _wav([6000, -6000] * 4000)                       # healthy, ~-14 dBFS
CLIPPED = _wav(([32767, -32768] * 40) + ([500, -500] * 3960))  # rails, then quiet
FAINT = _wav([90, -90] * 4000)                            # audible but far too soft
SILENT = _wav([0] * 4000)


class LevelStatsTest(unittest.TestCase):
    def test_healthy_speech(self):
        st = level_stats(SPEECH)
        self.assertLess(st.clipped_pct, _CLIP_PCT)
        self.assertGreater(st.rms_dbfs, _QUIET_DBFS)

    def test_clipping_is_detected_even_though_it_is_loud(self):
        st = level_stats(CLIPPED)
        self.assertGreaterEqual(st.clipped_pct, _CLIP_PCT)
        self.assertGreater(st.peak, 32000)          # the silence floor can't see this
        self.assertGreater(st.rms_dbfs, _QUIET_DBFS)  # ...nor can a quietness check

    def test_faint_input_is_detected(self):
        st = level_stats(FAINT)
        self.assertLess(st.rms_dbfs, _QUIET_DBFS)
        self.assertEqual(st.clipped_pct, 0.0)

    def test_empty_and_silent_are_safe(self):
        self.assertEqual(level_stats(b"").peak, 0)
        self.assertEqual(level_stats(b"\x00" * 30).peak, 0)
        self.assertEqual(level_stats(SILENT).peak, 0)


class _Rec:
    def __init__(self, wav): self.wav = wav
    def start(self): pass
    def stop(self): return self.wav


def _app(wav, injected):
    notes = []
    app = DictationApp(
        Config(api_key="k" * 40, min_duration=0.0),
        recorder=_Rec(wav),
        transcribe_fn=lambda w: "the transcript",
        injector=type("I", (), {"type_text": lambda s, t: injected.append(t),
                                "backend": "test"})(),
        notifier=lambda title, body="", **kw: notes.append((title, body)),
        run_async=False,
    )
    return app, notes


class WarningTest(unittest.TestCase):
    def _run(self, wav):
        injected = []
        app, notes = _app(wav, injected)
        app._state = app.state.__class__.RECORDING
        app._end_recording()
        return notes, injected

    def test_clipping_warns_but_still_delivers_the_text(self):
        notes, injected = self._run(CLIPPED)
        self.assertEqual(injected, ["the transcript"])  # words are NOT thrown away
        self.assertTrue(any("too loud" in t for t, _ in notes), notes)
        self.assertTrue(any("gain down" in b for _, b in notes), notes)

    def test_faint_warns_but_still_delivers_the_text(self):
        notes, injected = self._run(FAINT)
        self.assertEqual(injected, ["the transcript"])
        self.assertTrue(any("very quiet" in t for t, _ in notes), notes)

    def test_healthy_audio_is_not_nagged_about(self):
        notes, injected = self._run(SPEECH)
        self.assertEqual(injected, ["the transcript"])
        self.assertEqual(notes, [])

    def test_silence_still_skips_the_billed_api_call(self):
        injected = []
        calls = []
        app, notes = _app(SILENT, injected)
        app.transcribe_fn = lambda w: calls.append(w) or "x"
        app._state = app.state.__class__.RECORDING
        app._end_recording()
        self.assertEqual(calls, [])          # never sent, never billed
        self.assertEqual(injected, [])
        self.assertTrue(any("silent" in t for t, _ in notes), notes)

    def test_repeat_warnings_are_rate_limited(self):
        """A misconfigured mic must not fire a notification every sentence."""
        injected = []
        app, notes = _app(CLIPPED, injected)
        for _ in range(4):
            app._state = app.state.__class__.RECORDING
            app._end_recording()
        self.assertEqual(len(injected), 4)   # every utterance still transcribed
        self.assertEqual(len(notes), 1)      # ...but told once


if __name__ == "__main__":
    unittest.main()


class KeepRecordingsTest(unittest.TestCase):
    """[debug] keep_recordings: off by default (these are voice recordings);
    when on, the clip is saved exactly as sent and only the newest N are kept."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.dir = Path(tempfile.mkdtemp())
        patcher = mock.patch("ba_ge.app.paths.cache_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _dictate(self, wav, keep):
        app, _ = _app(wav, [])
        app.config.keep_recordings = keep
        app._state = app.state.__class__.RECORDING
        app._end_recording()

    def _takes(self):
        return sorted((self.dir / "recordings").glob("take-*.wav"))

    def test_off_by_default(self):
        self._dictate(_wav([8000, -8000] * 4000), keep=0)
        self.assertFalse((self.dir / "recordings").exists())

    def test_saves_the_clip_as_sent_and_keeps_only_the_newest(self):
        import time as _time
        clips = [_wav([1000 * (i + 1), -1000 * (i + 1)] * 4000) for i in range(4)]
        for clip in clips:
            self._dictate(clip, keep=2)
            _time.sleep(0.002)  # distinct millisecond in the file name
        takes = self._takes()
        self.assertEqual(len(takes), 2)
        self.assertEqual([t.read_bytes() for t in takes], clips[2:])
