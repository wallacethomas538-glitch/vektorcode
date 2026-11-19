"""Tests for the approval-gated sandboxed shell tool + skill endpoints
(hypit-integration, 2026-11-15).

Two levels: _run_tool_call on a stub handler (no network), and the
HTTP approve/skills endpoints against a live in-process server.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


def _http(port, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class ShellToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_db, cls.old_state = app.DB, app.VEKTOR_STATE
        app.DB = Path(cls.tmp.name) / "test.db"
        app.VEKTOR_STATE = Path(cls.tmp.name) / "state"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB, app.VEKTOR_STATE = cls.old_db, cls.old_state
        cls.tmp.cleanup()

    def setUp(self):
        self.stub = object.__new__(app.Handler)
        self.stub.headers = {}
        app._SHELL_APPROVALS.clear()
        app._SHELL_CALL_COUNTS.clear()
        self.stub.save_setting("tools", ["shell"])
        self.sid = "test-session"

    def _call(self, name, args):
        return app.Handler._run_tool_call(self.stub, name, args, self.sid)

    def _stage_and_approve(self, command):
        res, _ = self._call("run_shell", {"command": command})
        self.assertTrue(res.get("approval_required"), res)
        approval_id = res["approval_id"]
        status, body = _http(self.port, "POST", "/api/shell/approve",
                             {"approval_id": approval_id})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        return approval_id

    def test_disabled_when_tool_off(self):
        self.stub.save_setting("tools", [])
        res, _ = self._call("run_shell", {"command": "echo hi"})
        self.assertFalse(res["ok"])
        self.assertIn("disabled", res["error"])

    def test_stage_only_does_not_execute(self):
        res, _ = self._call("run_shell",
                            {"command": "touch staged-marker.txt"})
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("approval_required"))
        self.assertIn("approval_id", res)
        self.assertFalse(
            (app.VEKTOR_STATE / "sandbox" / "staged-marker.txt").exists())

    def test_execute_after_approval(self):
        approval_id = self._stage_and_approve(
            "echo hello-shell > out.txt")
        res, _ = self._call("run_shell", {"command": "echo hello-shell > out.txt",
                                          "approval_id": approval_id})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["returncode"], 0)
        self.assertEqual(
            (app.VEKTOR_STATE / "sandbox" / "out.txt").read_text().strip(),
            "hello-shell")

    def test_approval_is_single_use(self):
        approval_id = self._stage_and_approve("echo once")
        args = {"command": "echo once", "approval_id": approval_id}
        res, _ = self._call("run_shell", args)
        self.assertTrue(res["ok"], res)
        res, _ = self._call("run_shell", args)
        self.assertFalse(res["ok"])
        self.assertIn("already used", res["error"])

    def test_unapproved_call_refused(self):
        res, _ = self._call("run_shell", {"command": "echo hi"})
        approval_id = res["approval_id"]
        res, _ = self._call("run_shell", {"command": "echo hi",
                                          "approval_id": approval_id})
        self.assertFalse(res["ok"])
        self.assertIn("not yet approved", res["error"])

    def test_wrong_command_refused(self):
        approval_id = self._stage_and_approve("echo real")
        res, _ = self._call("run_shell", {"command": "echo different",
                                          "approval_id": approval_id})
        self.assertFalse(res["ok"])
        self.assertIn("does not match", res["error"])

    def test_unknown_approval_refused(self):
        res, _ = self._call("run_shell", {"command": "echo hi",
                                          "approval_id": "no-such-id"})
        self.assertFalse(res["ok"])
        self.assertIn("Unknown approval_id", res["error"])

    def test_expired_approval_refused(self):
        import time
        res, _ = self._call("run_shell", {"command": "echo hi"})
        approval_id = res["approval_id"]
        app._SHELL_APPROVALS[approval_id]["expires"] = time.time() - 1
        _http(self.port, "POST", "/api/shell/approve",
              {"approval_id": approval_id})
        res, _ = self._call("run_shell", {"command": "echo hi",
                                          "approval_id": approval_id})
        self.assertFalse(res["ok"])
        self.assertIn("expired", res["error"])

    def test_approve_unknown_id_404(self):
        status, body = _http(self.port, "POST", "/api/shell/approve",
                             {"approval_id": "no-such-id"})
        self.assertEqual(status, 404)

    def test_env_scrubbed_of_keys(self):
        # Every real key name the app uses must be absent from the child
        # environment — not just _API_KEY names (Finding 2: GEMINI_KEYS,
        # GROQ_KEYS and SUPABASE_KEY used to leak through `env`).
        key_vars = {
            "HYPIT_TEST_API_KEY": "do-not-leak-apikey",
            "GEMINI_KEYS": "do-not-leak-gemini",
            "GROQ_KEYS": "do-not-leak-groq",
            "SUPABASE_KEY": "do-not-leak-supabase",
            "OPENROUTER_KEYS": "do-not-leak-openrouter",
            "META_KEYS": "do-not-leak-meta",
            "QWEN_KEYS": "do-not-leak-qwen",
            "MISTRAL_KEYS": "do-not-leak-mistral",
            "TOGETHER_KEYS": "do-not-leak-together",
            "HUGGINGFACE_KEYS": "do-not-leak-hf",
        }
        for name, sentinel in key_vars.items():
            os.environ[name] = sentinel
        try:
            for name, sentinel in key_vars.items():
                # Fresh session per var: the 5-per-session execution cap
                # would otherwise refuse the later vars.
                self.sid = f"test-session-{name}"
                cmd = f'echo "X${{{name}}}X"'
                approval_id = self._stage_and_approve(cmd)
                res, _ = self._call(
                    "run_shell",
                    {"command": cmd, "approval_id": approval_id})
                self.assertTrue(res["ok"], res)
                self.assertNotIn(sentinel, res["stdout"],
                                 f"{name} leaked into child env")
                self.assertIn("XX", res["stdout"])
        finally:
            for name in key_vars:
                del os.environ[name]

    def test_extra_env_cannot_reintroduce_keys(self):
        # F2 hardening: extra_env is merged AFTER the os.environ
        # scrub, so it must be scrubbed with the same suffixes —
        # otherwise a caller could reintroduce a live key.
        from vektor_cloud.sandbox import Sandbox
        with tempfile.TemporaryDirectory() as tmp:
            sb = Sandbox(tmp)
            env = sb._scrubbed_env({"GEMINI_KEYS": "leak-me",
                                     "gemini_keys": "leak-me-too",
                                     "SUPABASE_KEY": "leak-me-three",
                                     "MY_OK_VAR": "fine",
                                     "PATH": "/custom/bin"})
            self.assertNotIn("GEMINI_KEYS", env)
            self.assertNotIn("gemini_keys", env)
            self.assertNotIn("SUPABASE_KEY", env)
            self.assertEqual(env.get("MY_OK_VAR"), "fine")
            self.assertEqual(env.get("PATH"), "/custom/bin")

    def test_output_capped(self):
        approval_id = self._stage_and_approve(
            "python3 -c \"print('x'*10000)\"")
        res, _ = self._call(
            "run_shell",
            {"command": "python3 -c \"print('x'*10000)\"",
             "approval_id": approval_id})
        self.assertTrue(res["ok"], res)
        self.assertLessEqual(len(res["stdout"]), 4000)

    def test_timeout(self):
        approval_id = self._stage_and_approve("sleep 3")
        res, _ = self._call("run_shell", {"command": "sleep 3",
                                          "approval_id": approval_id,
                                          "timeout": 1})
        self.assertTrue(res["timed_out"], res)
        self.assertEqual(res["returncode"], -1)

    def test_session_cap(self):
        for _ in range(app.SHELL_CALL_CAP_PER_SESSION):
            approval_id = self._stage_and_approve("true")
            res, _ = self._call("run_shell", {"command": "true",
                                              "approval_id": approval_id})
            self.assertTrue(res["ok"], res)
        approval_id = self._stage_and_approve("true")
        res, _ = self._call("run_shell", {"command": "true",
                                          "approval_id": approval_id})
        self.assertFalse(res["ok"])
        self.assertIn("cap reached", res["error"])

    def test_kill_switch_refuses(self):
        os.environ["VEKTOR_KILL_SWITCH"] = "1"
        try:
            res, _ = self._call("run_shell", {"command": "echo hi"})
            self.assertFalse(res["ok"])
            self.assertIn("Aegis gate refused", res["error"])
        finally:
            del os.environ["VEKTOR_KILL_SWITCH"]

    def test_bad_command_rejected(self):
        for bad in ("", "   ", "x" * 2001, "echo \x00hi"):
            res, _ = self._call("run_shell", {"command": bad})
            self.assertFalse(res["ok"], bad)
            self.assertIn("1-2000", res["error"])

    def test_declared_tools_include_shell_and_reference(self):
        tools = self.stub._declared_tools()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("run_shell", names)
        # No skill loaded for the turn: the reference tool stays out.
        self.assertNotIn("read_skill_reference", names)
        # With a skill loaded, the reference tool is declared.
        tools = self.stub._declared_tools("hypit")
        names = {t["function"]["name"] for t in tools}
        self.assertIn("read_skill_reference", names)

    def test_declared_tools_omit_reference_for_unknown_skill(self):
        tools = self.stub._declared_tools("no-such-skill")
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn("read_skill_reference", names)

    def test_declared_tools_omit_shell_when_disabled(self):
        self.stub.save_setting("tools", [])
        tools = self.stub._declared_tools()
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn("run_shell", names)

    def test_read_skill_reference_hypit(self):
        res, _ = self._call("read_skill_reference", {
            "skill": "hypit",
            "reference": "references/environment/distribution.md"})
        self.assertTrue(res["ok"], res)
        self.assertIn("Installations", res["content"])

    def test_read_skill_reference_escape_refused(self):
        res, _ = self._call("read_skill_reference", {
            "skill": "hypit", "reference": "../../README.md"})
        self.assertFalse(res["ok"])

    def test_skills_endpoint_lists_hypit(self):
        status, body = _http(self.port, "GET", "/api/skills")
        self.assertEqual(status, 200)
        by_slug = {s["slug"]: s for s in body["skills"]}
        self.assertIn("hypit", by_slug)
        self.assertEqual(by_slug["hypit"]["kind"], "skill")

    def test_skill_save_becomes_loadable(self):
        status, body = _http(self.port, "POST", "/api/skill/save", {
            "title": "My Saved Note", "content": "A saved skill note."})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("loadable"))
        status, body = _http(self.port, "GET", "/api/skills")
        by_slug = {s["slug"]: s for s in body["skills"]}
        self.assertIn("my-saved-note", by_slug)
        self.assertEqual(by_slug["my-saved-note"]["kind"], "note")

    def test_pending_approvals_endpoint(self):
        res, _ = self._call("run_shell", {"command": "echo pending"})
        approval_id = res["approval_id"]
        status, body = _http(self.port, "GET", "/api/shell/approvals")
        self.assertEqual(status, 200)
        pending = {a["approval_id"]: a for a in body["approvals"]}
        self.assertIn(approval_id, pending)
        self.assertEqual(pending[approval_id]["command"], "echo pending")
        self.assertFalse(pending[approval_id]["approved"])

    def test_resolve_invoked_skill(self):
        self.assertEqual(
            app.resolve_invoked_skill(
                {}, [{"role": "user", "content": "/skill hypit do it"}]),
            "hypit")
        self.assertEqual(
            app.resolve_invoked_skill({"skill": "hypit"}, []), "hypit")
        self.assertIsNone(
            app.resolve_invoked_skill(
                {}, [{"role": "user", "content": "hello"}]))


if __name__ == "__main__":
    unittest.main()
