import struct
import unittest

from ba_ge.audio import (
    _patch_wav_sizes,
    _wav_seconds,
    arecord_env,
    build_arecord_cmd,
    is_too_short,
    peak_amplitude,
)
from ba_ge.config import Config


def _wav(data_bytes: int) -> bytes:
    """A 44-byte PCM WAV header (with a bogus ~2GB size) + N data bytes."""
    header = bytearray(44)
    header[0:4] = b"RIFF"
    struct.pack_into("<I", header, 4, 0x7FFFFF00)  # unfinalized placeholder
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    struct.pack_into("<I", header, 16, 16)
    header[36:40] = b"data"
    struct.pack_into("<I", header, 40, 0x7FFFFF00)
    return bytes(header) + b"\x00" * data_bytes


class AudioTest(unittest.TestCase):
    def test_build_arecord_cmd(self):
        cfg = Config(audio_device="default", sample_rate=16000, channels=1)
        cmd = build_arecord_cmd(cfg, "/tmp/x.wav")
        self.assertEqual(cmd[0], "arecord")
        self.assertEqual(cmd[cmd.index("-D") + 1], "default")
        self.assertEqual(cmd[cmd.index("-r") + 1], "16000")
        self.assertEqual(cmd[cmd.index("-c") + 1], "1")
        self.assertIn("S16_LE", cmd)
        self.assertEqual(cmd[-1], "/tmp/x.wav")

    def test_raw_alsa_device_passthrough(self):
        cfg = Config(audio_device="plughw:1,0", sample_rate=44100)
        cmd = build_arecord_cmd(cfg, "/tmp/y.wav")
        self.assertEqual(cmd[cmd.index("-D") + 1], "plughw:1,0")
        self.assertEqual(cmd[cmd.index("-r") + 1], "44100")
        self.assertNotIn("PULSE_SOURCE", arecord_env(cfg, base={}))

    def test_pulse_source_name_routes_via_pulse_plugin(self):
        # A PipeWire source name -> arecord -D pulse + PULSE_SOURCE env.
        name = "alsa_input.usb-DJI_Wireless_Mic_Rx-01.analog-stereo"
        cfg = Config(audio_device=name)
        cmd = build_arecord_cmd(cfg, "/tmp/z.wav")
        self.assertEqual(cmd[cmd.index("-D") + 1], "pulse")
        self.assertEqual(arecord_env(cfg, base={})["PULSE_SOURCE"], name)

    def test_default_and_pipewire_follow_system_default(self):
        for dev in ("default", "pipewire", "pulse"):
            cmd = build_arecord_cmd(Config(audio_device=dev), "/tmp/a.wav")
            self.assertEqual(cmd[cmd.index("-D") + 1], dev)
            self.assertNotIn("PULSE_SOURCE", arecord_env(Config(audio_device=dev), base={}))

    def test_is_too_short(self):
        self.assertTrue(is_too_short(0.1, 0.3))
        self.assertFalse(is_too_short(0.5, 0.3))
        self.assertFalse(is_too_short(0.3, 0.3))

    def test_patch_wav_sizes_repairs_placeholder_header(self):
        raw = _wav(1000)
        fixed = _patch_wav_sizes(raw)
        self.assertEqual(struct.unpack_from("<I", fixed, 4)[0], len(raw) - 8)
        self.assertEqual(struct.unpack_from("<I", fixed, 40)[0], len(raw) - 44)

    def test_patch_wav_sizes_is_idempotent(self):
        once = _patch_wav_sizes(_wav(1000))
        twice = _patch_wav_sizes(once)
        self.assertEqual(once, twice)

    def test_patch_wav_sizes_ignores_non_wav(self):
        junk = b"not a wav file at all, really truly not, padding..............."
        self.assertEqual(_patch_wav_sizes(junk), junk)

    def test_wav_seconds_from_byte_count(self):
        # 16000 Hz mono S16 -> 32000 bytes/sec; 32000 data bytes == 1.0s.
        secs = _wav_seconds(_wav(32000), sample_rate=16000, channels=1)
        self.assertAlmostEqual(secs, 1.0, places=3)

    def test_wav_seconds_none_for_empty(self):
        self.assertIsNone(_wav_seconds(b"\x00" * 44, 16000, 1))

    def test_peak_amplitude(self):
        self.assertEqual(peak_amplitude(b"\x00" * 44 + b"\x00" * 200), 0)   # silent
        self.assertEqual(peak_amplitude(b"\x00" * 44 + b"\x10\x27" * 50), 10000)  # 0x2710
        self.assertEqual(peak_amplitude(b"\x00" * 30), 0)                   # header-only
        # full-negative sample (0x8000 = -32768) reported as 32768, not overflow
        self.assertEqual(peak_amplitude(b"\x00" * 44 + b"\x00\x80"), 32768)


if __name__ == "__main__":
    unittest.main()
