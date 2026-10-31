"""Vektor free-tier cascade — NEVER paid, auto-switch, multiple keys per provider.
Rules (Wallace, 2026-10-27):
- $0 guard: a call that would cost money never runs.
- Providers are tried in order, keys rotate inside each provider.
- On 429 / quota exhausted, that key is marked dry until its reset, and the next key/provider is tried.
- If all free keys/providers are dry, return honest degradation, never a paid fallback.
- Keys come from env only, never in code. Multiple keys per provider: comma-separated, e.g.
  GROQ_API_KEYS=key1,key2,key3  OPENROUTER_API_KEYS=key1,key2
"""
import os
PAID_MARKERS = ("gpt-", "claude", "openai")
def keys_for(provider: str) -> list:
    raw = os.environ.get(f"{provider}_API_KEYS", "") or os.environ.get(f"{provider}_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]
def is_paid_model(model: str) -> bool:
    return any(m in model.lower() for m in PAID_MARKERS)
class Cascade:
    def __init__(self, providers=("GROQ", "OPENROUTER", "GEMINI", "DEEPSEEK"), ledger=None):
        self.providers = providers
        self.dry = set()  # (provider, key_index)
        self.ledger = ledger  # optional CostLedger; None until wired
    def next_key(self):
        for prov in self.providers:
            for i, k in enumerate(keys_for(prov)):
                if (prov, i) not in self.dry:
                    return prov, i, k
        return None
    def mark_dry(self, prov, i):
        self.dry.add((prov, i))
    def dry_report(self) -> dict:
        """Which keys are dry and why. Phase 1: reason is always "manual"
        (mark_dry); quota/429 wiring comes when real calls exist — not this build."""
        return {"dry_keys": [{"provider": prov, "key_index": i, "reason": "manual"}
                             for prov, i in sorted(self.dry)]}
    def call(self, prompt: str, model: str = "free"):
        if is_paid_model(model):
            return {"status": "blocked_paid_model", "cost": 0, "executed": False}
        nxt = self.next_key()
        if not nxt:
            return {"status": "all_free_tiers_dry", "cost": 0, "executed": False}
        prov, i, _k = nxt
        if self.ledger is not None:
            self.ledger.record(prov, model, 0.0)
        # No real model call in scaffold — this is the routing decision, tested with fakes below.
        return {"status": "routed_free", "provider": prov, "key_index": i, "cost": 0, "executed": False}

# --- Real chat path (added 2026-10-31): actual HTTP calls, never paid. ---
import json
import urllib.request
import urllib.error
from urllib.parse import quote

_CHAT_MODELS = {
    "GROQ": "llama-3.3-70b-versatile",
    "OPENROUTER": "meta-llama/llama-3.3-70b-instruct:free",
    "GEMINI": "gemini-2.0-flash",
    "POLLINATIONS": None,  # keyless GET
}
_PROVIDER_ORDER = ("GROQ", "OPENROUTER", "GEMINI", "POLLINATIONS")


def _post_json(url, body, headers, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_openai_text(data):
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _call_provider(provider, key, prompt):
    """One real chat call. Returns reply text, or None if the key/provider failed."""
    from vektor_cloud import free_providers
    if provider == "POLLINATIONS":
        url = free_providers.text_url("POLLINATIONS") + quote(prompt)
        from vektor_cloud.egress import guard_url
        guard_url(url)
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read().decode("utf-8").strip() or None
    url = free_providers.text_url(provider)
    if not url:
        return None
    if provider == "GEMINI":
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        data = _post_json(f"{url}/{_CHAT_MODELS['GEMINI']}:generateContent?key={key}",
                          body, {"Content-Type": "application/json"})
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            return None
    # OpenAI-compatible (Groq, OpenRouter)
    body = {"model": _CHAT_MODELS[provider],
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}"}
    if provider == "OPENROUTER":
        headers["HTTP-Referer"] = "https://gojojoes.com"
        headers["X-Title"] = "Vektor"
    data = _post_json(url, body, headers)
    return _extract_openai_text(data) or None


def chat(self, prompt: str) -> dict:
    """Real conversational reply through the free cascade.

    Tries each provider's keys in order. On 429/quota or bad key, marks
    that key dry and moves on. Never touches a paid model. Returns
    {'status': 'ok', 'provider': ..., 'reply': ...} or honest degradation.
    """
    for prov in _PROVIDER_ORDER:
        keys = keys_for(prov) if prov != "POLLINATIONS" else [None]
        for i, k in enumerate(keys):
            if prov != "POLLINATIONS" and (prov, i) in self.dry:
                continue
            try:
                reply = _call_provider(prov, k, prompt)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    if prov != "POLLINATIONS":
                        self.mark_dry(prov, i)
                    continue
                return {"status": "error",
                        "reason": f"{prov} returned HTTP {e.code}"}
            except Exception as e:
                continue  # network/timeout: try next key/provider
            if reply:
                if self.ledger is not None:
                    self.ledger.record(prov, _CHAT_MODELS.get(prov) or "pollinations", 0.0)
                return {"status": "ok", "provider": prov, "reply": reply}
            if prov != "POLLINATIONS":
                self.mark_dry(prov, i)
    return {"status": "all_free_tiers_dry",
            "reason": "No free provider answered. Set a key (e.g. GEMINI_API_KEY) and try again."}


Cascade.chat = chat
