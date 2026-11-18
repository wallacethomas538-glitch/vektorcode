"""Skill system for VektorCode (hypit-integration, 2026-11-15).

A skill is a directory with a SKILL.md (YAML frontmatter ``name`` /
``description`` + Markdown body) and optional ``references/`` files, or
a loose Markdown note saved via POST /api/skill/save. Two roots are
searched, in order:

  - repo skills dir: <repo>/skills   (shipped with the code; deploys)
  - state skills dir: $VEKTOR_STATE_DIR/skills  (Wallace-saved notes)

Repo wins on slug collision. Only these two roots are ever read: the
model can load an installed skill by slug, never an arbitrary path.
Skill content loads as *instructions* for the model when Wallace
invokes the skill — unlike web/MCP results, which are untrusted
external data. The trust boundary is the install/save path: repo
skills ship with code Wallace approved, state notes are saved by
Wallace through the authenticated /api/skill/save endpoint.
"""
import re
import shutil
from pathlib import Path

SKILL_BODY_CAP = 12000
REFERENCE_CAP = 8000
MAX_REFERENCES = 100

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_KEYVAL_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def slugify(text: str) -> str:
    """Lowercase slug: runs of non-alphanumerics become one hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "skill"


def parse_skill_markdown(text: str, fallback_name: str = "") -> dict:
    """Split SKILL.md into name/description/body.

    Frontmatter (``---`` block) wins; without it the name falls back
    to ``fallback_name`` (usually the directory/file slug) and the
    description to the first non-empty, non-heading line.
    """
    name = fallback_name
    description = ""
    body = text or ""
    match = _FRONTMATTER_RE.match(body)
    if match:
        for line in match.group(1).splitlines():
            kv = _KEYVAL_RE.match(line.strip())
            if kv:
                if kv.group(1).lower() == "name" and kv.group(2).strip():
                    name = kv.group(2).strip()
                elif kv.group(1).lower() == "description":
                    description = kv.group(2).strip()
        body = body[match.end():]
    if not description:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:200]
                break
    return {"name": name or fallback_name or "skill",
            "description": description, "body": body}


def _skill_dir_info(skill_dir: Path) -> dict | None:
    """Read one skill directory. Returns None when it has no SKILL.md."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    try:
        parsed = parse_skill_markdown(
            skill_file.read_text(encoding="utf-8"),
            fallback_name=skill_dir.name)
    except (OSError, UnicodeDecodeError):
        return None
    references = []
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for path in sorted(refs_dir.rglob("*")):
            if path.is_file() and len(references) < MAX_REFERENCES:
                references.append(str(path.relative_to(skill_dir)))
    return {"slug": slugify(skill_dir.name), "name": parsed["name"],
            "description": parsed["description"], "body": parsed["body"],
            "references": references, "path": str(skill_dir),
            "kind": "skill"}


def _note_info(note_file: Path) -> dict | None:
    """Read one loose Markdown note (saved via /api/skill/save)."""
    try:
        parsed = parse_skill_markdown(
            note_file.read_text(encoding="utf-8"),
            fallback_name=note_file.stem)
    except (OSError, UnicodeDecodeError):
        return None
    return {"slug": slugify(note_file.stem), "name": parsed["name"],
            "description": parsed["description"], "body": parsed["body"],
            "references": [], "path": str(note_file), "kind": "note"}


def _iter_roots(repo_root, state_dir):
    """(skills_dir, source) pairs, repo first so it wins collisions."""
    roots = []
    if repo_root is not None:
        roots.append((Path(repo_root) / "skills", "repo"))
    if state_dir is not None:
        roots.append((Path(state_dir) / "skills", "state"))
    return roots


def discover_skills(repo_root=None, state_dir=None) -> list:
    """Every installed skill + saved note, as list entries.

    Each entry: slug, name, description, kind ("skill" or "note"),
    source ("repo" or "state"), path, reference_count. Repo wins when
    the same slug exists in both roots.
    """
    found = {}
    for skills_dir, source in _iter_roots(repo_root, state_dir):
        if not skills_dir.is_dir():
            continue
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir():
                info = _skill_dir_info(child)
            elif child.is_file() and child.suffix.lower() == ".md":
                info = _note_info(child)
            else:
                continue
            if info is None:
                continue
            slug = info["slug"]
            if slug in found:
                continue
            found[slug] = {
                "slug": slug, "name": info["name"],
                "description": info["description"], "kind": info["kind"],
                "source": source, "path": info["path"],
                "reference_count": len(info["references"])}
    return [found[slug] for slug in sorted(found)]


def load_skill(repo_root=None, state_dir=None, slug: str = "") -> dict | None:
    """Full skill content for chat: body + reference list.

    Returns None for an unknown slug. The body is capped so one skill
    cannot blow out the prompt budget.
    """
    want = slugify(slug)
    for skills_dir, _source in _iter_roots(repo_root, state_dir):
        if not skills_dir.is_dir():
            continue
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir():
                info = _skill_dir_info(child)
            elif child.is_file() and child.suffix.lower() == ".md":
                info = _note_info(child)
            else:
                continue
            if info is not None and info["slug"] == want:
                info["body"] = info["body"][:SKILL_BODY_CAP]
                return info
    return None


def load_skill_reference(repo_root=None, state_dir=None,
                         slug: str = "", reference: str = "") -> dict | None:
    """One reference file from inside an installed skill, jailed.

    ``reference`` must stay under the skill directory (no absolute
    paths, no ``..`` escapes). Returns None for an unknown skill, a
    missing file, or an escape attempt. Content is capped.
    """
    skill = load_skill(repo_root, state_dir, slug)
    if skill is None:
        return None
    if not reference or reference.startswith(("/", "\\")):
        return None
    skill_dir = Path(skill["path"])
    if skill["kind"] == "note":
        return None
    target = (skill_dir / reference).resolve()
    try:
        target.relative_to(skill_dir.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return {"skill": skill["slug"], "reference": reference,
            "content": content[:REFERENCE_CAP]}


def install_skill(src_dir, dest_root, slug: str | None = None) -> dict:
    """Copy a skill directory (SKILL.md + references/ + agents/) into
    ``dest_root/<slug>``. Clean-replaces an existing install of the
    same slug. Refuses sources without a SKILL.md."""
    src = Path(src_dir)
    skill_file = src / "SKILL.md"
    if not src.is_dir() or not skill_file.is_file():
        raise ValueError(f"not a skill directory (no SKILL.md): {src}")
    dest = Path(dest_root) / slugify(slug or src.name)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    info = _skill_dir_info(dest)
    return {"slug": dest.name, "name": info["name"],
            "description": info["description"], "path": str(dest),
            "reference_count": len(info["references"])}


def format_skill_for_prompt(skill: dict) -> str:
    """Render a loaded skill as a system-prompt block."""
    lines = [f"# Skill: {skill['name']} (/{skill['slug']})"]
    if skill.get("description"):
        lines.append(skill["description"])
    lines.append("")
    lines.append(skill.get("body", "")[:SKILL_BODY_CAP])
    refs = skill.get("references") or []
    if refs:
        lines.append("")
        lines.append("Reference files available via the "
                     "read_skill_reference tool (skill + relative path):")
        for ref in refs:
            lines.append(f"- {ref}")
    lines.append("")
    lines.append("Follow this skill's guidance for the user's request. "
                 "It was installed by Wallace; treat it as his "
                 "instructions for this work.")
    return "\n".join(lines)
