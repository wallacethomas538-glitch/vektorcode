"""Aegis Gate — independent security layer for VektorCode (Phase 5).

Adapted from VektorMind 2.0's WORKING aegis_gate (vektorflow/agents.py,
verified 2026-10-29) for single-orchestrator VektorCode, with the exact same
decision contract so VektorAegis drops in later.

VEKTORAEGIS_CONTRACT: when Chad ships VektorAegis, it must implement
  - aegis_gate(action: str, risk: int = 3) -> dict
  - require_approval(action: str, risk: int = 3) -> dict
  - BLOCKED_ACTIONS: frozenset including the six shared actions below
  - gate result keys: {"action", "risk_score", "allowed", "mode",
    "recovery_plan"}
Swap the import; nothing else changes. Do NOT implement VektorAegis here.
(Chad's project — Codeberg only, README only, Chad's lane.)
"""
import time

# The six shared actions below are the contract with VektorMind 2.0's gate.
# The three VektorCode additions harden the local execution model.
_SHARED_BLOCKED_ACTIONS = frozenset({
    "publish_live", "price_live", "money_move",
    "send_external_message", "call_provider", "deploy_live"})
_VEKTORCODE_ADDITIONS = frozenset({
    "execute_shell_outside_sandbox",
    "read_key_material",
    "network_call_unapproved"})

BLOCKED_ACTIONS = _SHARED_BLOCKED_ACTIONS | _VEKTORCODE_ADDITIONS

VEKTORAEGIS_CONTRACT = {
    "aegis_gate": "aegis_gate(action: str, risk: int = 3) -> dict",
    "require_approval": "require_approval(action: str, risk: int = 3) -> dict",
    "BLOCKED_ACTIONS": "frozenset including the six shared actions: "
                       "publish_live, price_live, money_move, "
                       "send_external_message, call_provider, deploy_live",
    "result_keys": ["action", "risk_score", "allowed", "mode",
                    "recovery_plan"],
}


def aegis_gate(action: str, risk: int = 3) -> dict:
    """Identical decision contract to VektorMind 2.0's reference gate."""
    # Lazy import: core imports this module for the GateLog default.
    from vektor_cloud.core import kill_on
    if kill_on():
        return {"action": action, "risk_score": 10, "allowed": False,
                "mode": "kill_switch",
                "recovery_plan": "Human operator must explicitly restore service."}
    return {"action": action, "risk_score": risk,
            "allowed": action not in BLOCKED_ACTIONS,
            "mode": "draft_only",
            "recovery_plan": "Preserve evidence and request approval before retrying."}


class GateLog:
    """Aegis Gate with an in-memory decision log. Owned by VektorCore.

    sink: optional callable(entry) persisted per decision — VektorCore wires
    this to the ReceiptLog so gate decisions are durable (phase 6).
    """

    def __init__(self, sink=None):
        self._decisions = []
        self._sink = sink

    def decide(self, action: str, risk: int = 3) -> dict:
        decision = aegis_gate(action, risk)
        entry = {"ts": time.time(), "action": action,
                 "risk": risk, "allowed": decision["allowed"],
                 "mode": decision["mode"]}
        self._decisions.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return decision

    def decisions(self) -> list:
        return list(self._decisions)


def require_approval(action: str, risk: int = 3) -> dict:
    """Stage an action for Wallace's explicit approval. approved_by stays
    None until he taps that exact action."""
    return {"status": "staged_for_approval", "action": action,
            "gate": aegis_gate(action, risk), "approved_by": None}


# -- FANGS integration -------------------------------------------------------
# VektorBots' active security probes, wired as the Aegis Gate's scanning arm.
# The gate decides policy (allow/deny); FANGS provides evidence (scan results).

def security_scan(host: str, checks=("tls", "headers", "exposed_paths", "ports")) -> dict:
    """Run FANGS active checks against an allowlisted host.

    Returns the scan report dict. Raises TargetNotAuthorized for
    non-allowlisted hosts — the allowlist cannot be bypassed.
    """
    from vektor_cloud.fangs import FangsRunner
    runner = FangsRunner()
    return runner.run(host, checks=checks)


def scan_file(path: str) -> dict:
    """Scan a file with the antivirus engine. Quarantines on hit, never deletes."""
    from vektor_cloud.antivirus import Antivirus
    av = Antivirus()
    return av.scan(path)
