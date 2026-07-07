"""Insert text at the cursor via ydotool (uinput keystroke synthesis).

ydotool injects at the kernel `/dev/uinput` level, so it is independent of the
display server: it works on **both X11 and Wayland** (unlike xdotool, which is
X11-only and now retired). Because it synthesizes keystrokes, it **never touches
the clipboard**.

Reliability: ydotool needs the **ydotoold** daemon running and access to
`/dev/uinput`. Daemonless, it registers a fresh uinput device per call and can
drop the first keystroke — so `install.sh` / the `.deb` set up ydotoold (a user
service) plus a uinput udev rule, and the app nudges the daemon on startup.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger("bage.inject")


class InjectionError(Exception):
    pass


def _socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return f"{runtime}/.ydotool_socket"


def _ydotool_env(socket: str = "") -> dict:
    env = os.environ.copy()
    env["YDOTOOL_SOCKET"] = socket or env.get("YDOTOOL_SOCKET") or _socket_path()
    return env


def resolve_backend(pref: str) -> str:
    # xdotool (X11-only) and paste (clipboard) are retired. ydotool is the single
    # backend and works on X11 + Wayland.
    return "ydotool"


def ensure_ydotoold(socket: str = "") -> None:
    """Best-effort: make sure ydotoold is running (needed for reliable typing).

    Prefer the systemd user service; if the socket still isn't there, spawn
    ydotoold detached. Requires /dev/uinput access (set up by install.sh/.deb).
    """
    sock = socket or _socket_path()
    if os.path.exists(sock):
        return
    for cmd in (["systemctl", "--user", "start", "ydotoold"],):
        try:
            subprocess.run(cmd, timeout=5, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass
    if os.path.exists(sock) or not shutil.which("ydotoold"):
        return
    try:
        subprocess.Popen(["ydotoold", "-p", sock, "-P", "0660"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        log.info("could not start ydotoold", exc_info=True)


class Injector:
    def __init__(self, config, runner=subprocess.run):
        self.key_delay_ms = config.key_delay_ms
        self.socket = getattr(config, "ydotool_socket", "")
        self._runner = runner
        self.backend = resolve_backend(getattr(config, "inject_backend", "ydotool"))
        self._warned_degraded = False

    def type_text(self, text: str) -> None:
        if not text:
            return
        self._type_ydotool(text)

    # ---- helpers ----

    def _run(self, cmd, text=None, env=None, timeout=30):
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
        if text is not None:
            kwargs["input"] = text if isinstance(text, bytes) else text.encode("utf-8")
        if env is not None:
            kwargs["env"] = env
        try:
            return self._runner(cmd, **kwargs)
        except FileNotFoundError as exc:
            raise InjectionError(f"{cmd[0]} not found — install ydotool.") from exc
        except subprocess.TimeoutExpired as exc:
            raise InjectionError(f"{cmd[0]} timed out after {timeout}s.") from exc

    @staticmethod
    def _rc(result) -> int:
        return getattr(result, "returncode", 1)

    @staticmethod
    def _err(result) -> str:
        return (getattr(result, "stderr", b"") or b"").decode("utf-8", "replace").strip()

    # ---- ydotool ----

    def _type_ydotool(self, text: str) -> None:
        cmd = ["ydotool", "type", "--key-delay", str(self.key_delay_ms), "--file", "-"]
        # Bound the wait to ~2x the expected typing time (+ headroom) so a hung or
        # unreachable ydotoold can never leave the app stuck in "transcribing" — it
        # times out, raises, and the state machine recovers to IDLE.
        timeout = max(8.0, 2 * len(text) * self.key_delay_ms / 1000 + 5)
        res = self._run(cmd, text, env=_ydotool_env(self.socket), timeout=timeout)
        stderr = self._err(res)
        if self._rc(res) != 0:
            raise InjectionError(
                f"ydotool failed (exit {self._rc(res)}). {stderr} "
                "Is ydotoold running and /dev/uinput accessible "
                "(input group / udev rule)?")
        if not self._warned_degraded and "ydotoold backend unavailable" in stderr:
            self._warned_degraded = True
            log.warning("ydotoold not running — typing via /dev/uinput directly, which can "
                        "drop the first keystroke. Start it: systemctl --user start ydotoold")
