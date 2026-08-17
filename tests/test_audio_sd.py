"""Tests for the sounddevice backend's long-lived-stream design (see ba_ge/audio_sd.py).

The point of these is the deadlock fix: arming/disarming a recording must never
call into PortAudio, because a per-utterance ``stream.stop()`` deadlocks against
CoreAudio on macOS and parks the app forever. Runs on any OS — PortAudio is faked.
"""

import sys
import threading
import time
import types
import unittest

from ba_ge.app import _call_with_timeout
from ba_ge.audio import peak_amplitude
from ba_ge.audio_sd import AudioError, SdRecorder
from ba_ge.config import Config


class _FakeStream:
    """Stands in for sounddevice.RawInputStream, recording every call."""

    def __init__(self, *, device=None, hang_on_stop=False, **kw):
        self.device = device
        self.kw = kw
        self.calls: list[str] = []
        self.active = False
        self.callback = kw.get("callback")
        self._hang = hang_on_stop

    def start(self):
        self.calls.append("start")
        self.active = True

    def stop(self):
        self.calls.append("stop")
        if self._hang:
            threading.Event().wait()  # never returns — the macOS deadlock
        self.active = False

    def close(self):
        self.calls.append("close")

    def feed(self, pcm: bytes):
        """Deliver a buffer the way the PortAudio thread would."""
        self.callback(pcm, len(pcm) // 2, None, None)


class _FakeSd:
    def __init__(self, hang_on_stop=False):
        self.opened: list[_FakeStream] = []
        self._hang = hang_on_stop

    def RawInputStream(self, **kw):  # noqa: N802 - mirrors sounddevice's name
        stream = _FakeStream(hang_on_stop=self._hang, **kw)
        self.opened.append(stream)
        return stream


class _SdPatch:
    """Install a fake ``sounddevice`` for the duration of a block."""

    def __init__(self, hang_on_stop=False):
        self.sd = _FakeSd(hang_on_stop)

    def __enter__(self):
        self._saved = sys.modules.get("sounddevice")
        module = types.ModuleType("sounddevice")
        # Late-bound on purpose, so a test can swap in a failing opener afterwards.
        module.RawInputStream = lambda **kw: self.sd.RawInputStream(**kw)
        sys.modules["sounddevice"] = module
        return self.sd

    def __exit__(self, *exc):
        if self._saved is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = self._saved
        return False


def _cfg(**kw):
    base = dict(audio_device="default", sample_rate=16000, channels=1, min_duration=0.0)
    base.update(kw)
    return Config(**base)


class LongLivedStreamTest(unittest.TestCase):
    def test_stop_never_calls_into_portaudio(self):
        """The whole fix: ending a recording must not touch the audio API."""
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            stream = sd.opened[0]
            stream.feed(b"\x10\x27" * 16000)
            wav = rec.stop()

        self.assertEqual(stream.calls, ["start"])  # no stop/close per utterance
        self.assertTrue(stream.active)
        self.assertEqual(peak_amplitude(wav), 10000)

    def test_stream_is_reused_across_recordings(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            for _ in range(3):
                rec.start()
                sd.opened[0].feed(b"\x10\x27" * 16000)
                rec.stop()
        self.assertEqual(len(sd.opened), 1)  # opened once, not per utterance
        self.assertEqual(sd.opened[0].calls, ["start"])

    def test_audio_outside_a_recording_is_dropped(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            stream = sd.opened[0]
            stream.feed(b"\x10\x27" * 16000)
            rec.stop()
            stream.feed(b"\x10\x27" * 16000)  # idle: stream runs, nothing buffered
            rec.start()
            stream.feed(b"\x01\x00" * 16000)
            wav = rec.stop()
        # Only the second recording's samples survive — no bleed from idle or run 1.
        self.assertEqual(peak_amplitude(wav), 1)

    def test_stop_without_start_returns_none(self):
        with _SdPatch():
            rec = SdRecorder(_cfg())
            self.assertIsNone(rec.stop())

    def test_short_tap_discarded(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg(min_duration=0.5))
            rec.start()
            sd.opened[0].feed(b"\x10\x27" * 1600)  # 0.1s
            self.assertIsNone(rec.stop())

    def test_device_change_reopens_stream(self):
        with _SdPatch() as sd:
            cfg = _cfg()
            rec = SdRecorder(cfg)
            rec.start()
            rec.stop()
            cfg.audio_device = "Some USB Mic"  # as a Settings save would
            rec.start()
            rec.stop()

        self.assertEqual(len(sd.opened), 2)
        self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])  # old released
        self.assertIsNone(sd.opened[0].device)
        self.assertEqual(sd.opened[1].device, "Some USB Mic")

    def test_dead_stream_is_replaced(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            rec.stop()
            sd.opened[0].active = False  # e.g. the mic was unplugged
            rec.start()
            rec.stop()
        self.assertEqual(len(sd.opened), 2)

    def test_open_failure_raises_audio_error(self):
        with _SdPatch() as sd:
            def boom(**kw):
                raise RuntimeError("no such device")
            sd.RawInputStream = boom
            rec = SdRecorder(_cfg())
            with self.assertRaises(AudioError):
                rec.start()

    def test_close_releases_the_microphone(self):
        with _SdPatch() as sd:
            rec = SdRecorder(_cfg())
            rec.start()
            rec.stop()
            self.assertTrue(rec.close())
        self.assertEqual(sd.opened[0].calls, ["start", "stop", "close"])

    def test_close_is_bounded_when_portaudio_deadlocks(self):
        """A wedged close must give up, not hang the caller (SIGKILL-proofing)."""
        import ba_ge.audio_sd as mod

        original, mod._CLOSE_TIMEOUT = mod._CLOSE_TIMEOUT, 0.2
        try:
            with _SdPatch(hang_on_stop=True):
                rec = SdRecorder(_cfg())
                rec.start()
                rec.stop()
                began = time.monotonic()
                self.assertFalse(rec.close())  # reports the stall
                self.assertLess(time.monotonic() - began, 2.0)  # but returns promptly
        finally:
            mod._CLOSE_TIMEOUT = original


class CallWithTimeoutTest(unittest.TestCase):
    def test_returns_value(self):
        self.assertEqual(_call_with_timeout(lambda: "ok", 1.0), (True, "ok"))

    def test_propagates_exception(self):
        with self.assertRaises(ValueError):
            _call_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 1.0)

    def test_reports_a_hang_instead_of_blocking(self):
        began = time.monotonic()
        finished, value = _call_with_timeout(lambda: threading.Event().wait(), 0.2)
        self.assertFalse(finished)
        self.assertIsNone(value)
        self.assertLess(time.monotonic() - began, 2.0)


if __name__ == "__main__":
    unittest.main()
