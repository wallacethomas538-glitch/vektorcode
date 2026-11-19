"""Tests for F5 sandbox resource limits (hypit-auth-followup-3/4).

Covers vektor_cloud/sandbox.py only — no server, no network.
A CPU burner must die by signal (the rlimit firing), not by the
wall-clock timeout; a 1 GB allocation must fail under the 512 MB
address-space cap; a normal command must still work.

Follow-up 4: the caps are applied by a launcher process, not
preexec_fn (unsafe in the multithreaded server). Two children at
~300 MB each must trip the 512 MB *tree* ceiling even though each
is under its own cap, and concurrent runs must keep their limits.
"""
import tempfile
import threading
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

    def test_tree_memory_ceiling_kills_two_hogs(self):
        # Two children at ~300 MB each: every single process is under
        # its own 512 MB RLIMIT_AS, so only the tree watchdog can stop
        # them — the aggregate (~630 MB) exceeds the 512 MB tree cap.
        # Each hog touches every page (so the RSS is real, not lazy)
        # and then sleeps 10s; the watchdog must kill the tree long
        # before the sleep finishes.
        hog = ("import time; a = bytearray(300 * 1024 * 1024); "
               "a[0::4096] = bytes(len(a) // 4096); time.sleep(10)")
        cmd = ["/bin/sh", "-c",
               f"python3 -c '{hog}' & python3 -c '{hog}' & wait"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30)
        elapsed = time.monotonic() - started
        self.assertTrue(res["tree_memory_killed"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 8, res)

    def test_concurrent_runs_keep_limits(self):
        # Four runs at once (three echoes + one CPU burner): every
        # run must return its own result, the burner must still die
        # by signal (not by the wall timeout), and nothing may hang
        # or leak into another run's output.
        results = []

        def run_echo(i):
            results.append((i, self.box.run(
                ["/bin/sh", "-c", f"echo out-{i}"], timeout=10)))

        def run_burner():
            results.append(("burner", self.box.run(
                ["/bin/sh", "-c", "while :; do :; done"],
                timeout=30, cpu_seconds=2)))

        threads = [threading.Thread(target=run_echo, args=(i,))
                   for i in range(3)]
        threads.append(threading.Thread(target=run_burner))
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=35)
        self.assertLess(time.monotonic() - started, 25)
        self.assertFalse(any(t.is_alive() for t in threads))
        by_key = dict(results)
        self.assertEqual(len(results), 4, results)
        for i in range(3):
            self.assertEqual(by_key[i]["returncode"], 0, by_key[i])
            self.assertEqual(by_key[i]["stdout"].strip(), f"out-{i}")
        burner = by_key["burner"]
        self.assertFalse(burner["timed_out"], burner)
        self.assertLess(burner["returncode"], 0, burner)


if __name__ == "__main__":
    unittest.main()
