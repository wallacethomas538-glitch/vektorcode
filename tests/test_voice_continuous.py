"""Automated tests for the VektorCode continuous-voice experiment.

These are SIMULATED tests: scripted VAD/turn-detector timelines,
in-memory conversation state, and a stub brain over HTTP. They prove
the logic, wiring, and failure modes — they do NOT constitute a
physical Android microphone/speaker test. That test is pending
Wallace on his phone (see voice_agent/README.md, "Phone test").

Directive §5 coverage (each test maps to a required validation):
  1. continuous audio sessions        -> test_continuous_session_*
  2. long pauses during speech         -> test_long_pause_*
  3. end-of-turn detection             -> test_end_of_turn_*
  4. false interruptions/background    -> test_backchannel_*
  5. barge-in while TTS is playing     -> test_barge_in_*
  6. immediate audio cancellation      -> test_playback_flush_*
  7. conversation-history consistency  -> test_history_*
  8. reconnection                      -> test_reconnect_*
  9. duplicate message prevention      -> test_duplicate_*
 10. local-only operation, no paid APIs-> test_local_only_*

Plus: voice token/status endpoints (503 when unconfigured, no
secrets in any response) and the voice flag on /api/chat.
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_agent.turn import TurnTaker
from voice_agent.conversation import ConversationState
from voice_agent.playback import AudioPlaybackQueue
from voice_agent.config import VoiceConfig
from voice_agent.brain import VektorBrain, BrainError


# ----------------------------------------------------------------------
# Helpers: drive the TurnTaker with a scripted timeline.
# A timeline is a list of (time, action) where action is one of:
#   "speak"      VAD speech start
#   "silence"    VAD speech end
#   "complete"   turn detector says the turn is complete
#   "incomplete" turn detector says the turn is incomplete
#   "agent"      agent starts speaking
#   "agent_done" agent stops speaking
# ----------------------------------------------------------------------
def run_timeline(taker, timeline, end=60.0):
    events = []
    for t, action in timeline:
        # Timers fire BEFORE the new event: in the real agent tick()
        # runs continuously, so a commit whose silence window elapsed
        # must land before the next speech event is processed.
        events.extend(taker.tick(t))
        if action == "speak":
            taker.on_vad_speech_start(t)
        elif action == "silence":
            taker.on_vad_speech_end(t)
        elif action == "complete":
            taker.on_turn_detector(True, t)
        elif action == "incomplete":
            taker.on_turn_detector(False, t)
        elif action == "agent":
            taker.on_agent_speech_start(t)
        elif action == "agent_done":
            taker.on_agent_speech_end(t)
        events.extend(taker.tick(t))
    events.extend(taker.tick(end))
    return events


def kinds(events):
    return [e.kind for e in events]


# 1. Continuous audio sessions -----------------------------------------
def test_continuous_session_three_turns():
    """Three back-to-back turns commit exactly once each."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "speak"), (2.0, "silence"), (2.1, "complete"),
        (3.0, "speak"), (5.0, "silence"), (5.1, "complete"),
        (6.0, "speak"), (9.0, "silence"), (9.1, "complete"),
    ], end=12.0)
    assert kinds(events).count("turn_committed") == 3
    assert taker.commits == 3


def test_continuous_session_no_commit_while_speaking():
    """No commit fires mid-utterance, however long it runs."""
    taker = TurnTaker()
    events = run_timeline(taker, [(0.0, "speak"), (30.0, "complete")], end=30.0)
    assert kinds(events) == []


# 2. Long pauses ---------------------------------------------------------
def test_long_pause_mid_sentence_does_not_commit():
    """A 2.5 s mid-sentence pause with the turn detector saying
    'incomplete' must NOT commit (max is 3.0 s). The user resumes
    and the turn commits once, at the real end."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "speak"), (2.0, "silence"), (2.1, "incomplete"),
        (4.5, "speak"),            # resumes after a 2.5 s pause
        (7.0, "silence"), (7.1, "complete"),
    ], end=10.0)
    assert kinds(events).count("turn_committed") == 1


def test_pause_past_max_always_commits():
    """A pause past the 3.0 s max commits even when the turn detector
    says incomplete — the user is never stuck waiting."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "speak"), (2.0, "silence"), (2.1, "incomplete"),
    ], end=6.0)  # silence reaches 4.0 s > 3.0 s max
    assert kinds(events).count("turn_committed") == 1


# 3. End-of-turn detection ----------------------------------------------
def test_end_of_turn_commits_at_min_delay():
    """Turn detector 'complete' + 0.5 s silence commits promptly —
    not after the 3.0 s max."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "speak"), (2.0, "silence"), (2.1, "complete"),
    ], end=2.6)
    committed = [e for e in events if e.kind == "turn_committed"]
    assert len(committed) == 1
    assert committed[0].at <= 2.6  # committed at ~2.5 s, not 5.0 s


def test_end_of_turn_waits_for_min_delay():
    """'Complete' verdict alone, with no silence yet, does not commit."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "speak"), (2.0, "silence"), (2.05, "complete"),
    ], end=2.2)
    assert kinds(events) == []


# 4. False interruptions / background noise ------------------------------
def test_backchannel_does_not_interrupt():
    """'Uh-huh' (0.4 s) while the agent speaks is ignored."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "agent"), (1.0, "speak"), (1.4, "silence"), (2.0, "agent_done"),
    ], end=3.0)
    assert kinds(events).count("backchannel_ignored") == 1
    assert kinds(events).count("interruption") == 0
    assert taker.interruptions == 0


def test_short_noise_burst_does_not_interrupt():
    """A 0.2 s noise burst over the agent is ignored, not a barge-in."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "agent"), (1.0, "speak"), (1.2, "silence"),
    ], end=2.0)
    assert kinds(events).count("interruption") == 0


# 5. Barge-in while TTS is playing ---------------------------------------
def test_barge_in_long_speech_interrupts():
    """1.2 s of user speech over the agent is a real interruption."""
    taker = TurnTaker()
    events = run_timeline(taker, [
        (0.0, "agent"), (1.0, "speak"), (2.2, "silence"),
    ], end=3.0)
    assert kinds(events).count("interruption") == 1
    assert taker.interruptions == 1


def test_barge_in_fires_without_waiting_for_silence():
    """The interruption fires once the user has spoken over the agent
    for 0.6 s — it does not wait for the user to stop speaking."""
    taker = TurnTaker()
    taker.on_agent_speech_start(0.0)
    taker.on_vad_speech_start(1.0)
    events = taker.tick(1.6)  # 0.6 s of overlap, user still speaking
    assert kinds(events).count("interruption") == 1


# 6. Immediate audio cancellation ----------------------------------------
def test_playback_flush_drops_unplayed_frames():
    queue = AudioPlaybackQueue()
    for i in range(10):
        queue.push(f"frame-{i}", text_span="word ")
    assert len(queue) == 10
    # Three frames played (heard), then the user interrupts.
    queue.consume(); queue.consume(); queue.consume()
    dropped = queue.flush()
    assert dropped == 7
    assert len(queue) == 0
    assert queue.heard_frames == 3
    assert queue.heard_text() == "word word word"


def test_playback_flush_empty_queue_is_safe():
    queue = AudioPlaybackQueue()
    assert queue.flush() == 0


# 7. Conversation-history consistency ------------------------------------
def test_interruption_truncates_assistant_turn():
    """History keeps only what was actually heard."""
    state = ConversationState()
    state.add_user_turn("Tell me about the store.", turn_key="t1")
    aid = state.begin_assistant_turn()
    state.append_assistant_text(aid, "The store has forty-two products and ")
    state.append_assistant_text(aid, "ships worldwide from Hampton.")
    # User interrupts after hearing only the first part.
    assert state.truncate_assistant(aid, "The store has forty-two products and")
    messages = state.to_messages()
    assert messages[-1] == {"role": "assistant",
                            "content": "The store has forty-two products and"}
    assert "ships worldwide" not in json.dumps(messages)


def test_interruption_with_nothing_heard_drops_turn():
    state = ConversationState()
    state.add_user_turn("Hi.", turn_key="t1")
    aid = state.begin_assistant_turn()
    state.append_assistant_text(aid, "Hello there, how can I help?")
    assert state.truncate_assistant(aid, "")
    assert state.to_messages() == [{"role": "user", "content": "Hi."}]


def test_partial_assistant_turn_excluded_from_llm_context():
    state = ConversationState()
    state.add_user_turn("Hi.", turn_key="t1")
    aid = state.begin_assistant_turn()
    state.append_assistant_text(aid, "Half-spoken reply...")
    # Partial (unfinished, not truncated) turns must never reach the brain.
    assert state.to_messages() == [{"role": "user", "content": "Hi."}]
    state.finish_assistant_turn(aid)
    assert state.to_messages()[-1]["content"] == "Half-spoken reply..."


# 8. Reconnection ---------------------------------------------------------
def test_reconnect_restores_history_verbatim(tmp_path):
    state = ConversationState()
    state.add_user_turn("What is VektorCode?", turn_key="t1")
    aid = state.begin_assistant_turn()
    state.append_assistant_text(aid, "VektorCode is a cloud coding agent.")
    state.finish_assistant_turn(aid)
    state.add_user_turn("And the store?", turn_key="t2")
    aid2 = state.begin_assistant_turn()
    state.append_assistant_text(aid2, "The store is JoJoes.")
    state.finish_assistant_turn(aid2)
    path = str(tmp_path / "voice-state.json")
    state.save(path)

    restored = ConversationState.load(path)
    assert restored.to_messages() == state.to_messages()
    # last_turns(3) is the LAST three complete turns, not the first three.
    assert [t.content for t in restored.last_turns(3)] == [
        "VektorCode is a cloud coding agent.",
        "And the store?",
        "The store is JoJoes.",
    ]


def test_reconnect_drops_partial_assistant_turn(tmp_path):
    """A half-spoken assistant turn is never restored as history."""
    state = ConversationState()
    state.add_user_turn("Hi.", turn_key="t1")
    aid = state.begin_assistant_turn()
    state.append_assistant_text(aid, "Half-spoken...")
    path = str(tmp_path / "voice-state.json")
    state.save(path)
    restored = ConversationState.load(path)
    assert restored.to_messages() == [{"role": "user", "content": "Hi."}]


# 9. Duplicate message prevention -----------------------------------------
def test_duplicate_user_turn_stored_once():
    state = ConversationState()
    first = state.add_user_turn("Hello.", turn_key="turn-1")
    assert first is not None
    assert state.add_user_turn("Hello.", turn_key="turn-1") is None
    assert len(state.to_messages()) == 1


def test_same_text_different_key_stored_twice():
    """Same words as a genuinely new turn (new key) are stored."""
    state = ConversationState()
    assert state.add_user_turn("Hello.", turn_key="turn-1")
    assert state.add_user_turn("Hello.", turn_key="turn-2")
    assert len(state.to_messages()) == 2


# 10. Local-only operation, no paid APIs -----------------------------------
def test_config_defaults_are_local_only():
    cfg = VoiceConfig.from_env({})
    assert cfg.brain_url.startswith("http://127.0.0.1")
    assert cfg.min_endpointing_delay == 0.5
    assert cfg.max_endpointing_delay == 3.0
    assert cfg.backchannel_max_duration == 0.6


def test_config_missing_lists_names_not_values():
    cfg = VoiceConfig.from_env({
        "LIVEKIT_URL": "wss://example.livekit.cloud",
        "LIVEKIT_API_KEY": "APxxxx",
        "LIVEKIT_API_SECRET": "s3cr3t-value",
    })
    status = cfg.public_status()
    assert status["livekit_configured"] is True
    assert "s3cr3t-value" not in json.dumps(status)
    assert "APxxxx" not in json.dumps(status)
    assert status["stt_backend"] == "sherpa-onnx (local)"
    assert status["tts_backend"] == "sherpa-onnx (local)"
    assert status["turn_detector"] == "livekit v1-mini (local CPU)"
    assert status["llm_backend"] == "VektorCode brain (/api/chat)"


def test_voice_adapters_are_local_classes():
    """The wired STT/TTS/LLM are the local adapters, not a cloud
    provider plugin: their modules live in voice_agent, and neither
    references a paid inference endpoint."""
    import inspect
    from voice_agent import sherpa_stt, sherpa_tts, vektor_llm
    for module in (sherpa_stt, sherpa_tts, vektor_llm):
        source = inspect.getsource(module)
        assert "livekit.cloud" not in source
        assert "api.openai.com" not in source
        assert "deepgram" not in source.lower()
        assert "elevenlabs" not in source.lower()
    assert sherpa_stt.SherpaOnnxSTT.__module__.startswith("voice_agent")
    assert sherpa_tts.SherpaOnnxTTS.__module__.startswith("voice_agent")
    assert vektor_llm.VektorBrainLLM.__module__.startswith("voice_agent")


# Brain client (stub HTTP server) -----------------------------------------
class _StubBrainHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send({"ok": True, "service": "stub-brain"})
        else:
            self._send({"ok": False}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/chat":
            messages = data.get("messages", [])
            last_user = next((m["content"] for m in reversed(messages)
                              if m.get("role") == "user"), "")
            self._send({"ok": True, "message": f"stub reply to: {last_user}",
                        "model": "stub", "provider": "stub",
                        "session_id": data.get("session_id", "")})
        else:
            self._send({"ok": False}, 404)

    def _send(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def stub_brain():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubBrainHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_brain_client_round_trip(stub_brain):
    brain = VektorBrain(stub_brain, "voice-test-session")
    assert brain.health() is True
    reply = brain.ask([{"role": "user", "content": "hello"}])
    assert reply == "stub reply to: hello"


def test_brain_client_raises_on_unreachable():
    brain = VektorBrain("http://127.0.0.1:1", "voice-test-session", timeout=2)
    assert brain.health() is False
    with pytest.raises(BrainError):
        brain.ask([{"role": "user", "content": "hello"}])


def test_brain_history_fetch_is_best_effort(stub_brain):
    brain = VektorBrain(stub_brain, "voice-test-session")
    assert brain.fetch_history() == []


# Voice endpoints on the clone app -----------------------------------------
def _run_app(port_env):
    import app as app_module
    server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture()
def clone_app(monkeypatch):
    monkeypatch.setenv("VEKTORCODE_DB", ":memory:")
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    server = _run_app(None)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _get(url, path):
    import urllib.request
    with urllib.request.urlopen(url + path, timeout=10) as resp:
        return json.loads(resp.read())


def _post(url, path, payload):
    import urllib.request
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except Exception as exc:  # HTTPError carries the body
        return exc.code, json.loads(exc.read() or b"{}")


def test_voice_status_when_unconfigured(clone_app):
    status = _get(clone_app, "/api/voice/status")
    assert status["ok"] is True
    voice = status["voice"]
    assert voice["livekit_configured"] is False
    assert "LIVEKIT_URL" in voice["missing"]
    assert "LIVEKIT_API_KEY" in voice["missing"]
    assert "LIVEKIT_API_SECRET" in voice["missing"]


def test_voice_token_503_when_unconfigured(clone_app):
    code, body = _post(clone_app, "/api/voice/token", {})
    assert code == 503
    assert body["ok"] is False
    assert "LIVEKIT_URL" in body.get("missing", [])
    assert "token" not in body


def test_voice_status_never_leaks_secrets(clone_app, monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "AP-SECRET-KEY-123")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "SUPER-SECRET-456")
    status = _get(clone_app, "/api/voice/status")
    blob = json.dumps(status)
    assert "AP-SECRET-KEY-123" not in blob
    assert "SUPER-SECRET-456" not in blob
    assert status["voice"]["livekit_configured"] is True
