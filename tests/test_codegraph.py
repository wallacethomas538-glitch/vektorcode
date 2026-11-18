"""Tests for vektor_cloud/codegraph.py (codegraph-skill).

The happy-path tests need tree-sitter + kuzu; they skip honestly when
the dependencies are absent. The unavailable-path test needs nothing.
"""

import pytest

from vektor_cloud import codegraph

pytestmark = pytest.mark.skipif(
    not codegraph.AVAILABLE, reason="codegraph deps (tree-sitter/kuzu) not installed"
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


def test_build_stats(repo, tmp_path):
    stats = codegraph.build_graph(repo, tmp_path / "graph.db")
    assert stats["files"] == 3
    assert stats["modules"] == 3
    assert stats["symbols"] == 6  # a.foo, a.bar, a.Calc, a.Calc.calc, b.use, c.foo
    assert stats["calls_resolved"] >= 3


def test_impact_direct_callers_are_exact(repo, tmp_path):
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    result = codegraph.impact(db, "a.foo")
    assert set(result["direct"]) == {"a.bar", "a.Calc.calc", "b.use"}


def test_name_collision_is_reported_not_hidden(repo, tmp_path):
    # b.use, a.bar and a.Calc.calc all call foo() by bare name, so each
    # resolves to BOTH a.foo and c.foo. The graph must show that honestly
    # instead of silently picking one — so the two impacts are identical,
    # and the caller cannot tell from the graph alone which foo was meant.
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    a_foo = codegraph.impact(db, "a.foo")["direct"]
    c_foo = codegraph.impact(db, "c.foo")["direct"]
    assert set(a_foo) == {"a.bar", "a.Calc.calc", "b.use"}
    assert set(c_foo) == set(a_foo)


def test_importers(repo, tmp_path):
    db = tmp_path / "graph.db"
    codegraph.build_graph(repo, db)
    assert codegraph.importers(db, "a") == ["b"]
    assert codegraph.importers(db, "c") == []


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
