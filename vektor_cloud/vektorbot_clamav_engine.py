"""ClamAV engine adapter for the VektorBots antivirus.

Wires the real ClamAV scanner (clamscan) into the antivirus module when it
is installed on the host. Honest by design:

- If clamscan is not on PATH, scan() returns a "staged" result and the
  caller falls back to the builtin hash+heuristic scan. A scan is NEVER
  faked — absent engine, absent scan.
- Signature updates come from freshclam, ClamAV's own updater. This module
  never runs freshclam and never auto-updates anything.
- Findings map to the same shape as the builtin scan, so quarantine and
  reporting work unchanged: quarantine still moves files aside and never
  deletes.

Scope: local filesystem only. clamscan runs as a subprocess against a
directory the caller validated. No network calls from this module, ever.
Install: `sudo apt install clamav` then `sudo freshclam`.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from .antivirus import sha256_of

# Seconds allowed per clamscan run before we call it stuck and stage it.
CLAMSCAN_TIMEOUT = 300

# clamscan prints "<path>: <SignatureName> FOUND" for infected files.
_FOUND_RE = re.compile(r"^(?P<path>.+?): (?P<sig>.+?) FOUND$")


class ClamAVEngine:
    """Adapter around the clamscan CLI."""

    def __init__(self, binary=None, timeout=CLAMSCAN_TIMEOUT):
        self.binary = binary or shutil.which("clamscan")
        self.timeout = timeout

    def available(self):
        """True only when the binary exists and is executable."""
        return (bool(self.binary)
                and os.path.isfile(self.binary)
                and os.access(self.binary, os.X_OK))

    def scan(self, target):
        """Run clamscan recursively over target.

        Returns {"engine": "clamav", "staged": bool, "reason": str|None,
        "findings": [...]}. Never raises on scan problems — problems become
        staged results, never fabricated findings.
        """
        target = Path(target)
        if not self.available():
            return {"engine": "clamav", "staged": True,
                    "reason": ("staged: clamscan not installed; install ClamAV "
                               "(e.g. `sudo apt install clamav`) and update "
                               "signatures with freshclam"),
                    "findings": []}
        try:
            proc = subprocess.run(
                [self.binary, "--no-summary", "-r", str(target)],
                capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"engine": "clamav", "staged": True,
                    "reason": f"staged: clamscan timed out after {self.timeout}s",
                    "findings": []}
        except OSError as e:
            return {"engine": "clamav", "staged": True,
                    "reason": f"staged: could not run clamscan: {e}",
                    "findings": []}
        # clamscan exit codes: 0 = clean, 1 = virus found, 2 = error.
        if proc.returncode not in (0, 1):
            err = (proc.stderr or "").strip()[:200]
            return {"engine": "clamav", "staged": True,
                    "reason": f"staged: clamscan exited with code {proc.returncode}: {err}",
                    "findings": []}
        findings = []
        for line in (proc.stdout or "").splitlines():
            m = _FOUND_RE.match(line.strip())
            if not m:
                continue
            path = m.group("path")
            sig = m.group("sig")
            try:
                digest = sha256_of(Path(path))
            except OSError:
                digest = None
            findings.append({
                "path": path,
                "sha256": digest,
                "engine": "clamav",
                "reasons": [{
                    "severity": "high",
                    "reason": f"ClamAV signature match: {sig}",
                }],
            })
        return {"engine": "clamav", "staged": False, "reason": None,
                "findings": findings}
