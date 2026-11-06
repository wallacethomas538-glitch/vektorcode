"""Local neural TTS for the voice agent, backed by sherpa-onnx.

Uses Kokoro-82M (Apache-2.0) running entirely on local CPU — no text
leaves the machine, no API key, no per-minute charge. Implements
LiveKit Agents' ``tts.TTS`` interface:

- ``synthesize(text)`` — sentence-by-sentence generation, frames
  pushed as they are ready (streaming playback: the first sentence
  plays while later ones are still synthesizing).
- ``stream()`` — buffers LLM tokens, synthesizes each completed
  sentence as it arrives.

sherpa-onnx is imported lazily; construction fails loudly with the
missing model paths instead of at import time.

Model files (see voice_agent/models.py):
  kokoro-int8-multi-lang-v1_1 (Apache-2.0; LICENSE file in the repo)
  model.int8.onnx, voices.bin, tokens.txt, lexicon-us-en.txt,
  espeak-ng-data/, dict/
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid

import numpy as np

from livekit import rtc
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NotGivenOr

MODEL_NAME = "kokoro-82m-int8 (sherpa-onnx)"
PROVIDER = "sherpa-onnx (local)"
SAMPLE_RATE = 24000  # Kokoro native output rate
FRAME_SAMPLES = 480  # 20 ms frames at 24 kHz

_REQUIRED_FILES = ["model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt"]
_REQUIRED_DIRS = ["espeak-ng-data", "dict"]

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _resolve_model_files(model_dir: str) -> dict[str, str]:
    missing = []
    paths = {}
    for name in _REQUIRED_FILES:
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            missing.append(name)
        paths[name] = path
    for name in _REQUIRED_DIRS:
        path = os.path.join(model_dir, name)
        if not os.path.isdir(path):
            missing.append(name + "/")
        paths[name] = path
    if missing:
        raise RuntimeError(
            "sherpa-onnx TTS model files missing in "
            f"{model_dir}: {', '.join(missing)}. Run: "
            "python -m voice_agent.models download-tts")
    return paths


def split_sentences(text: str) -> list[str]:
    """Split reply text into speakable sentences (kept public for tests)."""
    parts = [p.strip() for p in _SENTENCE_END.split((text or "").strip())]
    return [p for p in parts if p]


class SherpaOnnxTTS(tts.TTS):
    """Local neural TTS (sherpa-onnx Kokoro-82M)."""

    def __init__(self, model_dir: str, num_threads: int = 2, voice_id: int = 0,
                 speed: float = 1.0):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=SAMPLE_RATE, num_channels=1)
        self._model_dir = model_dir
        self._voice_id = voice_id
        self._speed = speed
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "sherpa-onnx is not installed; install voice_requirements.txt"
            ) from exc
        self._sherpa = sherpa_onnx
        paths = _resolve_model_files(model_dir)
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=paths["model.int8.onnx"],
                    voices=paths["voices.bin"],
                    lexicon=paths["lexicon-us-en.txt"],
                    data_dir=paths["espeak-ng-data"],
                    dict_dir=paths["dict"],
                ),
                num_threads=num_threads,
                debug=False,
                provider="cpu",
            ),
            rule_fsts="",
            max_num_sentences=1,
        )
        self._engine = sherpa_onnx.OfflineTts(config)
        self._output_sample_rate = int(self._engine.sample_rate)

    @property
    def model(self) -> str:
        return MODEL_NAME

    @property
    def provider(self) -> str:
        return PROVIDER

    def _synthesize_sentence(self, sentence: str) -> np.ndarray:
        """Blocking synthesis of one sentence -> float32 samples."""
        audio = self._engine.generate(sentence, sid=self._voice_id, speed=self._speed)
        return np.asarray(audio.samples, dtype=np.float32)

    @staticmethod
    def _frames_for(samples: np.ndarray, sample_rate: int):
        """Split float32 samples into 20 ms int16 AudioFrames."""
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
        for start in range(0, len(pcm), FRAME_SAMPLES):
            chunk = pcm[start:start + FRAME_SAMPLES]
            if len(chunk) == 0:
                continue
            frame = rtc.AudioFrame.create(sample_rate, 1, len(chunk))
            frame.data[:] = chunk
            yield frame

    async def _push_sentence(self, emitter, sentence: str) -> None:
        samples = await asyncio.to_thread(self._synthesize_sentence, sentence)
        emitter.start_segment(segment_id=str(uuid.uuid4()))
        for frame in self._frames_for(samples, self._output_sample_rate):
            emitter.push_frame(frame)
        emitter.end_segment()

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return _SherpaChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.SynthesizeStream:
        return _SherpaSynthesizeStream(tts=self, conn_options=conn_options)


class _SherpaChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        parent: SherpaOnnxTTS = self._tts
        for sentence in split_sentences(self._input_text):
            await parent._push_sentence(output_emitter, sentence)


class _SherpaSynthesizeStream(tts.SynthesizeStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        parent: SherpaOnnxTTS = self._tts
        buffer = ""
        async for token in self._input_ch:
            if isinstance(token, str):
                buffer += token
                sentences = split_sentences(buffer)
                # Synthesize every complete sentence; keep the last
                # (possibly incomplete) fragment buffered.
                if len(sentences) > 1 or (sentences and buffer.rstrip().endswith((".", "!", "?"))):
                    complete, buffer = sentences[:-1], sentences[-1] if sentences else ""
                    for sentence in complete:
                        await parent._push_sentence(output_emitter, sentence)
            elif isinstance(token, tts.SynthesizeStream._FlushSentinel):
                if buffer.strip():
                    await parent._push_sentence(output_emitter, buffer.strip())
                break
