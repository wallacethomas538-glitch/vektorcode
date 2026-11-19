"""Sandbox — hardened execution jail for engines (Phase 3).

Filesystem jail (no escapes), subprocess limits (timeout, output caps),
environment scrubbing (no key material leaks into child processes).
"""
import os
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


def _scan_proc(proc_dir=None):
    """One snapshot of the process table: {pid: info}.

    info: ppid, pgrp, rss_bytes, cpu_seconds (utime + stime + cutime +
    cstime, so a dead child's CPU stays counted through its parent).
    Reads /proc/<pid>/stat: the fields after the comm (which may
    contain spaces, so split after the last ')') are state, ppid,
    pgrp, ..., utime, stime, cutime, cstime, ..., rss.
    Raises OSError when the process table cannot be listed at all —
    the caller treats that as "cannot inspect", never as "empty".
    Per-entry read errors are skipped (a process that exited mid-scan
    is simply gone). proc_dir defaults to the _PROC_DIR module
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
    for name in os.listdir(proc_dir):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_dir, name, "stat"), "rb") as f:
                raw = f.read()
            rest = raw[raw.rfind(b")") + 2:].split()
            procs[int(name)] = {
                "ppid": int(rest[1]),
                "pgrp": int(rest[2]),
                "rss_bytes": int(rest[21]) * page,
                "cpu_seconds": (int(rest[11]) + int(rest[12])
                               + int(rest[13]) + int(rest[14])) / clk,
            }
        except (OSError, ValueError, IndexError):
            continue
    return procs


def _descendant_pids(root_pid, procs):
    """All pids descended from root_pid through ppid links.

    Follows the parent chain transitively (BFS from the root). A pid
    whose parent is not in the table (re-parented to init, or parent
    already exited) is NOT a descendant — that is exactly the
    daemonization gap: a double-forked daemon leaves this set.
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
    parent chain is intact. A daemonized descendant (re-parented to
    init) is in neither set — see _watch_tree and the F5 report.
    """
    members = {pid for pid, info in procs.items()
               if info["pgrp"] == pgid}
    members |= _descendant_pids(root_pid, procs)
    members.add(root_pid)
    return members


def _kill_tree(pgid: int, root_pid: int | None = None) -> None:
    """SIGKILL every process in the group, plus known descendants.

    The group kill catches the ordinary tree. The descendant kill
    catches children that left the group with setsid()/setpgid().
    A daemonized descendant (re-parented to init) is caught by
    neither — it is not identifiable as a member of this tree.

    Order matters: the descendant list is captured BEFORE any kill.
    Killing the parent first re-parents its children to init, which
    breaks the ppid chain the scan relies on — a child killed after
    its parent would already look like init's child and be missed
    (found live: a setsid hog survived the group kill and held the
    pipes for its full 10s sleep). A process born after the scan is
    still missed; that window is microseconds, not the whole kill.
    When /proc cannot be read at all, the group kill still runs.
    """
    descendants = set()
    if root_pid is not None:
        try:
            procs = _scan_proc()
            descendants = _descendant_pids(root_pid, procs)
        except OSError:
            descendants = set()
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _watch_tree(pgid: int, root_pid: int, mem_cap_bytes: int,
                cpu_budget_seconds: float, stop: threading.Event,
                mem_breach: threading.Event, cpu_breach: threading.Event,
                watchdog_failed: threading.Event) -> None:
    """Poll the process tree's aggregate RSS and CPU; kill past a cap.

    Members are the process group plus descendants by ancestry, so a
    child that left the group with setsid()/setpgid() is still
    monitored while its parent chain is intact. CPU is the tree
    total (utime + stime + children already reaped), not per-process:
    RLIMIT_CPU bounds each process on its own, this bounds the sum.

    Fail-closed: when /proc cannot be inspected at all, the watchdog
    kills the tree and reports watchdog_failed instead of letting the
    command run unmonitored. The caps are enforced within one poll
    interval (~50 ms), not instantly — a fast spike can briefly
    exceed a cap before the kill lands, and a process that lives and
    dies between two polls can escape CPU accounting.
    """
    while not stop.wait(_TREE_WATCH_INTERVAL_S):
        try:
            procs = _scan_proc()
        except OSError:
            watchdog_failed.set()
            _kill_tree(pgid, root_pid)
            return
        members = _tree_member_pids(pgid, root_pid, procs)
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

        How the caps are applied (follow-up 4/5): the command is NOT
        started with preexec_fn — that is unsafe in this server's
        multithreaded process. Instead the parent execs a small
        launcher (vektor_cloud/_sandbox_launcher.py), which sets the
        rlimits in a fresh single-threaded process and then execs the
        command. The command runs in its own process group, and the
        watchdog tracks the tree by process group PLUS ancestry
        (ppid links), so a child that left the group with
        setsid()/setpgid() is still monitored and killed while its
        parent chain is intact. A timeout or a breach kills the
        whole tree, not just the direct child. Returns
        "tree_memory_killed" / "tree_cpu_killed": True when the
        watchdog killed the tree for that cap, and
        "watchdog_failed": True when /proc could not be inspected
        (the run is then refused or the tree is killed — never left
        unmonitored).

        NOT enforced, and documented as such: process count
        (RLIMIT_NPROC counts per real UID, and the shell shares its
        UID with the server itself, so it is not a safe per-command
        limit — a fork bomb is bounded only by the wall-clock
        timeout and the tree-memory watchdog) and network egress (no
        per-process block exists without namespaces/iptables, which
        need root — the egress allowlist covers only the app's own
        HTTP, not the shell). The caps are enforced by a ~50 ms
        polling monitor, so a fast spike can briefly exceed a cap
        before the kill lands, and a process that lives and dies
        between two polls can escape CPU accounting. A daemonized
        descendant (double-fork, re-parented to init) leaves both
        the process group and the ancestry, so it is neither
        monitored nor killed — it keeps only the per-process
        RLIMIT_AS / RLIMIT_CPU it inherited.
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
                    "watchdog_failed": True}
        launcher_cmd = [sys.executable or "python3", _LAUNCHER_PATH,
                        str(cpu_limit), str(SANDBOX_MAX_MEMORY_BYTES),
                        *cmd]
        proc = subprocess.Popen(
            launcher_cmd, cwd=self._root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=self._scrubbed_env(extra_env), start_new_session=True)
        stop = threading.Event()
        mem_breach = threading.Event()
        cpu_breach = threading.Event()
        watchdog_failed = threading.Event()
        monitor = threading.Thread(
            target=_watch_tree,
            args=(proc.pid, proc.pid, SANDBOX_MAX_MEMORY_BYTES,
                  cpu_limit, stop, mem_breach, cpu_breach,
                  watchdog_failed),
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
        return {"returncode": returncode,
                "stdout": (out or "")[:MAX_OUTPUT_CHARS],
                "stderr": (err or "")[:MAX_OUTPUT_CHARS],
                "timed_out": timed_out,
                "tree_memory_killed": mem_breach.is_set(),
                "tree_cpu_killed": cpu_breach.is_set(),
                "watchdog_failed": watchdog_failed.is_set()}

    def list_files(self) -> list:
        """Audit helper for the verifier: all files under root, relative."""
        found = []
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                found.append(os.path.relpath(full, self._root))
        return sorted(found)
