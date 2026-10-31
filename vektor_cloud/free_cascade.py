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
