"""Clipboard-manager logic, run headless via Qt's offscreen platform.

QT_QPA_PLATFORM=offscreen keeps this fully in-process — it never touches the real
X display or the user's clipboard, so the suite is safe to run during a live session.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    _QT = True
except Exception:  # pragma: no cover - PySide6 always present in the dev venv
    _QT = False


@unittest.skipUnless(_QT, "PySide6 not available")
class ClipboardManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _mgr(self, size=20):
        from ba_ge.clipboard import ClipboardManager
        return ClipboardManager(self.app, history_size=size)

    def _record(self, m, *texts):
        for t in texts:
            m._clip.setText(t)          # fires dataChanged -> _on_change (offscreen: sync)
            self.app.processEvents()

    def test_records_a_copy(self):
        m = self._mgr()
        self._record(m, "alpha")
        self.assertEqual(m.history()[0], "alpha")

    def test_dedup_moves_existing_to_front(self):
        m = self._mgr()
        self._record(m, "a", "b", "a")
        self.assertEqual(m.history(), ["a", "b"])

    def test_suppress_excludes_own_paste(self):
        m = self._mgr()
        m._suppress += 1                       # our own clipboard write (a dictation)
        self._record(m, "dictated transcript")
        self.assertNotIn("dictated transcript", m.history())

    def test_history_is_capped(self):
        m = self._mgr(size=2)
        self._record(m, "a", "b", "c")
        self.assertEqual(m.history(), ["c", "b"])

    def test_empty_text_not_recorded(self):
        m = self._mgr()
        self._record(m, "")
        self.assertEqual(m.history(), [])

    def test_clone_preserves_text(self):
        from PySide6.QtCore import QMimeData
        from ba_ge.clipboard import ClipboardManager
        md = QMimeData()
        md.setText("keep me")
        clone = ClipboardManager._clone(md)
        self.assertEqual(clone.text(), "keep me")
        self.assertIsNot(clone, md)

    def test_clone_none_is_none(self):
        from ba_ge.clipboard import ClipboardManager
        self.assertIsNone(ClipboardManager._clone(None))


if __name__ == "__main__":
    unittest.main()
