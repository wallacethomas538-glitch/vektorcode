"""Sandbox — hardened execution jail for engines (Phase 3).

Filesystem jail (no escapes), subprocess limits (timeout, output caps),
environment scrubbing (no key material leaks into child processes).
"""
import ctypes
import errno
import json
import os
import select
import signal
import subprocess
import sys
import threading

_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_sandbox_launcher.py")

MAX_WRITE_CHARS = 20000
MAX_READ_CHARS = 8000
MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT = 120

# F5: per-command resource caps. Render's free tier gives the whole
# instance 512 MB, so one command cannot have more than that — the
# trade-off is real: a shell that grabbed the full 512 MB would
# starve the server itself, which is why this is a bound, not a
# guarantee of headroom.
SANDBOX_MAX_MEMORY_BYTES = 512 * 1024 * 1024

# How often the tree watchdog re-sums the process tree's memory/CPU.
_TREE_WATCH_INTERVAL_S = 0.05

# Where the tree watchdog reads process state (Linux /proc). A module
# constant (not a literal) so tests can point it at a dead path and
# prove the fail-closed behavior without touching the real /proc.
_PROC_DIR = "/proc"

# Grace period for draining the pipes after a timeout kill. An escaped
# descendant may hold a pipe write-end open, and communicate() would
# wait for EOF forever. After this long we stop waiting, reap the
# direct child, and return — the handler thread must never hang on an
# escaped descendant (F5 probe: a 3s timeout blocked 20s+ on the
# follow-up-4 code).
_PIPE_DRAIN_S = 3

# How long run() waits for the supervisor's JSON status report after
# the launcher exits. The report fd is inherited by the command's
# children, so a surviving descendant could hold it open — the read
# is bounded, and a missing report is treated as an incomplete
# teardown (fail closed), never as a clean run.
_REPORT_READ_TIMEOUT_S = 2

# Linux generic syscall numbers (x86_64 and aarch64 agree on these),
# for the pidfd path of _signal_pid. Render's Python is 3.11, which
# has no os.pidfd_open, so the raw syscalls are the portable route.
_SYS_PIDFD_OPEN = 434
_SYS_PIDFD_SEND_SIGNAL = 424

# Bound on the post-run survivor check: the watchdog records every
# (pid, starttime) it ever saw in the tree, and run() verifies none
# of them is still alive. A run that forks furiously could record
# thousands; cap the set so the check stays cheap.
_MAX_SEEN_PIDS = 10000


class _IncompleteProcScan(OSError):
    """Raised by _scan_proc when some process could not be inspected
    for a reason other than it exiting mid-scan. Carries the partial
    snapshot (.procs) and the inaccessible pids (.pids). The watchdog
    treats this as "cannot establish reliable coverage" and fails
    closed; the kill path uses the partial snapshot for what it
    knows."""

    def __init__(self, procs, pids):
        super().__init__("incomplete /proc inspection")
        self.procs = procs
        self.pids = pids


def _read_starttime(pid, proc_dir=None):
    """Start time (clock ticks since boot) of pid, or None when the
    process is gone or unreadable. Read from /proc/<pid>/stat: the
    fields after the comm are state, ppid, pgrp, ..., starttime is
    the 22nd field overall (index 19 after the ')'). Together with
    the pid it identifies a process: a new process reusing the
    number always has a later start time."""
    if proc_dir is None:
        proc_dir = _PROC_DIR
    try:
        with open(os.path.join(proc_dir, str(pid), "stat"), "rb") as f:
            raw = f.read()
        rest = raw[raw.rfind(b")") + 2:].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def _pidfd_open(pid):
    """A pidfd for pid, or None when pidfd is unavailable on this
    kernel/Python. Raises ProcessLookupError when the process is
    already gone."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid, 0)
    if os.uname().machine not in ("x86_64", "aarch64"):
        return None
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.syscall(_SYS_PIDFD_OPEN, pid, 0)
    if fd is None or fd < 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            raise ProcessLookupError(f"no such process: {pid}")
        return None
    return fd


def _signal_pid(pid, starttime, sig):
    """Signal one process by identity, not by number alone.

    Returns True when the signal was delivered, False when the
    process is already gone (or is not the process that was
    enumerated). The pidfd is opened FIRST, then the /proc
    starttime is read and compared: the fd pins the process that
    owned the pid at open time, so if the pid was reused between
    the open and the read the starttimes differ and the signal is
    refused, and if it is reused after the read the fd still refers
    to the verified process — the signal cannot land on the wrong
    one either way. Falls back to verifying the /proc starttime
    immediately before os.kill when pidfd is unavailable; that
    still leaves a microsecond check-then-kill window, which is
    why pidfd is preferred wherever the kernel allows it. Twin of
    _sandbox_launcher.py::_pidfd_kill — keep in sync.
    """
    fd = None
    try:
        fd = _pidfd_open(pid)
    except ProcessLookupError:
        return False
    except OSError:
        fd = None
    if fd is not None:
        try:
            now = _read_starttime(pid)
            if now is None or now != starttime:
                return False
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            ret = libc.syscall(_SYS_PIDFD_SEND_SIGNAL, fd, sig, None, 0)
            if ret == 0:
                return True
            return ctypes.get_errno() != errno.ESRCH
        except OSError:
            return False
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    # No pidfd: verify identity, then kill.
    now = _read_starttime(pid)
    if now is None or now != starttime:
        return False
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _scan_proc(proc_dir=None):
    """One snapshot of the process table: {pid: info}.

    info: ppid, pgrp, rss_bytes, cpu_seconds (utime + stime + cutime +
    cstime, so a dead child's CPU stays counted through its parent),
    starttime (clock ticks since boot — identifies the process across
    PID reuse). Reads /proc/<pid>/stat: the fields after the comm
    (which may contain spaces, so split after the last ')') are
    state, ppid, pgrp, ..., utime, stime, cutime, cstime, ...,
    starttime, vsize, rss.
    Raises OSError when the process table cannot be listed at all —
    the caller treats that as "cannot inspect", never as "empty".
    Raises _IncompleteProcScan when individual processes could not
    be inspected for a reason other than exiting mid-scan. Only a
    missing entry (FileNotFoundError / ProcessLookupError) is an
    ordinary exit race, skipped silently: the process died between
    the listdir and the stat read. A stat that exists but cannot
    be read (PermissionError, I/O error) or parsed (ValueError /
    IndexError on a malformed or truncated stat) is incomplete
    inspection, not an exit race — a live process with an
    unreadable entry is never treated as absent. (Follow-up 9:
    ValueError / IndexError were previously classified as "torn
    reads" and skipped; that treated a live process with a
    malformed stat as gone. The classification now matches
    _children_of in _sandbox_launcher.py, fixed the same way in
    follow-up 8.) proc_dir defaults to the _PROC_DIR module
    constant, read at call time so tests can redirect it.
    """
    if proc_dir is None:
        proc_dir = _PROC_DIR
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        page = 4096
    try:
        clk = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        clk = 100
    procs = {}
    incomplete = []
    for name in os.listdir(proc_dir):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(os.path.join(proc_dir, name, "stat"), "rb") as f:
                raw = f.read()
            rest = raw[raw.rfind(b")") + 2:].split()
            procs[pid] = {
                "ppid": int(rest[1]),
                "pgrp": int(rest[2]),
                "rss_bytes": int(rest[21]) * page,
                "cpu_seconds": (int(rest[11]) + int(rest[12])
                               + int(rest[13]) + int(rest[14])) / clk,
                "starttime": int(rest[19]),
            }
        except (FileNotFoundError, ProcessLookupError):
            # Exit race: the process died between the directory
            # listing and the stat read. Only a missing entry
            # proves the process is gone.
            continue
        except (OSError, ValueError, IndexError):
            # Incomplete inspection: the entry exists but cannot
            # be read (e.g. PermissionError) or parsed (a
            # malformed / truncated stat). A live process with an
            # unreadable entry is never treated as absent.
            incomplete.append(pid)
    if incomplete:
        raise _IncompleteProcScan(procs, incomplete)
    return procs


def _descendant_pids(root_pid, procs):
    """All pids descended from root_pid through ppid links.

    Follows the parent chain transitively (BFS from the root). A pid
    whose parent is not in the table (re-parented to init, or parent
    already exited) is NOT a descendant. Under the supervisor
    launcher (follow-up 6) a daemonized descendant is re-parented to
    the supervisor, not to init, so it stays in this set while the
    run is alive — the old daemonization gap (double-fork leaving
    this set) is closed by the subreaper, not by this function.
    """
    children = {}
    for pid, info in procs.items():
        children.setdefault(info["ppid"], []).append(pid)
    seen = {root_pid}
    found = set()
    queue = [root_pid]
    while queue:
        parent = queue.pop()
        for child in children.get(parent, ()):
            if child not in seen:
                seen.add(child)
                found.add(child)
                queue.append(child)
    return found


def _tree_member_pids(pgid, root_pid, procs):
    """Pids in the process group OR descended from the root.

    The group catches the ordinary tree; the ancestry catches
    children that left the group with setsid()/setpgid() while their
    parent chain is intact, and — under the supervisor launcher —
    daemonized descendants re-parented to the supervisor.
    """
    members = {pid for pid, info in procs.items()
               if info["pgrp"] == pgid}
    members |= _descendant_pids(root_pid, procs)
    members.add(root_pid)
    return members


def _kill_tree(pgid: int, root_pid: int | None = None) -> None:
    """SIGKILL the command tree, PID-safely (follow-up 6).

    Every signal goes through _signal_pid with the starttime
    recorded at scan time (or a pidfd), so a pid reused between the
    scan and the kill is never signalled: the pidfd refers to the
    process itself, and the starttime fallback refuses a number
    whose process has changed. killpg is used only for groups whose
    leader's identity still matches the scan — a group id reused
    after its leader died is skipped, and the by-pid kills cover
    the members instead.

    The supervisor's own group (pgid == root_pid) is NEVER killpg'd:
    the supervisor must survive to reap adopted orphans at teardown;
    run() SIGKILLs it only as a last resort after its reap wait
    times out. The command child runs in its own group (setsid in
    the launcher), so its group is killed via its own pgid below.

    Order matters: the descendant list is captured BEFORE any kill.
    Killing the parent first re-parents its children to init, which
    breaks the ppid chain the scan relies on — a child killed after
    its parent would already look like init's child and be missed
    (found live: a setsid hog survived the group kill and held the
    pipes for its full 10s sleep). A process born after the scan is
    still missed by this function; the supervisor's teardown loop
    re-scans and catches it, and the SIGTERM below asks the
    supervisor to terminate from inside. When /proc cannot be read
    at all, this function signals only the supervisor (its SIGTERM
    handler kills the command child by pid, needing no /proc) — it
    never guesses at pids it cannot identify. Residual: the killpg
    leader check is check-then-kill, so a microsecond window remains
    in which the leader could exit and its pgid be reused; the
    by-pid kills (pidfd) are not affected by it.
    """
    procs = {}
    if root_pid is not None:
        try:
            procs = _scan_proc()
        except _IncompleteProcScan as exc:
            procs = exc.procs
        except OSError:
            procs = {}
    if not procs:
        # No usable snapshot: there is no safe target to aim at by
        # pid or by group. Signal the supervisor instead — it is our
        # direct child, so its pid cannot have been reused, and its
        # SIGTERM handler kills the command child from inside (it
        # knows the child's pid exactly and needs no /proc). If the
        # supervisor is already gone, there is nothing to signal.
        if root_pid is not None:
            try:
                os.kill(root_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return
    descendants = _descendant_pids(root_pid, procs)
    # Group kills: one per direct child's group, never the root's
    # own group, only when the group leader is still the process
    # the scan saw.
    seen_pgids = set()
    for pid, info in procs.items():
        if info["ppid"] != root_pid:
            continue
        pgid = info["pgrp"]
        if pgid == root_pid or pgid in seen_pgids:
            continue
        seen_pgids.add(pgid)
        leader = procs.get(pgid)
        if leader is None:
            continue
        if _read_starttime(pgid) != leader["starttime"]:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in descendants:
        info = procs.get(pid)
        if info is None:
            continue
        _signal_pid(pid, info["starttime"], signal.SIGKILL)
    # Finally, ask the supervisor to terminate the command from
    # inside as well: its handler kills the command child by pid and
    # tears down the orphans, which covers anything this scan missed
    # (a process born after the scan, or one the scan could not
    # identify). The supervisor is our direct child, so signalling
    # it by pid is safe. Harmless when the tree is already dead.
    if root_pid is not None:
        try:
            os.kill(root_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _watch_tree(pgid: int, root_pid: int, mem_cap_bytes: int,
                cpu_budget_seconds: float, stop: threading.Event,
                mem_breach: threading.Event, cpu_breach: threading.Event,
                watchdog_failed: threading.Event,
                seen: set | None = None) -> None:
    """Poll the process tree's aggregate RSS and CPU; kill past a cap.

    Members are the process group plus descendants by ancestry, so a
    child that left the group with setsid()/setpgid() is still
    monitored while its parent chain is intact, and a daemonized
    descendant re-parented to the supervisor is monitored too. CPU
    is the tree total (utime + stime + children already reaped), not
    per-process: RLIMIT_CPU bounds each process on its own, this
    bounds the sum.

    Fail-closed: when /proc cannot be inspected at all, or when some
    process cannot be inspected for a reason other than exiting
    (incomplete coverage), the watchdog kills the tree and reports
    watchdog_failed instead of letting the command run unmonitored
    — a run is never reported as successfully monitored when
    required process information is missing. The caps are enforced
    within one poll interval (~50 ms), not instantly — a fast spike
    can briefly exceed a cap before the kill lands, and a process
    that lives and dies between two polls can escape CPU accounting.

    `seen` (when given) accumulates every (pid, starttime) ever
    observed in the tree, so run() can verify after the fact that
    none of them survived.
    """
    while not stop.wait(_TREE_WATCH_INTERVAL_S):
        try:
            procs = _scan_proc()
        except _IncompleteProcScan:
            watchdog_failed.set()
            _kill_tree(pgid, root_pid)
            return
        except OSError:
            watchdog_failed.set()
            _kill_tree(pgid, root_pid)
            return
        members = _tree_member_pids(pgid, root_pid, procs)
        if seen is not None and len(seen) < _MAX_SEEN_PIDS:
            for pid in members:
                info = procs.get(pid)
                if info is not None:
                    seen.add((pid, info["starttime"]))
        rss = sum(procs[pid]["rss_bytes"] for pid in members
                  if pid in procs)
        cpu = sum(procs[pid]["cpu_seconds"] for pid in members
                  if pid in procs)
        if rss > mem_cap_bytes:
            mem_breach.set()
            _kill_tree(pgid, root_pid)
            return
        if cpu > cpu_budget_seconds:
            cpu_breach.set()
            _kill_tree(pgid, root_pid)
            return

# Env keys ending with any of these (case-insensitive) are scrubbed before
# a child process runs. Keys never leave the parent process.
# NOTE: the app's own provider keys end in _KEYS (GEMINI_KEYS, GROQ_KEYS,
# ...) and SUPABASE_KEY ends in _KEY — both suffixes must stay listed here,
# or an approved shell command can dump live keys with `env`.
_SCRUB_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD",
                    "_KEYS", "_KEY")


class SandboxEscape(Exception):
    """Raised when a path would escape the sandbox root."""


class Sandbox:
    def __init__(self, root: str):
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise ValueError(f"sandbox root must be an existing directory: {root!r}")
        self._root = real

    @property
    def root(self) -> str:
        """The jailed root (verifier uses this for inside-sandbox checks)."""
        return self._root

    def resolve(self, relpath: str) -> str:
        """Resolve relpath inside the root. Absolute paths and escapes raise
        SandboxEscape."""
        if os.path.isabs(relpath):
            raise SandboxEscape(f"absolute path refused: {relpath!r}")
        full = os.path.realpath(os.path.join(self._root, relpath))
        if os.path.commonpath([self._root, full]) != self._root:
            raise SandboxEscape(f"path escapes sandbox root: {relpath!r}")
        return full

    def write(self, relpath: str, content: str) -> dict:
        try:
            full = self.resolve(relpath)
        except SandboxEscape:
            return {"status": "refused", "reason": "path_escape"}
        if len(content) > MAX_WRITE_CHARS:
            return {"status": "refused", "reason": "too_large"}
        os.makedirs(os.path.dirname(full) or self._root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "written", "path": full}

    def read(self, relpath: str, max_chars: int = MAX_READ_CHARS) -> str:
        full = self.resolve(relpath)
        with open(full, encoding="utf-8") as f:
            return f.read(max_chars)

    def _scrubbed_env(self, extra_env: dict | None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.upper().endswith(_SCRUB_SUFFIXES)}
        if extra_env:
            # Scrub extra_env with the same suffixes: it is merged
            # AFTER the os.environ scrub, so without this a caller
            # could reintroduce a live key through extra_env.
            env.update({k: v for k, v in extra_env.items()
                        if not k.upper().endswith(_SCRUB_SUFFIXES)})
        return env

    def run(self, cmd: list, timeout: int = DEFAULT_TIMEOUT,
            extra_env: dict | None = None,
            cpu_seconds: int | None = None) -> dict:
        """Run cmd with cwd=root, a scrubbed environment, and F5
        resource caps. cpu_seconds (defaulting to the wall timeout)
        is BOTH the per-process RLIMIT_CPU and the whole-tree CPU
        budget; address space is 512 MB per process, and 512 MB for
        the whole process tree together, enforced by a watchdog.

        How the caps are applied (follow-up 4/5/6): the command is
        NOT started with preexec_fn — that is unsafe in this
        server's multithreaded process. Instead the parent execs a
        small launcher (vektor_cloud/_sandbox_launcher.py), which
        sets the rlimits in a fresh single-threaded process, makes
        itself a child subreaper, and then supervises the command.
        The command runs in its own process group, and the watchdog
        tracks the tree by process group PLUS ancestry (ppid links),
        so a child that left the group with setsid()/setpgid() is
        still monitored and killed while its parent chain is intact.
        A daemonized descendant is re-parented to the launcher
        (the subreaper), so it stays in the ancestry, is monitored,
        and is reaped by the launcher's teardown before the launcher
        exits — no process outlives run() by daemonizing (follow-up
        6; the old escape is now a passing containment test, not a
        limitation). A timeout or a breach kills the whole tree, not
        just the direct child. Returns "tree_memory_killed" /
        "tree_cpu_killed": True when the watchdog killed the tree
        for that cap, "watchdog_failed": True when /proc could not
        be inspected (the run is then refused or the tree is killed
        — never left unmonitored), "teardown_incomplete": True when
        the launcher reports orphans survived its teardown (or its
        report is missing, or the post-run survivor check finds a
        tree process still alive), and "supervisor_report_missing":
        True when the launcher's status report never arrived.

        NOT enforced, and documented as such: process count
        (RLIMIT_NPROC counts per real UID, and the shell shares its
        UID with the server itself, so it is not a safe per-command
        limit — a fork bomb is bounded only by the wall-clock
        timeout and the tree-memory watchdog, and the teardown loop
        is bounded at 3s, after which it reports incomplete rather
        than waiting forever) and network egress (no per-process
        block exists without namespaces/iptables, which need root —
        the egress allowlist covers only the app's own HTTP, not the
        shell). The caps are enforced by a ~50 ms polling monitor,
        so a fast spike can briefly exceed a cap before the kill
        lands, and a process that lives and dies between two polls
        can escape CPU accounting.
        """
        cpu_limit = timeout if cpu_seconds is None else max(1, cpu_seconds)
        # Fail closed: the aggregate caps need /proc. Refuse to start
        # an unmonitored command instead of running it with only the
        # per-process limits. (The per-process RLIMITs still apply to
        # every command that does start.)
        try:
            os.listdir(_PROC_DIR)
        except OSError:
            return {"returncode": 1,
                    "stdout": "",
                    "stderr": ("sandbox watchdog unavailable: /proc "
                               "cannot be inspected; command not run"),
                    "timed_out": False,
                    "tree_memory_killed": False,
                    "tree_cpu_killed": False,
                    "watchdog_failed": True,
                    "teardown_incomplete": False,
                    "supervisor_report_missing": False}
        report_r, report_w = os.pipe()
        launcher_cmd = [sys.executable or "python3", _LAUNCHER_PATH,
                        str(cpu_limit), str(SANDBOX_MAX_MEMORY_BYTES),
                        str(report_w), *cmd]
        proc = subprocess.Popen(
            launcher_cmd, cwd=self._root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=self._scrubbed_env(extra_env), start_new_session=True,
            pass_fds=(report_w,))
        # The parent's copy of the write end must close, or the
        # report read below would never see EOF.
        try:
            os.close(report_w)
        except OSError:
            pass
        stop = threading.Event()
        mem_breach = threading.Event()
        cpu_breach = threading.Event()
        watchdog_failed = threading.Event()
        seen: set = set()
        monitor = threading.Thread(
            target=_watch_tree,
            args=(proc.pid, proc.pid, SANDBOX_MAX_MEMORY_BYTES,
                  cpu_limit, stop, mem_breach, cpu_breach,
                  watchdog_failed, seen),
            daemon=True)
        monitor.start()
        timed_out = False
        returncode = None
        try:
            out, err = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as e:
            _kill_tree(proc.pid, proc.pid)
            # Bounded drain: an escaped descendant may hold a pipe
            # write-end open, and communicate() would wait for EOF
            # forever (the F5 probe: a 3s timeout blocked 20s+).
            # Give the pipes a short grace period, then stop waiting,
            # reap the direct child, and return. The reader threads
            # inside communicate() are daemon threads; one per pipe
            # may linger until the escaped descendant exits — the
            # handler thread itself returns regardless.
            try:
                out, err = proc.communicate(timeout=_PIPE_DRAIN_S)
            except subprocess.TimeoutExpired as e2:
                out = e2.stdout if isinstance(e2.stdout, str) else (
                    e.stdout if isinstance(e.stdout, str) else "")
                err = e2.stderr if isinstance(e2.stderr, str) else (
                    e.stderr if isinstance(e.stderr, str) else "")
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Last resort: the supervisor is not exiting
                    # (its teardown is stuck). SIGKILL it directly:
                    # it is our own unreaped child, so its pid
                    # cannot have been reused. Anything it was still
                    # reaping may survive, and the missing report
                    # says so.
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
            # The timeout sentinel stays -1 (the run_shell contract):
            # the process itself died by SIGKILL from the group kill,
            # but callers distinguish "timed out" by this value.
            returncode = -1
            timed_out = True
        finally:
            stop.set()
            monitor.join(timeout=2)
        # The launcher's status report: one JSON line on the report
        # pipe. Bounded read — a surviving descendant could hold the
        # write end open, so never wait on it unboundedly. A missing
        # report is treated as an incomplete teardown, never as a
        # clean run.
        report = None
        try:
            ready, _, _ = select.select([report_r], [], [],
                                        _REPORT_READ_TIMEOUT_S)
            if ready:
                raw = os.read(report_r, 65536)
                if raw:
                    report = json.loads(raw.decode("utf-8").strip())
        except (OSError, ValueError):
            report = None
        finally:
            try:
                os.close(report_r)
            except OSError:
                pass
        report_missing = report is None
        if (not timed_out and isinstance(report, dict)
                and isinstance(report.get("child_returncode"), int)):
            returncode = report["child_returncode"]
        teardown_incomplete = report_missing
        if isinstance(report, dict) and not report.get(
                "teardown_complete", False):
            teardown_incomplete = True
        # Independent survivor check: no process the watchdog ever
        # saw in the tree may still be alive (matched by pid AND
        # starttime, so a reused number is not mistaken for it).
        for pid, starttime in seen:
            now = _read_starttime(pid)
            if now is not None and now == starttime:
                teardown_incomplete = True
                break
        return {"returncode": returncode,
                "stdout": (out or "")[:MAX_OUTPUT_CHARS],
                "stderr": (err or "")[:MAX_OUTPUT_CHARS],
                "timed_out": timed_out,
                "tree_memory_killed": mem_breach.is_set(),
                "tree_cpu_killed": cpu_breach.is_set(),
                "watchdog_failed": watchdog_failed.is_set(),
                "teardown_incomplete": teardown_incomplete,
                "supervisor_report_missing": report_missing}

    def list_files(self) -> list:
        """Audit helper for the verifier: all files under root, relative."""
        found = []
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                found.append(os.path.relpath(full, self._root))
        return sorted(found)
