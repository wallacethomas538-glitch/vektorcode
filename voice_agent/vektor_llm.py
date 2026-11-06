"""LiveKit LLM adapter that calls the VektorCode brain.

The voice agent does NOT pick its own model: every turn goes to the
existing ``/api/chat`` (``VektorBrain``), with the same session id the
text UI uses. The brain's provider fallback chain and free-tier-only
rules therefore apply to voice exactly as they do to text, and voice
turns appear in the same session history.

Only user/assistant turns are sent; the brain prepends its own system
prompt server-side. ``voice=True`` is passed so the brain adds a
short spoken-style note ("keep replies short, plain words, no
markdown") to the system prompt for this call only — the flag is not
persisted in history.

The brain is non-streaming, so one turn yields one chunk. Token
streaming is a later improvement (SSE ``/api/chat?stream=true``);
the session still speaks sentence-by-sentence because the TTS
adapter synthesizes per sentence.
"""
from __future__ import annotations

import asyncio
import uuid

from livekit.agents import llm
from livekit.agents.types import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS

from .brain import BrainError, VektorBrain


class VektorBrainLLM(llm.LLM):
    """LLM whose 'model' is the VektorCode brain."""

    def __init__(self, brain: VektorBrain):
        super().__init__()
        self._brain = brain

    @property
    def model(self) -> str:
        return "vektorcode-brain"

    @property
    def provider(self) -> str:
        return "VektorCode (/api/chat)"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=None,
        tool_choice=None,
        extra_kwargs=None,
    ) -> llm.LLMStream:
        return _VektorBrainLLMStream(
            llm=self, chat_ctx=chat_ctx, tools=tools or [],
            conn_options=conn_options)


class _VektorBrainLLMStream(llm.LLMStream):
    async def _run(self) -> None:
        brain: VektorBrain = self._llm._brain
        messages = []
        for item in self._chat_ctx.items:
            if isinstance(item, llm.ChatMessage) and item.role in ("user", "assistant"):
                text = item.text_content or ""
                if text.strip():
                    messages.append({"role": item.role, "content": text})
        try:
            reply = await asyncio.to_thread(brain.ask, messages, voice=True)
        except BrainError as exc:
            # Speak the failure instead of leaving the user in silence.
            # The real error is logged by the worker; the spoken text
            # stays short and contains no internals.
            reply = "Sorry — the brain didn't answer that one. Try again?"
            import logging
            logging.getLogger("vektorcode.voice").warning("brain failed: %s", exc)
        self._event_ch.send_nowait(llm.ChatChunk(
            id=str(uuid.uuid4()),
            delta=llm.ChoiceDelta(role="assistant", content=reply),
        ))
