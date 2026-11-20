# VektorCode dashboard

Local, dependency-free Python dashboard for model selection, health, sessions, tools, Composio status, receipts, and the kill switch.

Run:

```sh
cd ~/vektorcode
python3 main.py
```

Open http://127.0.0.1:8765. The server binds to `0.0.0.0` and honors Render's `PORT` variable (or `VEKTORCODE_PORT` locally). By default it calls local Ollama at `http://127.0.0.1:11434/v1/chat/completions` with model `llama3.2`.

For a hosted provider, set the provider-specific API key (for example `GROQ_API_KEY` or `OPENAI_API_KEY`) before starting the dashboard. The key stays server-side.

VektorCode reads the shared identity and memory from `../vektor/SOUL.md` and
`~/.vektor/memories/`. Explicitly approved notes can be saved with
`POST /api/memory/save` (`category`: `USER` or `MEMORY`) and tool/skill notes
with `POST /api/skill/save`. These endpoints require the configured tool
password when `VEKTORCODE_TOOL_PASSWORD` is set; nothing is saved automatically
from chat.

## Skills (hypit-integration)

Skills are instruction packs the chat model follows when Wallace invokes
them. A skill is a directory with a `SKILL.md` (frontmatter `name` /
`description` + Markdown body) and optional `references/` files, or a
loose Markdown note saved via `POST /api/skill/save`.

- Repo skills ship with the code: `skills/<slug>/SKILL.md`. State notes
  live in `$VEKTOR_STATE_DIR/skills/` (same dir `/api/skill/save` writes).
  The loader (`vektor_cloud/skills.py`) reads both; repo wins on a slug
  collision. Only these two roots are ever read — the model can never
  load an arbitrary path.
- Invoke in chat with `/skill <slug>` (e.g. `/skill hypit`) or a `skill`
  field in the `/api/chat` payload. The skill body loads as a system
  message for that turn, and its reference files are readable via the
  `read_skill_reference` tool (read-only, jailed to the skill directory).
- `GET /api/skills` lists installed skills; `/api/state` includes them.
- Installed: `hypit` — Hypit's video-production skill (copied from
  hypit-ai/hypit `skills/hypit/`, 71 files). It guides the model in
  authoring video workflows; rendering still needs the Hypit runtime
  (Node 22 + headless Chromium) — see `docs/hypit-runtime-findings.md`.

## Sandboxed shell (hypit-integration)

The `shell` tool (Settings toggle, risky/auth-gated like the other tools)
lets the model run one shell command inside a sandbox work dir
(`$VEKTOR_STATE_DIR/sandbox`, cwd jailed, environment scrubbed of key
material, 120s timeout, 4000-char output cap, 5 executed commands per
session). Nothing runs without Wallace's per-command approval:

1. The model calls `run_shell` with `{command}` — this only STAGES the
   command and returns an `approval_id`.
2. Wallace approves that exact command: `POST /api/shell/approve`
   `{"approval_id": "..."}` (auth required). Pending approvals list at
   `GET /api/shell/approvals`.
3. The model re-issues `run_shell` with the same command + `approval_id`
   and it runs once. Approvals are single-use, expire after 10 minutes,
   and are bound to the exact command string and session. Every stage is
   receipted (`shell_staged` / `shell_approved` / `shell_executed` /
   `shell_refused`); the Aegis gate refuses when the kill switch is on.

Honest limit: the sandbox is not a chroot — the jail covers cwd and the
file helpers, but a shell command can name absolute paths outside the
work dir. The approval gate (Wallace sees the exact command) is the
control for that; do not claim stronger confinement.
