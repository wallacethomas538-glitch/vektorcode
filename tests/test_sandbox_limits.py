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

Follow-up 6: the launcher is now a supervisor — it makes itself a
child subreaper, so a double-forked daemon is adopted by the
supervisor and killed in its teardown, and the daemon LIMITATION
test is converted into a containment test. _scan_proc raises
_IncompleteProcScan when individual processes cannot be inspected
for a reason other than an exit race, and the watchdog fails closed
on it. _kill_tree signals by pidfd-verified identity (pidfd opened
first, starttime read and compared, then the signal) and asks the
supervisor to terminate from inside (its SIGTERM handler kills the
command child by pid, needing no /proc). The pipe-holding-daemon
test is converted too: the daemon no longer escapes, so run()
returns the child's exit code promptly and never reaches the
timeout path; the -1 timeout sentinel is pinned by a test that
stubs a stuck supervisor instead, since a live stuck supervisor
has no deterministic trigger (its teardown bound is 3s).
"""
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vektor_cloud.sandbox import (
    Sandbox,
    _kill_tree,
    _scan_proc,
)


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

    def test_daemonized_descendant_is_contained(self):
        # F5 (follow-up 6, Priority 1): CONVERTED from the
        # follow-up-5 LIMITATION test test_daemonized_descendant_
        # escapes (same daemon shape - double fork plus setsid).
        # The launcher is now a supervisor (child subreaper), so
        # the daemon is adopted by the supervisor and killed in its
        # teardown. The daemon writes its pid to a pidfile first so
        # this test can prove it is dead (ESRCH) after run() returns,
        # and the marker must never appear even after the daemon's
        # full 30-second sleep would have elapsed. The result must
        # report the containment as complete, not missing.
        marker = os.path.join(self.tmp.name, "daemon-marker.txt")
        pidfile = os.path.join(self.tmp.name, "daemon-pid.txt")
        daemon = (
            "import os,time; pid = os.fork()\n"
            "if pid > 0: os._exit(0)\n"
            "os.setsid()\n"
            "pid2 = os.fork()\n"
            "if pid2 > 0: os._exit(0)\n"
            f"open(\"{pidfile}\", \"w\").write(str(os.getpid()))\n"
            "time.sleep(30)\n"
            f"open(\"{marker}\", \"w\").write(\"daemon-alive\")\n"
        )
        cmd = ["/bin/sh", "-c", f"python3 -c '{daemon}'"]
        started = time.monotonic()
        res = self.box.run(cmd, timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(res["returncode"], 0, res)
        self.assertFalse(res["teardown_incomplete"], res)
        self.assertFalse(res["supervisor_report_missing"], res)
        self.assertLess(elapsed, 5, res)
        deadline = time.monotonic() + 2
        while not os.path.exists(pidfile) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pidfile),
                        "daemon never started")
        daemon_pid = int(Path(pidfile).read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(daemon_pid, 0)
        time.sleep(3)
        self.assertFalse(os.path.exists(marker),
                         "daemon outlived the run and wrote its marker")

    def test_pipe_holding_daemon_is_contained_no_timeout(self):
        # F5 (follow-up 6): CONVERTED from the follow-up-5 test
        # test_escaped_descendant_holding_pipes_does_not_hang_
        # timeout. On follow-up-4 code a daemonized descendant
        # holding the stdout pipe made run(timeout=3) block until
        # the daemon exited (a 3s timeout blocked 20s+ in the
        # probe), and the bounded drain was the only defence. The
        # daemon no longer escapes at all — the supervisor kills
        # it in teardown — so run() returns the child's exit code
        # promptly and never reaches the timeout path. The -1
        # timeout sentinel is pinned separately by
        # test_timeout_sentinel_when_supervisor_stuck, since a
        # live stuck supervisor has no deterministic trigger (its
        # teardown bound is 3s).
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
        self.assertEqual(res["returncode"], 0, res)
        self.assertFalse(res["timed_out"], res)
        self.assertFalse(res["teardown_incomplete"], res)
        self.assertLess(elapsed, 10, res)

    def test_timeout_sentinel_when_supervisor_stuck(self):
        # F5 (follow-up 6): the -1 sentinel. A live supervisor
        # always exits within its 3s teardown bound, so there is
        # no deterministic live trigger for a stuck supervisor;
        # this test stubs Popen.communicate to raise TimeoutExpired
        # on both the wall-clock call and the drain-grace call,
        # simulating a supervisor that never exits, and pins that
        # run() then returns -1 with timed_out True instead of
        # hanging. The real tree is still killed by _kill_tree
        # (the sleep is dead afterwards), so the stub only changes
        # what run() observes, not what it kills.
        real_popen = subprocess.Popen
        calls = {"n": 0}

        def failing_communicate(self, *args, **kwargs):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

        started = time.monotonic()
        with mock.patch.object(subprocess.Popen, "communicate",
                               failing_communicate):
            res = self.box.run(["/bin/sh", "-c", "sleep 30"],
                               timeout=1)
        elapsed = time.monotonic() - started
        self.assertEqual(calls["n"], 2, res)
        self.assertEqual(res["returncode"], -1, res)
        self.assertTrue(res["timed_out"], res)
        self.assertLess(elapsed, 15, res)

    def test_watchdog_incomplete_inspection_fails_closed(self):
        # F5 (follow-up 6, Priority 4): if the watchdog cannot read
        # a tree member's /proc stat for a reason other than an
        # exit race, it must fail closed - watchdog_failed True
        # and the tree killed - never silently report success.
        # The patched open raises PermissionError for
        # /proc/<pid>/stat reads only; everything else delegates
        # to the real open. The kill itself does not depend on
        # /proc: _kill_tree signals the supervisor, whose handler
        # kills the command child by pid from inside.
        real_open = open

        def guarded(path, *args, **kwargs):
            s = os.fspath(path)
            if isinstance(s, str) and re.fullmatch(r"/proc/\d+/stat", s):
                raise PermissionError("simulated inspection failure")
            return real_open(path, *args, **kwargs)

        started = time.monotonic()
        with mock.patch("builtins.open", side_effect=guarded):
            res = self.box.run(["/bin/sh", "-c", "sleep 30"],
                               timeout=10)
        elapsed = time.monotonic() - started
        self.assertTrue(res["watchdog_failed"], res)
        self.assertLess(res["returncode"], 0, res)
        self.assertLess(elapsed, 10, res)

    def test_watchdog_exit_race_stays_silent_skip(self):
        # F5 (follow-up 6, Priority 4): the other side of the
        # contract - a FileNotFoundError on a /proc stat read is
        # an ordinary exit race (the process died between the
        # listdir and the read) and stays a silent skip, NOT a
        # watchdog failure. Here every stat read "races", so the
        # watchdog sees an empty tree and never trips; the run
        # ends on the wall-clock timeout instead, killed from
        # inside by the supervisor.
        real_open = open

        def guarded(path, *args, **kwargs):
            s = os.fspath(path)
            if isinstance(s, str) and re.fullmatch(r"/proc/\d+/stat", s):
                raise FileNotFoundError("simulated exit race")
            return real_open(path, *args, **kwargs)

        started = time.monotonic()
        with mock.patch("builtins.open", side_effect=guarded):
            res = self.box.run(["/bin/sh", "-c", "sleep 30"],
                               timeout=3)
        elapsed = time.monotonic() - started
        self.assertFalse(res["watchdog_failed"], res)
        self.assertTrue(res["timed_out"], res)
        self.assertLess(elapsed, 10, res)

    # -- P6 helpers -------------------------------------------------
    def _spawn_sleep_tree(self, root_cmd):
        # Start a root process that spawns a sleep child, and return
        # (root_proc, child_pid) once the child shows up in the
        # process table. The root keeps sleeping itself, so it
        # survives _kill_tree (the root is never a kill target).
        root = subprocess.Popen(root_cmd, start_new_session=True)
        deadline = time.monotonic() + 5
        child = None
        while time.monotonic() < deadline:
            procs = _scan_proc()
            for pid, info in procs.items():
                if info["ppid"] == root.pid:
                    child = pid
                    break
            if child is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(child, "sleep child never appeared")
        return root, child

    def _alive(self, pid):
        # Alive means running, not merely present: a zombie (killed
        # but not yet reaped by its parent, which may be sleeping
        # for a minute) still answers kill(pid, 0) with success,
        # so read the state from /proc and treat "Z" as dead.
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
        except OSError:
            return False
        rest = raw[raw.rfind(b")") + 2:].split()
        return rest[0] != b"Z"

    def test_kill_tree_kills_descendant_by_identity(self):
        # P6 positive control: _kill_tree on a real tree kills the
        # descendant (verified by pidfd + starttime) and leaves the
        # root alone - the root is never a kill target.
        # The root ignores SIGTERM, like the supervisor does:
        # _kill_tree signals the root (its SIGTERM handler kills
        # the command child from inside), and a plain root would
        # die on it. The identity checks below are about the
        # descendant, not the root.
        root, child = self._spawn_sleep_tree(
            ["python3", "-c",
             "import signal,subprocess,time; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "subprocess.Popen(['sleep', '30']); time.sleep(60)"])
        try:
            self.assertTrue(self._alive(child))
            _kill_tree(pgid=root.pid, root_pid=root.pid)
            deadline = time.monotonic() + 3
            while self._alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(self._alive(child),
                             "descendant survived _kill_tree")
            self.assertIsNone(root.poll(), "root was killed too")
        finally:
            if root.poll() is None:
                root.kill()
            root.wait()

    def test_kill_tree_skips_reused_pid(self):
        # P6 (Priority 6): simulate PID reuse - the scan reports
        # the sleep child with a WRONG starttime, as if its pid
        # had been reused by a different process. _kill_tree must
        # NOT signal it: the pidfd path opens the fd first, then
        # reads the real starttime and refuses on the mismatch.
        # The root ignores SIGTERM, like the supervisor does:
        # _kill_tree signals the root (its SIGTERM handler kills
        # the command child from inside), and a plain root would
        # die on it. The identity checks below are about the
        # descendant, not the root.
        root, child = self._spawn_sleep_tree(
            ["python3", "-c",
             "import signal,subprocess,time; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "subprocess.Popen(['sleep', '30']); time.sleep(60)"])
        try:
            real = _scan_proc()
            self.assertIn(child, real)
            forged = dict(real)
            info = dict(forged[child])
            info["starttime"] = info["starttime"] + 12345
            forged[child] = info
            with mock.patch("vektor_cloud.sandbox._scan_proc",
                           return_value=forged):
                _kill_tree(pgid=root.pid, root_pid=root.pid)
            time.sleep(0.3)
            self.assertTrue(self._alive(child),
                            "reused pid was signalled anyway")
            self.assertIsNone(root.poll())
        finally:
            if root.poll() is None:
                root.kill()
            root.wait()
            # The child outlives the root (re-parented); kill it.
            if self._alive(child):
                os.kill(child, signal.SIGKILL)

    def test_kill_tree_skips_group_when_leader_identity_changed(self):
        # P6 (Priority 6): the direct child setsid'd into its own
        # group and spawned a sleep in that group. The scan reports
        # the group leader with a WRONG starttime (reused pid) but
        # the sleep with its correct one. killpg must be skipped
        # (the leader's identity no longer matches), the leader
        # itself must not be signalled by pid either, but the
        # correctly identified sleep must still be killed by pid.
        root = subprocess.Popen(
            ["/bin/sh", "-c",
             "python3 -c \"import os,time; os.setsid(); "
             "os.system('sleep 30'); time.sleep(30)\" & wait"],
            start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            leader = sleeper = None
            while time.monotonic() < deadline:
                procs = _scan_proc()
                for pid, info in procs.items():
                    if info["ppid"] == root.pid and info["pgrp"] == pid:
                        leader = pid
                    if info["ppid"] == leader and leader is not None:
                        sleeper = pid
                if leader is not None and sleeper is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(leader, "group leader never appeared")
            self.assertIsNotNone(sleeper, "group sleeper never appeared")
            real = _scan_proc()
            forged = dict(real)
            info = dict(forged[leader])
            info["starttime"] = info["starttime"] + 12345
            forged[leader] = info
            with mock.patch("vektor_cloud.sandbox._scan_proc",
                           return_value=forged):
                _kill_tree(pgid=root.pid, root_pid=root.pid)
            deadline = time.monotonic() + 3
            while self._alive(sleeper) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(self._alive(sleeper),
                             "correctly identified sleeper survived")
            self.assertTrue(self._alive(leader),
                            "leader with changed identity was killed")
        finally:
            for pid in (leader, sleeper):
                if pid is not None and self._alive(pid):
                    os.kill(pid, signal.SIGKILL)
            if root.poll() is None:
                root.kill()
            root.wait()


if __name__ == "__main__":
    unittest.main()
