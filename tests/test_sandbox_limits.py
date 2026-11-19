"""Tests for F5 sandbox resource limits (hypit-auth-followup-3/4).

Covers vektor_cloud/sandbox.py only — no server, no network.
A CPU burner must die by signal (the rlimit firing), not by the
wall-clock timeout; a 1 GB allocation must fail under the 512 MB
address-space cap; a normal command must still work.

Follow-up 4: the caps are applied by a launcher process, not
preexec_fn (unsafe in the multithreaded server). Two children at
~300 MB each must trip the 512 MB *tree* ceiling even though each
is under its own cap, and concurrent runs must keep their limits.

Follow-up 5: the watchdog tracks the tree by process group PLUS
ancestry (setsid/setpgid children stay monitored); cpu_seconds is
also the tree-total CPU budget; a missing /proc refuses the run
(fail-closed); a daemonized descendant holding the pipes cannot hang
a timeout (bounded drain). The daemonization escape itself is still
open and is pinned by a LIMITATION test, not a passing fix.
"""
import os
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


    def test_setsid_child_is_still_monitored(self):
        # F5 (follow-up 5): two hogs that leave the process group
        # with setsid() — on follow-up-4 code the pgrp-only watchdog
        # never saw them (they ran the full 10s sleep, and
        # tree_memory_killed stayed False). The ancestry-aware
        # watchdog must kill them.
        hog = ("import os,time; os.setsid(); "
               "a = bytearray(300 * 1024 * 1024); "
               "a[0::4096] = bytes(len(a) // 4096); time.sleep(10)")
        cmd = ["/bin/sh", "-c",
               f"python3 -c '{hog}' & python3 -c '{hog}' & wait"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30)
        elapsed = time.monotonic() - started
        self.assertTrue(res["tree_memory_killed"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 8, res)

    def test_setpgid_child_is_still_monitored(self):
        # F5 (follow-up 5): same as setsid, via setpgid(0, 0) — the
        # child joins a new process group of its own.
        hog = ("import os,time; os.setpgid(0, 0); "
               "a = bytearray(300 * 1024 * 1024); "
               "a[0::4096] = bytes(len(a) // 4096); time.sleep(10)")
        cmd = ["/bin/sh", "-c",
               f"python3 -c '{hog}' & python3 -c '{hog}' & wait"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30)
        elapsed = time.monotonic() - started
        self.assertTrue(res["tree_memory_killed"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 8, res)

    def test_aggregate_cpu_budget_kills_tree(self):
        # F5 (follow-up 5): RLIMIT_CPU bounds each process on its
        # own — two burners with cpu_seconds=2 could burn 4
        # CPU-seconds between them. The tree CPU budget (cpu_seconds
        # total) must kill the tree at ~2 CPU-seconds, well before
        # either burner reaches its own 2s rlimit and well before
        # the 30s wall timeout.
        cmd = ["/bin/sh", "-c",
               "while :; do :; done & while :; do :; done & wait"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30, cpu_seconds=2)
        elapsed = time.monotonic() - started
        self.assertTrue(res["tree_cpu_killed"], res)
        self.assertFalse(res["tree_memory_killed"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 10, res)

    def test_watchdog_unavailable_refuses_run(self):
        # F5 (follow-up 5): when /proc cannot be inspected, the run
        # is refused instead of starting unmonitored (fail-closed).
        # The command must not run at all — no marker file — and
        # the result says so plainly.
        import vektor_cloud.sandbox as sandbox_mod

        marker = os.path.join(self.tmp.name, "should-not-exist.txt")
        old = sandbox_mod._PROC_DIR
        try:
            sandbox_mod._PROC_DIR = "/nonexistent-proc-dir-for-test"
            res = self.box.run(
                ["/bin/sh", "-c", f"touch {marker}"], timeout=10)
        finally:
            sandbox_mod._PROC_DIR = old
        self.assertTrue(res["watchdog_failed"], res)
        self.assertEqual(res["returncode"], 1, res)
        self.assertEqual(res["stdout"], "")
        self.assertIn("/proc", res["stderr"], res)
        self.assertFalse(os.path.exists(marker))

    def test_daemonized_descendant_escapes(self):
        # F5 (follow-up 5) LIMITATION: a double-forked daemon leaves
        # both the process group and the ancestry (re-parented to
        # init), so it is neither monitored nor killed. This test
        # PASSES while the limitation exists — the daemon writes its
        # marker after run() has returned. It must be UPDATED (not
        # deleted) the day real containment lands, like the F3
        # tripwire test.
        marker = os.path.join(self.tmp.name, "daemon-marker.txt")
        daemon = (
            "import os,time; pid = os.fork()\n"
            "if pid > 0: os._exit(0)\n"
            "os.setsid()\n"
            "pid2 = os.fork()\n"
            "if pid2 > 0: os._exit(0)\n"
            "time.sleep(2)\n"
            f"open(\"{marker}\", \"w\").write(\"daemon-alive\")\n"
        )
        cmd = ["/bin/sh", "-c", f"python3 -c '{daemon}'"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(res["returncode"], 0, res)
        self.assertLess(elapsed, 5, res)
        deadline = time.monotonic() + 8
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(os.path.exists(marker),
                        "daemon did not outlive the run")

    def test_escaped_descendant_holding_pipes_does_not_hang_timeout(self):
        # F5 (follow-up 5): on follow-up-4 code, a daemonized
        # descendant holding the stdout pipe made run(timeout=3)
        # block until the daemon exited (a 3s timeout blocked 20s+
        # in the probe). The bounded drain must return the handler
        # thread at timeout + drain grace, with the -1 sentinel.
        daemon = (
            "import os,time,sys; pid = os.fork()\n"
            "if pid > 0: os._exit(0)\n"
            "os.setsid()\n"
            "pid2 = os.fork()\n"
            "if pid2 > 0: os._exit(0)\n"
            # The grandchild holds the inherited stdout pipe open by
            # staying alive (no write needed): EOF cannot arrive
            # until it exits.
            "time.sleep(12)\n"
        )
        cmd = ["/bin/sh", "-c", f"python3 -c '{daemon}'"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual(res["returncode"], -1, res)
        self.assertTrue(res["timed_out"], res)
        self.assertLess(elapsed, 10, res)


if __name__ == "__main__":
    unittest.main()
