import unittest

from ba_ge.config import Config
from ba_ge.inject import InjectionError, Injector, resolve_backend


class FakeClipboard:
    """Stand-in for the Qt ClipboardManager (records paste requests)."""

    def __init__(self):
        self.calls = []

    def paste_text(self, text, send_key):
        self.calls.append((text, send_key))


class ResolveBackendTest(unittest.TestCase):
    def test_always_paste(self):
        # xdotool and ydotool are retired; paste is the single Linux backend.
        for pref in ("paste", "auto", "xdotool", "ydotool", ""):
            self.assertEqual(resolve_backend(pref), "paste")


class InjectorTest(unittest.TestCase):
    def _inj(self, clip=None):
        return Injector(Config(), clipboard=clip)

    def test_empty_is_noop(self):
        clip = FakeClipboard()
        self._inj(clip).type_text("")
        self.assertEqual(clip.calls, [])

    def test_missing_clipboard_raises(self):
        with self.assertRaises(InjectionError):
            self._inj(None).type_text("hi")

    def test_delegates_text_and_key_callable(self):
        clip = FakeClipboard()
        self._inj(clip).type_text("hello world with spaces")
        self.assertEqual(len(clip.calls), 1)
        text, send_key = clip.calls[0]
        self.assertEqual(text, "hello world with spaces")  # verbatim, atomic
        self.assertTrue(callable(send_key))                # deferred keystroke

    def test_bind_clipboard(self):
        inj = Injector(Config())  # no clipboard yet
        clip = FakeClipboard()
        inj.bind_clipboard(clip)
        inj.type_text("x")
        self.assertEqual(clip.calls[0][0], "x")


class TerminalDetectionTest(unittest.TestCase):
    def _patch_class(self, value):
        import ba_ge.inject as mod
        orig = mod._active_window_class
        mod._active_window_class = lambda: value
        self.addCleanup(lambda: setattr(mod, "_active_window_class", orig))

    def test_terminal_window_is_detected(self):
        self._patch_class("ghostty com.mitchellh.ghostty")
        self.assertTrue(Injector(Config())._active_is_terminal())

    def test_gui_window_is_not_terminal(self):
        self._patch_class("google-chrome Google-chrome")
        self.assertFalse(Injector(Config())._active_is_terminal())

    def test_unknown_window_is_not_terminal(self):
        self._patch_class("")  # detection failed / no X
        self.assertFalse(Injector(Config())._active_is_terminal())


if __name__ == "__main__":
    unittest.main()
