"""Cancellable playback queue.

The agent's spoken reply is a stream of audio frames. The queue is
what makes barge-in *immediate*: on interruption the worker calls
``flush()`` and every frame not yet played is dropped in one step —
no stale audio keeps playing after the user cut in.

``consume()`` marks frames as heard (played). ``heard_frames`` is how
the conversation history knows how much of the reply the user
actually heard, so the truncated reply matches reality.
"""
from __future__ import annotations

from collections import deque


class AudioPlaybackQueue:
    """FIFO of (audio_frame, text_span) pairs.

    ``text_span`` is the reply text the frame belongs to, so the
    caller can map "frames heard" back to "words heard" when an
    interruption truncates the assistant turn.
    """

    def __init__(self) -> None:
        self._queue: deque = deque()
        self._heard: list = []  # (frame, text_span) already played
        self.flushed_total = 0

    def push(self, frame, text_span: str = "") -> None:
        self._queue.append((frame, text_span))

    def consume(self):
        """Take the next frame for playback. Returns None when empty."""
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._heard.append(item)
        return item

    def flush(self) -> int:
        """Drop everything not yet played. Returns frames dropped."""
        dropped = len(self._queue)
        self.flushed_total += dropped
        self._queue.clear()
        return dropped

    def heard_text(self) -> str:
        """Reply text corresponding to the frames actually played."""
        return "".join(span for _, span in self._heard).strip()

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def heard_frames(self) -> int:
        return len(self._heard)
