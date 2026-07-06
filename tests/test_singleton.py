import unittest

from ptt_dictation import singleton


class SingletonTest(unittest.TestCase):
    def test_second_acquire_same_name_fails(self):
        name = "ptt-dictation-test-7f3a91"
        self.assertTrue(singleton.acquire(name))
        self.assertFalse(singleton.acquire(name))

    def test_distinct_names_both_succeed(self):
        self.assertTrue(singleton.acquire("ptt-test-a1"))
        self.assertTrue(singleton.acquire("ptt-test-b2"))


if __name__ == "__main__":
    unittest.main()
