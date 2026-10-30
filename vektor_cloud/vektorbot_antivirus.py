"""VektorBots antivirus: hash + heuristic scanning with quarantine.

HONEST LIMITS — read this before relying on it:
- This is a file scanner and quarantine tool. It is NOT a replacement for
  real antivirus software.
- No real-time protection: it only finds what it scans, when it scans.
- No signature updates of its own: the builtin blocklist is a local text
  file you maintain. When the ClamAV engine is used, signatures come from
  ClamAV's own databases, updated by freshclam — this module never runs
  freshclam and never auto-updates anything.
- No rootkit detection, no memory scanning, no behavioral analysis.
- Heuristics flag suspicious shapes; they can false-positive. Every finding
  carries a reason so a human can judge.

What it DOES do, safely:
- Use the real ClamAV engine when clamscan is installed (Antivirus.scan
  with engine="auto" or "clamav"); fall back to the builtin hash+heuristic
  scan with an honest note when it is not. A scan is never faked.
- SHA256 every file under a directory and compare against a local blocklist.
- Flag double extensions (report.pdf.exe), executables hiding in temp or
  download folders, MZ/PE headers in files that are not executables, and
  zero-byte files masquerading as documents.
- QUARANTINE, never delete: flagged files are moved aside with their hash,
  original path, and timestamp logged. A restore function puts them back.
- Report everything in the repo's signed JSON report shape for the
  orchestrator.

Scope: local filesystem only. No network calls, ever.
"""
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPORT_SCHEMA = "1.0"

# Suffixes treated as executable for heuristic purposes.
EXECUTABLE_SUFFIXES = frozenset({
    ".exe", ".bat", ".cmd", ".scr", ".msi", ".com", ".pif", ".ps1",
    ".vbs", ".vbe", ".js", ".jse", ".jar", ".apk", ".dmg", ".sh",
    ".bin", ".run", ".elf", ".gadget", ".wsf", ".hta",
})

# Directory names that should never legitimately contain executables.
TEMP_DIR_NAMES = frozenset({"tmp", "temp", "downloads", "download"})

# Suffixes that imply real document/media content; zero bytes = masquerade.
CONTENT_SUFFIXES = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".jpg", ".jpeg", ".png", ".gif",
    ".zip", ".mp4", ".mp3", ".txt", ".csv",
})

_CHUNK = 65536


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class Antivirus:
    """Scanner + quarantine rooted at a base dir (default ~/.vektorbots).

    Pass an explicit base_dir in tests so the real home directory is never
    touched.
    """

    def __init__(self, base_dir=None):
        self.base = Path(base_dir) if base_dir else Path.home() / ".vektorbots"
        self.blocklist_file = self.base / "blocklist.txt"
        self.quarantine_dir = self.base / "quarantine"
        self.manifest_file = self.base / "quarantine_manifest.jsonl"

    # -- setup ---------------------------------------------------------------
    def ensure_dirs(self):
        self.base.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        if not self.blocklist_file.exists():
            self.blocklist_file.write_text("# one SHA256 per line; lines starting with # ignored\n")

    def load_blocklist(self):
        self.ensure_dirs()
        hashes = set()
        for line in self.blocklist_file.read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#") and len(line) == 64:
                hashes.add(line)
        return hashes

    # -- heuristics ----------------------------------------------------------
    @staticmethod
    def heuristic_reasons(path: Path):
        """Return a list of (severity, reason) tuples. Never just a verdict."""
        reasons = []
        name = path.name
        suffix = path.suffix.lower()
        stem = Path(path.stem)
        # 1. Double extension: report.pdf.exe
        if suffix in EXECUTABLE_SUFFIXES and stem.suffix:
            reasons.append(("medium",
                            f"double extension: {stem.suffix} masquerading as executable {suffix}"))
        # 2. Executable sitting in a temp/download folder
        if suffix in EXECUTABLE_SUFFIXES and any(
                part.lower() in TEMP_DIR_NAMES for part in path.parts):
            reasons.append(("medium",
                            "executable located in a temp/download directory"))
        # 3. MZ/PE header in a file that is not an executable
        try:
            with open(path, "rb") as f:
                magic = f.read(2)
            if magic == b"MZ" and suffix not in EXECUTABLE_SUFFIXES:
                reasons.append(("high",
                                "MZ/PE executable header inside a non-executable file"))
        except OSError:
            pass
        # 4. Zero-byte file masquerading as a document
        try:
            if path.stat().st_size == 0 and suffix in CONTENT_SUFFIXES:
                reasons.append(("low",
                                f"zero-byte file masquerading as {suffix} document"))
        except OSError:
            pass
        return reasons

    # -- scanning --------------------------------------------------------------
    def scan_directory(self, target):
        """Hash-scan + heuristic-scan every file under target. Returns findings."""
        target = Path(target)
        if not target.is_dir():
            raise ValueError(f"scan target must be a directory: {target}")
        blocklist = self.load_blocklist()
        findings = []
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                digest = sha256_of(path)
            except OSError:
                continue
            reasons = []
            if digest in blocklist:
                reasons.append(("high", "SHA256 matches local blocklist"))
            reasons.extend(self.heuristic_reasons(path))
            if reasons:
                findings.append({"path": str(path), "sha256": digest,
                                 "engine": "builtin",
                                 "reasons": [{"severity": s, "reason": r} for s, r in reasons]})
        return findings

    # -- engine dispatch: ClamAV when available, builtin fallback --------------
    def scan(self, target, engine="auto"):
        """Scan target, choosing the engine.

        engine="auto" (default): use ClamAV when clamscan is installed,
            otherwise fall back to the builtin hash+heuristic scan with an
            honest note. engine="clamav": require ClamAV; returns a staged
            result if clamscan is missing — never a faked scan.
            engine="builtin": skip ClamAV entirely.

        Returns {"engine": "clamav"|"builtin", "staged": bool,
        "reason": str|None, "findings": [...]}.
        """
        target = Path(target)
        if not target.is_dir():
            raise ValueError(f"scan target must be a directory: {target}")
        if engine not in ("auto", "clamav", "builtin"):
            raise ValueError(f"unknown engine: {engine!r}")
        if engine in ("auto", "clamav"):
            from .clamav_engine import ClamAVEngine
            clamav = ClamAVEngine()
            if clamav.available():
                return clamav.scan(target)
            if engine == "clamav":
                return clamav.scan(target)  # staged: binary missing
        findings = self.scan_directory(target)
        note = None
        if engine == "auto":
            note = "clamscan not installed; used builtin hash+heuristic scan"
        return {"engine": "builtin", "staged": False, "reason": note,
                "findings": findings}

    # -- quarantine (move, never delete) ---------------------------------------
    def _manifest_append(self, entry):
        self.ensure_dirs()
        with open(self.manifest_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _manifest_read(self):
        if not self.manifest_file.exists():
            return []
        return [json.loads(line) for line in
                self.manifest_file.read_text().splitlines() if line.strip()]

    def quarantine(self, path, reasons):
        """Move a flagged file aside. NOTHING is ever deleted."""
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"cannot quarantine non-file: {path}")
        self.ensure_dirs()
        digest = sha256_of(path)
        qname = f"{digest[:16]}_{path.name}"
        dest = self.quarantine_dir / qname
        counter = 1
        while dest.exists():
            dest = self.quarantine_dir / f"{digest[:16]}_{counter}_{path.name}"
            counter += 1
        shutil.move(str(path), str(dest))
        entry = {"quarantined_name": dest.name,
                 "original_path": str(path),
                 "sha256": digest,
                 "quarantined_at": datetime.now(timezone.utc).isoformat(),
                 "reasons": reasons,
                 "status": "quarantined"}
        self._manifest_append(entry)
        return entry

    def restore(self, quarantined_name):
        """Move a quarantined file back to its original path."""
        entries = self._manifest_read()
        match = next((e for e in entries
                      if e["quarantined_name"] == quarantined_name
                      and e["status"] == "quarantined"), None)
        if not match:
            raise ValueError(f"no quarantined entry named {quarantined_name!r}")
        src = self.quarantine_dir / quarantined_name
        dest = Path(match["original_path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        for e in entries:
            if e["quarantined_name"] == quarantined_name:
                e["status"] = "restored"
                e["restored_at"] = datetime.now(timezone.utc).isoformat()
        self.manifest_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return {"restored_to": str(dest), "sha256": match["sha256"]}

    def quarantine_manifest(self):
        return self._manifest_read()

    # -- reporting ---------------------------------------------------------------
    def build_report(self, target, findings, bot_name="vektor-antivirus", engine=None):
        return {"schema_version": REPORT_SCHEMA, "report_id": str(uuid.uuid4()),
                "bot": bot_name, "kind": "antivirus",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "results": [{"target": str(target), "findings": findings,
                             "engine_used": engine or "builtin",
                             "quarantined": sum(1 for f in findings if f.get("quarantined")),
                             "note": "Scanner + quarantine only; not a replacement for real antivirus"}],
                "errors": [],
                "content_trust": "local_filesystem_data",
                "scope": "local filesystem scan; nothing deleted, ever",
                "finished_at": datetime.now(timezone.utc).isoformat()}
