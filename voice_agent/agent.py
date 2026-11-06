"""LiveKit Agents worker for VektorCode continuous voice.

The worker joins a LiveKit room as a participant and runs the full
voice loop:

  phone mic (WebRTC) -> Silero VAD -> TurnDetector v1-mini (local CPU)
      -> sherpa-onnx streaming STT (local) -> VektorCode brain
      (/api/chat, the existing chat logic) -> sherpa-onnx Kokoro TTS
      (local) -> phone speaker (WebRTC)

Barge-in is the framework's: VAD firing on the user's track cancels
in-flight TTS and rolls back the interrupted turn, with the adaptive
interruption model deciding backchannel vs real interruption. The
directive's endpointing bounds (0.5–3.0 s) are set on the session.

Run (isolated clone only; never on production):
  LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=devkey \
  LIVEKIT_API_SECRET=secret \
  VOICE_STT_MODEL_DIR=voice_agent/models/stt-zipformer-en \
  VOICE_TTS_MODEL_DIR=voice_agent/models/tts-kokoro-en \
  python -m voice_agent.agent start

Modes: ``start`` (worker, waits for dispatch), ``dev`` (same, with
dev logging), ``check`` (validate config + load STT/TTS models, then
exit — no LiveKit connection, no audio).

No secrets are read from files; everything is env vars. The worker
never calls a paid API: STT/TTS/turn detection are local, and the
brain is VektorCode's own free-tier chat.
"""
from __future__ import annotations

import logging
import sys

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.agents.inference import TurnDetector
from livekit.plugins import silero

from .brain import VektorBrain
from .config import VoiceConfig
from .sherpa_stt import SherpaOnnxSTT
from .sherpa_tts import SherpaOnnxTTS
from .vektor_llm import VektorBrainLLM

AGENT_NAME = "vektorcode-voice"
LOGGER = logging.getLogger("vektorcode.voice")


class VoiceAgent(Agent):
    """The voice persona. Instructions stay short: the brain owns the
    real system prompt (SOUL.md + memory); this only sets the spoken
    style for the LiveKit session itself."""

    def __init__(self) -> None:
        super().__init__(instructions=(
            "You are Vektor, the VektorCode voice assistant. "
            "Answer briefly and plainly, as if speaking aloud: "
            "short sentences, no markdown, no lists unless asked."))


async def entrypoint(ctx: JobContext) -> None:
    cfg = VoiceConfig.from_env()
    if not cfg.is_livekit_configured():
        raise RuntimeError(
            "LiveKit is not configured; set LIVEKIT_URL, LIVEKIT_API_KEY, "
            "LIVEKIT_API_SECRET (see voice_agent/README.md). "
            f"Missing: {', '.join(cfg.missing())}")
    # The room name doubles as the brain session id, so a voice turn
    # and a typed turn share one session and one history.
    session_id = f"voice-{ctx.room.name}"
    brain = VektorBrain(cfg.brain_url, session_id)
    if not brain.health():
        LOGGER.warning(
            "brain /health did not answer at %s — voice turns will fail "
            "until the VektorCode app is running", cfg.brain_url)

    stt = SherpaOnnxSTT(cfg.stt_model_dir, language=cfg.language)
    tts = SherpaOnnxTTS(cfg.tts_model_dir)
    llm = VektorBrainLLM(brain)
    vad = silero.VAD.load(min_silence_duration=0.55)
    turn_detector = TurnDetector(version="v1-mini", local_fallback=True)

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        turn_detection=turn_detector,
        min_endpointing_delay=cfg.min_endpointing_delay,
        max_endpointing_delay=cfg.max_endpointing_delay,
        allow_interruptions=True,
        min_interruption_duration=0.5,
        min_interruption_words=0,
    )
    await ctx.connect()
    await session.start(agent=VoiceAgent(), room=ctx.room)
    LOGGER.info("voice session started in room %s (brain session %s)",
                ctx.room.name, session_id)


def check() -> int:
    """Validate config and load the local models without connecting."""
    cfg = VoiceConfig.from_env()
    print("config:", cfg.public_status())
    missing = cfg.missing()
    if missing:
        print("MISSING (set these env vars):", ", ".join(missing))
        return 2
    print("loading STT ...")
    SherpaOnnxSTT(cfg.stt_model_dir, language=cfg.language)
    print("STT ok")
    print("loading TTS ...")
    SherpaOnnxTTS(cfg.tts_model_dir)
    print("TTS ok")
    print("check passed: config + local models load. LiveKit not contacted.")
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "start"
    if mode == "check":
        return check()
    if mode not in ("start", "dev"):
        print(__doc__)
        return 2
    cfg = VoiceConfig.from_env()
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=AGENT_NAME,
        ws_url=cfg.livekit_url or None,
        api_key=cfg.livekit_api_key or None,
        api_secret=cfg.livekit_api_secret or None,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
