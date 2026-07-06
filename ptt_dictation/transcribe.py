"""Speech-to-text via the ElevenLabs Scribe API (stdlib HTTP, no SDK dependency).

`transcribe()` is the dictation path (WAV -> text). `transcribe_verbose()` is the
file path (audio -> full payload with diarized, timestamped words). Both share
`_post_stt()`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .config import sanitize_keyterms

log = logging.getLogger("ptt.transcribe")

_BOUNDARY = "----pttDictationBoundaryV1xK9sLpQ2"


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


def _post_stt(audio: bytes, config, fields: dict, *, filename: str,
              content_type: str, timeout: float) -> dict:
    if not config.api_key_valid:
        raise TranscriptionError("ElevenLabs API key is not configured.")
    body = _multipart(fields, audio, filename, content_type)
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
        raise TranscriptionError(f"ElevenLabs API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TranscriptionError(f"Network error reaching ElevenLabs: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise TranscriptionError(f"Network error reaching ElevenLabs: {exc}") from exc


def transcribe(wav: bytes, config, *, timeout: float = 60.0) -> str:
    """Dictation: WAV bytes -> transcript text."""
    fields = _base_fields(config)
    payload = _post_stt(wav, config, fields, filename="audio.wav",
                        content_type="audio/wav", timeout=timeout)
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
