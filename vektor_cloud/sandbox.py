"""Sandbox — hardened execution jail for engines (Phase 3).

Filesystem jail (no escapes), subprocess limits (timeout, output caps),
environment scrubbing (no key material leaks into child processes).
"""
import os
import subprocess

try:
    import resource
except ImportError:  # pragma: no cover - non-Unix platforms
    resource = None

MAX_WRITE_CHARS = 20000
MAX_READ_CHARS = 8000
MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT = 120

# F5: per-command resource caps, enforced with rlimits in the child
# (see _limit_child). Render's free tier gives the whole instance
# 512 MB, so the child cannot have more than that — the trade-off is
# real: a shell that grabbed the full 512 MB would starve the server
# itself, which is why this is a bound, not a guarantee of headroom.
SANDBOX_MAX_MEMORY_BYTES = 512 * 1024 * 1024

# Env keys ending with any of these (case-insensitive) are scrubbed before
# a child process runs. Keys never leave the parent process.
# NOTE: the app's own provider keys end in _KEYS (GEMINI_KEYS, GROQ_KEYS,
# ...) and SUPABASE_KEY ends in _KEY — both suffixes must stay listed here,
# or an approved shell command can dump live keys with `env`.
_SCRUB_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD",
                    "_KEYS", "_KEY")


class SandboxEscape(Exception):
    """Raised when a path would escape the sandbox root."""


def _limit_child(cpu_seconds: int):
    """preexec_fn: cap the child process's CPU and address space (F5).

    Runs in the child after fork, before exec — only plain
    setrlimit syscalls here, never anything that takes a lock
    (preexec_fn runs while the parent's other threads are frozen).
    RLIMIT_CPU is seconds of CPU time (soft == hard: SIGXCPU, then
    SIGKILL if the process ignores it); RLIMIT_AS caps virtual
    memory. Both limits are inherited by the child's children, so
    the whole process tree is bounded.
    """
    if resource is None:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS,
                       (SANDBOX_MAX_MEMORY_BYTES, SANDBOX_MAX_MEMORY_BYTES))


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
        timeout; address space = 512 MB).

        NOT enforced, and documented as such: process count
        (RLIMIT_NPROC counts per real UID, and the shell shares its
        UID with the server itself, so it is not a safe per-command
        limit — a fork bomb is bounded only by the wall-clock
        timeout) and network egress (no per-process block exists
        without namespaces/iptables, which need root — the egress
        allowlist covers only the app's own HTTP, not the shell).
        """
        cpu_limit = timeout if cpu_seconds is None else max(1, cpu_seconds)
        try:
            r = subprocess.run(cmd, cwd=self._root, capture_output=True,
                               text=True, timeout=timeout,
                               env=self._scrubbed_env(extra_env),
                               preexec_fn=lambda: _limit_child(cpu_limit))
            return {"returncode": r.returncode,
                    "stdout": r.stdout[:MAX_OUTPUT_CHARS],
                    "stderr": r.stderr[:MAX_OUTPUT_CHARS],
                    "timed_out": False}
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            err = e.stderr if isinstance(e.stderr, str) else ""
            return {"returncode": -1,
                    "stdout": out[:MAX_OUTPUT_CHARS],
                    "stderr": err[:MAX_OUTPUT_CHARS],
                    "timed_out": True}

    def list_files(self) -> list:
        """Audit helper for the verifier: all files under root, relative."""
        found = []
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                found.append(os.path.relpath(full, self._root))
        return sorted(found)
