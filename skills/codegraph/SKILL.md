---
name: VektorGraph
description: VektorGraph — a living graph of the codebase (Tree-sitter + KuzuDB) so you can answer "what breaks if I touch this?" before editing, instead of re-reading files every session and breaking working code you do not understand.
---

# VektorGraph

You are working with a living map of the codebase, not a pile of text files.
Build the graph once, then ask it before you touch anything.

## What it is

`vektor_cloud/codegraph.py` parses every Python file in a repo with
Tree-sitter, stores modules, symbols, imports and calls in an embedded
KuzuDB graph (no server), and answers impact questions. It is the
codebase's memory: functions, classes, who calls them, who imports them.

## When to use it

- Before editing any function, class or module: ask the graph what calls
  it and what imports it. If the answer is wide, say so and be careful.
- When you are about to claim work is done: the graph is part of the
  proof — name what the change touches.
- When you are new to a repo: build the graph first instead of reading
  files one by one.

## How to use it

The module needs `tree-sitter`, `tree-sitter-python` and `kuzu`
installed. Check first, through the approval-gated shell:

```python
from vektor_cloud.codegraph import available
available()  # True means the graph can run
```

If it is False, say so plainly and do not claim a graph exists. Install
with `pip install tree-sitter tree-sitter-python kuzu`, or run where
those packages are installed. Never invent graph answers.

Build (or rebuild after the code changes) — the database lives under
the state dir so it survives deploys of new code:

```python
from vektor_cloud.codegraph import build_graph
build_graph(repo_root, db_path)  # returns file/module/symbol/call counts
```

Ask the impact question:

```python
from vektor_cloud.codegraph import impact, importers
impact(db_path, "vektor_cloud.skills.load_skill")
# {"direct": [...], "transitive": [...], "direct_ambiguous": bool,
#  "ambiguous_definitions": [...]}
importers(db_path, "vektor_cloud.skills")  # modules importing it
```

Direct callers are NAME-BASED and potentially ambiguous. If
`direct_ambiguous` is true, say so plainly and list the colliding
definitions from `ambiguous_definitions` — those callers may belong to
a same-named symbol elsewhere, not to this one. Never present
ambiguous direct callers as proven. Report transitive callers as an
upper bound, always.

Check every build for completeness before quoting it:

```python
stats = build_graph(repo_root, db_path)
stats["complete"]  # False when files failed to parse
stats["failed_files"]  # [{"path": ..., "error": ...}, ...]
```

Never present an incomplete graph as complete. Pass `strict=True` to
fail the build instead of returning an incomplete graph — and a failed
strict build leaves the previous graph untouched, because the code is
parsed and validated before the old database is replaced. Files with
syntax errors are recorded as failed files, never silently skipped.
The database file is replaced on rebuild only when build_graph() can
verify it is a VektorGraph database: it opens the file read-only and
checks the actual definitions against the expected schema — table
types, columns, data types, primary keys, defaults, relationship
endpoints, and no extra tables. Table names alone are not
verification. It refuses to delete anything else — including
the repository itself, a symlink destination, an unverified file, or
any non-empty directory.

## Honest limits

- Call resolution is NAME-BASED: `foo()` resolves to any defined symbol
  named `foo`; `obj.method()` resolves by the last segment. No type
  inference, no dynamic dispatch. Direct callers are potentially
  ambiguous when the same simple name is defined in more than one
  place — check `direct_ambiguous` and `ambiguous_definitions` before
  reporting them as fact.
- Only static calls visible in the tree are captured. `eval`, `exec`,
  `getattr` and dynamically built names are missed.
- The graph goes stale when the code changes. Rebuild it after edits,
  and never quote an old graph as current.
- A build can be incomplete: files that fail to parse are recorded, and
  the stats report `complete: False`. Never present an incomplete graph
  as complete.
- On Render the app is stdlib-only by design, so the graph runs where
  its dependencies are installed (local, Daytona, or a service with
  them in requirements.txt) and degrades honestly everywhere else.
  Adding the dependencies to `requirements.txt` is a deploy decision
  for Big Chat review, not something to slip in quietly.
