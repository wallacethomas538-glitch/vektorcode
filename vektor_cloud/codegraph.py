"""VektorGraph — the living code graph for VektorCode (codegraph-skill, 2026-11-18).

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
    POTENTIALLY AMBIGUOUS whenever the same simple name is defined in
    more than one place — impact() reports this in ``direct_ambiguous``
    and ``ambiguous_definitions``. Never present ambiguous direct
    callers as proven callers of one symbol. Transitive callers are
    always an upper bound.
  - Only static calls visible in the tree are captured. eval/exec/getattr
    and dynamically built names are missed.
  - Imports come from import statements only; relative imports are
    recorded only when they name a resolvable in-repo module.
  - A build can be INCOMPLETE: files that fail to parse are recorded in
    ``failed_files`` and the stats report ``complete: False``. Never
    present an incomplete graph as complete; pass ``strict=True`` to
    fail the build instead.
  - The database file is REPLACED on rebuild. build_graph() only
    replaces a file it can verify is a VektorGraph database: it opens
    the file read-only and requires BOTH the exact expected schema —
    table types, columns, data types, primary keys, defaults,
    relationship endpoints, and no extra tables — AND valid
    VektorGraph identity metadata (the VektorGraphMeta table holding
    the identity record and a supported schema version). Table names
    alone are NOT verification, and neither is the schema alone: an
    unrelated database can carry the same names and definitions
    without the identity record. The identity check is an ownership
    discriminator against accidental deletion, not cryptographic
    proof. It refuses to delete the repository, anything containing
    it, the filesystem root, the home directory, a symlink
    destination, an unverified file, or any non-empty directory. The
    old database is replaced only AFTER the code is parsed and
    validated, so a failed strict build leaves the previous graph
    intact.
"""

import os
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


# The tables a VektorGraph database must have. Replacement of an
# existing database file is allowed only when the file opens read-only
# as a KuzuDB database AND its actual definitions match this schema
# exactly: table types, columns, data types, primary keys, defaults,
# relationship endpoints, and no extra tables. Table NAMES alone are
# not verification — an unrelated database can carry the same names
# with incompatible columns or relationships. AND the database must
# carry VektorGraph identity metadata (see below): a database with
# the exact schema but no identity record is still not ours. This is
# an ownership discriminator against accidental deletion, not
# cryptographic proof of ownership.
_SCHEMA_VERSION = "1"
_SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})
_IDENTITY_KEY = "identity"
_IDENTITY_VALUE = "vektorgraph"
_SCHEMA_VERSION_KEY = "schema_version"
_EXPECTED_NODE_TABLES = {
    # table: {property: (data type, is primary key, default)}
    "Module": {
        "name": ("STRING", True, "NULL"),
        "path": ("STRING", False, "NULL"),
    },
    "Symbol": {
        "name": ("STRING", True, "NULL"),
        "kind": ("STRING", False, "NULL"),
        "module": ("STRING", False, "NULL"),
    },
    "VektorGraphMeta": {
        "key": ("STRING", True, "NULL"),
        "value": ("STRING", False, "NULL"),
    },
}
_EXPECTED_REL_TABLES = {
    # table: {(from table, to table, from property, to property)}
    "DEFINES": {("Module", "Symbol", "name", "name")},
    "IMPORTS": {("Module", "Module", "name", "name")},
    "CALLS": {("Symbol", "Symbol", "name", "name")},
}


def _is_vektorgraph_db(db_path) -> bool:
    """True only when the file is a VektorGraph database.

    Opens the file read-only and requires BOTH:
    (a) the exact expected schema — six tables, node tables with the
    right columns, data types, primary keys and defaults, relationship
    tables with no extra properties and the right endpoints; and
    (b) valid VektorGraph identity metadata — the VektorGraphMeta
    table holding exactly the identity record ("vektorgraph") and a
    supported schema version. Anything that cannot be opened, or
    whose definitions or identity differ in any way, is not a
    VektorGraph database — including a database with the exact schema
    but no identity record.
    """
    try:
        db = kuzu.Database(str(db_path), read_only=True)
        conn = kuzu.Connection(db)
        tables = {row[1]: row[2] for row in conn.execute(
            "CALL SHOW_TABLES() RETURN *")}
        expected_types = {"Module": "NODE", "Symbol": "NODE",
                          "VektorGraphMeta": "NODE",
                          "DEFINES": "REL", "IMPORTS": "REL",
                          "CALLS": "REL"}
        if tables != expected_types:
            return False
        for table, expected in _EXPECTED_NODE_TABLES.items():
            actual = {row[1]: (row[2], row[4], row[3]) for row in conn.execute(
                f"CALL TABLE_INFO('{table}') RETURN *")}
            if actual != expected:
                return False
        for table, expected in _EXPECTED_REL_TABLES.items():
            if list(conn.execute(f"CALL TABLE_INFO('{table}') RETURN *")):
                return False
            actual = {tuple(row) for row in conn.execute(
                f"CALL SHOW_CONNECTION('{table}') RETURN *")}
            if actual != expected:
                return False
        meta = {row[0]: row[1] for row in conn.execute(
            "MATCH (m:VektorGraphMeta) RETURN m.key, m.value")}
        if set(meta) != {_IDENTITY_KEY, _SCHEMA_VERSION_KEY}:
            return False
        if meta[_IDENTITY_KEY] != _IDENTITY_VALUE:
            return False
        if meta[_SCHEMA_VERSION_KEY] not in _SUPPORTED_SCHEMA_VERSIONS:
            return False
        return True
    except Exception:
        return False


def _validate_db_path(repo_root, db_path) -> Path:
    """Validate a database path before anything is deleted.

    Returns the resolved path. Raises ValueError when the path is unsafe
    to replace: the filesystem root, the home directory, the repository
    itself, or anything containing the repository. A symlink destination
    is refused outright — resolving it could point the deletion at an
    unrelated directory, so the link is checked before resolve() hides
    it. Symlinks are resolved first for the remaining checks, so a link
    to a protected directory is caught too.
    """
    if os.path.islink(db_path):
        raise ValueError(
            f"refusing to use database path {db_path}: it is a symlink; "
            "a symlink destination must never be deleted"
        )
    repo_root = Path(repo_root).resolve()
    db_path = Path(db_path).resolve()
    for guard in (Path("/").resolve(), Path.home().resolve(), repo_root):
        if guard.is_relative_to(db_path):
            raise ValueError(
                f"refusing to use database path {db_path}: it is, or "
                f"contains, {guard}, which must never be deleted"
            )
    return db_path


def _prepare_db_path(db_path: Path) -> None:
    """Make room for a fresh database, deleting only what is verified.

    The database is a single KuzuDB file. An existing file is removed
    only when _is_vektorgraph_db() verifies it — a read-only open plus
    the expected schema. An existing directory is removed only when
    empty — a directory is never a KuzuDB database, so a non-empty one
    is never deleted. Anything else raises instead of being deleted.
    """
    if not db_path.exists():
        return
    if db_path.is_dir():
        if list(db_path.iterdir()):
            raise ValueError(
                f"refusing to use database path {db_path}: it is a "
                "non-empty directory that is not a VektorGraph database"
            )
        shutil.rmtree(db_path)
        return
    if not _is_vektorgraph_db(db_path):
        raise ValueError(
            f"refusing to use database path {db_path}: it exists and is "
            "not a verified VektorGraph database"
        )
    db_path.unlink()


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
    if tree.root_node.has_error:
        # A syntax error is a failed file, not a partial graph: raise
        # so build_graph() records it instead of silently building
        # from a broken tree.
        raise SyntaxError(f"syntax error in {path} (tree-sitter parse error)")
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


def build_graph(repo_root, db_path, strict=False):
    """(Re)build the code graph for ``repo_root`` into ``db_path``.

    Returns a stats dict: files, files_parsed, failed_files, complete,
    modules, symbols, import_edges, call_sites, calls_resolved,
    calls_unresolved.

    ``failed_files`` lists {"path": ..., "error": ...} for every .py file
    that could not be parsed; ``complete`` is False when any file failed.
    Never present an incomplete graph as complete. With ``strict=True``
    the build raises RuntimeError listing the failed files instead of
    returning an incomplete graph.

    The database file is replaced on rebuild, but only AFTER the code
    is parsed and validated: _validate_db_path() refuses protected
    paths and symlinks, the parse phase records failures, the strict
    check raises before anything is deleted, and only then does
    _prepare_db_path() replace the old database. A failed strict
    build leaves the previous graph intact.
    """
    _require_available()
    repo_root = Path(repo_root).resolve()
    db_path = _validate_db_path(repo_root, db_path)

    py_files = sorted(
        p for p in repo_root.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    )
    modules, all_defs, all_imports, all_calls = {}, [], [], []
    failed_files = []
    for f in py_files:
        try:
            mod, defs, imports, calls = parse_file(f, repo_root)
        except Exception as exc:
            failed_files.append({
                "path": str(f.relative_to(repo_root)),
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        modules[mod] = str(f.relative_to(repo_root))
        all_defs.extend(defs)
        all_imports.extend((mod, imp) for imp in imports)
        all_calls.extend((mod, enc, callee) for enc, callee in calls)

    complete = not failed_files
    if strict and failed_files:
        names = ", ".join(entry["path"] for entry in failed_files)
        raise RuntimeError(
            f"code graph build incomplete: {len(failed_files)} file(s) "
            f"failed to parse ({names}); refusing to report an "
            "incomplete graph as complete"
        )

    # Only now, with parsing done and validation passed, is the old
    # database replaced.
    _prepare_db_path(db_path)

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
    conn.execute("CREATE NODE TABLE VektorGraphMeta(key STRING, value STRING, PRIMARY KEY(key))")
    conn.execute("CREATE REL TABLE DEFINES(FROM Module TO Symbol)")
    conn.execute("CREATE REL TABLE IMPORTS(FROM Module TO Module)")
    conn.execute("CREATE REL TABLE CALLS(FROM Symbol TO Symbol)")
    # Identity metadata: every genuine VektorGraph database carries
    # these two rows. Replacement requires them (see
    # _is_vektorgraph_db) — they are the ownership discriminator
    # against accidentally deleting an unrelated database that happens
    # to share the schema. Not cryptographic proof of ownership.
    conn.execute(
        "CREATE (:VektorGraphMeta {key: $k, value: $v})",
        {"k": _IDENTITY_KEY, "v": _IDENTITY_VALUE},
    )
    conn.execute(
        "CREATE (:VektorGraphMeta {key: $k, value: $v})",
        {"k": _SCHEMA_VERSION_KEY, "v": _SCHEMA_VERSION},
    )
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
        "files_parsed": len(py_files) - len(failed_files),
        "failed_files": failed_files,
        "complete": complete,
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

    Returns {"direct": [...], "transitive": [...],
    "direct_ambiguous": bool, "ambiguous_definitions": [...]}.

    Resolution is NAME-BASED: a call to ``foo()`` resolves to every
    symbol named ``foo``. When the target's simple name is defined in
    more than one place, the direct callers are POTENTIALLY AMBIGUOUS —
    they may be callers of a same-named symbol elsewhere, not of this
    one. ``direct_ambiguous`` is True then, and
    ``ambiguous_definitions`` lists every qualified name sharing the
    simple name. Check both before reporting direct callers as fact.
    Transitive callers are always an upper bound.
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
    simple = symbol.split(".")[-1]
    definitions = sorted(
        r[0]
        for r in conn.execute("MATCH (s:Symbol) RETURN s.name")
        if r[0].split(".")[-1] == simple
    )
    return {
        "direct": direct,
        "transitive": transitive,
        "direct_ambiguous": len(definitions) > 1,
        "ambiguous_definitions": definitions,
    }


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
