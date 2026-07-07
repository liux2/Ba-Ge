import unittest

from ba_ge.config import Config
from ba_ge.inject import InjectionError, Injector, resolve_backend


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
    def test_always_ydotool(self):
        # xdotool (X11-only) and paste (clipboard) are retired — ydotool works on
        # X11 + Wayland and is the single backend.
        for pref in ("ydotool", "auto", "xdotool", "paste", ""):
            self.assertEqual(resolve_backend(pref), "ydotool")


class YdotoolBackendTest(unittest.TestCase):
    def _inj(self, runner, **kw):
        return Injector(Config(inject_backend="ydotool", **kw), runner=runner)

    def test_builds_stdin_command_and_sets_socket_env(self):
        runner = FakeRunner(Result())
        self._inj(runner, key_delay_ms=7).type_text("hello world with spaces")
        cmd, kwargs = runner.calls[0]
        self.assertEqual(cmd, ["ydotool", "type", "--key-delay", "7", "--file", "-"])
        self.assertEqual(kwargs["input"], b"hello world with spaces")  # spaces intact
        self.assertIn("YDOTOOL_SOCKET", kwargs["env"])

    def test_nonzero_exit_raises(self):
        runner = FakeRunner(Result(returncode=1, stderr=b"uinput: permission denied"))
        with self.assertRaises(InjectionError) as ctx:
            self._inj(runner).type_text("hi")
        self.assertIn("ydotool failed", str(ctx.exception))

    def test_daemonless_notice_on_exit0_does_not_raise(self):
        runner = FakeRunner(Result(returncode=0, stderr=b"ydotoold backend unavailable"))
        self._inj(runner).type_text("hi")  # must not raise


class CommonInjectTest(unittest.TestCase):
    def test_empty_text_is_noop(self):
        runner = FakeRunner(Result())
        Injector(Config(), runner=runner).type_text("")
        self.assertEqual(runner.calls, [])

    def test_missing_binary_raises(self):
        def boom(*a, **k):
            raise FileNotFoundError()
        with self.assertRaises(InjectionError):
            Injector(Config(), runner=boom).type_text("hi")


if __name__ == "__main__":
    unittest.main()
