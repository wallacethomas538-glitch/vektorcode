# VektorCode Continuous Voice — isolated experiment

**Status: research + isolated-clone implementation only. Nothing here
touches production.** This package lives in the `vektorcode-voice-clone`
directory on branch `voice/livekit-continuous`. It is never pushed,
never merged, never deployed to Render, and it never changes
production environment variables. The production VektorCode app is
untouched; the only changes to the shared code are in this clone
(`app.py`: a `voice` flag on `/api/chat` + two new endpoints;
`static/index.html`: one link to the experiment page).

The goal, per Wallace's directive: natural, continuous, hands-free
voice conversation — the microphone stays open, pauses don't end a
turn early, the assistant can be interrupted mid-sentence, and the
conversation history survives interruptions and reconnects. It is
explicitly NOT a `continuous = true` toggle on the old browser
SpeechRecognition code.

## Architecture

```
Android phone (Chrome)                    This machine / VM
  mic + speaker                                (voice worker)
       |                                            |
       +-- WebRTC audio --------------------------> LiveKit Cloud (Build tier, $0)
       |   (room: vektorkode-voice)                 |  media server only:
       |                                            |  signalling + WebRTC relay
       |                                            v
       |                                   LiveKit Agents worker
       |                                     (voice_agent/agent.py)
       |                                       |
       |   Silero VAD (local) -> TurnDetector v1-mini (local CPU)
       |                                       |
       |   sherpa-onnx streaming STT (local) -> transcript
       |                                       |
       |   VektorCode brain: POST /api/chat (existing chat logic,
       |   same session id, same free-tier provider chain)
       |                                       |
       |   sherpa-onnx Kokoro TTS (local) -> spoken reply
       |                                            |
       +-- WebRTC audio <--------------------------+
```

Key design points:

- **The brain is VektorCode's own `/api/chat`.** The voice agent
  does not pick models and does not reimplement chat. A voice turn
  and a typed turn share one session id (`voice-<room>`) and one
  history, so the text UI sees voice turns and vice versa.
- **STT/TTS/turn detection are local.** sherpa-onnx (Zipformer STT,
  Kokoro-82M TTS) runs on local CPU; LiveKit TurnDetector `v1-mini`
  runs on local CPU. No audio or text goes to a paid inference API.
  LiveKit Cloud is used only as the media server (WebRTC transport).
- **Dynamic endpointing, 0.5–3.0 s** (directive §1): a turn commits
  after 0.5 s of silence once the turn detector says the turn is
  complete, and always commits by 3.0 s of silence, so Wallace is
  never stuck waiting. A mid-sentence pause with the turn detector
  saying "incomplete" does not commit.
- **Barge-in:** short speech over the agent ("uh-huh", under 0.6 s)
  is a backchannel and is ignored; longer speech is a real
  interruption — playback stops immediately (the playback queue is
  flushed) and the new utterance is taken.
- **History safety:** the assistant turn is truncated to the text
  actually heard on interruption; partial (unfinished) turns never
  reach the brain and are never restored after a reconnect. A
  retried user turn (same `turn_key`) is stored exactly once.

## Files

| File | What it does |
| --- | --- |
| `voice_agent/config.py` | Env config; `public_status()` returns booleans only — secrets are never logged, written, or returned |
| `voice_agent/turn.py` | Framework-agnostic turn-taking state machine (commit / interrupt / backchannel). Pure logic, no network, no models |
| `voice_agent/conversation.py` | Conversation state: dedupe by `turn_key`, interruption rollback, save/load with partial turns dropped |
| `voice_agent/playback.py` | Cancellable audio queue; `flush()` drops unplayed frames on barge-in |
| `voice_agent/brain.py` | Stdlib-only client for the existing `/api/chat` (+ `/health`, best-effort history) |
| `voice_agent/vektor_llm.py` | LiveKit LLM adapter that calls the brain (`voice=True` = short spoken style, not persisted) |
| `voice_agent/sherpa_stt.py` | Local streaming STT (sherpa-onnx Zipformer), LiveKit `stt.STT` interface |
| `voice_agent/sherpa_tts.py` | Local neural TTS (sherpa-onnx Kokoro-82M), sentence-by-sentence streaming |
| `voice_agent/models.py` | Model download/check commands (open-licensed, local CPU, $0) |
| `voice_agent/agent.py` | LiveKit Agents worker: `start` / `dev` / `check` modes |
| `voice_requirements.txt` | Pinned voice deps (NOT in `requirements.txt`; the web app stays stdlib-only) |
| `tests/test_voice_continuous.py` | 28 simulated tests mapping all 10 directive §5 validations |
| `static/voice-live.html` + `static/voice-live.js` | Phone page: Start/Stop, live state, transcript log. Uses LiveKit JS client from CDN |
| `app.py` (clone only) | `voice` flag on `/api/chat`; `GET /api/voice/status`; `POST /api/voice/token` (mints a room token, dispatches the worker) |

## Dependencies and licenses

Pinned in `voice_requirements.txt` (verified from installed package
metadata, 2026-11-06):

- `livekit-agents==1.8.11` — Apache-2.0
- `livekit-plugins-silero==1.8.11` — Apache-2.0 (Silero VAD model: MIT)
- `sherpa-onnx==1.13.8` — Apache-2.0
- `numpy==2.5.4` — BSD-3-Clause
- `pytest==8.3.4` — test-only
- `livekit-client@2.15.0` (JS, CDN in `voice-live.html`) — Apache-2.0

Models (downloaded by `voice_agent/models.py`, not shipped in the repo):

- STT: `sherpa-onnx-streaming-zipformer-en-2023-06-26` — Apache-2.0, ~75 MB
- TTS: `kokoro-int8-multi-lang-v1_1` (Kokoro-82M int8) — Apache-2.0, ~110 MB
- Turn detector: LiveKit `v1-mini` — LiveKit Model License
  (Agents-only), runs locally, no cloud call. Downloaded on first use
  by the LiveKit local-inference package.

No component here bills per minute or needs a payment method.

## Zero-cost operating path (verified 2026-11-06)

- **LiveKit Cloud, Build tier, $0/month, no credit card required**
  (confirmed on livekit.io/pricing and in LiveKit's own
  quotas-and-limits docs): 1,000 agent-session minutes/month,
  5,000 WebRTC participant minutes/month, 50 GB downstream data
  transfer/month, $2.50 LiveKit Inference credits/month, up to 5
  concurrent agent sessions. **Hard cap, no overage** — on the Build
  plan the allowance is a cap: when it is used up, new requests fail
  instead of charging. This experiment does not use LiveKit Inference
  at all (STT/LLM/TTS are all local or VektorCode's own brain), so
  the $2.50 inference credit is not the binding limit here — the
  1,000 agent minutes are.
- **Self-hosted LiveKit on Render: impossible.** A LiveKit media
  server needs inbound UDP 50000–60000 for media plus UDP 3478 for
  TURN (two independent sources citing LiveKit's own Helm/config;
  TCP 7880/7881 covers signalling and a TCP fallback only). Render
  exposes exactly one public TCP/HTTPS port per web service and, per
  Render's own staff, "only supports connections with the public
  Internet over HTTPS" with UDP "not currently on our roadmap".
  Do not plan to host the media server on Render on any plan.
- **Where a self-hosted LiveKit could run instead:** any host with a
  public IP and UDP control — e.g. Oracle Always Free (Ampere ARM
  instance) — but that needs a credit card at signup, which Wallace
  does not want to give, and it is a real infrastructure job, not
  part of this experiment. This VM has no public IP (Tailscale only),
  so it can host a LiveKit server for tailnet-only tests at most;
  that path is untested. The phone itself cannot run the LiveKit
  server (Go binary, no Android build). **For this experiment the
  verified $0 path is LiveKit Cloud Build.**
- **The brain stays free-tier-only:** voice turns go through the
  same `/api/chat` provider chain as text (Groq → OpenRouter → Meta
  → … → Qwen → Gemini → Mistral → HuggingFace → local Ollama), all
  free-tier keys, no card on file.

## Run it locally (isolated clone only)

```bash
cd ~/workspace/vektorcode-voice-clone
git checkout voice/livekit-continuous

# 1. Venv + deps (voice worker only; the web app stays stdlib-only)
python3 -m venv .venv-voice
.venv-voice/bin/pip install -r voice_requirements.txt

# 2. Models (~185 MB total, open-licensed, one-time download)
.venv-voice/bin/python -m voice_agent.models download-all
.venv-voice/bin/python -m voice_agent.models check   # expect "missing: none"

# 3. LiveKit Cloud free account (no card): https://cloud.livekit.io
#    Create a project, copy the URL + API key + secret into env vars.
#    Nothing is written to any file; env vars only.

# 4. Start the clone web app (binds 0.0.0.0:8765 by default)
VEKTORCODE_DB=:memory: .venv-voice/bin/python app.py

# 5. In a second terminal, validate config + models (no LiveKit call)
LIVEKIT_URL=wss://<your-project>.livekit.cloud \
LIVEKIT_API_KEY=<key> LIVEKIT_API_SECRET=<secret> \
VOICE_STT_MODEL_DIR=voice_agent/models/stt-zipformer-en \
VOICE_TTS_MODEL_DIR=voice_agent/models/tts-kokoro-en \
.venv-voice/bin/python -m voice_agent.agent check

# 6. Start the voice worker (waits for a room dispatch)
LIVEKIT_URL=wss://<your-project>.livekit.cloud \
LIVEKIT_API_KEY=<key> LIVEKIT_API_SECRET=<secret> \
VOICE_STT_MODEL_DIR=voice_agent/models/stt-zipformer-en \
VOICE_TTS_MODEL_DIR=voice_agent/models/tts-kokoro-en \
.venv-voice/bin/python -m voice_agent.agent start
```

Then open the phone page (see "Phone test" below) and tap
**Start voice conversation**. The app mints a room token and
dispatches the worker into the room.

## Run the tests

```bash
cd ~/workspace/vektorcode-voice-clone
.venv-voice/bin/python -m pytest tests/ test_app.py -q
```

Measured 2026-11-06 on this VM: **134 passed, 0 failed**
(106 pre-existing suite + 28 new voice tests in
`tests/test_voice_continuous.py`). The voice tests are SIMULATED —
scripted VAD/turn-detector timelines, in-memory conversation state,
and a stub brain over HTTP. They prove the logic, wiring, and
failure modes. They do NOT constitute a physical Android
microphone/speaker test.

What the tests do not cover (honest gaps):

- The sherpa model files were NOT downloaded on this VM
  (`voice_agent/models/stt-zipformer-en` is empty; no TTS dir), so
  `agent check` with full config fails loudly with the missing-file
  list — that error path is verified, but real STT/TTS loading,
  TurnDetector `v1-mini` model download, and `AgentSession`
  construction were not run here.
- No end-to-end voice call was run (no LiveKit account, no models).
- No physical Android test was run (see below).

## Phone test (for Wallace — PENDING, not yet run)

This is the test that actually proves it. It needs: this VM running
(the clone app + the voice worker), your phone on the same Tailscale
network, and a LiveKit Cloud free account (no card). Do it in Chrome
on your Android phone:

1. On the VM, start the clone app and the voice worker (commands
   above). Keep both terminals open.
2. On your phone, in Chrome, open
   `http://100.88.198.15:8765/static/voice-live.html`
   (the VM's Tailscale address; the clone app binds 0.0.0.0).
   If that address has changed, ask Muse for the current one.
3. Tap **Start voice conversation**. Allow microphone access when
   Chrome asks. The state box should go green ("Listening").
4. Say: "Hello, what can you do?" — the agent should answer out
   loud without you tapping anything else.
5. Mid-sentence pause: say "Tell me about the store… [wait 2–3
   seconds] …how many products does it have?" — it must NOT answer
   after the pause; it must wait for the real end.
6. Interrupt: while the agent is speaking, talk over it ("Stop —
   tell me about VektorCode instead"). Its audio must stop
   immediately and it must take the new question.
7. Backchannel: while it speaks, say just "uh-huh" — it should keep
   talking, not stop.
8. Keep talking back and forth for 3–4 turns, then tap **Stop**.
9. Reconnect: tap Start again — the conversation should continue
   where it left off (history kept), not start over.
10. Tell Muse what happened for each step — what worked, what cut
    you off, how long the pauses felt. That report is the test result.

If the page says "Live voice is not configured" or the worker never
joins the room, stop and tell Muse — do not keep tapping.

## Known limitations

- No end-to-end voice call has been run anywhere yet. The worker,
  the models, and the LiveKit room join are implemented but
  unproven together.
- Model files are not in this clone (empty `models/` dir). The first
  real run downloads ~185 MB of models plus the v1-mini turn-detector
  model on first use.
- The voice worker's RAM/CPU needs are unmeasured. The full local
  pipeline (Zipformer + Kokoro + v1-mini) was sized for a normal
  machine, not for Render free (512 MB / 0.1 CPU) — do not assume it
  fits there; the worker is not deployed to Render.
- The brain is non-streaming for voice: one turn yields one reply,
  spoken sentence-by-sentence. Token streaming is a later
  improvement.
- `voice-live.html` loads the LiveKit JS client from a CDN
  (jsdelivr). If the CDN is unreachable, the page cannot connect.
- LiveKit Cloud Build caps: 1,000 agent minutes/month, hard cap.
  A long afternoon of testing can use a real share of it — watch
  the usage dashboard at cloud.livekit.io.
- The Render-hosted VektorCode app is a separate deployment; this
  clone's voice endpoints exist only here.

## Rollback

There is nothing to roll back in production — production was never
touched. To undo everything:

```bash
# Delete the whole experiment:
rm -rf ~/workspace/vektorcode-voice-clone
# Production VektorCode (~/workspace/vektorcode-build, Render,
# GitHub main) is exactly as it was.
```

Inside the clone, the voice work is one local branch
(`voice/livekit-continuous`) on top of the pre-existing follow-up
batch. To drop just the voice changes and keep the clone:

```bash
cd ~/workspace/vektorcode-voice-clone
git checkout 7375f3f   # baseline commit, before voice work
```

The two app changes (the `voice` flag on `/api/chat` and the
`/api/voice/*` endpoints) are additive and inert when LiveKit is
not configured: `/api/voice/status` reports `livekit_configured:
false`, and `/api/voice/token` returns 503. No existing behaviour
changes — the full pre-existing suite (106 tests) stays green.
