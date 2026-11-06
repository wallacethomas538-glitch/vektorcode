"""VektorCode continuous voice (isolated experiment).

Modules:
  config       env config, no secrets in output
  turn         framework-agnostic turn-taking state machine
  conversation conversation state: dedupe, interruption rollback, restore
  playback     cancellable audio queue (barge-in = flush)
  brain        client for the existing VektorCode /api/chat brain
  sherpa_stt   local streaming STT (sherpa-onnx Zipformer)
  sherpa_tts   local neural TTS (sherpa-onnx Kokoro-82M)
  vektor_llm   LiveKit LLM adapter that calls the brain
  models       model downloads (open-licensed, local CPU, $0)
  agent        LiveKit Agents worker (entrypoint)

Free-tier only. No paid APIs. See README.md.
"""

__version__ = "0.1.0-experiment"
