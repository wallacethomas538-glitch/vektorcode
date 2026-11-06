"""Local streaming STT for the voice agent, backed by sherpa-onnx.

Uses the streaming Zipformer transducer (Apache-2.0) running entirely
on local CPU — no audio leaves the machine, no API key, no per-minute
charge. Implements LiveKit Agents' ``stt.STT`` interface:

- ``stream()`` — a ``RecognizeStream`` that feeds 16 kHz audio frames
  into the sherpa online recognizer and emits interim + final
  transcripts. The session flushes the stream on turn commit
  (``manual_flush=True``), which finalizes the transcript.
- ``recognize()`` — one-shot transcription of a buffered utterance.

sherpa-onnx is imported lazily so this module (and the whole
``voice_agent`` package) imports cleanly on a machine without the
models installed; construction fails loudly with the missing paths
instead of at import time.

Model files (see voice_agent/models.py):
  sherpa-onnx-streaming-zipformer-en-2023-06-26 (Apache-2.0)
  encoder/decoder/joiner *-chunk-16-left-128.int8.onnx, tokens.txt,
  bpe.model
"""
from __future__ import annotations

import asyncio
import os
import uuid

import numpy as np

from livekit import rtc
from livekit.agents import stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NotGivenOr

MODEL_NAME = "sherpa-onnx-streaming-zipformer-en-2023-06-26"
PROVIDER = "sherpa-onnx (local)"
SAMPLE_RATE = 16000

_REQUIRED = {
    "tokens": "tokens.txt",
    "encoder": "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "decoder": "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "joiner": "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "bpe_vocab": "bpe.model",
}


def _resolve_model_files(model_dir: str) -> dict[str, str]:
    missing = []
    paths = {}
    for key, name in _REQUIRED.items():
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            missing.append(name)
        paths[key] = path
    if missing:
        raise RuntimeError(
            "sherpa-onnx STT model files missing in "
            f"{model_dir}: {', '.join(missing)}. Run: "
            "python -m voice_agent.models download-stt")
    return paths


class SherpaOnnxSTT(stt.STT):
    """Streaming local STT (sherpa-onnx Zipformer)."""

    def __init__(self, model_dir: str, num_threads: int = 2, language: str = "en"):
        super().__init__(capabilities=stt.STTCapabilities(
            streaming=True, interim_results=True, manual_flush=True))
        self._model_dir = model_dir
        self._num_threads = num_threads
        self._language = language
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "sherpa-onnx is not installed; install voice_requirements.txt"
            ) from exc
        self._sherpa = sherpa_onnx
        paths = _resolve_model_files(model_dir)
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=paths["tokens"],
            encoder=paths["encoder"],
            decoder=paths["decoder"],
            joiner=paths["joiner"],
            num_threads=num_threads,
            sample_rate=SAMPLE_RATE,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,  # VAD + turn detector own endpointing
            modeling_unit="bpe",
            bpe_vocab=paths["bpe_vocab"],
        )

    @property
    def model(self) -> str:
        return MODEL_NAME

    @property
    def provider(self) -> str:
        return PROVIDER

    def _new_stream(self):
        return self._recognizer.create_stream()

    @staticmethod
    def _frame_to_float32(frame: rtc.AudioFrame) -> np.ndarray:
        samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
        return samples / 32768.0

    async def _recognize_impl(
        self,
        buffer: stt.AudioBuffer,
        *,
        language: NotGivenOr[str] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        frames = buffer if isinstance(buffer, list) else [buffer]
        stream = self._new_stream()
        for frame in frames:
            samples = self._frame_to_float32(frame)
            if frame.sample_rate != SAMPLE_RATE:
                raise ValueError(
                    f"expected {SAMPLE_RATE} Hz frames, got {frame.sample_rate}")
            stream.accept_waveform(SAMPLE_RATE, samples)
        while self._recognizer.is_ready(stream):
            await asyncio.to_thread(self._recognizer.decode_stream, stream)
        text = self._recognizer.get_result(stream).strip()
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=str(uuid.uuid4()),
            alternatives=[stt.SpeechData(language=self._language, text=text)],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _SherpaRecognizeStream(
            stt=self, conn_options=conn_options, sample_rate=SAMPLE_RATE)


class _SherpaRecognizeStream(stt.RecognizeStream):
    """Feeds LiveKit audio frames into one sherpa online stream."""

    def __init__(self, *, stt: SherpaOnnxSTT, conn_options, sample_rate: int):
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._parent = stt

    async def _run(self) -> None:
        recognizer = self._parent._recognizer
        sherpa_stream = self._parent._new_stream()
        last_text = ""

        async def _emit(text: str, final: bool) -> None:
            event_type = (stt.SpeechEventType.FINAL_TRANSCRIPT if final
                          else stt.SpeechEventType.INTERIM_TRANSCRIPT)
            self._event_ch.send_nowait(stt.SpeechEvent(
                type=event_type,
                request_id=str(uuid.uuid4()),
                alternatives=[stt.SpeechData(
                    language=self._parent._language, text=text)],
            ))

        async for ev in self._input_ch:
            if isinstance(ev, rtc.AudioFrame):
                samples = self._parent._frame_to_float32(ev)
                sherpa_stream.accept_waveform(SAMPLE_RATE, samples)
                while recognizer.is_ready(sherpa_stream):
                    await asyncio.to_thread(recognizer.decode_stream, sherpa_stream)
                text = recognizer.get_result(sherpa_stream).strip()
                if text and text != last_text:
                    last_text = text
                    await _emit(text, final=False)
            elif isinstance(ev, stt.RecognizeStream._FlushSentinel):
                while recognizer.is_ready(sherpa_stream):
                    await asyncio.to_thread(recognizer.decode_stream, sherpa_stream)
                text = recognizer.get_result(sherpa_stream).strip()
                if text:
                    await _emit(text, final=True)
                break
