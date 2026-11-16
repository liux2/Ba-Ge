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
    def setUp(self):
        p = mock.patch("ba_ge.transcribe.time.sleep")
        p.start()
        self.addCleanup(p.stop)

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


def _wav(seconds=0.5, rate=16000):
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))))
    return buf.getvalue()


class OpusUploadTest(unittest.TestCase):
    """Dictation uploads Opus (~11x smaller than WAV) and falls back to WAV."""

    def _send(self, opus_result):
        body = json.dumps({"text": "ok"}).encode()
        with mock.patch("ba_ge.transcribe.to_opus", return_value=opus_result), \
                mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            self.assertEqual(transcribe(b"RIFFwav", _cfg()), "ok")
        return m.call_args.args[0].data

    def test_sends_opus_when_encoding_works(self):
        sent = self._send(b"OggSfake")
        self.assertIn(b'filename="audio.ogg"', sent)
        self.assertIn(b"Content-Type: audio/ogg", sent)
        self.assertIn(b"OggSfake", sent)
        self.assertNotIn(b"RIFFwav", sent)

    def test_falls_back_to_wav(self):
        sent = self._send(None)
        self.assertIn(b'filename="audio.wav"', sent)
        self.assertIn(b"RIFFwav", sent)

    def test_missing_ffmpeg_returns_none(self):
        from ba_ge.transcribe import to_opus
        with mock.patch("ba_ge.platform.ffmpeg_exe", return_value="/nonexistent/ffmpeg"):
            self.assertIsNone(to_opus(_wav()))

    def test_garbage_input_returns_none(self):
        from ba_ge import platform
        from ba_ge.transcribe import to_opus
        import shutil
        if not shutil.which(platform.ffmpeg_exe()):
            self.skipTest("ffmpeg not installed")
        self.assertIsNone(to_opus(b"not a wav file"))

    def test_real_encode_is_ogg_and_smaller(self):
        from ba_ge import platform
        from ba_ge.transcribe import to_opus
        import shutil
        if not shutil.which(platform.ffmpeg_exe()):
            self.skipTest("ffmpeg not installed")
        wav = _wav(2.0)
        out = to_opus(wav)
        if out is None:
            self.skipTest("ffmpeg lacks libopus")
        self.assertTrue(out.startswith(b"OggS"))
        self.assertLess(len(out), len(wav) // 4)


class RetryTest(unittest.TestCase):
    """Dictation retries a dropped request instead of losing the utterance."""

    def setUp(self):
        p = mock.patch("ba_ge.transcribe.time.sleep")   # no real pauses
        p.start()
        self.addCleanup(p.stop)
        o = mock.patch("ba_ge.transcribe.to_opus", return_value=None)
        o.start()
        self.addCleanup(o.stop)

    @staticmethod
    def _ok():
        return _Resp(json.dumps({"text": "hello"}).encode())

    @staticmethod
    def _http(code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"detail":"d"}'))

    def test_network_drop_is_retried(self):
        import ssl
        drop = ssl.SSLEOFError("EOF occurred in violation of protocol")
        with mock.patch("urllib.request.urlopen",
                        side_effect=[drop, self._ok()]) as m:
            self.assertEqual(transcribe(b"RIFF", _cfg()), "hello")
        self.assertEqual(m.call_count, 2)

    def test_remote_disconnect_is_retried(self):
        import http.client
        gone = http.client.RemoteDisconnected("Remote end closed connection")
        with mock.patch("urllib.request.urlopen", side_effect=[gone, self._ok()]):
            self.assertEqual(transcribe(b"RIFF", _cfg()), "hello")

    def test_server_error_and_rate_limit_are_retried(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self._http(503), self._http(429), self._ok()]) as m:
            self.assertEqual(transcribe(b"RIFF", _cfg()), "hello")
        self.assertEqual(m.call_count, 3)

    def test_gives_up_after_three_attempts(self):
        err = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=err) as m:
            with self.assertRaises(TranscriptionError) as ctx:
                transcribe(b"RIFF", _cfg())
        self.assertEqual(m.call_count, 3)
        self.assertIn("Network error", str(ctx.exception))

    def test_client_errors_are_not_retried(self):
        for code in (400, 401, 422):
            with mock.patch("urllib.request.urlopen",
                            side_effect=self._http(code)) as m:
                with self.assertRaises(TranscriptionError):
                    transcribe(b"RIFF", _cfg())
            self.assertEqual(m.call_count, 1, code)

    def test_no_retry_once_the_budget_is_spent(self):
        """A retry that could not finish before app.py's 90 s reset is skipped."""
        import ba_ge.transcribe as tr
        err = urllib.error.URLError("timed out")
        clock = iter([0.0, 0.0, 70.0, 70.0, 70.0])   # first attempt ate 70 of 75 s
        with mock.patch.object(tr.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch("urllib.request.urlopen", side_effect=err) as m:
            with self.assertRaises(TranscriptionError):
                tr._post_stt(b"x", _cfg(), {}, filename="a.wav", content_type="audio/wav",
                             timeout=60, attempts=3, budget=75)
        self.assertEqual(m.call_count, 1)

    def test_attempt_timeout_is_capped_to_the_budget(self):
        import ba_ge.transcribe as tr
        with mock.patch("urllib.request.urlopen", return_value=self._ok()) as m:
            tr._post_stt(b"x", _cfg(), {}, filename="a.wav", content_type="audio/wav",
                         timeout=60, attempts=3, budget=20)
        self.assertLessEqual(m.call_args.kwargs["timeout"], 20)

    def test_file_transcription_does_not_retry(self):
        err = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=err) as m:
            with self.assertRaises(TranscriptionError):
                transcribe_verbose(b"AUDIO", _cfg())
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
