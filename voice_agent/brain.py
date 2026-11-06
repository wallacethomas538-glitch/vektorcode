"""Client for the VektorCode brain (the existing ``/api/chat``).

The voice agent does NOT reimplement chat: it calls the same HTTP
endpoint the text UI uses, with the same ``session_id``, so a voice
turn and a typed turn share one session, one history, and one
provider fallback chain (Groq -> OpenRouter -> Meta -> ... -> Qwen ->
Gemini -> Mistral -> HuggingFace -> local Ollama, free-tier only).

Stdlib only (urllib) — no new dependency. No keys are handled here:
the brain's provider keys live in the VektorCode server's environment,
never in the voice worker.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class BrainError(Exception):
    """The brain could not answer (unreachable, misconfigured, or every
    provider failed). The message is safe to show the user."""


class VektorBrain:
    def __init__(self, base_url: str, session_id: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "User-Agent": "VektorCode-Voice/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
                detail = body.get("error", "") or str(exc)
            except (ValueError, UnicodeError):
                detail = str(exc)
            raise BrainError(f"brain HTTP {exc.code}: {detail}"[:240]) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BrainError(f"brain unreachable: {exc}"[:240]) from exc

    def health(self) -> bool:
        """True when the brain's /health answers."""
        try:
            return bool(self._request("GET", "/health").get("ok"))
        except BrainError:
            return False

    def ask(self, messages: list[dict], voice: bool = False) -> str:
        """One non-streaming turn. Returns the reply text.

        ``messages`` is the full LLM context (system + history + new
        user turn). The brain persists the turn in its session, so the
        text UI sees voice turns and vice versa. ``voice=True`` asks
        the brain to answer in spoken style (short, plain, no
        markdown); the flag itself is not persisted.
        """
        result = self._request("POST", "/api/chat", {
            "messages": messages,
            "session_id": self.session_id,
            "stream": False,
            "voice": bool(voice),
        })
        if not result.get("ok"):
            raise BrainError(str(result.get("error", "brain error"))[:240])
        reply = result.get("message", "")
        if not isinstance(reply, str) or not reply.strip():
            raise BrainError("brain returned an empty reply")
        return reply

    def fetch_history(self) -> list[dict]:
        """Server-side session history (best effort). Returns [] when
        the brain needs auth the worker does not have — the local
        ConversationState is the fallback, not a failure."""
        try:
            result = self._request(
                "GET", f"/api/session/{self.session_id}/messages")
            return result.get("messages", []) if result.get("ok") else []
        except BrainError:
            return []
