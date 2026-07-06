"""Cross-platform config / cache / lock locations.

On Linux these resolve to the same XDG paths the app has always used
(~/.config/ptt-dictation, ~/.cache/ptt-dictation), so existing installs keep
working with no migration. On macOS they resolve to ~/Library/..., on Windows to
%LOCALAPPDATA%\\ptt-dictation.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="ptt-dictation", appauthor=False)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def config_path() -> Path:
    return config_dir() / "config.toml"


def cache_dir() -> Path:
    return Path(_dirs.user_cache_dir)


def transcript_log() -> Path:
    return cache_dir() / "transcripts.log"


def lock_path() -> Path:
    return cache_dir() / "ptt-dictation.lock"
