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

# How often the tree watchdog re-sums the process group's memory.
_TREE_WATCH_INTERVAL_S = 0.05


def _kill_tree(pgid: int) -> None:
    """SIGKILL every process in the group (launcher + descendants)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tree_rss_bytes(pgid: int) -> int:
    """Sum of resident memory of every process in the group (Linux).

    Reads /proc/<pid>/stat: the pgrp is the field after the comm
    (which may contain spaces, so split after the last ')'), and
    rss is 21 fields past that. Raises OSError when /proc is
    unavailable — the caller treats that as "no tree data".
    """
    total = 0
    page = os.sysconf("SC_PAGE_SIZE")
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                raw = f.read()
            rest = raw[raw.rfind(b")") + 2:].split()
            if int(rest[2]) != pgid:
                continue
            total += int(rest[21]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total


def _watch_tree(pgid: int, cap_bytes: int, stop: threading.Event,
                breach: threading.Event) -> None:
    """Poll the process group's aggregate RSS; kill it past the cap.

    RLIMIT_AS bounds each process on its own — a tree of N processes
    could otherwise hold N x 512 MB. This monitor enforces the
    combined ceiling instead. It is best-effort: a fast spike can
    briefly exceed the cap inside one poll interval before the kill
    lands, so the ceiling is enforced within ~50 ms, not instantly.
    """
    while not stop.wait(_TREE_WATCH_INTERVAL_S):
        try:
            if _tree_rss_bytes(pgid) > cap_bytes:
                breach.set()
                _kill_tree(pgid)
                return
        except OSError:
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
        resource caps (CPU = cpu_seconds, defaulting to the wall
        timeout; address space = 512 MB per process, and 512 MB for
        the whole process tree together, enforced by a watchdog).

        How the caps are applied (follow-up 4): the command is NOT
        started with preexec_fn — that is unsafe in this server's
        multithreaded process. Instead the parent execs a small
        launcher (vektor_cloud/_sandbox_launcher.py), which sets the
        rlimits in a fresh single-threaded process and then execs the
        command. The command runs in its own process group, so a
        timeout or a tree-memory breach kills the whole tree, not
        just the direct child (the old code left grandchildren
        running after a timeout). Returns "tree_memory_killed": True
        when the watchdog killed the tree.

        NOT enforced, and documented as such: process count
        (RLIMIT_NPROC counts per real UID, and the shell shares its
        UID with the server itself, so it is not a safe per-command
        limit — a fork bomb is bounded only by the wall-clock
        timeout and the tree-memory watchdog) and network egress (no
        per-process block exists without namespaces/iptables, which
        need root — the egress allowlist covers only the app's own
        HTTP, not the shell). The tree ceiling is enforced by a
        ~50 ms polling monitor, so a fast spike can briefly exceed
        it before the kill lands.
        """
        cpu_limit = timeout if cpu_seconds is None else max(1, cpu_seconds)
        launcher_cmd = [sys.executable or "python3", _LAUNCHER_PATH,
                        str(cpu_limit), str(SANDBOX_MAX_MEMORY_BYTES),
                        *cmd]
        proc = subprocess.Popen(
            launcher_cmd, cwd=self._root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=self._scrubbed_env(extra_env), start_new_session=True)
        stop = threading.Event()
        breach = threading.Event()
        monitor = threading.Thread(
            target=_watch_tree,
            args=(proc.pid, SANDBOX_MAX_MEMORY_BYTES, stop, breach),
            daemon=True)
        monitor.start()
        timed_out = False
        returncode = None
        try:
            out, err = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as e:
            _kill_tree(proc.pid)
            out, err = proc.communicate()
            out = e.stdout if isinstance(e.stdout, str) else (out or "")
            err = e.stderr if isinstance(e.stderr, str) else (err or "")
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
                "tree_memory_killed": breach.is_set()}

    def list_files(self) -> list:
        """Audit helper for the verifier: all files under root, relative."""
        found = []
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                found.append(os.path.relpath(full, self._root))
        return sorted(found)
