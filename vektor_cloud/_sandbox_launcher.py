"""Launcher for sandboxed shell commands (F5, Hypit auth follow-up 4).

The server runs in a multithreaded process (ThreadingHTTPServer), and
``preexec_fn`` is not safe there: it runs arbitrary Python in the
forked child while the parent's other threads may hold locks, and
CPython documents it as unsafe in the presence of threads. Instead,
the parent forks and execs this launcher immediately — the only work
between fork and exec is CPython's own async-signal-safe
``_execute_child`` path — and the launcher, a fresh single-threaded
process, sets the rlimits and then execs the real command. Limits are
inherited across exec, so the command and every process it spawns
carry the same per-process caps as before.

Usage: python3 _sandbox_launcher.py <cpu_seconds> <mem_bytes> <cmd...>
On a non-Unix platform (no ``resource`` module) the launcher simply
execs the command without limits — the same fallback the old
preexec_fn path had.
"""
import os
import sys

try:
    import resource
except ImportError:  # pragma: no cover - non-Unix platforms
    resource = None


def main(argv):
    cpu_seconds = int(argv[1])
    mem_bytes = int(argv[2])
    cmd = argv[3:]
    if resource is not None:
        resource.setrlimit(resource.RLIMIT_CPU,
                           (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv)
