"""Single-instance guard — one backend per OS, same ``acquire(name) -> bool``.

* Linux: abstract-namespace unix socket (kernel auto-cleans on exit).
* macOS / other POSIX: exclusive ``flock`` on a lock file in the cache dir.
* Windows: a named mutex (auto-released when the process dies).

Each backend keeps its handle alive for the process lifetime so the lock holds.
Only the Linux path runs in the dev environment; the macOS/Windows backends are
UNVERIFIED here — see docs/PORTING.md single-instance TEST steps.
"""

from __future__ import annotations

import logging
import sys

from . import paths

log = logging.getLogger("bage.singleton")

# Keep sockets / file handles / mutex handles alive for the whole process.
_held: list = []
# Names already acquired by THIS process. POSIX advisory locks (flock/lockf) are
# owned by the process, so a second acquire of the same file from the same process
# would merely refresh the lock instead of failing — track names to reproduce the
# Linux abstract-socket semantics (a duplicate acquire in one process fails too).
_held_names: set[str] = set()


def acquire(name: str = "ba-ge") -> bool:
    if sys.platform == "win32":
        return _acquire_windows_mutex(name)
    if sys.platform == "linux":
        return _acquire_abstract_socket(name)
    return _acquire_lockfile(name)  # macOS and other POSIX


def _acquire_abstract_socket(name: str) -> bool:
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind("\0" + name)  # abstract namespace (Linux only)
    except OSError:
        sock.close()
        return False
    _held.append(sock)
    return True


def _acquire_lockfile(name: str) -> bool:
    import fcntl

    if name in _held_names:  # same-process duplicate — flock alone wouldn't catch it
        return False
    path = paths.lock_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released on process death
    except OSError:
        return False
    _held.append(fh)
    _held_names.add(name)
    return True


def _acquire_windows_mutex(name: str) -> bool:
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle or ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return False
    _held.append(handle)
    return True
