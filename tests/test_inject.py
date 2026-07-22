import unittest

from ba_ge.config import Config
from ba_ge.inject import InjectionError, Injector, resolve_backend


class FakeClipboard:
    """Stand-in for the Qt ClipboardManager (records paste requests)."""

    def __init__(self):
        self.calls = []

    def paste_text(self, text, send_key):
        self.calls.append((text, send_key))


def _patch(testcase, **attrs):
    """Patch module-level ba_ge.inject attributes for the duration of a test.

    IMPORTANT: tests must never invoke the real detection/typing, or they would
    inject keystrokes into the live session. We force detection + typing to fakes.
    """
    import ba_ge.inject as mod
    for name, value in attrs.items():
        orig = getattr(mod, name)
        setattr(mod, name, value)
        testcase.addCleanup(lambda n=name, o=orig: setattr(mod, n, o))


class ResolveBackendTest(unittest.TestCase):
    def test_always_paste(self):
        for pref in ("paste", "auto", "xdotool", "ydotool", ""):
            self.assertEqual(resolve_backend(pref), "paste")


class InjectorTest(unittest.TestCase):
    def setUp(self):
        # Non-terminal by default → the (faked) paste path, never real typing.
        _patch(self, _active_window_class=lambda: "")

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
        self.assertEqual(text, "hello world with spaces")
        self.assertTrue(callable(send_key))

    def test_bind_clipboard(self):
        inj = Injector(Config())
        clip = FakeClipboard()
        inj.bind_clipboard(clip)
        inj.type_text("x")
        self.assertEqual(clip.calls[0][0], "x")


class TerminalModeTest(unittest.TestCase):
    """inject_terminal_mode routes terminals to type or paste (patched — no real
    injection). GUI apps always paste regardless."""

    def _setup(self, window_class):
        self.typed = []
        _patch(self,
               _active_window_class=lambda: window_class,
               _type_via_uinput=lambda text: (self.typed.append(text) or True))

    def test_terminal_type_mode_types(self):
        self._setup("ghostty com.mitchellh.ghostty")
        clip = FakeClipboard()
        Injector(Config(inject_terminal_mode="type"), clipboard=clip).type_text("hi there")
        self.assertEqual(self.typed, ["hi there"])
        self.assertEqual(clip.calls, [])          # typed, not pasted

    def test_terminal_paste_mode_pastes(self):
        self._setup("ghostty com.mitchellh.ghostty")
        clip = FakeClipboard()
        Injector(Config(inject_terminal_mode="paste"), clipboard=clip).type_text("hi there")
        self.assertEqual(self.typed, [])
        self.assertEqual(clip.calls[0][0], "hi there")   # pasted

    def test_type_mode_gui_still_pastes(self):
        self._setup("google-chrome Google-chrome")
        clip = FakeClipboard()
        Injector(Config(inject_terminal_mode="type"), clipboard=clip).type_text("hi")
        self.assertEqual(self.typed, [])          # GUI is not a terminal
        self.assertEqual(clip.calls[0][0], "hi")


class TerminalDetectionTest(unittest.TestCase):
    def test_terminal_window_is_detected(self):
        _patch(self, _active_window_class=lambda: "ghostty com.mitchellh.ghostty")
        self.assertTrue(Injector(Config())._active_is_terminal())

    def test_gui_window_is_not_terminal(self):
        _patch(self, _active_window_class=lambda: "google-chrome Google-chrome")
        self.assertFalse(Injector(Config())._active_is_terminal())

    def test_unknown_window_is_not_terminal(self):
        _patch(self, _active_window_class=lambda: "")
        self.assertFalse(Injector(Config())._active_is_terminal())


if __name__ == "__main__":
    unittest.main()
