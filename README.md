# VektorCode dashboard

Local, dependency-free Python dashboard for model selection, health, sessions, tools, Composio status, receipts, and the kill switch.

Run:

```sh
cd ~/vektorcode
python3 app.py
```

Open http://127.0.0.1:8765. Hermes uses an OpenAI-compatible endpoint. By default it calls local Ollama at `http://127.0.0.1:11434/v1/chat/completions` with model `llama3.2`.

For a hosted provider, set `HERMES_LLM_URL`, `HERMES_LLM_MODEL`, and `HERMES_LLM_API_KEY` before starting the dashboard. The key stays server-side.

VektorCode reads the shared identity and memory from `../vektor/SOUL.md` and
`~/.vektor/memories/`. Explicitly approved notes can be saved with
`POST /api/memory/save` (`category`: `USER` or `MEMORY`) and tool/skill notes
with `POST /api/skill/save`. These endpoints require the configured tool
password when `VEKTORCODE_TOOL_PASSWORD` is set; nothing is saved automatically
from chat.
