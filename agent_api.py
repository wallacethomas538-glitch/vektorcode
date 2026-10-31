#!/usr/bin/env python3
"""VektorCode cloud agent API — Render entry point.

This is NOT the dashboard. The dashboard (dashboard/) is Termux-only.
This service is the cloud agent: chat through the free cloud cascade,
draft-only task planning, health. No UI, no static files.

Mode/role come from the environment:
  VEKTOR_MODE=cloud   (auto-detected on Render)
  VEKTOR_ROLE=coder   (auto-detected on Render)

The coder role runs behind the protective shell: kill switch, Aegis gate,
egress allowlist, sandbox. Nothing executes without approval; code work is
staged, never auto-run.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("VEKTORCODE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("VEKTORCODE_PORT", "8765")))

from vektor_cloud.free_cascade import Cascade, vektor_mode, vektor_role, can_code
from vektor_cloud.core import VektorCore, kill_on


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            self.send_json({
                "ok": True,
                "service": "vektorcode-agent",
                "mode": vektor_mode(),
                "role": vektor_role(),
                "kill_switch": kill_on(),
                "time": int(time.time()),
            })
            return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if kill_on():
            self.send_json({"ok": False, "error": "Kill switch is active"}, 423)
            return
        data = self.body()
        if self.path == "/api/chat":
            self.handle_chat(data)
            return
        if self.path == "/api/do":
            self.handle_do(data)
            return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def handle_chat(self, data):
        """Conversational reply through the free cloud cascade."""
        prompt = data.get("prompt") or data.get("message", "")
        if not isinstance(prompt, str) or not prompt.strip():
            self.send_json({"ok": False, "error": "prompt must be a non-empty string"}, 400)
            return
        started = time.time()
        result = Cascade().chat(prompt.strip()[:4000])
        if result.get("status") == "ok":
            self.send_json({
                "ok": True,
                "message": result["reply"],
                "provider": result["provider"],
                "mode": vektor_mode(),
                "latency_ms": round((time.time() - started) * 1000),
            })
        else:
            self.send_json({"ok": False, "error": result.get("reason", "no provider answered")}, 502)

    def handle_do(self, data):
        """Draft-only task planning. Coder role only; staged, never executed."""
        if not can_code():
            self.send_json({
                "ok": False,
                "error": "code work needs the coder role (this copy runs '%s')" % vektor_role(),
            }, 403)
            return
        task = data.get("task", "")
        if not isinstance(task, str) or not task.strip():
            self.send_json({"ok": False, "error": "task must be a non-empty string"}, 400)
            return
        result = VektorCore().run_command(task.strip()[:2000], approved=False)
        self.send_json({
            "ok": True,
            "status": result.get("status"),
            "plan": result.get("plan"),
            "receipt_id": result.get("receipt_id"),
            "executed": False,
        })


if __name__ == "__main__":
    # Cloud agent always runs cloud mode unless explicitly told otherwise.
    os.environ.setdefault("VEKTOR_MODE", "cloud")
    os.environ.setdefault("VEKTOR_ROLE", "coder")
    print(f"VektorCode cloud agent ({vektor_mode()}/{vektor_role()}): http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
