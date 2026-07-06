"""Tests for the cross-platform backends (run on Linux; verify pure logic + wiring)."""

import unittest

from ba_ge.audio import _wav_seconds, peak_amplitude
from ba_ge.audio_sd import _to_wav
from ba_ge.config import Config

try:
    import pynput  # noqa: F401
    _HAVE_PYNPUT = True
except Exception:
    _HAVE_PYNPUT = False


class AudioSdTest(unittest.TestCase):
    def test_to_wav_produces_canonical_16k_mono(self):
        pcm = b"\x10\x27" * 16000  # 1.0s of 0x2710 samples @ 16 kHz mono
        data = _to_wav(pcm, 16000, 1)
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(peak_amplitude(data), 10000)   # downstream guard sees signal
        self.assertAlmostEqual(_wav_seconds(data, 16000, 1), 1.0, places=2)


class _FakeKb:
    def __init__(self):
        self.events = []

    def type(self, text):
        self.events.append(("type", text))

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))

    def pressed(self, modifier):
        kb = self

        class _Ctx:
            def __enter__(self):
                kb.events.append(("mod_down", modifier))
                return self

            def __exit__(self, *exc):
                kb.events.append(("mod_up", modifier))
                return False

        return _Ctx()


class _FakeClip:
    def __init__(self, initial=""):
        self.value = initial
        self.history = []

    def copy(self, value):
        self.value = value
        self.history.append(value)

    def paste(self):
        return self.value


@unittest.skipUnless(_HAVE_PYNPUT, "pynput not installed")
class PynputInjectorTest(unittest.TestCase):
    def _make(self, backend, initial_clip=""):
        from ba_ge.inject_pynput import PynputInjector
        kb, clip = _FakeKb(), _FakeClip(initial_clip)
        inj = PynputInjector(Config(inject_backend=backend), controller=kb, clipboard=clip)
        inj._paste_restore_delay = 0
        return inj, kb, clip

    def test_auto_defaults_to_paste(self):
        inj, _, _ = self._make("auto")
        self.assertEqual(inj.backend, "paste")

    def test_paste_sets_clipboard_sends_v_and_restores(self):
        inj, kb, clip = self._make("paste", initial_clip="PRIOR")
        inj.type_text("hello world with spaces")
        self.assertIn("hello world with spaces", clip.history)   # whole string, atomic
        self.assertIn(("press", "v"), kb.events)
        self.assertEqual(clip.value, "PRIOR")                    # clipboard restored

    def test_type_backend_uses_controller_type(self):
        inj, kb, _ = self._make("type")
        inj.type_text("abc")
        self.assertIn(("type", "abc"), kb.events)


if __name__ == "__main__":
    unittest.main()
