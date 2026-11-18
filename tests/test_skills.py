"""Tests for the skill system (hypit-integration, 2026-11-15).

Covers vektor_cloud/skills.py only — no server, no network.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from vektor_cloud.skills import (
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


if __name__ == "__main__":
    unittest.main()
