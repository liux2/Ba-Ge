import subprocess
import unittest

from ptt_dictation.config import Config
from ptt_dictation.inject import InjectionError, Injector, resolve_backend


class Result:
    def __init__(self, returncode=0, stderr=b"", stdout=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.result


class ResolveBackendTest(unittest.TestCase):
    def test_explicit_choice_wins(self):
        self.assertEqual(resolve_backend("xdotool"), "xdotool")
        self.assertEqual(resolve_backend("ydotool"), "ydotool")

    def test_auto_returns_a_real_backend(self):
        self.assertIn(resolve_backend("auto"), ("xdotool", "ydotool"))


class YdotoolBackendTest(unittest.TestCase):
    def _inj(self, runner, **kw):
        return Injector(Config(inject_backend="ydotool", **kw), runner=runner)

    def test_builds_stdin_command_and_sets_socket_env(self):
        runner = FakeRunner(Result())
        self._inj(runner, key_delay_ms=7).type_text("hello world")
        cmd, kwargs = runner.calls[0]
        self.assertEqual(cmd, ["ydotool", "type", "--key-delay", "7", "--file", "-"])
        self.assertEqual(kwargs["input"], b"hello world")
        self.assertIn("YDOTOOL_SOCKET", kwargs["env"])

    def test_nonzero_exit_raises(self):
        runner = FakeRunner(Result(returncode=1, stderr=b"uinput: permission denied"))
        with self.assertRaises(InjectionError) as ctx:
            self._inj(runner).type_text("hi")
        self.assertIn("ydotool failed", str(ctx.exception))

    def test_daemonless_notice_on_exit0_does_not_raise(self):
        runner = FakeRunner(Result(returncode=0, stderr=b"ydotoold backend unavailable"))
        self._inj(runner).type_text("hi")  # must not raise


class XdotoolBackendTest(unittest.TestCase):
    def _inj(self, runner, **kw):
        return Injector(Config(inject_backend="xdotool", **kw), runner=runner)

    def test_builds_xtest_command_with_spaces_preserved(self):
        runner = FakeRunner(Result())
        self._inj(runner, key_delay_ms=12).type_text("a b c")
        cmd, kwargs = runner.calls[0]
        self.assertEqual(
            cmd, ["xdotool", "type", "--clearmodifiers", "--delay", "12",
                  "--file", "/dev/stdin"])
        self.assertEqual(kwargs["input"], b"a b c")  # spaces intact

    def test_nonzero_exit_raises(self):
        runner = FakeRunner(Result(returncode=1, stderr=b"Cannot open display"))
        with self.assertRaises(InjectionError) as ctx:
            self._inj(runner).type_text("hi")
        self.assertIn("xdotool failed", str(ctx.exception))


class PasteBackendTest(unittest.TestCase):
    class SeqRunner:
        """Returns queued results in order; records every call."""
        def __init__(self, results):
            self.results = list(results)
            self.calls = []

        def __call__(self, cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return self.results.pop(0) if self.results else Result()

    def test_paste_sets_clipboard_and_sends_paste_key(self):
        runner = self.SeqRunner([
            Result(returncode=0),                                   # save clipboard
            Result(returncode=0),                                   # set clipboard
            Result(returncode=0, stdout=b"hello world with spaces"),  # verify -> confirms
            Result(returncode=0, stdout=b"0x123"),                  # getactivewindow -> wid
            Result(returncode=0, stdout=b'WM_CLASS(STRING) = "firefox", "firefox"'),  # not a terminal
            Result(returncode=0),                                   # xdotool key
            Result(returncode=0),                                   # restore
        ])
        inj = Injector(Config(inject_backend="paste"), runner=runner)
        inj._paste_restore_delay = 0  # don't sleep in tests
        inj.type_text("hello world with spaces")

        # the whole string lands on the clipboard verbatim (spaces intact)
        set_calls = [k for c, k in runner.calls
                     if c == ["xclip", "-selection", "clipboard", "-i"] and k.get("input")]
        self.assertEqual(set_calls[0]["input"], b"hello world with spaces")
        cmds = [c for c, _ in runner.calls]
        self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+v"], cmds)

    def test_paste_uses_ctrl_shift_v_in_terminals(self):
        runner = self.SeqRunner([
            Result(returncode=0),                              # save clipboard
            Result(returncode=0),                              # set clipboard
            Result(returncode=0, stdout=b"x y z"),             # verify -> confirms
            Result(returncode=0, stdout=b"0x123"),             # getactivewindow -> wid
            Result(returncode=0, stdout=b'WM_CLASS(STRING) = "ghostty", "com.mitchellh.ghostty"'),
            Result(returncode=0),                              # xdotool key
            Result(returncode=0),                              # restore
        ])
        inj = Injector(Config(inject_backend="paste"), runner=runner)
        inj._paste_restore_delay = 0
        inj.type_text("x y z")
        cmds = [c for c, _ in runner.calls]
        self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"], cmds)


class CommonInjectTest(unittest.TestCase):
    def test_empty_text_is_noop(self):
        runner = FakeRunner(Result())
        Injector(Config(inject_backend="ydotool"), runner=runner).type_text("")
        self.assertEqual(runner.calls, [])

    def test_missing_binary_raises(self):
        def boom(*a, **k):
            raise FileNotFoundError()
        with self.assertRaises(InjectionError):
            Injector(Config(inject_backend="ydotool"), runner=boom).type_text("hi")


if __name__ == "__main__":
    unittest.main()
