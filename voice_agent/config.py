"""Configuration for the VektorCode continuous-voice experiment.

Everything comes from environment variables. Secrets are never logged,
never written to files, and never returned by any status endpoint —
``public_status()`` exposes booleans only.

Free-tier rule: this module references no paid provider. STT/TTS are
local (sherpa-onnx); the LLM is VektorCode's own brain (``/api/chat``);
the media server is either LiveKit Cloud's free Build tier or a
self-hosted LiveKit server. See voice_agent/README.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _clean(value: str | None) -> str:
    return (value or "").strip()


@dataclass
class VoiceConfig:
    # LiveKit server (Cloud free Build tier, or a self-hosted server).
    # Unset => voice is "not configured"; nothing connects anywhere.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # VektorCode brain: the existing /api/chat. Local by default.
    brain_url: str = "http://127.0.0.1:8765"

    # Room the phone client and the agent join. A fresh room per call
    # keeps one voice session isolated from any other.
    room_name: str = "vektorcode-voice"

    # Local sherpa-onnx model directories (see voice_agent/models.py).
    stt_model_dir: str = ""
    tts_model_dir: str = ""

    # Dynamic endpointing bounds (seconds), per the directive:
    # never commit before 0.5 s of silence, always commit by 3.0 s.
    min_endpointing_delay: float = 0.5
    max_endpointing_delay: float = 3.0

    # Speech shorter than this while the agent is speaking counts as a
    # backchannel ("uh-huh", "yeah") and must NOT interrupt it.
    backchannel_max_duration: float = 0.6

    language: str = "en"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "VoiceConfig":
        env = os.environ if env is None else env
        cfg = cls(
            livekit_url=_clean(env.get("LIVEKIT_URL")),
            livekit_api_key=_clean(env.get("LIVEKIT_API_KEY")),
            livekit_api_secret=_clean(env.get("LIVEKIT_API_SECRET")),
            brain_url=_clean(env.get("VEKTORCODE_BRAIN_URL")) or "http://127.0.0.1:8765",
            room_name=_clean(env.get("VOICE_ROOM")) or "vektorcode-voice",
            stt_model_dir=_clean(env.get("VOICE_STT_MODEL_DIR")),
            tts_model_dir=_clean(env.get("VOICE_TTS_MODEL_DIR")),
            language=_clean(env.get("VOICE_LANGUAGE")) or "en",
        )
        try:
            cfg.min_endpointing_delay = float(env.get("VOICE_MIN_ENDPOINTING_DELAY", "0.5"))
        except ValueError:
            cfg.min_endpointing_delay = 0.5
        try:
            cfg.max_endpointing_delay = float(env.get("VOICE_MAX_ENDPOINTING_DELAY", "3.0"))
        except ValueError:
            cfg.max_endpointing_delay = 3.0
        # Clamp to the directive's bounds: 0.5 s floor, 3.0 s ceiling.
        cfg.min_endpointing_delay = min(max(cfg.min_endpointing_delay, 0.1), 2.0)
        cfg.max_endpointing_delay = min(max(cfg.max_endpointing_delay, cfg.min_endpointing_delay), 5.0)
        return cfg

    def is_livekit_configured(self) -> bool:
        """True only when all three LiveKit values are present."""
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)

    def missing(self) -> list[str]:
        """Names of unset required items (names only — never values)."""
        out = []
        if not self.livekit_url:
            out.append("LIVEKIT_URL")
        if not self.livekit_api_key:
            out.append("LIVEKIT_API_KEY")
        if not self.livekit_api_secret:
            out.append("LIVEKIT_API_SECRET")
        if not self.stt_model_dir:
            out.append("VOICE_STT_MODEL_DIR")
        if not self.tts_model_dir:
            out.append("VOICE_TTS_MODEL_DIR")
        return out

    def public_status(self) -> dict:
        """Safe for /api/voice/status: booleans and bounds, no secrets."""
        return {
            "livekit_configured": self.is_livekit_configured(),
            "livekit_url_set": bool(self.livekit_url),
            "stt_model_dir_set": bool(self.stt_model_dir),
            "tts_model_dir_set": bool(self.tts_model_dir),
            "stt_backend": "sherpa-onnx (local)",
            "tts_backend": "sherpa-onnx (local)",
            "turn_detector": "livekit v1-mini (local CPU)",
            "vad": "silero (local)",
            "llm_backend": "VektorCode brain (/api/chat)",
            "min_endpointing_delay": self.min_endpointing_delay,
            "max_endpointing_delay": self.max_endpointing_delay,
            "backchannel_max_duration": self.backchannel_max_duration,
            "language": self.language,
            "missing": self.missing(),
        }
