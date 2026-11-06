"""Conversation state for the voice agent.

Owns the message list the brain sees, with three safety properties
the directive requires:

1. Duplicate prevention — every user turn carries a ``turn_key``
   (client-generated id). A retried/redelivered turn with the same key
   is stored exactly once.
2. Interruption rollback — when the user interrupts the agent, the
   assistant turn is truncated to the text that was actually heard
   (``truncate_assistant``). History never contains unheard text.
3. Reconnection restore — state saves to/loads from JSON. A partial
   assistant turn is NEVER restored as complete: on load it is dropped
   (the agent re-speaks it or the user repeats), because presenting a
   half-spoken reply as heard would corrupt the history.

The brain itself also persists turns server-side (VektorCode
``/api/chat`` stores every turn in its session). This local state is
the agent-side mirror used for in-memory turn handling and for
restoring a dropped LiveKit session; the server-side session is the
source of truth for the text UI.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field


@dataclass
class Turn:
    turn_id: str
    role: str  # "user" | "assistant"
    content: str
    status: str = "complete"  # "complete" | "partial"
    turn_key: str = ""  # idempotency key (user turns only)


@dataclass
class ConversationState:
    turns: list[Turn] = field(default_factory=list)

    # -- user turns -----------------------------------------------------
    def add_user_turn(self, text: str, turn_key: str = "") -> str | None:
        """Append a user turn. Returns the turn id, or None if this
        ``turn_key`` was already stored (duplicate delivery)."""
        text = (text or "").strip()
        if not text:
            return None
        if turn_key:
            for turn in self.turns:
                if turn.role == "user" and turn.turn_key == turn_key:
                    return None  # duplicate: already stored
        turn_id = str(uuid.uuid4())
        self.turns.append(Turn(turn_id=turn_id, role="user",
                               content=text, turn_key=turn_key))
        return turn_id

    # -- assistant turns ------------------------------------------------
    def begin_assistant_turn(self) -> str:
        turn_id = str(uuid.uuid4())
        self.turns.append(Turn(turn_id=turn_id, role="assistant",
                               content="", status="partial"))
        return turn_id

    def append_assistant_text(self, turn_id: str, text: str) -> None:
        for turn in self.turns:
            if turn.turn_id == turn_id and turn.role == "assistant":
                turn.content += text
                return

    def finish_assistant_turn(self, turn_id: str) -> None:
        for turn in self.turns:
            if turn.turn_id == turn_id and turn.role == "assistant":
                turn.status = "complete"
                return

    def truncate_assistant(self, turn_id: str, heard_text: str) -> bool:
        """Interruption rollback: keep only the text actually heard.

        Returns False when the turn id is unknown. An empty heard text
        drops the turn entirely (nothing was heard).
        """
        heard = (heard_text or "").strip()
        for i, turn in enumerate(self.turns):
            if turn.turn_id == turn_id and turn.role == "assistant":
                if not heard:
                    del self.turns[i]
                else:
                    turn.content = heard
                    turn.status = "complete"
                return True
        return False

    # -- views ----------------------------------------------------------
    def to_messages(self) -> list[dict]:
        """LLM context: complete turns only, in order. A partial
        assistant turn is excluded — the brain must never see a
        half-spoken reply as if it were said."""
        return [{"role": t.role, "content": t.content}
                for t in self.turns if t.status == "complete" and t.content.strip()]

    def last_turns(self, n: int = 3) -> list[Turn]:
        """Last ``n`` complete turns, for restore verification."""
        complete = [t for t in self.turns if t.status == "complete"]
        return complete[-n:]

    # -- persistence ----------------------------------------------------
    def save(self, path: str) -> None:
        data = {"turns": [
            {"turn_id": t.turn_id, "role": t.role, "content": t.content,
             "status": t.status, "turn_key": t.turn_key}
            for t in self.turns]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    @classmethod
    def load(cls, path: str) -> "ConversationState":
        """Restore saved state. Partial assistant turns are dropped —
        they were never fully heard, so restoring them as history
        would be a lie. Partial user turns cannot exist (user turns
        are only added complete)."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        state = cls()
        for raw in data.get("turns", []):
            if raw.get("role") == "assistant" and raw.get("status") != "complete":
                continue
            state.turns.append(Turn(
                turn_id=raw.get("turn_id") or str(uuid.uuid4()),
                role=raw.get("role", "user"),
                content=raw.get("content", ""),
                status="complete" if raw.get("status") == "complete" else "complete",
                turn_key=raw.get("turn_key", ""),
            ))
        return state
