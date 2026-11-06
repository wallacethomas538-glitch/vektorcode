"""Turn-taking state machine for continuous voice conversation.

Framework-agnostic pure logic: no LiveKit, no network, no models.
It consumes timed events (VAD speech start/end, audio turn-detector
verdicts, agent speech start/end) and emits decisions:

- ``turn_committed`` — the user finished a turn; send the transcript
  to the brain. Never fires mid-sentence: it needs either the audio
  turn detector to call the turn complete (after ``min_endpointing``
  of silence) or ``max_endpointing`` of silence, whichever comes first.
- ``interruption`` — the user is really interrupting the agent while
  it speaks; stop playback immediately and take the new utterance.
- ``backchannel_ignored`` — short speech ("uh-huh", "yeah") while the
  agent speaks; the agent keeps talking.

Dynamic endpointing (directive §1): the commit silence window is
bounded between ``min_endpointing`` (default 0.5 s) and
``max_endpointing`` (default 3.0 s). A long mid-sentence pause does
not commit while the turn detector says the turn is incomplete; a
pause past the max always commits, so the user is never stuck.

All times are seconds on one monotonic clock supplied by the caller
(the agent passes ``time.monotonic()``; tests pass scripted times).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnEvent:
    kind: str  # "turn_committed" | "interruption" | "backchannel_ignored"
    at: float
    detail: str = ""


class TurnTaker:
    """One conversation's turn-taking state.

    States: idle -> user_speaking -> endpointing -> (commit) -> idle,
    and agent_speaking with interruption candidates.
    """

    def __init__(
        self,
        min_endpointing: float = 0.5,
        max_endpointing: float = 3.0,
        backchannel_max_duration: float = 0.6,
    ):
        self.min_endpointing = min_endpointing
        self.max_endpointing = max_endpointing
        self.backchannel_max_duration = backchannel_max_duration
        self.state = "idle"
        self._speech_start: float | None = None
        self._silence_start: float | None = None
        self._turn_complete = False  # latest audio turn-detector verdict
        self._interrupt_candidate_start: float | None = None
        self._events: list[TurnEvent] = []
        self.commits = 0
        self.interruptions = 0
        self.backchannels_ignored = 0

    # -- inputs ---------------------------------------------------------
    def on_vad_speech_start(self, t: float) -> None:
        """VAD: user speech began."""
        if self.state == "agent_speaking":
            # Possible interruption: start the candidate clock. The
            # decision (backchannel vs real interruption) is made on
            # duration, in tick() / on_vad_speech_end().
            if self._interrupt_candidate_start is None:
                self._interrupt_candidate_start = t
            return
        if self.state == "endpointing":
            # User resumed mid-pause: the turn is not over after all.
            self.state = "user_speaking"
            self._silence_start = None
            self._turn_complete = False
            return
        if self.state in ("idle", "user_speaking"):
            if self.state == "idle":
                self.state = "user_speaking"
                self._speech_start = t
            return

    def on_vad_speech_end(self, t: float) -> None:
        """VAD: user speech stopped (silence began)."""
        if self.state == "agent_speaking":
            self._resolve_interrupt_candidate(t, ended=True)
            return
        if self.state == "user_speaking":
            self.state = "endpointing"
            self._silence_start = t

    def on_turn_detector(self, complete: bool, t: float) -> None:
        """Audio turn detector verdict for the current utterance."""
        self._turn_complete = bool(complete)
        if self.state == "endpointing" and complete:
            self._maybe_commit(t)

    def on_agent_speech_start(self, t: float) -> None:
        """The agent began speaking its reply."""
        self.state = "agent_speaking"
        self._interrupt_candidate_start = None

    def on_agent_speech_end(self, t: float) -> None:
        """The agent finished (or was cancelled from) its reply."""
        if self.state == "agent_speaking":
            self.state = "idle"
        self._interrupt_candidate_start = None

    def tick(self, t: float) -> list[TurnEvent]:
        """Advance timers to time ``t``; returns new events."""
        if self.state == "endpointing":
            self._maybe_commit(t)
        elif self.state == "agent_speaking" and self._interrupt_candidate_start is not None:
            # User has been speaking over the agent for a while: this
            # is a real interruption, not a backchannel. Cancel now —
            # do not wait for VAD speech end.
            if t - self._interrupt_candidate_start >= self.backchannel_max_duration:
                self._interrupt(t, "user speech over agent for "
                                f"{t - self._interrupt_candidate_start:.2f}s")
        return self.drain_events()

    # -- internals ------------------------------------------------------
    def _maybe_commit(self, t: float) -> None:
        if self.state != "endpointing" or self._silence_start is None:
            return
        silence = t - self._silence_start
        if self._turn_complete and silence >= self.min_endpointing:
            self._commit(t, "turn detector complete + "
                         f"{silence:.2f}s silence")
        elif silence >= self.max_endpointing:
            self._commit(t, "max endpointing reached "
                         f"({silence:.2f}s silence)")

    def _commit(self, t: float, detail: str) -> None:
        self.state = "idle"
        self._silence_start = None
        self._turn_complete = False
        self.commits += 1
        self._events.append(TurnEvent("turn_committed", t, detail))

    def _resolve_interrupt_candidate(self, t: float, ended: bool) -> None:
        if self._interrupt_candidate_start is None:
            return
        duration = t - self._interrupt_candidate_start
        if duration >= self.backchannel_max_duration:
            self._interrupt(t, f"user speech {duration:.2f}s over agent")
        else:
            self.backchannels_ignored += 1
            self._events.append(TurnEvent(
                "backchannel_ignored", t,
                f"short speech {duration:.2f}s over agent — kept speaking"))
        self._interrupt_candidate_start = None

    def _interrupt(self, t: float, detail: str) -> None:
        self.interruptions += 1
        self.state = "user_speaking"
        self._speech_start = t
        self._silence_start = None
        self._turn_complete = False
        self._interrupt_candidate_start = None
        self._events.append(TurnEvent("interruption", t, detail))

    def drain_events(self) -> list[TurnEvent]:
        events, self._events = self._events, []
        return events
