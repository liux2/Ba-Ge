import io
import json
import unittest
import urllib.error
from unittest import mock

from ba_ge.config import Config
from ba_ge.transcribe import TranscriptionError, transcribe, transcribe_verbose


def _cfg():
    return Config(api_key="sk-real")


class _Resp(io.BytesIO):
    """Context-manager BytesIO standing in for an http.client response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TranscribeTest(unittest.TestCase):
    def test_returns_text_and_builds_request(self):
        body = json.dumps({"text": "hello world", "language_code": "eng"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            out = transcribe(b"RIFF....fake-wav", _cfg())
        self.assertEqual(out, "hello world")
        req = m.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/v1/speech-to-text"))
        self.assertEqual(req.get_header("Xi-api-key"), "sk-real")
        self.assertIn("multipart/form-data", req.get_header("Content-type"))

    def test_auth_error_maps_to_friendly_message(self):
        err = urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TranscriptionError) as ctx:
                transcribe(b"x", _cfg())
        self.assertIn("Authentication failed", str(ctx.exception))

    def test_network_error(self):
        err = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TranscriptionError) as ctx:
                transcribe(b"x", _cfg())
        self.assertIn("Network error", str(ctx.exception))

    def test_missing_key_raises_before_network(self):
        with mock.patch("urllib.request.urlopen") as m:
            with self.assertRaises(TranscriptionError):
                transcribe(b"x", Config())  # placeholder key
        m.assert_not_called()

    def test_keyterms_sent_as_repeated_fields(self):
        cfg = Config(api_key="sk-real", keyterms=["Foo", "Bar Baz"])
        body = json.dumps({"text": "ok"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            transcribe(b"WAV", cfg)
        sent = m.call_args.args[0].data
        # one Content-Disposition per keyterm, not a single JSON array
        self.assertEqual(sent.count(b'name="keyterms"'), 2)
        self.assertIn(b"Foo", sent)
        self.assertIn(b"Bar Baz", sent)

    def test_no_keyterms_field_when_empty(self):
        body = json.dumps({"text": "ok"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            transcribe(b"WAV", Config(api_key="sk-real"))
        self.assertNotIn(b'name="keyterms"', m.call_args.args[0].data)

    def test_verbose_sends_diarize_and_returns_payload(self):
        body = json.dumps({"text": "hi", "words": [{"text": "hi"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            payload = transcribe_verbose(b"AUDIO", _cfg(), filename="audio.mp3",
                                         content_type="audio/mpeg")
        self.assertEqual(payload["words"], [{"text": "hi"}])
        sent = m.call_args.args[0].data
        self.assertIn(b'name="diarize"', sent)
        self.assertIn(b"audio/mpeg", sent)


if __name__ == "__main__":
    unittest.main()
