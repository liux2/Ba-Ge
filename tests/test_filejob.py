import unittest

from ba_ge.filejob import (
    _fmt_ts,
    build_ffmpeg_cmd,
    default_txt_path,
    format_segments,
)


def _word(text, start, spk=None, wtype="word"):
    w = {"text": text, "start": start, "type": wtype}
    if spk is not None:
        w["speaker_id"] = spk
    return w


class FfmpegCmdTest(unittest.TestCase):
    def test_mp3_has_bitrate(self):
        cmd = build_ffmpeg_cmd("in.m4a", "out.mp3")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "16000")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "libmp3lame")
        self.assertIn("-b:a", cmd)
        self.assertEqual(cmd[-1], "out.mp3")

    def test_wav_omits_bitrate(self):
        cmd = build_ffmpeg_cmd("in.m4a", "out.wav", codec="pcm_s16le")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_s16le")
        self.assertNotIn("-b:a", cmd)


class TimestampTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_fmt_ts(0), "00:00")
        self.assertEqual(_fmt_ts(65), "01:05")
        self.assertEqual(_fmt_ts(3661), "1:01:01")


class DefaultTxtPathTest(unittest.TestCase):
    def test_replaces_suffix(self):
        self.assertEqual(str(default_txt_path("/a/b/talk.mp3")), "/a/b/talk.txt")
        self.assertEqual(str(default_txt_path("/a/b/talk.tar.gz")), "/a/b/talk.tar.txt")


class FormatSegmentsTest(unittest.TestCase):
    def test_multi_speaker_labels_and_timestamps(self):
        payload = {"words": [
            _word("Hello", 0.0, "spk_a"),
            _word(" ", 0.5, "spk_a", "spacing"),
            _word("there", 0.6, "spk_a"),
            _word(".", 1.0, "spk_a"),
            _word(" ", 1.1, wtype="spacing"),
            _word("Hi", 5.0, "spk_b"),
            _word(".", 5.3, "spk_b"),
        ]}
        out = format_segments(payload)
        self.assertEqual(out, "[00:00] Speaker 1: Hello there.\n[00:05] Speaker 2: Hi.")

    def test_single_speaker_omits_labels(self):
        payload = {"words": [
            _word("One", 0.0, "spk_a"),
            _word(" ", 0.4, "spk_a", "spacing"),
            _word("two", 0.5, "spk_a"),
        ]}
        self.assertEqual(format_segments(payload), "[00:00] One two")

    def test_no_words_falls_back_to_text(self):
        self.assertEqual(format_segments({"text": "plain text"}), "plain text")

    def test_speaker_numbers_follow_first_seen_order(self):
        payload = {"words": [
            _word("B", 0.0, "spk_b"),
            _word("A", 2.0, "spk_a"),
            _word("B", 4.0, "spk_b"),
        ]}
        out = format_segments(payload)
        self.assertIn("Speaker 1: B", out)   # spk_b seen first -> Speaker 1
        self.assertIn("Speaker 2: A", out)


if __name__ == "__main__":
    unittest.main()
