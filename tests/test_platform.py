import sys
import unittest

from ba_ge import platform


class PrewarmMacInputSourceTest(unittest.TestCase):
    """prewarm_macos_input_source keeps pynput's TIS calls off background threads.

    On macOS 14+ pynput's listener thread would SIGTRAP calling the main-thread-
    only Text Input Source API; the prewarm caches the layout on the main thread.
    """

    def test_noop_off_macos(self):
        if platform.IS_MAC:
            self.skipTest("checks the non-macOS no-op path")
        # Must not raise and must not require importing pynput at all off macOS.
        platform.prewarm_macos_input_source()

    @unittest.skipUnless(sys.platform == "darwin", "macOS-only behaviour")
    def test_caches_and_patches_keycode_context(self):
        from pynput._util import darwin as pd
        from pynput.keyboard import _darwin as kd

        orig_util, orig_kbd = pd.keycode_context, kd.keycode_context
        orig_flag = platform._macos_input_source_cached
        try:
            platform._macos_input_source_cached = False
            platform.prewarm_macos_input_source()

            # keycode_context is rebound in BOTH modules to the cached version.
            self.assertIsNot(pd.keycode_context, orig_util)
            self.assertIs(pd.keycode_context, kd.keycode_context)

            # The cached context yields the plain (keyboard_type, layout_data) tuple.
            with pd.keycode_context() as ctx:
                self.assertEqual(len(ctx), 2)

            # Idempotent: a second call doesn't re-patch or re-query TIS.
            patched = pd.keycode_context
            platform.prewarm_macos_input_source()
            self.assertIs(pd.keycode_context, patched)
        finally:
            pd.keycode_context = orig_util
            kd.keycode_context = orig_kbd
            platform._macos_input_source_cached = orig_flag


if __name__ == "__main__":
    unittest.main()
