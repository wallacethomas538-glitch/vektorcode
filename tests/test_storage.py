"""Dashboard storage: SQLite fallback and Supabase configuration."""
from vektor_cloud.storage import DashboardStore, supabase_config


def test_supabase_config_requires_both_values(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert supabase_config() is None
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    assert supabase_config() is None
    monkeypatch.setenv("SUPABASE_KEY", "test-key-not-real")
    assert supabase_config() == ("https://example.supabase.co",
                                 "test-key-not-real")


def test_sqlite_fallback_roundtrip(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    store = DashboardStore(tmp_path / "state.db")
    assert store.backend == "sqlite"

    store.set_setting("tools", ["web"])
    assert store.get_setting("tools", []) == ["web"]

    store.create_session("sid-1", "Test", "groq")
    assert [s["id"] for s in store.list_sessions()] == ["sid-1"]
    store.resume_session("sid-1")
    store.delete_session("sid-1")
    assert store.list_sessions() == []

    rid = store.add_receipt("probe", {"ok": True}, False, False)
    assert rid
    assert store.list_receipts()[0]["action"] == "probe"


def test_unreachable_supabase_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://127.0.0.1:9")
    monkeypatch.setenv("SUPABASE_KEY", "test-key-not-real")
    store = DashboardStore(tmp_path / "state.db")
    assert store.backend == "supabase"

    store.set_setting("tools", ["file_ops"])
    assert store.get_setting("tools", []) == ["file_ops"]
    assert store.last_error
