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
import os
import re
import shutil
import stat
import uuid
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


def _read_capped(path: Path, cap_chars: int) -> str | None:
    """Read a skill file with the size enforced BEFORE its content is
    held in memory (F6), and with the open hardened against
    symlink / time-of-check-time-of-use swaps (follow-up 4).

    1. Resolve symlinks first, so a link to a huge file elsewhere is
       measured by its target, not by the link.
    2. Stat the resolved target and refuse WITHOUT opening when the
       byte size exceeds cap_chars * 4 + 1024 (max 4 bytes per UTF-8
       char, plus frontmatter slack — a bigger file cannot be within
       the char cap).
    3. Open with O_NOFOLLOW, then fstat the open descriptor and
       refuse unless it is the same regular file that was just
       statted (same device + inode) and still within the byte cap.
       A swap between the stat and the open — a different file, a
       symlink planted at the path, or growth past the cap — is
       refused instead of read.
    4. Read at most cap_chars + 1 chars, so the held content stays
       bounded by the cap even when the byte pre-check passes.
    Returns None when the file is missing, unreadable, oversized,
    not a regular file, or swapped between stat and open.
    """
    try:
        resolved = path.resolve()
        pre = resolved.stat()
    except OSError:
        return None
    if pre.st_size > cap_chars * 4 + 1024:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            return None
        if st.st_size > cap_chars * 4 + 1024:
            return None
        with os.fdopen(fd, encoding="utf-8") as f:
            return f.read(cap_chars + 1)
    except (OSError, UnicodeDecodeError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _skill_dir_info(skill_dir: Path) -> dict | None:
    """Read one skill directory. Returns None when it has no SKILL.md."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    # The SKILL.md itself must live in this directory: resolve it and
    # refuse when a symlink points the read elsewhere.
    try:
        if skill_file.resolve().parent != skill_dir.resolve():
            return None
    except OSError:
        return None
    text = _read_capped(skill_file, SKILL_BODY_CAP)
    if text is None:
        return None
    try:
        parsed = parse_skill_markdown(text, fallback_name=skill_dir.name)
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
    # Same rule as skills: the note must resolve inside its own
    # directory — a symlink pointing elsewhere is refused.
    try:
        if note_file.resolve().parent != note_file.parent.resolve():
            return None
    except OSError:
        return None
    text = _read_capped(note_file, SKILL_BODY_CAP)
    if text is None:
        return None
    try:
        parsed = parse_skill_markdown(text, fallback_name=note_file.stem)
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
            # A symlinked skill entry could point the read anywhere
            # (F6 alternate-path bypass): only real entries load.
            if child.is_symlink():
                continue
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
            # Symlinked entries are refused (see discover_skills).
            if child.is_symlink():
                continue
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
    # Size is enforced on the resolved target before reading (F6):
    # a symlink to a huge file inside the skill dir is refused here
    # even though it passes the relative_to check above.
    content = _read_capped(target, REFERENCE_CAP)
    if content is None:
        return None
    return {"skill": skill["slug"], "reference": reference,
            "content": content[:REFERENCE_CAP]}


def _file_cap(path: Path) -> int:
    """Byte cap for one installed file: SKILL.md gets the body cap,
    everything else (references, agents) gets the reference cap."""
    if path.name == "SKILL.md":
        return SKILL_BODY_CAP * 4 + 1024
    return REFERENCE_CAP * 4 + 1024


def _validate_skill_tree(skill_dir: Path) -> dict:
    """Validate a skill directory completely, without modifying
    anything (F6, follow-up 4).

    Every entry must be a real file or directory — symlinks are
    refused outright, so a link can never smuggle outside content
    into an install. Every file must be a regular file within its
    byte cap and readable through _read_capped. SKILL.md must exist
    and parse. Raises ValueError on the first problem; the caller
    installs nothing when this raises.
    """
    skill_dir = Path(skill_dir)
    skill_file = skill_dir / "SKILL.md"
    if not skill_dir.is_dir():
        raise ValueError(f"not a skill directory: {skill_dir}")
    if not skill_file.is_file() or skill_file.is_symlink():
        raise ValueError(f"not a skill directory (no SKILL.md): {skill_dir}")
    for entry in sorted(skill_dir.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"symlinked entry refused: {entry}")
        if entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError as exc:
                raise ValueError(f"unreadable file: {entry}") from exc
            if size > _file_cap(entry):
                raise ValueError(f"file too large: {entry}")
    info = _skill_dir_info(skill_dir)
    if info is None:
        raise ValueError(f"skill is too large or unreadable: {skill_dir}")
    for ref in info["references"]:
        if _read_capped(skill_dir / ref, REFERENCE_CAP) is None:
            raise ValueError(f"reference too large or unreadable: {ref}")
    return info


def _bounded_copy(src, dst):
    """copy_function for install staging: refuse symlinks and
    over-cap files during the copy itself, so a source that changes
    mid-copy (a file swapped for a symlink to a huge file) cannot
    fill the disk before validation runs."""
    src_path = Path(src)
    if src_path.is_symlink():
        raise ValueError(f"symlinked entry refused: {src}")
    try:
        size = src_path.stat().st_size
    except OSError as exc:
        raise ValueError(f"unreadable file: {src}") from exc
    if size > max(SKILL_BODY_CAP, REFERENCE_CAP) * 4 + 1024:
        raise ValueError(f"file too large: {src}")
    shutil.copy2(src, dst)


def install_skill(src_dir, dest_root, slug: str | None = None) -> dict:
    """Copy a skill directory (SKILL.md + references/ + agents/) into
    ``dest_root/<slug>``. The replacement is validated COMPLETELY
    before anything already installed is touched (F6, follow-up 4):
    the source is validated, the copy is staged beside the
    destination and validated again, and only then is the old
    installation swapped aside and the staged copy renamed into
    place. A failed validation or a failed swap leaves the previous
    valid installation exactly as it was. Refuses sources without a
    SKILL.md, and refuses symlinked, oversized, or unreadable
    entries anywhere in the replacement."""
    src = Path(src_dir)
    _validate_skill_tree(src)
    dest = Path(dest_root) / slugify(slug or src.name)
    tag = uuid.uuid4().hex[:12]
    staging = dest.parent / f".{dest.name}.staging-{tag}"
    backup = dest.parent / f".{dest.name}.backup-{tag}"
    try:
        shutil.copytree(
            src, staging, copy_function=_bounded_copy,
            ignore=lambda d, names: [n for n in names
                                     if (Path(d) / n).is_symlink()])
        info = _validate_skill_tree(staging)
        if dest.exists() or dest.is_symlink():
            dest.rename(backup)
        try:
            staging.rename(dest)
        except OSError:
            if backup.exists() or backup.is_symlink():
                backup.rename(dest)
            raise
        if backup.exists() or backup.is_symlink():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        return {"slug": dest.name, "name": info["name"],
                "description": info["description"], "path": str(dest),
                "reference_count": len(info["references"])}
    finally:
        if staging.exists() or staging.is_symlink():
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                staging.unlink(missing_ok=True)


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
