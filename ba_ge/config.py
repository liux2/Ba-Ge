"""Configuration: defaults + ~/.config/ba-ge/config.toml + env override."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger("bage.config")

# ElevenLabs keyterm-prompting limits (batch): <=1000 terms, <=50 chars, <=5 words.
KEYTERMS_MAX = 1000
KEYTERM_MAX_CHARS = 50
KEYTERM_MAX_WORDS = 5


def parse_keyterms(text: str) -> list[str]:
    """Split a free-form vocabulary entry into terms.

    Forgiving on separators: newlines, commas, and semicolons all delimit terms,
    so the user doesn't have to know one specific format. Whitespace inside a term
    is preserved (multi-word phrases like "Bare Metal" stay intact).
    """
    normalized = (text or "").replace(",", "\n").replace(";", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def validate_keyterms(terms) -> tuple[list[str], list[str]]:
    """Return (accepted, warnings). Warnings explain which terms were dropped/capped."""
    accepted: list[str] = []
    seen: set[str] = set()
    too_long = too_many_words = dupes = 0
    for raw in terms or []:
        term = str(raw).strip()
        if not term:
            continue
        if len(term) > KEYTERM_MAX_CHARS:
            too_long += 1
            continue
        if len(term.split()) > KEYTERM_MAX_WORDS:
            too_many_words += 1
            continue
        if term.lower() in seen:
            dupes += 1
            continue
        seen.add(term.lower())
        accepted.append(term)

    over = 0
    if len(accepted) > KEYTERMS_MAX:
        over = len(accepted) - KEYTERMS_MAX
        accepted = accepted[:KEYTERMS_MAX]

    warnings = []
    if too_long:
        warnings.append(f"{too_long} over {KEYTERM_MAX_CHARS} chars")
    if too_many_words:
        warnings.append(f"{too_many_words} over {KEYTERM_MAX_WORDS} words")
    if dupes:
        warnings.append(f"{dupes} duplicate{'s' if dupes > 1 else ''}")
    if over:
        warnings.append(f"{over} beyond the {KEYTERMS_MAX}-term limit")
    return accepted, warnings


def sanitize_keyterms(terms) -> list[str]:
    """The terms the API will accept (see validate_keyterms for the warnings)."""
    return validate_keyterms(terms)[0]

# Shown verbatim in the example config; treated as "not configured".
PLACEHOLDER_API_KEY = "YOUR_ELEVENLABS_API_KEY_HERE"

# scribe_v1 is deprecated (removal 2026-07-09); scribe_v2 is the current batch model.
DEPRECATED_MODELS = {"scribe_v1"}

CONFIG_DIR = paths.config_dir()
CONFIG_PATH = paths.config_path()

EXAMPLE_CONFIG = """\
# Ba-Ge configuration
# Get an API key at https://elevenlabs.io  (Profile -> API Keys)

[elevenlabs]
# REQUIRED: paste your ElevenLabs API key here (replaces the placeholder).
# You can also set the ELEVENLABS_API_KEY environment variable instead,
# which takes precedence over this file.
api_key = "YOUR_ELEVENLABS_API_KEY_HERE"
model_id = "scribe_v2"
# language_code = "eng"   # omit to auto-detect
# Bias recognition toward domain terms / names (adds ~20% to each call's cost):
# keyterms = ["Kubernetes", "OAuth", "ElevenLabs"]

[hotkey]
# Hold-to-talk key. Examples: "f9", "pause", "ctrl_r", "scroll_lock"
key = "f9"

[audio]
device = "default"        # ALSA device; "default" follows the PipeWire default mic
sample_rate = 16000
channels = 1
min_duration = 0.3        # ignore accidental taps shorter than this (seconds)

[inject]
backend = "auto"          # typing method: auto (xdotool on X11, else ydotool) | xdotool | ydotool
key_delay_ms = 20         # per-keystroke delay; raise if chars/spaces drop (mainly ydotool)
# ydotool_socket = "/run/user/1000/.ydotool_socket"   # override ydotoold socket (auto if unset)
"""


@dataclass
class Config:
    api_key: str = PLACEHOLDER_API_KEY
    model_id: str = "scribe_v2"
    language: str | None = None
    hotkey: str = "f9"
    min_duration: float = 0.3
    sample_rate: int = 16000
    channels: int = 1
    audio_device: str = "default"
    key_delay_ms: int = 20  # inter-keystroke delay; too low drops chars/spaces (ydotool)
    inject_backend: str = "auto"  # auto | xdotool | ydotool
    ui_scale: float = 0.0  # UI zoom; 0 = auto-detect from display DPI
    ydotool_socket: str = ""  # override ydotoold socket path; "" = auto-detect
    keyterms: list = field(default_factory=list)  # bias recognition toward these terms
    api_base: str = "https://api.elevenlabs.io"

    @property
    def api_key_valid(self) -> bool:
        return bool(self.api_key) and self.api_key.strip() != PLACEHOLDER_API_KEY


def _num(section: dict, key: str, cast):
    """Coerce one numeric field, returning None (and logging) on bad input.

    A single malformed value must not abort parsing of the rest of the config.
    """
    if key not in section:
        return None
    try:
        return cast(section[key])
    except (TypeError, ValueError):
        log.warning("ignoring invalid value for %s: %r", key, section[key])
        return None


def _apply_toml(cfg: Config, data: dict) -> None:
    el = data.get("elevenlabs", {})
    if "api_key" in el:
        cfg.api_key = str(el["api_key"])
    if "model_id" in el:
        cfg.model_id = str(el["model_id"])
    if el.get("language_code"):
        cfg.language = str(el["language_code"])
    if "api_base" in el:
        cfg.api_base = str(el["api_base"])
    if "keyterms" in el:
        kt = el["keyterms"]
        if isinstance(kt, (list, tuple)):
            cfg.keyterms = [str(x).strip() for x in kt if str(x).strip()]
        elif isinstance(kt, str):
            cfg.keyterms = parse_keyterms(kt)

    hk = data.get("hotkey", {})
    if "key" in hk:
        cfg.hotkey = str(hk["key"])

    au = data.get("audio", {})
    if "device" in au:
        cfg.audio_device = str(au["device"])
    if (v := _num(au, "sample_rate", int)) is not None:
        cfg.sample_rate = v
    if (v := _num(au, "channels", int)) is not None:
        cfg.channels = v
    if (v := _num(au, "min_duration", float)) is not None:
        cfg.min_duration = v

    inj = data.get("inject", {})
    if (v := _num(inj, "key_delay_ms", int)) is not None:
        cfg.key_delay_ms = v
    if "backend" in inj:
        cfg.inject_backend = str(inj["backend"])
    if "ydotool_socket" in inj:
        cfg.ydotool_socket = str(inj["ydotool_socket"])

    ui = data.get("ui", {})
    if (v := _num(ui, "scale", float)) is not None:
        cfg.ui_scale = v


def load_config(config_path: Path | None = None, env: dict | None = None) -> Config:
    """Build a Config from defaults, then the TOML file, then env (highest priority)."""
    env = os.environ if env is None else env
    cfg = Config()

    path = CONFIG_PATH if config_path is None else Path(config_path)
    try:
        if path.exists():
            with open(path, "rb") as fh:
                _apply_toml(cfg, tomllib.load(fh))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # Unreadable / syntactically broken config: fall back to defaults, but say so.
        log.warning("could not read config %s: %s", path, exc)

    key = env.get("ELEVENLABS_API_KEY")
    if key:
        cfg.api_key = key

    return cfg


def ensure_config_file(path: Path | None = None) -> Path:
    """Create the config file with the placeholder key if it does not exist."""
    path = CONFIG_PATH if path is None else Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG)
    return path


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_toml(cfg: Config) -> str:
    """Render a Config back to TOML (used by the settings panel to save)."""
    if cfg.language:
        lang = f"language_code = {_toml_str(cfg.language)}"
    else:
        lang = '# language_code = "eng"   # omit to auto-detect'
    if cfg.ydotool_socket:
        sock = f"ydotool_socket = {_toml_str(cfg.ydotool_socket)}"
    else:
        sock = ('# ydotool_socket = "/run/user/1000/.ydotool_socket"   '
                "# override ydotoold socket (auto if unset)")
    if cfg.keyterms:
        terms = ", ".join(_toml_str(t) for t in cfg.keyterms)
        keyterms = f"keyterms = [{terms}]   # ~20% cost when used"
    else:
        keyterms = ('# keyterms = ["Kubernetes", "OAuth", "ElevenLabs"]   '
                    "# bias recognition (~20% cost when used)")
    return (
        "# Ba-Ge configuration\n"
        "# Get an API key at https://elevenlabs.io  (Profile -> API Keys)\n\n"
        "[elevenlabs]\n"
        f"api_key = {_toml_str(cfg.api_key)}\n"
        f"model_id = {_toml_str(cfg.model_id)}\n"
        f"{lang}\n"
        f"{keyterms}\n\n"
        "[hotkey]\n"
        f"key = {_toml_str(cfg.hotkey)}\n\n"
        "[audio]\n"
        f"device = {_toml_str(cfg.audio_device)}\n"
        f"sample_rate = {cfg.sample_rate}\n"
        f"channels = {cfg.channels}\n"
        f"min_duration = {cfg.min_duration}\n\n"
        "[inject]\n"
        f'backend = {_toml_str(cfg.inject_backend)}   # auto | xdotool | ydotool\n'
        f"key_delay_ms = {cfg.key_delay_ms}\n"
        f"{sock}\n\n"
        "[ui]\n"
        f"scale = {cfg.ui_scale}   # 0 = auto-detect from display DPI (e.g. 1.5, 2.0)\n"
    )


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = CONFIG_PATH if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(cfg))
    return path
