"""Supervisor launcher for sandboxed shell commands (F5, Hypit
auth follow-ups 4-6).

The server runs in a multithreaded process (ThreadingHTTPServer), and
``preexec_fn`` is not safe there: it runs arbitrary Python in the
forked child while the parent's other threads may hold locks, and
CPython documents it as unsafe in the presence of threads. Instead,
the parent forks and execs this launcher immediately — the only work
between fork and exec is CPython's own async-signal-safe
``_execute_child`` path — and the launcher, a fresh single-threaded
process, sets the rlimits and then supervises the real command.
Limits are inherited across exec, so the command and every process
it spawns carry the same per-process caps as before.

Containment (follow-up 6): the launcher makes itself a child
subreaper (``prctl(PR_SET_CHILD_SUBREAPER)``) BEFORE forking the
command, so any descendant that orphans itself (double-fork
daemonization, setsid escape) is re-parented to the launcher instead
of init. When the command exits — or is killed — the launcher reaps
every adopted orphan it can find (bounded teardown loop) and only
then exits itself. A process can therefore no longer outlive
``Sandbox.run()`` by daemonizing: ``run()`` waits for the launcher,
and the launcher does not exit until its adopted children are dead
(or the teardown bound is hit, which it reports). This is a
supervisor boundary, not a cgroup: cgroup v2 is writable in this dev
VM (root), but the deployment (Render free tier) runs unprivileged
without cgroup write access, so a cgroup boundary would work here
and fail there. The subreaper needs no privilege and works in both
places. It does NOT add filesystem or network containment — F3
Decision B is unchanged; the shell child is still cwd-only.

Usage:
  python3 _sandbox_launcher.py <cpu_seconds> <mem_bytes> <report_fd> <cmd...>
report_fd is a file descriptor to write one JSON status line to at
exit ({"child_returncode", "teardown_complete", "orphans_killed",
"teardown_rounds"}), or -1 for no report. The parent passes the
write end of a pipe it reads after the launcher exits.

On a non-Unix platform (no ``resource`` module) the launcher simply
execs the command without limits — the same fallback the old
preexec_fn path had. On Unix, a failure to set the rlimits or the
subreaper is fatal (exit 2, command not run) — fail closed, never
run uncontained.
"""
import ctypes
import errno
import json
import os
import signal
import sys
import time

try:
    import resource
except ImportError:  # pragma: no cover - non-Unix platforms
    resource = None

_PR_SET_CHILD_SUBREAPER = 36
# Linux generic syscall numbers (x86_64 and aarch64 agree on these).
_SYS_PIDFD_OPEN = 434
_SYS_PIDFD_SEND_SIGNAL = 424

# Teardown bound: 30 rounds x 0.1s = 3s max. The parent's reap wait
# is 5s, so a launcher that hits this bound still exits in time for
# the parent to observe it.
_TEARDOWN_ROUNDS = 30
_TEARDOWN_DELAY_S = 0.1


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _read_starttime(pid):
    """Start time (clock ticks since boot) of pid, or None when the
    process is gone or unreadable. Read from /proc/<pid>/stat: the
    fields after the comm are state, ppid, pgrp, ..., starttime is
    the 22nd field overall (index 19 after the ')')."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        rest = raw[raw.rfind(b")") + 2:].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def _pidfd_kill(pid, starttime, sig):
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
    immediately before os.kill when pidfd is unavailable — that
    still leaves a microsecond check-then-kill window, documented
    at the caller. Twin of vektor_cloud/sandbox.py::_signal_pid —
    keep in sync (this launcher is standalone and cannot import it).
    """
    fd = None
    try:
        if hasattr(os, "pidfd_open"):
            fd = os.pidfd_open(pid, 0)
        elif os.uname().machine in ("x86_64", "aarch64"):
            libc = _libc()
            fd = libc.syscall(_SYS_PIDFD_OPEN, pid, 0)
            if fd is not None and fd < 0:
                err = ctypes.get_errno()
                if err == errno.ESRCH:
                    return False
                fd = None
    except (ProcessLookupError, OSError):
        return False
    if fd is not None:
        try:
            now = _read_starttime(pid)
            if now is None or now != starttime:
                return False
            ret = _libc().syscall(_SYS_PIDFD_SEND_SIGNAL, fd, sig,
                                  None, 0)
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


def _children_of(pid):
    """{pid: starttime} for processes whose parent is pid.

    Returns None when /proc cannot be listed at all — the caller
    must treat that as "unknown", never as "no children"."""
    found = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        child = int(name)
        try:
            with open(f"/proc/{child}/stat", "rb") as f:
                raw = f.read()
            rest = raw[raw.rfind(b")") + 2:].split()
            if int(rest[1]) == pid:
                found[child] = int(rest[19])
        except (OSError, ValueError, IndexError):
            continue
    return found


def _reap_any():
    """Reap any zombie children (adopted orphans that already died)."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return


def _teardown(child_pid=None):
    """Kill every adopted orphan, then wait for them to die.

    Loops because killing one orphan re-parents ITS children to us:
    each round re-enumerates. Bounded: after _TEARDOWN_ROUNDS the
    launcher gives up and reports teardown_complete False — it must
    still exit (the parent cannot wait forever), and the parent
    treats an incomplete teardown as a failed containment.
    When /proc cannot be inspected at all (children is None), the
    known command child is still killed by pid and the teardown is
    reported incomplete — "unknown" is never reported as clean.
    Returns (complete, orphans_killed, rounds).
    """
    killed = 0
    for round_no in range(1, _TEARDOWN_ROUNDS + 1):
        children = _children_of(os.getpid())
        if children is None:
            if child_pid is not None:
                starttime = _read_starttime(child_pid)
                if starttime is not None:
                    _pidfd_kill(child_pid, starttime, signal.SIGKILL)
            _reap_any()
            return False, killed, round_no
        if not children:
            _reap_any()
            return True, killed, round_no
        for pid, starttime in sorted(children.items()):
            if _pidfd_kill(pid, starttime, signal.SIGKILL):
                killed += 1
        _reap_any()
        time.sleep(_TEARDOWN_DELAY_S)
    # One last look: anything still here survived the bound.
    if not _children_of(os.getpid()):
        _reap_any()
        return True, killed, _TEARDOWN_ROUNDS
    _reap_any()
    return False, killed, _TEARDOWN_ROUNDS


# Set by the SIGTERM/SIGINT handler: the parent (run()) asks the
# supervisor to terminate the command from inside, which is the
# kill channel that does not depend on /proc — the supervisor knows
# the command child's pid exactly.
_terminate_requested = False


def _request_terminate(signum, frame):
    global _terminate_requested
    _terminate_requested = True


def main(argv):
    cpu_seconds = int(argv[1])
    mem_bytes = int(argv[2])
    report_fd = int(argv[3])
    cmd = argv[4:]
    if resource is None:  # pragma: no cover - non-Unix platforms
        os.execvp(cmd[0], cmd)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    try:
        ret = _libc().prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (AttributeError, OSError):
        ret = -1
    if ret != 0:
        sys.stderr.write(
            "sandbox supervisor unavailable: cannot set child "
            "subreaper; command not run\n")
        return 2
    signal.signal(signal.SIGTERM, _request_terminate)
    signal.signal(signal.SIGINT, _request_terminate)
    child = os.fork()
    if child == 0:
        # Command child: own session/group (so the parent can kill
        # its group without touching the supervisor), then exec.
        # The rlimits are inherited across the fork and the exec.
        try:
            os.setsid()
        except OSError:
            pass
        try:
            os.execvp(cmd[0], cmd)
        except OSError:
            os._exit(127)
    # Supervisor: wait for the command child, reaping adopted
    # orphans' zombies as we go, then tear down the orphans. When
    # the parent asks us to terminate (SIGTERM, the kill channel
    # that needs no /proc), kill the command child and its group
    # from inside — we know the child's pid exactly — and then tear
    # down as usual.
    status = None
    while True:
        if _terminate_requested:
            try:
                os.killpg(child, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(child, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            pid, st = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            status = 0
            break
        if pid == child:
            status = st
            break
        _reap_any()
        time.sleep(0.02)
    child_exitcode = os.waitstatus_to_exitcode(status)
    complete, killed, rounds = _teardown(child)
    if report_fd >= 0:
        report = {"child_returncode": child_exitcode,
                  "teardown_complete": complete,
                  "orphans_killed": killed,
                  "teardown_rounds": rounds}
        try:
            os.write(report_fd,
                     (json.dumps(report) + "\n").encode("utf-8"))
        except OSError:
            pass
        try:
            os.close(report_fd)
        except OSError:
            pass
    if child_exitcode < 0:
        # Die by the same signal the command died by, so the parent
        # sees the same negative returncode it saw before the
        # supervisor existed.
        sig = -child_exitcode
        try:
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        except (OSError, ValueError):
            pass
        os._exit(128 + sig)
    return child_exitcode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
