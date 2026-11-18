"""Living code graph for VektorCode (codegraph-skill, 2026-11-18).

Parses a Python repo with Tree-sitter, stores modules / symbols / imports /
calls in an embedded KuzuDB graph, and answers the impact question:
"what breaks if I touch this symbol?"

What it is for: VektorCode's memory of the codebase. Instead of re-reading
files every session, the agent builds the graph once, then asks it — before
an edit — which functions call the thing about to change, and which modules
import it. That is the whole job: stop breaking working code you do not
understand, and stop claiming work is done without proof of what it touches.

Dependencies (optional, import-guarded):
  - tree-sitter + tree-sitter-python (MIT) — parsing.
  - kuzu (MIT) — embedded graph database, no server.
Both are free open-source libraries. Our code here is ours; the libraries
keep their own licenses and we do not claim to have written them. If they
are not installed, every function here raises a plain RuntimeError telling
the caller how to install them — nothing silently pretends the graph
exists. On Render the app is stdlib-only by design, so the graph runs
where the dependencies are installed (local / Daytona / a service with
them in requirements.txt), and degrades honestly everywhere else.

Honest limits (by design, not hidden):
  - Call resolution is NAME-BASED: a call to ``foo()`` resolves to any
    defined symbol named ``foo``; ``obj.method()`` resolves by the last
    segment. No type inference, no dynamic dispatch. Direct callers are
    usually exact; transitive callers are an upper bound and can include
    same-name collisions — say so when reporting them.
  - Only static calls visible in the tree are captured. eval/exec/getattr
    and dynamically built names are missed.
  - Imports come from import statements only; relative imports are
    recorded only when they name a resolvable in-repo module.
"""

import shutil
from pathlib import Path

try:
    import kuzu
    import tree_sitter_python as _tspython
    from tree_sitter import Language as _Language
    from tree_sitter import Parser as _Parser

    _PY = _Language(_tspython.language())
    _PARSER = _Parser(_PY)
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - missing-dependency path
    kuzu = None
    _PARSER = None
    AVAILABLE = False
    IMPORT_ERROR = _e


def available() -> bool:
    """True when tree-sitter + kuzu are installed and the graph can run."""
    return AVAILABLE


def _require_available() -> None:
    if not AVAILABLE:
        raise RuntimeError(
            "code graph unavailable: install tree-sitter, tree-sitter-python "
            "and kuzu first (pip install tree-sitter tree-sitter-python kuzu). "
            f"Import error was: {IMPORT_ERROR!r}"
        )


def module_name(repo_root: Path, path: Path) -> str:
    rel = path.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def parse_file(path: Path, repo_root: Path):
    """Return (module, defs, imports, calls) for one .py file.

    defs: list of (qualified_name, kind, module); kind is 'function'/'class'.
    imports: list of imported module strings.
    calls: list of (enclosing_symbol_qualified_or_None, callee_simple_name).
    """
    src = path.read_bytes()
    tree = _PARSER.parse(src)
    mod = module_name(repo_root, path)
    defs, imports, calls = [], [], []
    stack = []

    def field_text(node, field):
        child = node.child_by_field_name(field)
        return src[child.start_byte:child.end_byte].decode("utf-8", "replace") if child else None

    def walk(node):
        t = node.type
        if t == "function_definition":
            name = field_text(node, "name")
            if name:
                qname = ".".join([mod] + [s[1] for s in stack] + [name])
                defs.append((qname, "function", mod))
                stack.append(("function", name))
                for ch in node.children:
                    walk(ch)
                stack.pop()
                return
        if t == "class_definition":
            name = field_text(node, "name")
            if name:
                qname = ".".join([mod] + [s[1] for s in stack] + [name])
                defs.append((qname, "class", mod))
                stack.append(("class", name))
                for ch in node.children:
                    walk(ch)
                stack.pop()
                return
        if t == "import_statement":
            for ch in node.children:
                if ch.type == "dotted_name":
                    imports.append(src[ch.start_byte:ch.end_byte].decode())
            return
        if t == "import_from_statement":
            # The module is the first dotted_name child BEFORE the `import`
            # keyword. (The "name" field points at the imported symbols,
            # not the module — using it records names, not modules.)
            for ch in node.children:
                if ch.type == "import":
                    break
                if ch.type == "dotted_name":
                    imports.append(src[ch.start_byte:ch.end_byte].decode())
                    break
            return
        if t == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    callee = src[fn.start_byte:fn.end_byte].decode()
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    callee = (
                        src[attr.start_byte:attr.end_byte].decode()
                        if attr is not None
                        else None
                    )
                else:
                    callee = None
                if callee:
                    enclosing = (
                        ".".join([mod] + [s[1] for s in stack]) if stack else None
                    )
                    calls.append((enclosing, callee))
            for ch in node.children:
                walk(ch)
            return
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return mod, defs, imports, calls


def build_graph(repo_root, db_path):
    """(Re)build the code graph for ``repo_root`` into ``db_path``.

    Returns a stats dict: files, modules, symbols, import_edges,
    call_sites, calls_resolved, calls_unresolved.
    """
    _require_available()
    repo_root = Path(repo_root).resolve()
    db_path = Path(db_path)
    if db_path.is_dir():
        shutil.rmtree(db_path)
    elif db_path.exists():
        db_path.unlink()

    py_files = sorted(
        p for p in repo_root.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    )
    modules, all_defs, all_imports, all_calls = {}, [], [], []
    for f in py_files:
        try:
            mod, defs, imports, calls = parse_file(f, repo_root)
        except Exception:
            continue
        modules[mod] = str(f.relative_to(repo_root))
        all_defs.extend(defs)
        all_imports.extend((mod, imp) for imp in imports)
        all_calls.extend((mod, enc, callee) for enc, callee in calls)

    by_simple = {}
    for qname, _kind, _mod in all_defs:
        by_simple.setdefault(qname.split(".")[-1], []).append(qname)
    resolved, unresolved = [], []
    for _mod, enc, callee in all_calls:
        if enc is None:
            continue
        cands = by_simple.get(callee, [])
        if cands:
            for c in cands:
                resolved.append((enc, c))
        else:
            unresolved.append((enc, callee))
    resolved = sorted(set(resolved))

    db = kuzu.Database(str(db_path))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE Module(name STRING, path STRING, PRIMARY KEY(name))")
    conn.execute("CREATE NODE TABLE Symbol(name STRING, kind STRING, module STRING, PRIMARY KEY(name))")
    conn.execute("CREATE REL TABLE DEFINES(FROM Module TO Symbol)")
    conn.execute("CREATE REL TABLE IMPORTS(FROM Module TO Module)")
    conn.execute("CREATE REL TABLE CALLS(FROM Symbol TO Symbol)")
    conn.execute(
        "UNWIND $rows AS r CREATE (:Module {name: r[1], path: r[2]})",
        {"rows": [[m, p] for m, p in modules.items()]},
    )
    conn.execute(
        "UNWIND $rows AS r CREATE (:Symbol {name: r[1], kind: r[2], module: r[3]})",
        {"rows": [[q, k, m] for q, k, m in all_defs]},
    )
    conn.execute(
        "UNWIND $rows AS r MATCH (a:Module {name: r[1]}), (b:Symbol {name: r[2]}) "
        "CREATE (a)-[:DEFINES]->(b)",
        {"rows": [[m, q] for q, _k, m in all_defs]},
    )
    conn.execute(
        "UNWIND $rows AS r MATCH (a:Module {name: r[1]}), (b:Module {name: r[2]}) "
        "CREATE (a)-[:IMPORTS]->(b)",
        {"rows": [[s, t] for s, t in sorted(set(all_imports)) if t in modules]},
    )
    conn.execute(
        "UNWIND $rows AS r MATCH (a:Symbol {name: r[1]}), (b:Symbol {name: r[2]}) "
        "CREATE (a)-[:CALLS]->(b)",
        {"rows": [list(e) for e in resolved]},
    )
    return {
        "files": len(py_files),
        "modules": len(modules),
        "symbols": len(all_defs),
        "import_edges": len(set(all_imports)),
        "call_sites": len(all_calls),
        "calls_resolved": len(resolved),
        "calls_unresolved": len(set(unresolved)),
    }


def _connect(db_path):
    _require_available()
    return kuzu.Connection(kuzu.Database(str(db_path)))


def impact(db_path, symbol):
    """What breaks if ``symbol`` (qualified name) changes.

    Returns {"direct": [...], "transitive": [...]} — direct callers are
    exact; transitive callers are an upper bound (name-based resolution
    can pull in same-name collisions). Say so when reporting them.
    """
    conn = _connect(db_path)
    direct = sorted(
        r[0]
        for r in conn.execute(
            "MATCH (c:Symbol)-[:CALLS]->(t:Symbol {name: $t}) "
            "RETURN DISTINCT c.name",
            {"t": symbol},
        )
    )
    transitive = sorted(
        r[0]
        for r in conn.execute(
            "MATCH (c:Symbol)-[:CALLS*1..10]->(t:Symbol {name: $t}) "
            "RETURN DISTINCT c.name",
            {"t": symbol},
        )
    )
    return {"direct": direct, "transitive": transitive}


def importers(db_path, module):
    """Modules that import ``module`` (in-repo imports only)."""
    conn = _connect(db_path)
    return sorted(
        r[0]
        for r in conn.execute(
            "MATCH (m:Module)-[:IMPORTS]->(t:Module {name: $t}) "
            "RETURN DISTINCT m.name",
            {"t": module},
        )
    )


def stats(db_path):
    """Counts for a built graph: modules, symbols, defines/imports/calls."""
    conn = _connect(db_path)
    def count(q):
        return list(conn.execute(q))[0][0]

    return {
        "modules": count("MATCH (m:Module) RETURN count(m)"),
        "symbols": count("MATCH (s:Symbol) RETURN count(s)"),
        "defines": count("MATCH ()-[:DEFINES]->() RETURN count(*)"),
        "imports": count("MATCH ()-[:IMPORTS]->() RETURN count(*)"),
        "calls": count("MATCH ()-[:CALLS]->() RETURN count(*)"),
    }
