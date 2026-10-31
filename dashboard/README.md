# VektorCode Dashboard — Termux only

This dashboard is Wallace's local command center. It runs **only on his phone
in Termux**. It is **not** deployed to Render and must never be wired to
Render config.

- Start it: `cd ~/vektorcode/dashboard && python3 main.py`
- Open it: `http://localhost:8765`
- Default model: local Ollama Llama on the phone, then the free cloud fallback.

The cloud agent lives in `vektor_cloud/` and deploys to Render from the repo
root (`agent_api.py`, `render.yaml`). The two never share config: dashboard
settings stay in `dashboard/`, cloud settings stay in Render's Environment.
