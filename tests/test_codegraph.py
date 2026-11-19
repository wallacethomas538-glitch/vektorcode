"""Tests for vektor_cloud/codegraph.py (codegraph-skill).

Most tests need tree-sitter + kuzu and skip honestly when the
dependencies are absent — each of those carries its own skip mark.
The unavailable-path test has NO mark on purpose: it must execute in
both environments (it monkeypatches AVAILABLE off itself). A
module-level skip mark used to swallow it in the no-deps environment;
that arrangement is the bug Big Chat caught, and this file is the
regression guard for it.
"""

import os
from pathlib import Path

import pytest

from vektor_cloud import codegraph

needs_deps = pytest.mark.skipif(
    not codegraph.AVAILABLE,
    reason="codegraph deps (tree-sitter/kuzu) not installed",
)


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "a.py").write_text(
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return foo()\n"
        "\n"
        "class Calc:\n"
        "    def calc(self):\n"
        "        return foo()\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import foo\n"
        "\n"
        "def use():\n"
        "    return foo()\n"
    )
    (tmp_path / "c.py").write_text("def foo():\n    return 2\n")
    return tmp_path


@needs_deps
def test_build_stats(repo, tmp_path):
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["files"] == 3
    assert stats["modules"] == 3
    assert stats["symbols"] == 6  # a.foo, a.bar, a.Calc, a.Calc.calc, b.use, c.foo
    assert stats["calls_resolved"] >= 3
    assert stats["complete"] is True
    assert stats["failed_files"] == []


@needs_deps
def test_impact_direct_callers_flagged_ambiguous_on_collision(repo, tmp_path):
    # a.foo and c.foo share the simple name "foo", so the direct callers
    # of a.foo are POTENTIALLY AMBIGUOUS: they may be callers of c.foo.
    # The API must say so instead of presenting them as exact.
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    result = codegraph.impact(db, "a.foo")
    assert set(result["direct"]) == {"a.bar", "a.Calc.calc", "b.use"}
    assert result["direct_ambiguous"] is True
    assert result["ambiguous_definitions"] == ["a.foo", "c.foo"]


@needs_deps
def test_impact_direct_callers_unambiguous_when_unique(repo, tmp_path):
    # "bar" is defined exactly once, so its direct callers are not
    # ambiguous — the flag must stay off for the unique case.
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    result = codegraph.impact(db, "a.bar")
    assert result["direct"] == []
    assert result["direct_ambiguous"] is False
    assert result["ambiguous_definitions"] == ["a.bar"]


@needs_deps
def test_name_collision_is_reported_not_hidden(repo, tmp_path):
    # b.use, a.bar and a.Calc.calc all call foo() by bare name, so each
    # resolves to BOTH a.foo and c.foo. The graph must show that honestly
    # instead of silently picking one — so the two impacts are identical,
    # and the caller cannot tell from the graph alone which foo was meant.
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    a_foo = codegraph.impact(db, "a.foo")
    c_foo = codegraph.impact(db, "c.foo")
    assert set(a_foo["direct"]) == {"a.bar", "a.Calc.calc", "b.use"}
    assert set(c_foo["direct"]) == set(a_foo["direct"])
    assert a_foo["direct_ambiguous"] is True
    assert c_foo["direct_ambiguous"] is True


@needs_deps
def test_importers(repo, tmp_path):
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    assert codegraph.importers(db, "a") == ["b"]
    assert codegraph.importers(db, "c") == []


@needs_deps
def test_stats_counts(repo, tmp_path):
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    s = codegraph.stats(db)
    assert s["modules"] == 3
    assert s["symbols"] == 6
    assert s["defines"] == 6
    assert s["imports"] == 1
    assert s["calls"] >= 3


def test_build_raises_plainly_without_deps(monkeypatch, tmp_path):
    monkeypatch.setattr(codegraph, "AVAILABLE", False)
    with pytest.raises(RuntimeError, match="code graph unavailable"):
        codegraph.build_graph(tmp_path, tmp_path / "graph.db")


# --- Correction 1: database path safety -------------------------------
# build_graph() replaces the database directory on rebuild. It must
# only delete a directory it can verify is a VektorGraph database (or
# an empty one). Everything else must raise, with the data intact.


@needs_deps
def test_build_refuses_repo_root(repo, tmp_path):
    with pytest.raises(ValueError, match="refusing to use database path"):
        codegraph.build_graph(repo, repo)
    assert (repo / "a.py").is_file()  # repo untouched


@needs_deps
def test_build_refuses_parent_of_repo(repo, tmp_path):
    with pytest.raises(ValueError, match="refusing to use database path"):
        codegraph.build_graph(repo, repo.parent)
    assert (repo / "a.py").is_file()


@needs_deps
def test_build_refuses_filesystem_root(repo, tmp_path):
    with pytest.raises(ValueError, match="refusing to use database path"):
        codegraph.build_graph(repo, Path("/"))


@needs_deps
def test_build_refuses_home_directory(repo, tmp_path):
    with pytest.raises(ValueError, match="refusing to use database path"):
        codegraph.build_graph(repo, Path.home())


@needs_deps
def test_build_refuses_unmarked_nonempty_dir(repo, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep-me.txt"
    sentinel.write_text("not a database")
    with pytest.raises(ValueError, match="not a VektorGraph database"):
        codegraph.build_graph(repo, foreign)
    assert sentinel.is_file()  # nothing deleted


@needs_deps
def test_build_refuses_existing_file(repo, tmp_path):
    victim = tmp_path / "not-a-dir"
    victim.write_text("precious")
    with pytest.raises(ValueError, match="not a verified VektorGraph database"):
        codegraph.build_graph(repo, victim)
    assert victim.read_text() == "precious"


@needs_deps
def test_build_refuses_forged_marker(repo, tmp_path):
    # The old marker check accepted any file carrying the predictable
    # marker string. A forged marker must NOT authorize deletion.
    # (Negative control: this test FAILS on the follow-up 1 code.)
    victim = tmp_path / "graph.db"
    victim.write_text("unrelated data, do not delete")
    marker = tmp_path / "graph.db.vektorgraph-db"
    marker.write_text("vektorgraph-db-v1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a verified VektorGraph database"):
        codegraph.build_graph(repo, victim)
    assert victim.read_text() == "unrelated data, do not delete"


@needs_deps
def test_build_refuses_valid_kuzu_db_wrong_schema(repo, tmp_path):
    # A real KuzuDB database built for something else is not ours,
    # even though it opens cleanly.
    import kuzu

    victim = tmp_path / "graph.db"
    db = kuzu.Database(str(victim))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE Other(name STRING, PRIMARY KEY(name))")
    del conn, db
    with pytest.raises(ValueError, match="not a verified VektorGraph database"):
        codegraph.build_graph(repo, victim)
    assert victim.is_file()
    check = kuzu.Database(str(victim), read_only=True)
    names = {r[0] for r in kuzu.Connection(check).execute(
        "CALL SHOW_TABLES() RETURN name")}
    assert names == {"Other"}


@needs_deps
def test_build_refuses_random_bytes_file(repo, tmp_path):
    victim = tmp_path / "graph.db"
    victim.write_bytes(b"\x00\x01not a database at all\xff\xfe")
    with pytest.raises(ValueError, match="not a verified VektorGraph database"):
        codegraph.build_graph(repo, victim)
    assert victim.read_bytes().startswith(b"\x00\x01")


@needs_deps
def test_build_refuses_symlink_to_repo_root(repo, tmp_path):
    link = tmp_path / "db-link"
    os.symlink(repo, link)
    with pytest.raises(ValueError, match="refusing to use database path"):
        codegraph.build_graph(repo, link)
    assert (repo / "a.py").is_file()


@needs_deps
def test_build_refuses_symlink_to_empty_dir(repo, tmp_path):
    # The old code resolved the symlink, saw an empty directory, and
    # rmtree'd the TARGET — an unrelated directory the caller never
    # meant as the database. The link itself must be refused.
    # (Negative control: this test FAILS on the follow-up 1 code.)
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    link = tmp_path / "db-link"
    os.symlink(victim, link)
    with pytest.raises(ValueError, match="symlink"):
        codegraph.build_graph(repo, link)
    assert victim.is_dir()  # untouched
    assert list(victim.iterdir()) == []


@needs_deps
def test_build_replaces_real_database(repo, tmp_path):
    # A database built by build_graph() verifies by its actual schema,
    # so a rebuild may replace it.
    db = tmp_path / "graph.db"
    first = codegraph.build_graph(repo, db)
    assert first["complete"] is True
    assert db.is_file()  # KuzuDB stores the database as a single file
    second = codegraph.build_graph(repo, db)
    assert second["complete"] is True
    assert second["files"] == 3


# --- Correction 3: incomplete graph detection --------------------------
# Files that fail to parse must be recorded, never silently skipped.


@needs_deps
def test_build_records_parse_failures(repo, tmp_path):
    # A directory named *.py is found by rglob but cannot be read as a
    # file, so it fails to parse for real — no monkeypatching needed.
    (tmp_path / "broken.py").mkdir()
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["complete"] is False
    assert stats["files"] == 4
    assert stats["files_parsed"] == 3
    assert [entry["path"] for entry in stats["failed_files"]] == ["broken.py"]
    assert stats["modules"] == 3  # the good files still build


@needs_deps
def test_build_records_parse_failures_monkeypatched(repo, tmp_path, monkeypatch):
    real_parse = codegraph.parse_file

    def flaky(path, root):
        if path.name == "b.py":
            raise OSError("disk read failed (test)")
        return real_parse(path, root)

    monkeypatch.setattr(codegraph, "parse_file", flaky)
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["complete"] is False
    assert [entry["path"] for entry in stats["failed_files"]] == ["b.py"]
    assert "OSError" in stats["failed_files"][0]["error"]


@needs_deps
def test_build_strict_fails_on_parse_failure(repo, tmp_path):
    (tmp_path / "broken.py").mkdir()
    with pytest.raises(RuntimeError, match="incomplete.*broken.py"):
        codegraph.build_graph(repo, tmp_path / "graph.db", strict=True)


@needs_deps
def test_build_records_malformed_python(repo, tmp_path):
    # Tree-sitter's has_error marks this file: a syntax error is a
    # failed file, never a partial graph, and never "complete".
    (tmp_path / "bad.py").write_text("def broken(:\n    pass\n")
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["complete"] is False
    assert stats["files"] == 4
    assert stats["files_parsed"] == 3
    assert [entry["path"] for entry in stats["failed_files"]] == ["bad.py"]
    assert "SyntaxError" in stats["failed_files"][0]["error"]
    assert stats["modules"] == 3  # the good files still build


@needs_deps
def test_build_strict_preserves_previous_graph(repo, tmp_path):
    # A failed strict build must leave the previous graph intact:
    # parse and validate happen BEFORE the old database is replaced.
    # (Negative control: this test FAILS on the follow-up 1 code,
    # which deleted the database before parsing.)
    db = tmp_path / "graph.db"
    first = codegraph.build_graph(repo, db)
    assert first["complete"] is True
    before = codegraph.stats(db)
    assert before["modules"] == 3
    assert before["symbols"] == 6
    # Break a file after the good build.
    (tmp_path / "a.py").write_text("def broken(:\n    pass\n")
    with pytest.raises(RuntimeError, match="incomplete.*a.py"):
        codegraph.build_graph(repo, db, strict=True)
    # The old graph is still there, answering as before.
    assert db.is_file()
    after = codegraph.stats(db)
    assert after == before
    assert set(codegraph.impact(db, "a.foo")["direct"]) == {
        "a.bar", "a.Calc.calc", "b.use"}


@needs_deps
def test_build_complete_when_all_parse(repo, tmp_path):
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["complete"] is True
    assert stats["failed_files"] == []
    assert stats["files_parsed"] == stats["files"] == 3
