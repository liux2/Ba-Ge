"""Transcribe an audio file: normalize with ffmpeg -> Scribe (diarize + timestamps)
-> speaker-labeled, timestamped text.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from . import platform
from .transcribe import TranscriptionError, transcribe_verbose

log = logging.getLogger("bage.filejob")

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus",
                    ".aac", ".wma", ".aiff", ".aif", ".mka", ".webm")


class FileJobError(Exception):
    pass


def default_txt_path(path) -> Path:
    return Path(path).with_suffix(".txt")


def build_ffmpeg_cmd(src, dst, *, exe: str = "ffmpeg", codec: str = "libmp3lame",
                     sample_rate: int = 16000, bitrate: str = "64k") -> list[str]:
    cmd = [exe, "-nostdin", "-y", "-i", str(src),
           "-ac", "1", "-ar", str(sample_rate), "-c:a", codec]
    if not codec.startswith("pcm"):
        cmd += ["-b:a", bitrate]
    cmd.append(str(dst))
    return cmd


def _last_line(data: bytes) -> str:
    lines = (data or b"").decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else ""


def prepare_audio(path) -> tuple[bytes, str, str]:
    """Down-mix any audio to a small 16 kHz mono clip; (bytes, filename, content_type).

    Prefers mp3; falls back to WAV if the mp3 encoder is unavailable.
    """
    src = Path(path)
    if not src.exists():
        raise FileJobError(f"File not found: {src}")

    last_err = ""
    exe = platform.ffmpeg_exe()
    for codec, ext, ctype in (("libmp3lame", ".mp3", "audio/mpeg"),
                              ("pcm_s16le", ".wav", "audio/wav")):
        fd, tmp = tempfile.mkstemp(prefix="bage-file-", suffix=ext)
        os.close(fd)
        try:
            proc = subprocess.run(build_ffmpeg_cmd(src, tmp, exe=exe, codec=codec),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            os.unlink(tmp)
            raise FileJobError("ffmpeg not found — install ffmpeg.") from exc

        if proc.returncode == 0:
            data = Path(tmp).read_bytes()
            os.unlink(tmp)
            if len(data) > 100:
                return data, "audio" + ext, ctype
            last_err = "produced no audio (empty or non-audio file?)"
        else:
            last_err = _last_line(proc.stderr)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    raise FileJobError(f"ffmpeg could not convert the file: {last_err}")


def _fmt_ts(seconds: float) -> str:
    total = int(seconds or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_segments(payload: dict) -> str:
    """Group Scribe words by speaker into timestamped, speaker-labeled segments."""
    words = payload.get("words") or []
    if not words:
        return (payload.get("text") or "").strip()

    segments = []
    cur = None
    for w in words:
        if w.get("type") == "spacing":
            if cur is not None:
                cur["text"] += w.get("text", "")
            continue
        spk = w.get("speaker_id")
        if cur is None or spk != cur["speaker"]:
            if cur is not None:
                segments.append(cur)
            cur = {"speaker": spk, "start": w.get("start", 0.0), "text": w.get("text", "")}
        else:
            cur["text"] += w.get("text", "")
    if cur is not None:
        segments.append(cur)

    multi = len({s["speaker"] for s in segments if s["speaker"] is not None}) > 1
    speaker_num: dict = {}
    lines = []
    for seg in segments:
        body = seg["text"].strip()
        if not body:
            continue
        ts = _fmt_ts(seg["start"])
        if multi and seg["speaker"] is not None:
            n = speaker_num.setdefault(seg["speaker"], len(speaker_num) + 1)
            lines.append(f"[{ts}] Speaker {n}: {body}")
        else:
            lines.append(f"[{ts}] {body}")
    return "\n".join(lines)


def transcribe_file(path, config, progress=None) -> tuple[str, dict]:
    """Full pipeline: prepare audio -> Scribe -> formatted transcript. Returns (text, payload)."""
    if not config.api_key_valid:
        raise FileJobError("ElevenLabs API key is not configured "
                           "(Settings, or set ELEVENLABS_API_KEY).")
    if progress:
        progress("Preparing audio…")
    audio, filename, ctype = prepare_audio(path)
    if progress:
        progress("Transcribing… (this can take a while for long files)")
    try:
        payload = transcribe_verbose(audio, config, filename=filename, content_type=ctype)
    except TranscriptionError as exc:
        raise FileJobError(str(exc)) from exc

    text = format_segments(payload)
    if not text.strip():
        raise FileJobError("The transcript came back empty.")
    return text, payload
