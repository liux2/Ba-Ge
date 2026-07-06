import tempfile
import textwrap
import unittest
from pathlib import Path

from ptt_dictation.config import (
    Config,
    PLACEHOLDER_API_KEY,
    dump_toml,
    load_config,
    parse_keyterms,
    sanitize_keyterms,
    save_config,
    validate_keyterms,
)


class ConfigTest(unittest.TestCase):
    def test_defaults_use_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(config_path=Path(d) / "missing.toml", env={})
        self.assertEqual(cfg.api_key, PLACEHOLDER_API_KEY)
        self.assertFalse(cfg.api_key_valid)
        self.assertEqual(cfg.hotkey, "f9")
        self.assertEqual(cfg.model_id, "scribe_v2")

    def test_toml_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(textwrap.dedent("""
                [elevenlabs]
                api_key = "sk-test"
                language_code = "eng"
                [hotkey]
                key = "pause"
                [audio]
                sample_rate = 8000
                min_duration = 0.5
            """))
            cfg = load_config(config_path=p, env={})
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertTrue(cfg.api_key_valid)
        self.assertEqual(cfg.language, "eng")
        self.assertEqual(cfg.hotkey, "pause")
        self.assertEqual(cfg.sample_rate, 8000)
        self.assertEqual(cfg.min_duration, 0.5)

    def test_env_overrides_toml(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text('[elevenlabs]\napi_key = "from-file"\n')
            cfg = load_config(config_path=p, env={"ELEVENLABS_API_KEY": "from-env"})
        self.assertEqual(cfg.api_key, "from-env")

    def test_malformed_toml_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.toml"
            p.write_text("this is = = not valid toml [[[")
            cfg = load_config(config_path=p, env={})
        self.assertEqual(cfg.api_key, PLACEHOLDER_API_KEY)

    def test_malformed_numeric_field_keeps_other_settings(self):
        # A bad numeric value must not discard the API key or other valid fields.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(textwrap.dedent("""
                [elevenlabs]
                api_key = "sk-keep-me"
                [audio]
                sample_rate = "not-a-number"
                channels = 2
            """))
            cfg = load_config(config_path=p, env={})
        self.assertEqual(cfg.api_key, "sk-keep-me")   # survived
        self.assertEqual(cfg.sample_rate, 16000)       # bad value -> default
        self.assertEqual(cfg.channels, 2)              # later field still applied

    def test_placeholder_not_valid(self):
        self.assertFalse(Config(api_key=PLACEHOLDER_API_KEY).api_key_valid)
        self.assertFalse(Config(api_key="").api_key_valid)
        self.assertTrue(Config(api_key="real-key").api_key_valid)

    def test_save_load_roundtrip(self):
        cfg = Config(api_key="sk-xyz", model_id="scribe_v2", language="eng",
                     hotkey="pause", audio_device="plughw:1,0", sample_rate=24000,
                     channels=2, min_duration=0.45, key_delay_ms=9)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            save_config(cfg, p)
            back = load_config(config_path=p, env={})
        for field in ("api_key", "model_id", "language", "hotkey", "audio_device",
                      "sample_rate", "channels", "min_duration", "key_delay_ms"):
            self.assertEqual(getattr(back, field), getattr(cfg, field), field)

    def test_dump_toml_handles_empty_language(self):
        text = dump_toml(Config(api_key="k"))
        self.assertIn("# language_code", text)        # commented out when unset
        self.assertIn('api_key = "k"', text)

    def test_keyterms_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            save_config(Config(api_key="k", keyterms=["Kubernetes", "Bare Metal"]), p)
            back = load_config(config_path=p, env={})
        self.assertEqual(back.keyterms, ["Kubernetes", "Bare Metal"])

    def test_keyterms_default_empty_and_dump_commented(self):
        self.assertEqual(Config().keyterms, [])
        self.assertIn("# keyterms", dump_toml(Config(api_key="k")))

    def test_sanitize_keyterms(self):
        terms = ["  Foo  ", "", "Foo", "foo",             # trim, drop empty, dedupe
                 "a" * 60,                                 # too long -> dropped
                 "one two three four five six",            # >5 words -> dropped
                 "Bare Metal"]
        self.assertEqual(sanitize_keyterms(terms), ["Foo", "Bare Metal"])

    def test_validate_keyterms_reports_reasons(self):
        accepted, warnings = validate_keyterms(
            ["ok", "Ok", "a" * 60, "one two three four five six"])
        self.assertEqual(accepted, ["ok"])
        joined = " ".join(warnings)
        self.assertIn("over 50 chars", joined)
        self.assertIn("over 5 words", joined)
        self.assertIn("duplicate", joined)

    def test_parse_keyterms_mixed_separators(self):
        # newline / comma / semicolon all separate; multi-word phrases stay intact
        self.assertEqual(
            parse_keyterms("Kubernetes, OAuth ; Bare Metal\nElevenLabs"),
            ["Kubernetes", "OAuth", "Bare Metal", "ElevenLabs"])
        self.assertEqual(parse_keyterms(""), [])
        self.assertEqual(parse_keyterms("  ,; \n  "), [])

    def test_validate_keyterms_clean_has_no_warnings(self):
        accepted, warnings = validate_keyterms(["Kubernetes", "OAuth"])
        self.assertEqual(accepted, ["Kubernetes", "OAuth"])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
