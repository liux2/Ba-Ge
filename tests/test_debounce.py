import unittest

from ba_ge.debounce import HoldDebouncer


class FakeTimer:
    """Controllable stand-in for threading.Timer; fire() runs the callback."""

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class HoldDebouncerTest(unittest.TestCase):
    def setUp(self):
        self.timers = []
        self.starts = 0
        self.stops = 0

    def _make(self):
        def factory(delay, cb):
            t = FakeTimer(delay, cb)
            self.timers.append(t)
            return t

        return HoldDebouncer(
            on_start=lambda: setattr(self, "starts", self.starts + 1),
            on_stop=lambda: setattr(self, "stops", self.stops + 1),
            timer_factory=factory,
        )

    def test_fresh_press_starts_once(self):
        d = self._make()
        d.press()
        self.assertEqual(self.starts, 1)
        self.assertEqual(self.stops, 0)

    def test_genuine_release_stops_after_timer(self):
        d = self._make()
        d.press()
        d.release()
        self.assertEqual(self.stops, 0)        # not yet — timer pending
        self.timers[-1].fire()                 # genuine release: no press chased it
        self.assertEqual(self.stops, 1)

    def test_autorepeat_release_press_does_not_stop(self):
        d = self._make()
        d.press()                              # real press -> start
        d.release()                            # synthetic auto-repeat release
        d.press()                              # auto-repeat press cancels the stop
        self.assertTrue(self.timers[-1].cancelled)
        self.timers[-1].fire()                 # cancelled -> no-op
        self.assertEqual(self.starts, 1)       # still a single hold
        self.assertEqual(self.stops, 0)        # never stopped mid-hold

    def test_sustained_hold_then_release(self):
        d = self._make()
        d.press()
        for _ in range(5):                     # 5 auto-repeat cycles
            d.release()
            d.press()
        d.release()                            # final, real release
        self.timers[-1].fire()
        self.assertEqual(self.starts, 1)
        self.assertEqual(self.stops, 1)

    def test_release_without_press_is_noop(self):
        d = self._make()
        d.release()
        self.assertEqual(self.timers, [])
        self.assertEqual(self.stops, 0)

    def test_callback_exception_does_not_propagate(self):
        d = HoldDebouncer(
            on_start=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            on_stop=lambda: None,
            timer_factory=FakeTimer,
        )
        d.press()  # must not raise


if __name__ == "__main__":
    unittest.main()
