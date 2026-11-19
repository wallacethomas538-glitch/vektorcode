"""Tests for F3 shell filesystem containment (hypit-auth-followup-3).

Two parts, kept separate on purpose:

1. The file helpers (read/write/resolve) ARE genuinely jailed:
   traversal, absolute paths, and symlinks that point outside the
   root are refused.
2. The shell child is NOT contained — cwd is only a starting
   directory, and the command can read outside the root. The gap
   test below asserts that fact as it stands today. It is a
   tripwire: if real containment is ever implemented, this test
   MUST start failing, and must then be replaced with a test that
   asserts the escape is refused. Do not delete it to make the
   suite green.
"""
import tempfile
import unittest
from pathlib import Path

from vektor_cloud.sandbox import Sandbox, SandboxEscape


class ResolveContainmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside secret")
        self.box = Sandbox(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_refused(self):
        for bad in ("../secret.txt", "sub/../../secret.txt",
                    "sub/../../../etc/passwd"):
            with self.assertRaises(SandboxEscape, msg=bad):
                self.box.resolve(bad)

    def test_absolute_path_refused(self):
        with self.assertRaises(SandboxEscape):
            self.box.resolve("/etc/passwd")
        with self.assertRaises(SandboxEscape):
            self.box.resolve(str(self.outside / "secret.txt"))

    def test_symlink_file_outside_refused(self):
        (self.root / "link.txt").symlink_to(self.outside / "secret.txt")
        with self.assertRaises(SandboxEscape):
            self.box.resolve("link.txt")
        with self.assertRaises(SandboxEscape):
            self.box.read("link.txt")

    def test_symlink_dir_outside_refused(self):
        (self.root / "linkdir").symlink_to(self.outside,
                                           target_is_directory=True)
        with self.assertRaises(SandboxEscape):
            self.box.resolve("linkdir/secret.txt")
        res = self.box.write("linkdir/evil.txt", "x")
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["reason"], "path_escape")
        self.assertFalse((self.outside / "evil.txt").exists())

    def test_symlink_inside_root_allowed(self):
        (self.root / "real.txt").write_text("real")
        (self.root / "alias.txt").symlink_to(self.root / "real.txt")
        self.assertEqual(self.box.read("alias.txt"), "real")

    def test_normal_read_write_round_trip(self):
        res = self.box.write("sub/file.txt", "hello")
        self.assertEqual(res["status"], "written")
        self.assertEqual(self.box.read("sub/file.txt"), "hello")


class ShellContainmentGapTest(unittest.TestCase):
    """The shell child is NOT filesystem-contained (F3 gap)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        (self.base / "outside-marker.txt").write_text("marker-12345")
        self.box = Sandbox(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_shell_child_can_read_outside_root(self):
        # TRIPWIRE (see module docstring): the shell runs with
        # cwd=root only. It can walk out and read files outside the
        # jail. This test proves the gap exists; replace it with a
        # refusal test the day containment is implemented.
        res = self.box.run(["/bin/sh", "-c", "cat ../outside-marker.txt"],
                           timeout=10)
        self.assertEqual(res["returncode"], 0, res)
        self.assertIn("marker-12345", res["stdout"])


if __name__ == "__main__":
    unittest.main()
