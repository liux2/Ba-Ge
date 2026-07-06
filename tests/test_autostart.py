import tempfile
import unittest
from pathlib import Path

from ba_ge import autostart


class AutostartTest(unittest.TestCase):
    def test_disabled_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(autostart.is_enabled(Path(d) / "x.desktop"))

    def test_enable_then_disable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bage.desktop"
            autostart.set_enabled(True, "ba-ge", p)
            self.assertTrue(p.exists())
            self.assertTrue(autostart.is_enabled(p))
            self.assertIn("Exec=ba-ge", p.read_text())
            autostart.set_enabled(False, path=p)
            self.assertFalse(p.exists())
            self.assertFalse(autostart.is_enabled(p))

    def test_explicit_false_flag_counts_as_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bage.desktop"
            p.write_text("[Desktop Entry]\nX-GNOME-Autostart-enabled=false\n")
            self.assertFalse(autostart.is_enabled(p))


if __name__ == "__main__":
    unittest.main()
