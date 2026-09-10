import array
import io
import os
import struct
import tempfile
import time
import unittest
import wave
from unittest import mock

from ba_ge.audio import (
    AudioError,
    Recorder,
    _capture_channels,
    _downmix_loudest,
    _patch_wav_sizes,
    _source_channels,
    _wav_seconds,
    arecord_env,
    build_arecord_cmd,
    is_too_short,
    peak_amplitude,
)
from ba_ge.config import Config


class _FakeProc:
    """Stands in for a finished arecord Popen (already signalled/waited)."""

    def __init__(self, stderr=b"", rc=0):
        self.returncode = rc
        self.stderr = io.BytesIO(stderr)
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


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


def _multichannel_wav(channels: list[list[int]], rate: int = 16000) -> bytes:
    """A real PCM WAV built by interleaving per-channel sample lists (equal length)."""
    n = len(channels[0])
    inter = array.array("h")
    for i in range(n):
        for ch in channels:
            inter.append(ch[i])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(inter.tobytes())
    return buf.getvalue()


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

    # ---- multi-channel receiver (e.g. DJI dual wireless): keep the loudest ----

    def test_downmix_keeps_loud_channel_when_mic_switched(self):
        # TX1 on ch0 went silent; user switched to TX2 on ch1 (loud). Capturing a
        # single channel would keep ch0 (silence) — the reported bug. We must keep ch1.
        n = 200
        wav = _multichannel_wav([[0] * n, [12000] * n])
        mono, chosen = _downmix_loudest(wav)
        self.assertEqual(chosen, 1)
        self.assertEqual(peak_amplitude(mono), 12000)  # the live mic, not silence
        self.assertEqual(struct.unpack_from("<H", mono, 22)[0], 1)  # output is mono

    def test_downmix_keeps_channel_zero_when_loudest(self):
        n = 200
        wav = _multichannel_wav([[9000] * n, [3] * n])
        mono, chosen = _downmix_loudest(wav)
        self.assertEqual(chosen, 0)
        self.assertEqual(peak_amplitude(mono), 9000)

    def test_downmix_is_noop_for_mono(self):
        wav = _multichannel_wav([[100] * 50])
        mono, chosen = _downmix_loudest(wav)
        self.assertEqual(chosen, -1)
        self.assertEqual(mono, wav)

    def test_downmix_ignores_non_wav(self):
        junk = b"not a wav" + b"\x00" * 60
        mono, chosen = _downmix_loudest(junk)
        self.assertEqual(chosen, -1)
        self.assertEqual(mono, junk)

    def test_capture_channels_does_not_expand_pulse_source(self):
        # arecord -D pulse -c 2 fails to open while resampling, so we must NOT force
        # extra channels here — that broke capture entirely. Multi-channel is a
        # native-rate follow-up (see _capture_channels docstring).
        name = "alsa_input.usb-DJI_Wireless_Mic_Rx-01.analog-stereo"
        self.assertEqual(_capture_channels(Config(audio_device=name, channels=1)), 1)

    def test_capture_channels_raw_alsa_unchanged(self):
        self.assertEqual(_capture_channels(Config(audio_device="plughw:1,0", channels=1)), 1)

    def test_capture_channels_default_device_unchanged(self):
        self.assertEqual(_capture_channels(Config(audio_device="default", channels=1)), 1)

    def test_capture_channels_honours_explicit_config(self):
        self.assertEqual(_capture_channels(Config(audio_device="default", channels=2)), 2)

    def test_source_channels_parses_pactl_short_list(self):
        name = "alsa_input.usb-DJI_Technology-01.analog-stereo"
        listing = (
            f"53\talsa_output.pci.monitor\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
            f"2725\t{name}\tPipeWire\ts24le 2ch 48000Hz\tSUSPENDED\n"
        )
        completed = mock.Mock(stdout=listing)
        with mock.patch("ba_ge.audio.subprocess.run", return_value=completed):
            self.assertEqual(_source_channels(name), 2)

    def test_source_channels_none_when_pactl_missing(self):
        name = "alsa_input.usb-DJI-01.analog-stereo"
        with mock.patch("ba_ge.audio.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(_source_channels(name))

    def test_build_arecord_cmd_honours_explicit_channels(self):
        cfg = Config(audio_device="default", channels=1)
        cmd = build_arecord_cmd(cfg, "/tmp/x.wav", channels=2)
        self.assertEqual(cmd[cmd.index("-c") + 1], "2")

    # ---- empty capture must be visible, not silent ----

    def _recorder_with_empty_capture(self, wall, stderr=b"Unable to create stream: Timeout"):
        rec = Recorder(Config(min_duration=0.3))
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(_wav(0))  # 44-byte header, no audio frames
        rec._proc = _FakeProc(stderr=stderr, rc=1)
        rec._path = path
        rec._start = time.monotonic() - wall
        return rec

    def test_empty_capture_after_holding_raises_actionable_error(self):
        # Held well past min_duration but got no audio -> mic muted / device wedged.
        rec = self._recorder_with_empty_capture(wall=1.0)
        with self.assertRaises(AudioError) as cm:
            rec.stop()
        self.assertIn("microphone", str(cm.exception).lower())

    def test_empty_capture_after_quick_tap_is_silent_none(self):
        # A genuine sub-min_duration tap is normal; must NOT raise/notify.
        rec = self._recorder_with_empty_capture(wall=0.05)
        self.assertIsNone(rec.stop())

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
