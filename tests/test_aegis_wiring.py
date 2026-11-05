"""Wiring tests for the Aegis Gate + antivirus/ClamAV chain.

Regression coverage for the track-b dead-code cleanup (2026-11-05):
aegis.security_scan()/scan_file() used to import the nonexistent modules
vektor_cloud.fangs and vektor_cloud.antivirus; the real modules are
vektorbot_fangs / vektorbot_antivirus, and vektorbot_antivirus in turn
lazy-imported the nonexistent `.clamav_engine` instead of
`.vektorbot_clamav_engine`.

security_scan() is NOT executed here — it performs real network probes
against a host. Only its import chain is verified.
"""
import pytest

from vektor_cloud import aegis
from vektor_cloud.vektorbot_antivirus import Antivirus
from vektor_cloud.vektorbot_clamav_engine import ClamAVEngine
from vektor_cloud.vektorbot_fangs import FangsRunner


def test_aegis_gate_allows_unlisted_action():
    result = aegis.aegis_gate("read_status")
    assert result["allowed"] is True
    assert result["action"] == "read_status"


def test_aegis_gate_blocks_shared_action():
    result = aegis.aegis_gate("deploy_live")
    assert result["allowed"] is False


def test_gate_log_records_decision():
    log = aegis.GateLog()
    log.decide("read_status")
    assert len(log.decisions()) == 1
    assert log.decisions()[0]["allowed"] is True


def test_security_scan_import_chain_resolves():
    # Would have raised ModuleNotFoundError before the fix
    # (vektor_cloud.fangs did not exist).
    assert callable(aegis.security_scan)
    assert callable(FangsRunner().run)


def test_scan_file_clean_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hello world\n")
    report = aegis.scan_file(str(target))
    assert report["engine"] == "builtin"
    assert report["staged"] is False
    assert report["findings"] == []


def test_scan_file_directory_target(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("plain notes\n")
    report = aegis.scan_file(str(tmp_path))
    assert report["engine"] == "builtin"
    assert report["findings"] == []


def test_scan_file_filters_to_that_file_only(tmp_path):
    # A suspicious file next to the target must not leak into the report.
    bad = tmp_path / "report.pdf.exe"
    bad.write_bytes(b"not a real exe")
    good = tmp_path / "clean.txt"
    good.write_text("clean\n")
    report = aegis.scan_file(str(good))
    assert all(f["path"] == str(good) for f in report["findings"])


def test_antivirus_scan_falls_back_to_builtin_without_clamscan(tmp_path):
    # Would have raised ModuleNotFoundError before the fix
    # (`.clamav_engine` did not exist).
    av = Antivirus()
    report = av.scan(tmp_path)
    assert report["engine"] == "builtin"
    assert report["staged"] is False


def test_clamav_engine_imports_and_reports_staged(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    engine = ClamAVEngine()
    assert engine.available() is False
    report = engine.scan(tmp_path)
    assert report["engine"] == "clamav"
    assert report["staged"] is True
