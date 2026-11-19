"""Tests for the skill system (hypit-integration, 2026-11-15).

Covers vektor_cloud/skills.py only — no server, no network.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from vektor_cloud.skills import (
    REFERENCE_CAP,
    SKILL_BODY_CAP,
    _note_info,
    _read_capped,
    _skill_dir_info,
    discover_skills,
    format_skill_for_prompt,
    install_skill,
    load_skill,
    load_skill_reference,
    parse_skill_markdown,
    slugify,
)

SKILL_MD = """---
name: demo-skill
description: A demo skill for tests.
---

# Demo

Do the demo thing.
"""


class ParseTest(unittest.TestCase):
    def test_frontmatter(self):
        parsed = parse_skill_markdown(SKILL_MD, "demo")
        self.assertEqual(parsed["name"], "demo-skill")
        self.assertEqual(parsed["description"], "A demo skill for tests.")
        self.assertIn("Do the demo thing.", parsed["body"])
        self.assertNotIn("---", parsed["body"])

    def test_no_frontmatter_falls_back(self):
        parsed = parse_skill_markdown("# Title\n\nFirst line here.\n",
                                      "fallback")
        self.assertEqual(parsed["name"], "fallback")
        self.assertEqual(parsed["description"], "First line here.")

    def test_slugify(self):
        self.assertEqual(slugify("My Skill! 2"), "my-skill-2")
        self.assertEqual(slugify(""), "skill")


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.state = Path(self.tmp.name) / "state"
        (self.repo / "skills" / "demo").mkdir(parents=True)
        (self.repo / "skills" / "demo" / "SKILL.md").write_text(SKILL_MD)
        (self.repo / "skills" / "demo" / "references").mkdir()
        (self.repo / "skills" / "demo" / "references" / "guide.md").write_text(
            "guide content")
        (self.state / "skills").mkdir(parents=True)
        (self.state / "skills" / "note-one.md").write_text(
            "## Note\nSaved note body.")

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_finds_skill_and_note(self):
        found = discover_skills(self.repo, self.state)
        by_slug = {s["slug"]: s for s in found}
        self.assertIn("demo", by_slug)
        self.assertEqual(by_slug["demo"]["kind"], "skill")
        self.assertEqual(by_slug["demo"]["source"], "repo")
        self.assertEqual(by_slug["demo"]["reference_count"], 1)
        self.assertIn("note-one", by_slug)
        self.assertEqual(by_slug["note-one"]["kind"], "note")
        self.assertEqual(by_slug["note-one"]["source"], "state")

    def test_repo_wins_slug_collision(self):
        (self.state / "skills" / "demo.md").write_text("state version")
        found = discover_skills(self.repo, self.state)
        by_slug = {s["slug"]: s for s in found}
        self.assertEqual(by_slug["demo"]["source"], "repo")
        self.assertEqual(by_slug["demo"]["kind"], "skill")

    def test_load_skill_body_and_refs(self):
        skill = load_skill(self.repo, self.state, "demo")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "demo-skill")
        self.assertIn("Do the demo thing.", skill["body"])
        self.assertEqual(skill["references"],
                         ["references/guide.md"])

    def test_load_unknown_slug_returns_none(self):
        self.assertIsNone(load_skill(self.repo, self.state, "nope"))

    def test_load_note_as_skill(self):
        note = load_skill(self.repo, self.state, "note-one")
        self.assertIsNotNone(note)
        self.assertEqual(note["kind"], "note")
        self.assertIn("Saved note body.", note["body"])

    def test_reference_ok(self):
        ref = load_skill_reference(self.repo, self.state, "demo",
                                   "references/guide.md")
        self.assertIsNotNone(ref)
        self.assertEqual(ref["content"], "guide content")

    def test_reference_escape_refused(self):
        for bad in ("../SKILL.md", "../../etc/passwd",
                    "/etc/passwd", "references/../../SKILL.md"):
            self.assertIsNone(
                load_skill_reference(self.repo, self.state, "demo", bad),
                bad)

    def test_reference_missing_and_unknown_skill(self):
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "demo", "references/nope.md"))
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "nope", "references/guide.md"))

    def test_reference_on_note_returns_none(self):
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "note-one", "references/guide.md"))

    def test_format_for_prompt(self):
        skill = load_skill(self.repo, self.state, "demo")
        text = format_skill_for_prompt(skill)
        self.assertIn("Skill: demo-skill", text)
        self.assertIn("Do the demo thing.", text)
        self.assertIn("references/guide.md", text)

    def test_install_skill(self):
        src = Path(self.tmp.name) / "src-skill"
        (src / "references").mkdir(parents=True)
        (src / "SKILL.md").write_text(SKILL_MD)
        (src / "references" / "a.md").write_text("a")
        info = install_skill(src, self.repo / "skills", "demo2")
        self.assertEqual(info["slug"], "demo2")
        self.assertTrue((self.repo / "skills" / "demo2" / "SKILL.md").is_file())
        self.assertTrue((self.repo / "skills" / "demo2" / "references" / "a.md").is_file())
        self.assertIn("demo2", {s["slug"] for s in discover_skills(self.repo, self.state)})

    def test_install_skill_refuses_without_skill_md(self):
        src = Path(self.tmp.name) / "not-a-skill"
        src.mkdir()
        (src / "random.md").write_text("hi")
        with self.assertRaises(ValueError):
            install_skill(src, self.repo / "skills", "bad")

    def test_install_skill_clean_replaces(self):
        src = Path(self.tmp.name) / "src2"
        src.mkdir()
        (src / "SKILL.md").write_text(SKILL_MD)
        dest = self.repo / "skills" / "demo2"
        dest.mkdir(parents=True)
        (dest / "stale.txt").write_text("stale")
        install_skill(src, self.repo / "skills", "demo2")
        self.assertFalse((dest / "stale.txt").exists())


class SizeEnforcementTest(unittest.TestCase):
    """F6: file sizes are enforced BEFORE the content is read, and
    symlink tricks cannot bypass the size or location checks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.state = self.base / "state"
        (self.repo / "skills" / "demo").mkdir(parents=True)
        (self.repo / "skills" / "demo" / "SKILL.md").write_text(SKILL_MD)
        (self.repo / "skills" / "demo" / "references").mkdir()
        (self.repo / "skills" / "demo" / "references" / "guide.md").write_text(
            "guide content")
        (self.state / "skills").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _slugs(self):
        return {s["slug"] for s in discover_skills(self.repo, self.state)}

    def test_oversized_skill_md_skipped(self):
        big = self.repo / "skills" / "big"
        big.mkdir()
        (big / "SKILL.md").write_text("x" * 100_000)
        self.assertNotIn("big", self._slugs())
        self.assertIsNone(load_skill(self.repo, self.state, "big"))
        self.assertIsNone(_skill_dir_info(big))

    def test_oversized_reference_refused(self):
        (self.repo / "skills" / "demo" / "references" / "huge.md").write_text(
            "y" * 100_000)
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "demo", "references/huge.md"))

    def test_symlink_skill_md_outside_refused(self):
        # Even a SMALL file outside the skill dir must be refused:
        # this is the location rule, not the size rule.
        outside = self.base / "outside.md"
        outside.write_text(SKILL_MD)
        linked = self.repo / "skills" / "linked"
        linked.mkdir()
        (linked / "SKILL.md").symlink_to(outside)
        self.assertNotIn("linked", self._slugs())
        self.assertIsNone(load_skill(self.repo, self.state, "linked"))
        self.assertIsNone(_skill_dir_info(linked))

    def test_symlinked_skill_dir_skipped(self):
        real = self.base / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text(SKILL_MD)
        (self.repo / "skills" / "fake").symlink_to(real, target_is_directory=True)
        self.assertNotIn("fake", self._slugs())
        self.assertIsNone(load_skill(self.repo, self.state, "fake"))

    def test_symlink_reference_to_huge_inside_refused(self):
        # The huge target lives INSIDE the skill dir, so it passes the
        # path jail — the size check must still refuse it.
        (self.repo / "skills" / "demo" / "huge-target.md").write_text(
            "z" * 100_000)
        (self.repo / "skills" / "demo" / "references" / "big.md").symlink_to(
            self.repo / "skills" / "demo" / "huge-target.md")
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "demo", "references/big.md"))

    def test_symlink_reference_outside_refused(self):
        outside = self.base / "outside-ref.md"
        outside.write_text("small but outside")
        (self.repo / "skills" / "demo" / "references" / "escape.md").symlink_to(
            outside)
        self.assertIsNone(load_skill_reference(
            self.repo, self.state, "demo", "references/escape.md"))

    def test_symlinked_note_refused(self):
        outside = self.base / "outside-note.md"
        outside.write_text("## Note\nOutside note body.")
        (self.state / "skills" / "note-link.md").symlink_to(outside)
        self.assertNotIn("note-link", self._slugs())
        self.assertIsNone(_note_info(self.state / "skills" / "note-link.md"))

    def test_multibyte_body_loads_fully(self):
        # 11,000 'e-acute' = 22,000 bytes: under the byte pre-check,
        # and 11,000 chars < the 12,000-char cap, so all of it loads.
        body = "é" * 11_000
        mb = self.repo / "skills" / "multibyte"
        mb.mkdir()
        (mb / "SKILL.md").write_text(body)
        skill = load_skill(self.repo, self.state, "multibyte")
        self.assertIsNotNone(skill)
        self.assertEqual(len(skill["body"]), 11_000)
        self.assertEqual(skill["body"], body)

    def test_fifty_thousand_char_body_refused_not_truncated(self):
        # 50,000 ASCII chars = 50,000 bytes > the 49,024-byte pre-check,
        # so the skill is refused outright, not silently truncated.
        big = self.repo / "skills" / "huge-body"
        big.mkdir()
        (big / "SKILL.md").write_text("x" * 50_000)
        self.assertNotIn("huge-body", self._slugs())
        self.assertIsNone(load_skill(self.repo, self.state, "huge-body"))

    def test_body_just_over_cap_truncates(self):
        # 13,000 ASCII chars pass the byte pre-check (13,000 < 49,024)
        # but exceed the 12,000-char cap: the body is truncated to the
        # cap, which is the documented boundary behaviour.
        over = self.repo / "skills" / "over-cap"
        over.mkdir()
        (over / "SKILL.md").write_text("x" * 13_000)
        skill = load_skill(self.repo, self.state, "over-cap")
        self.assertIsNotNone(skill)
        self.assertEqual(len(skill["body"]), 12_000)


class InstallValidationTest(unittest.TestCase):
    """F6 (follow-up 4): a replacement is validated completely
    before the existing install is touched, and a failed
    replacement leaves the previous valid install intact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.dest_root = self.base / "skills"
        self.dest_root.mkdir()
        good_src = self.base / "good"
        (good_src / "references").mkdir(parents=True)
        (good_src / "SKILL.md").write_text(SKILL_MD)
        (good_src / "references" / "a.md").write_text("a")
        install_skill(good_src, self.dest_root, "demo")
        self.dest = self.dest_root / "demo"

    def tearDown(self):
        self.tmp.cleanup()

    def _old_install_intact(self):
        self.assertEqual((self.dest / "SKILL.md").read_text(), SKILL_MD)
        self.assertEqual((self.dest / "references" / "a.md").read_text(), "a")
        self.assertIn("demo",
                      {s["slug"] for s in discover_skills(self.base, None)})
        leftovers = [p for p in self.dest_root.iterdir()
                     if p.name.startswith(".demo.")]
        self.assertEqual(leftovers, [], [str(p) for p in leftovers])

    def _bad_src(self, name):
        src = self.base / name
        src.mkdir()
        return src

    def test_oversized_skill_replacement_preserves_old(self):
        src = self._bad_src("big")
        (src / "SKILL.md").write_text("x" * 60000)
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_oversized_reference_replacement_preserves_old(self):
        src = self._bad_src("bigref")
        (src / "references").mkdir()
        (src / "SKILL.md").write_text(SKILL_MD)
        (src / "references" / "big.md").write_text("y" * 40000)
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_unreadable_replacement_preserves_old(self):
        # SKILL.md as a directory: not a regular file, so the
        # replacement is refused. (A chmod-0 file would not test
        # anything as root, which reads through permissions.)
        src = self._bad_src("nodata")
        (src / "SKILL.md").mkdir()
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_symlink_skill_replacement_preserves_old(self):
        outside = self.base / "outside.md"
        outside.write_text("outside content")
        src = self._bad_src("linky")
        (src / "SKILL.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_symlink_reference_replacement_preserves_old(self):
        outside = self.base / "outside.md"
        outside.write_text("outside content")
        src = self._bad_src("linkref")
        (src / "references").mkdir()
        (src / "SKILL.md").write_text(SKILL_MD)
        (src / "references" / "evil.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_read_capped_refuses_directory(self):
        # The fstat regular-file check: opening a directory with
        # O_RDONLY succeeds on Linux, so the type check is what
        # refuses it.
        self.assertIsNone(_read_capped(self.base, SKILL_BODY_CAP))
        self.assertIsNone(_read_capped(self.base, REFERENCE_CAP))


if __name__ == "__main__":
    unittest.main()
