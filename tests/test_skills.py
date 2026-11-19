"""Tests for the skill system (hypit-integration, 2026-11-15).

Covers vektor_cloud/skills.py only — no server, no network.
"""
import multiprocessing
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from vektor_cloud.skills import (
    REFERENCE_CAP,
    SKILL_BODY_CAP,
    _copy_skill_file,
    _copy_skill_tree,
    _note_info,
    _read_capped,
    _skill_dir_info,
    discover_skills,
    format_skill_for_prompt,
    install_skill,
    load_skill,
    load_skill_reference,
    parse_skill_markdown,
    _slug_lock,
    recover_interrupted_installs,
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
                     if p.name.startswith(".demo.") and not p.name.endswith(".lock")]
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


class InstallHardeningTest(unittest.TestCase):
    """F6 (follow-up 5): symlinked roots, special files, concurrent
    installs, crash recovery, and traversal hardening. Same good
    install as InstallValidationTest, then one bad shape at a time.
    """

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
                     if p.name.startswith(".demo.") and not p.name.endswith(".lock")]
        self.assertEqual(leftovers, [], [str(p) for p in leftovers])

    def _bad_src(self, name):
        src = self.base / name
        src.mkdir()
        return src

    def _skill_md(self, name):
        return SKILL_MD.replace("demo-skill", name)

    def test_install_rejects_symlinked_source_root(self):
        # The source root itself is a symlink to a valid skill dir:
        # refused before anything is read or copied.
        real = self.base / "real"
        (real / "references").mkdir(parents=True)
        (real / "SKILL.md").write_text(self._skill_md("evil-skill"))
        (real / "references" / "a.md").write_text("a")
        link = self.base / "linkroot"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            install_skill(link, self.dest_root, "demo")
        self._old_install_intact()

    def test_install_rejects_fifo(self):
        # A FIFO in the source: refused as a special file at
        # validation time. (The old code skipped it in validation
        # and failed later with a shutil.Error from copy2; a FIFO
        # in a reference would also block _read_capped — see the
        # O_NONBLOCK test below.)
        src = self._bad_src("fifosrc")
        (src / "SKILL.md").write_text(SKILL_MD)
        os.mkfifo(src / "references-fifo")
        with self.assertRaises(ValueError):
            install_skill(src, self.dest_root, "demo")
        self._old_install_intact()

    def test_install_rejects_socket(self):
        # A Unix socket in the source: refused like any special file.
        src = self._bad_src("socksrc")
        (src / "SKILL.md").write_text(SKILL_MD)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(src / "agent.sock"))
            with self.assertRaises(ValueError):
                install_skill(src, self.dest_root, "demo")
        finally:
            sock.close()
        self._old_install_intact()

    def test_read_capped_fifo_returns_none_quickly(self):
        # _read_capped opens with O_NONBLOCK: a FIFO returns None
        # immediately instead of blocking until a writer appears.
        # The read runs in a thread so a regression fails the test
        # instead of hanging the suite.
        fifo = self.base / "reader.fifo"
        os.mkfifo(fifo)
        holder = {}

        def _read():
            holder["result"] = _read_capped(fifo, SKILL_BODY_CAP)

        thread = threading.Thread(target=_read, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(holder["result"])

    def test_concurrent_installs_same_slug_serialized(self):
        # Two threads install different contents for the same slug
        # at once: both must succeed, and the final install must be
        # exactly one complete version — never a mix, never an error.
        v1 = self._bad_src("conc-v1")
        (v1 / "SKILL.md").write_text(self._skill_md("version-one"))
        v2 = self._bad_src("conc-v2")
        (v2 / "SKILL.md").write_text(self._skill_md("version-two"))
        errors = []

        def _install(src):
            try:
                install_skill(src, self.dest_root, "demo")
            except Exception as exc:  # noqa: BLE001 — collected, asserted
                errors.append(exc)

        threads = [threading.Thread(target=_install, args=(v1,)),
                   threading.Thread(target=_install, args=(v2,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        final = (self.dest / "SKILL.md").read_text()
        self.assertIn(final, [self._skill_md("version-one"),
                              self._skill_md("version-two")])
        leftovers = [p for p in self.dest_root.iterdir()
                     if p.name.startswith(".demo.") and not p.name.endswith(".lock")]
        self.assertEqual(leftovers, [], [str(p) for p in leftovers])

    def _simulate_crash_between_renames(self):
        # The state after the first rename, before the second: the
        # destination is missing and the old install sits in a
        # backup directory (a process crash, not an OSError).
        backup = self.dest_root / ".demo.backup-crashsim"
        self.dest.rename(backup)
        self.assertFalse(self.dest.exists())
        return backup

    def test_crash_recovery_restores_missing_dest(self):
        self._simulate_crash_between_renames()
        result = recover_interrupted_installs(self.dest_root)
        self.assertEqual(result["recovered"], ["demo"])
        self.assertEqual((self.dest / "SKILL.md").read_text(), SKILL_MD)
        self.assertEqual((self.dest / "references" / "a.md").read_text(), "a")
        leftovers = [p for p in self.dest_root.iterdir()
                     if p.name.startswith(".demo.") and not p.name.endswith(".lock")]
        self.assertEqual(leftovers, [], [str(p) for p in leftovers])

    def test_install_after_crash_installs_new_version(self):
        # A crash leaves the old install in a backup; the next
        # install heals it first, then swaps in the new version.
        self._simulate_crash_between_renames()
        v2 = self._bad_src("after-crash")
        (v2 / "SKILL.md").write_text(self._skill_md("version-two"))
        install_skill(v2, self.dest_root, "demo")
        self.assertEqual((self.dest / "SKILL.md").read_text(),
                         self._skill_md("version-two"))
        leftovers = [p for p in self.dest_root.iterdir()
                     if p.name.startswith(".demo.") and not p.name.endswith(".lock")]
        self.assertEqual(leftovers, [], [str(p) for p in leftovers])

    def test_recovery_cleans_orphan_backup_and_staging(self):
        # Destination exists (a completed install) plus an orphan
        # backup and a staging leftover: recovery removes both and
        # leaves the live install untouched.
        orphan = self.dest_root / ".demo.backup-orphan"
        shutil.copytree(self.dest, orphan)
        staging = self.dest_root / ".demo.staging-left"
        (staging / "references").mkdir(parents=True)
        (staging / "SKILL.md").write_text(self._skill_md("staged"))
        result = recover_interrupted_installs(self.dest_root)
        self.assertEqual(result["cleaned"], ["demo", "demo"])
        self.assertEqual((self.dest / "SKILL.md").read_text(), SKILL_MD)
        self.assertFalse(orphan.exists())
        self.assertFalse(staging.exists())

    def test_copy_refuses_symlinked_subdir(self):
        # The fd-based copy refuses a symlinked directory itself —
        # it does not trust the listing, and the target is never read.
        outside = self.base / "outside-dir"
        outside.mkdir()
        (outside / "marker.txt").write_text("outside content")
        src = self._bad_src("copylink")
        (src / "SKILL.md").write_text(SKILL_MD)
        (src / "refs").symlink_to(outside, target_is_directory=True)
        dst = self.base / "copy-out"
        with self.assertRaises(ValueError):
            _copy_skill_tree(src, dst)
        self.assertFalse((dst / "refs" / "marker.txt").exists())

    def test_copy_refuses_symlinked_root(self):
        real = self.base / "copyreal"
        (real / "references").mkdir(parents=True)
        (real / "SKILL.md").write_text(SKILL_MD)
        link = self.base / "copylinkroot"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            _copy_skill_tree(link, self.base / "copy-out2")

    def test_copy_refuses_fifo(self):
        src = self._bad_src("copyfifo")
        (src / "SKILL.md").write_text(SKILL_MD)
        os.mkfifo(src / "blocked.fifo")
        with self.assertRaises(ValueError):
            _copy_skill_tree(src, self.base / "copy-out3")

    def test_copy_mid_traversal_swap(self):
        # A directory swapped for a symlink DURING the copy: the
        # first subdir holds many small files so the copy is still
        # running when the swap lands on the last subdir. The copy
        # must refuse the symlinked directory and never copy through
        # it to the outside marker.
        outside = self.base / "swap-outside"
        outside.mkdir()
        (outside / "marker.txt").write_text("outside content")
        src = self._bad_src("swap")
        (src / "SKILL.md").write_text(SKILL_MD)
        slow = src / "aaa-slow"
        slow.mkdir()
        for i in range(1000):
            (slow / f"f{i:04d}.txt").write_text("x")
        target_dir = src / "zzz-target"
        target_dir.mkdir()
        (target_dir / "orig.txt").write_text("original")
        dst = self.base / "swap-out"
        errors = []

        def _copy():
            try:
                _copy_skill_tree(src, dst)
            except Exception as exc:  # noqa: BLE001 — asserted below
                errors.append(exc)

        thread = threading.Thread(target=_copy, daemon=True)
        thread.start()
        shutil.rmtree(target_dir)
        (src / "zzz-target").symlink_to(outside, target_is_directory=True)
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], ValueError)
        self.assertFalse((dst / "zzz-target" / "marker.txt").exists())

    def test_discover_skips_dot_prefixed_entries(self):
        # Staging/backup leftovers must never appear as skills in a
        # concurrent listing (the follow-up-4 phantom-entry residual).
        for name in (".demo.staging-x", ".demo.backup-y"):
            d = self.dest_root / name
            d.mkdir()
            (d / "SKILL.md").write_text(self._skill_md("phantom"))
        slugs = {s["slug"] for s in discover_skills(self.base, None)}
        self.assertIn("demo", slugs)
        self.assertFalse(any("staging" in s or "backup" in s
                             for s in slugs), slugs)


if __name__ == "__main__":
    unittest.main()

# -- Follow-up 6 child-process helpers (module level so the fork
# start method can run them) -------------------------------------
def _install_in_child(src, dest_root, slug, queue):
    """install_skill in a child process; report ok/error on queue."""
    try:
        install_skill(src, dest_root, slug)
        queue.put("ok")
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        queue.put(("error", repr(exc)))


def _recover_in_child(dest_root, slug, queue, ready):
    """recover_interrupted_installs in a child; report on queue.

    Waits for `ready` first: the parent takes the per-slug lock
    before setting it, so the child is forked BEFORE the lock fd
    exists and cannot inherit it (fork + flock: a child that
    inherits the lock fd deadlocks against its own copy).
    """
    try:
        ready.wait(timeout=10)
        recover_interrupted_installs(dest_root, slug)
        queue.put("ok")
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        queue.put(("error", repr(exc)))


class InstallFollowup6Test(unittest.TestCase):
    """F6 (follow-up 6): FIFO race, bounded copying, cross-process
    install locking. The FIFO and growth tests are written so the
    OLD code fails them instead of hanging: the FIFO test runs the
    copy in a worker thread with a join timeout, and the growth
    test uses a real writer thread coordinated through the target
    open.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.dest_root = self.base / "skills"
        self.dest_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _good_src(self, name, body=SKILL_MD, ref="a"):
        src = self.base / name
        (src / "references").mkdir(parents=True)
        (src / "SKILL.md").write_text(body)
        (src / "references" / "a.md").write_text(ref)
        return src

    def test_copy_refuses_fifo_swapped_in_between_stat_and_open(self):
        # F6 (follow-up 6, Priority 2): a regular file swapped for
        # a FIFO between the pre-stat and the open. O_NOFOLLOW does
        # NOT refuse a FIFO, so without O_NONBLOCK the open would
        # block until a writer appears; with it, the open returns
        # immediately and the fstat S_ISREG check refuses it. The
        # copy runs in a worker thread so old code FAILS (thread
        # still alive at the join timeout) instead of hanging the
        # suite; the cleanup opens the FIFO for writing to release
        # a stuck reader.
        src = self._good_src("good")
        dst = self.base / "out"
        target_file = src / "a.md"
        (src / "a.md").write_text("a" * 100)
        real_open = os.open
        swapped = {"done": False}

        def swapping_open(path, flags, *args, **kwargs):
            name = os.path.basename(os.fspath(path))
            is_file_open = (kwargs.get("dir_fd") is not None
                            or "dir_fd" in kwargs)
            # dir_fd arrives as a keyword in _copy_skill_file.
            if (name == "a.md" and not swapped["done"]
                    and not (flags & getattr(os, "O_DIRECTORY", 0))):
                swapped["done"] = True
                os.unlink(target_file)
                os.mkfifo(target_file)
            return real_open(path, flags, *args, **kwargs)

        result = {}

        def run_copy():
            try:
                _copy_skill_tree(src, dst)
                result["ok"] = True
            except Exception as exc:  # noqa: BLE001 - captured
                result["error"] = exc

        import unittest.mock as mock
        with mock.patch("os.open", side_effect=swapping_open):
            worker = threading.Thread(target=run_copy, daemon=True)
            worker.start()
            worker.join(timeout=5)
            try:
                # The discriminating assertion: with O_NONBLOCK the
                # open returns immediately and the copy finishes in
                # milliseconds, so the thread must already be done.
                # Without it, the open blocks until a writer appears
                # and the thread is still alive here. The cleanup
                # below only releases a stuck reader so the suite
                # does not leak a thread; it must NOT run before
                # this assertion, or it would mask the failure.
                self.assertFalse(worker.is_alive(),
                                 "copy blocked on the swapped-in FIFO "
                                 "(no O_NONBLOCK)")
            finally:
                if worker.is_alive():
                    try:
                        wfd = real_open(target_file, os.O_WRONLY)
                        os.close(wfd)
                    except OSError:
                        pass
                    worker.join(timeout=2)
            self.assertIsInstance(result.get("error"), ValueError,
                                  result)
            self.assertFalse((dst / "a.md").exists(),
                             "partial output left in place")

    def test_copy_aborts_when_file_grows_past_cap(self):
        # F6 (follow-up 6, Priority 3): a file that grows during
        # the copy. The writer thread is started by the target
        # open (which strictly precedes the first read), so the
        # file is within cap at the pre-stat and over cap at the
        # first read. The copy must abort with the growth error
        # and leave no partial target behind.
        src_dir = self.base / "grow"
        src_dir.mkdir()
        src_file = src_dir / "a.md"
        src_file.write_bytes(b"x" * 1024)
        out_dir = self.base / "out"
        out_dir.mkdir()
        target = out_dir / "a.md"
        cap = 4096
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                with open(src_file, "ab") as f:
                    f.write(b"y" * 16384)
                time.sleep(0.001)

        real_open = os.open
        started = {"thread": None}

        def triggering_open(path, flags, *args, **kwargs):
            if (os.fspath(path) == os.fspath(target)
                    and (flags & os.O_CREAT)
                    and started["thread"] is None):
                # The first append is done synchronously here, in
                # the open hook: a thread-start race made the test
                # flaky under load (the copy's first read could
                # return the whole 1 KB file before the writer
                # thread was even scheduled). The writer thread
                # then keeps appending concurrently while the copy
                # streams. Either way the growth lands after the
                # pre-stat and before/during the streaming reads —
                # that is the window the cumulative cap must catch.
                with open(src_file, "ab") as f:
                    f.write(b"y" * 16384)
                started["thread"] = threading.Thread(target=writer,
                                                     daemon=True)
                started["thread"].start()
            return real_open(path, flags, *args, **kwargs)

        dfd = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            pre = os.stat("a.md", dir_fd=dfd, follow_symlinks=False)
            import unittest.mock as mock
            with mock.patch("os.open", side_effect=triggering_open):
                with self.assertRaisesRegex(ValueError, "grew past cap"):
                    _copy_skill_file(dfd, "a.md", pre, target, cap)
            self.assertFalse(target.exists(),
                             "partial output left after growth abort")
        finally:
            stop.set()
            if started["thread"] is not None:
                started["thread"].join(timeout=2)
            os.close(dfd)

    def test_cross_process_installs_serialized(self):
        # F6 (follow-up 6, Priority 5): two processes install the
        # same slug concurrently from different sources. The flock
        # serializes them: both succeed, and the final install is
        # exactly one coherent version (body + reference match).
        # Three rounds to shake out races.
        ctx = multiprocessing.get_context("fork")
        for round_no in range(3):
            src_v1 = self._good_src(f"v1-{round_no}", SKILL_MD.replace(
                "Do the demo thing.", "version one."), "v1")
            src_v2 = self._good_src(f"v2-{round_no}", SKILL_MD.replace(
                "Do the demo thing.", "version two."), "v2")
            queue = ctx.Queue()
            p1 = ctx.Process(target=_install_in_child,
                             args=(src_v1, self.dest_root, "demo", queue))
            p2 = ctx.Process(target=_install_in_child,
                             args=(src_v2, self.dest_root, "demo", queue))
            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)
            self.assertFalse(p1.is_alive(), "installer 1 hung")
            self.assertFalse(p2.is_alive(), "installer 2 hung")
            results = [queue.get(timeout=5), queue.get(timeout=5)]
            self.assertEqual(results, ["ok", "ok"], results)
            body = (self.dest_root / "demo" / "SKILL.md").read_text()
            ref = (self.dest_root / "demo" / "references"
                   / "a.md").read_text()
            coherent = ((body == SKILL_MD.replace(
                "Do the demo thing.", "version one.") and ref == "v1")
                or (body == SKILL_MD.replace(
                    "Do the demo thing.", "version two.") and ref == "v2"))
            self.assertTrue(coherent,
                            f"incoherent final install: body={body!r} "
                            f"ref={ref!r}")

    def test_recovery_cannot_delete_active_staging_dir(self):
        # F6 (follow-up 6, Priority 5): while one process holds the
        # per-slug lock with a staging directory present, another
        # process's recovery must block on the lock and must NOT
        # delete the staging directory. Once the lock is released,
        # the recovery proceeds and removes the leftover.
        ctx = multiprocessing.get_context("fork")
        staging = self.dest_root / ".demo.staging-active"
        queue = ctx.Queue()
        ready = ctx.Event()
        child = ctx.Process(target=_recover_in_child,
                            args=(self.dest_root, "demo", queue, ready))
        # Forked BEFORE the lock is taken, so the child cannot
        # inherit the lock fd (see _recover_in_child).
        child.start()
        try:
            with _slug_lock(self.dest_root, "demo"):
                staging.mkdir()
                (staging / "SKILL.md").write_text(SKILL_MD)
                ready.set()
                time.sleep(0.5)
                self.assertTrue(child.is_alive(),
                                "recovery did not block on the lock")
                self.assertTrue(staging.exists(),
                                "recovery deleted an active staging dir")
            child.join(timeout=5)
            self.assertFalse(child.is_alive(), "recovery hung")
            self.assertEqual(queue.get(timeout=5), "ok")
            self.assertFalse(staging.exists(),
                             "leftover staging dir was not recovered")
        finally:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
