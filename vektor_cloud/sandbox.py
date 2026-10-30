"""Sandbox — hardened execution jail for engines (Phase 3).

Filesystem jail (no escapes), subprocess limits (timeout, output caps),
environment scrubbing (no key material leaks into child processes).
"""
import os
import subprocess

MAX_WRITE_CHARS = 20000
MAX_READ_CHARS = 8000
MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT = 120

# Env keys ending with any of these (case-insensitive) are scrubbed before
# a child process runs. Keys never leave the parent process.
_SCRUB_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


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
            env.update(extra_env)
        return env

    def run(self, cmd: list, timeout: int = DEFAULT_TIMEOUT,
            extra_env: dict | None = None) -> dict:
        """Run cmd with cwd=root and a scrubbed environment."""
        try:
            r = subprocess.run(cmd, cwd=self._root, capture_output=True,
                               text=True, timeout=timeout,
                               env=self._scrubbed_env(extra_env))
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
