"""Tests for F5 sandbox resource limits (hypit-auth-followup-3).

Covers vektor_cloud/sandbox.py only — no server, no network.
A CPU burner must die by signal (the rlimit firing), not by the
wall-clock timeout; a 1 GB allocation must fail under the 512 MB
address-space cap; a normal command must still work.
"""
import tempfile
import time
import unittest
from pathlib import Path

from vektor_cloud.sandbox import Sandbox


class ResourceLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cpu_burner_dies_by_signal_not_timeout(self):
        # A tight loop burns CPU: with a 3-second CPU cap and a
        # 30-second wall timeout, the rlimit (SIGXCPU) must kill it
        # long before the wall clock does — so timed_out stays False
        # and the returncode is a negative signal number.
        started = time.monotonic()
        res = self.box.run(["/bin/sh", "-c", "while :; do :; done"],
                           timeout=30, cpu_seconds=3)
        elapsed = time.monotonic() - started
        self.assertFalse(res["timed_out"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 15)

    def test_memory_hog_fails_fast(self):
        # 1 GB in one allocation exceeds the 512 MB address-space cap
        # (MemoryError -> nonzero exit), and it fails in seconds.
        started = time.monotonic()
        res = self.box.run(
            ["python3", "-c",
             "a = bytearray(1024 * 1024 * 1024); print(len(a))"],
            timeout=30)
        elapsed = time.monotonic() - started
        self.assertNotEqual(res["returncode"], 0, res)
        self.assertLess(elapsed, 20)

    def test_normal_command_still_works(self):
        res = self.box.run(["/bin/sh", "-c", "echo hi"], timeout=10)
        self.assertEqual(res["returncode"], 0, res)
        self.assertEqual(res["stdout"].strip(), "hi")
        self.assertFalse(res["timed_out"])


if __name__ == "__main__":
    unittest.main()
