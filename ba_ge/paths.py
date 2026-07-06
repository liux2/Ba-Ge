"""Cross-platform config / cache / lock locations.

On Linux these resolve to the same XDG paths the app has always used
(~/.config/ba-ge, ~/.cache/ba-ge), so existing installs keep
working with no migration. On macOS they resolve to ~/Library/..., on Windows to
%LOCALAPPDATA%\\ba-ge.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="ba-ge", appauthor=False)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def config_path() -> Path:
    return config_dir() / "config.toml"


def cache_dir() -> Path:
    return Path(_dirs.user_cache_dir)


def transcript_log() -> Path:
    return cache_dir() / "transcripts.log"


def lock_path() -> Path:
    return cache_dir() / "ba-ge.lock"
