"""Speech-to-text via the ElevenLabs Scribe API (stdlib HTTP, no SDK dependency).

`transcribe()` is the dictation path (WAV -> text). `transcribe_verbose()` is the
file path (audio -> full payload with diarized, timestamped words). Both share
`_post_stt()`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .config import sanitize_keyterms

log = logging.getLogger("bage.transcribe")

_BOUNDARY = "----BaGeBoundaryV1xK9sLpQ2"


class TranscriptionError(Exception):
    pass


def _base_fields(config) -> dict:
    """Common Scribe form fields. A list value (keyterms) becomes repeated fields."""
    fields: dict = {"model_id": config.model_id}
    if config.language:
        fields["language_code"] = config.language
    terms = sanitize_keyterms(getattr(config, "keyterms", []))
    if terms:
        fields["keyterms"] = terms  # ElevenLabs wants one keyterms field per term
    return fields


def _multipart(fields: dict, audio: bytes, filename: str, content_type: str) -> bytes:
    out = bytearray()

    def add_field(name: str, value) -> None:
        out.extend(f"--{_BOUNDARY}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.extend(str(value).encode("utf-8"))
        out.extend(b"\r\n")

    for name, value in fields.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                add_field(name, item)
        else:
            add_field(name, value)
    out += f"--{_BOUNDARY}\r\n".encode()
    out += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
    out += f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    out += audio
    out += b"\r\n"
    out += f"--{_BOUNDARY}--\r\n".encode()
    return bytes(out)


class _Retryable(TranscriptionError):
    """A failure that may succeed on a second try (network drop, 5xx, 429)."""


# Retries for dictation. A flaky link/proxy drops a request now and then
# ("Remote end closed connection", "SSL: UNEXPECTED_EOF_WHILE_READING") and the
# utterance was simply lost. Each retry re-sends the whole (small, Opus) clip.
# The budget keeps every attempt inside app.py's _BUSY_TIMEOUT (90 s), which
# force-resets a hung request; a retry that couldn't finish in time is pointless.
_RETRY_ATTEMPTS = 3
_RETRY_BUDGET = 75.0
_RETRY_PAUSE = 1.0
_MIN_ATTEMPT_TIME = 10.0   # don't start an attempt with less time than this left


def _post_stt(audio: bytes, config, fields: dict, *, filename: str,
              content_type: str, timeout: float, attempts: int = 1,
              budget: float | None = None) -> dict:
    """POST to Scribe. With attempts > 1, retry network/5xx/429 failures within
    ``budget`` seconds in total (each attempt's timeout is capped to what's left)."""
    if not config.api_key_valid:
        raise TranscriptionError("ElevenLabs API key is not configured.")
    body = _multipart(fields, audio, filename, content_type)
    deadline = None if budget is None else time.monotonic() + budget
    for attempt in range(1, attempts + 1):
        this_timeout = timeout
        if deadline is not None:
            this_timeout = min(timeout, deadline - time.monotonic())
        try:
            return _post_once(body, config, this_timeout)
        except _Retryable as exc:
            left = None if deadline is None else deadline - time.monotonic()
            if attempt == attempts or (
                    left is not None and left < _MIN_ATTEMPT_TIME + _RETRY_PAUSE):
                raise TranscriptionError(str(exc)) from exc
            log.warning("transcription attempt %d/%d failed (%s) — retrying",
                        attempt, attempts, exc)
            time.sleep(_RETRY_PAUSE)
    raise AssertionError("unreachable")


def _post_once(body: bytes, config, timeout: float) -> dict:
    url = config.api_base.rstrip("/") + "/v1/speech-to-text"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "xi-api-key": config.api_key,
            "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code in (401, 403):
            raise TranscriptionError(
                f"Authentication failed ({exc.code}). Check your API key. {detail}") from exc
        if exc.code == 429 or exc.code >= 500:
            raise _Retryable(f"ElevenLabs API error {exc.code}: {detail}") from exc
        raise TranscriptionError(f"ElevenLabs API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise _Retryable(f"Network error reaching ElevenLabs: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise _Retryable(f"Network error reaching ElevenLabs: {exc}") from exc


_OPUS_BITRATE = "24k"      # wideband speech; transcript identical to WAV in testing
_ENCODE_TIMEOUT = 10.0


def to_opus(wav: bytes) -> bytes | None:
    """WAV -> Ogg/Opus (~11x smaller), or None if ffmpeg can't do it.

    Raw 16 kHz WAV is 32 KB per second of speech; on a slow uplink (measured
    14-28 KB/s) that uploads SLOWER than real time and dictation stalls for
    30-60 s. Opus at 24 kbps is ~3 KB/s with the same Scribe transcript. Any
    failure (no ffmpeg, no libopus, bad input) falls back to sending the WAV."""
    import subprocess

    from . import platform

    cmd = [platform.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-f", "wav", "-i", "pipe:0",
           "-c:a", "libopus", "-b:a", _OPUS_BITRATE, "-application", "voip",
           "-f", "ogg", "pipe:1"]
    try:
        proc = subprocess.run(cmd, input=wav, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=_ENCODE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("opus encode unavailable (%s) — sending WAV", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.warning("opus encode failed (rc=%s: %s) — sending WAV", proc.returncode,
                    proc.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    return proc.stdout


def transcribe(wav: bytes, config, *, timeout: float = 60.0) -> str:
    """Dictation: WAV bytes -> transcript text (uploaded as Opus when possible)."""
    fields = _base_fields(config)
    t0 = time.monotonic()
    opus = to_opus(wav)
    t1 = time.monotonic()
    if opus:
        audio, filename, ctype = opus, "audio.ogg", "audio/ogg"
    else:
        audio, filename, ctype = wav, "audio.wav", "audio/wav"
    payload = _post_stt(audio, config, fields, filename=filename,
                        content_type=ctype, timeout=timeout,
                        attempts=_RETRY_ATTEMPTS, budget=_RETRY_BUDGET)
    log.info("uploaded %d KB %s (wav %d KB, encode %d ms), response in %.1f s",
             len(audio) // 1024, "opus" if opus else "wav", len(wav) // 1024,
             (t1 - t0) * 1000, time.monotonic() - t1)
    text = payload.get("text")
    if not isinstance(text, str):
        raise TranscriptionError(f"Unexpected API response: {payload!r}")
    return text


def transcribe_verbose(audio: bytes, config, *, filename: str = "audio.mp3",
                       content_type: str = "audio/mpeg", timeout: float = 600.0) -> dict:
    """File transcription: audio bytes -> full payload (text + diarized word timings)."""
    fields = _base_fields(config)
    fields["diarize"] = "true"
    payload = _post_stt(audio, config, fields, filename=filename,
                        content_type=content_type, timeout=timeout)
    if not isinstance(payload.get("text"), str):
        raise TranscriptionError(f"Unexpected API response: {payload!r}")
    return payload


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
        data = json.loads(raw)
        return str(data.get("detail", data))[:300]
    except Exception:
        return ""
