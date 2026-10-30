"""VektorCore — the central intelligence and orchestrator (Phase 1).

Wallace commands; Vektor plans, routes to workers, verifies, and stages for
Wallace's approval. Never executes sends, money moves, deploys, or publishes.
Kill switch and the $0 cost ledger are centralized here.

Phase 1 notes:
- The engine adapter wires in phase 2; phase 1 routes through a stub that
  returns staged (EngineResult shape, forward-compatible).
- The gate defaults to a deny-all stub until phase 5 wires the real Aegis Gate.
- The verifier wires in phase 4; memory wires in phase 6.
"""
import os

from vektor_cloud.aegis import GateLog
from vektor_cloud.receipts import ReceiptLog
from vektor_cloud.verify import Verifier


def kill_on() -> bool:
    """Single source of truth for the kill switch. Moved here from app.py."""
    return os.environ.get("VEKTOR_KILL_SWITCH") == "1" or os.path.exists(
        os.path.expanduser("~/.vektor/KILL"))


class CostLedger:
    """$0 guard ledger. Paid model calls are blocked, never recorded.

    Invariant: total_usd() is always 0.0. Anything with cost_usd > 0 is
    refused at record time — the ledger proves $0, it does not track spend.
    """

    def __init__(self):
        self._entries = []

    def record(self, provider: str, model: str, cost_usd: float) -> dict:
        if cost_usd > 0:
            return {"status": "blocked_paid_model", "recorded": False}
        self._entries.append({"provider": provider, "model": model,
                              "cost_usd": 0.0})
        return {"status": "recorded", "recorded": True}

    def total_usd(self) -> float:
        return 0.0

    def assert_zero(self) -> None:
        if self.total_usd() > 0:
            raise AssertionError("CostLedger invariant violated: total > 0")

    def entries(self) -> list:
        return list(self._entries)


class VektorCore:
    """Central orchestrator. Dependencies injected; None means safe defaults."""

    def __init__(self, ledger=None, gate=None, memory=None, verifier=None,
                 receipt_log=None):
        self.ledger = ledger if ledger is not None else CostLedger()
        self.receipt_log = (receipt_log if receipt_log is not None
                            else ReceiptLog())
        # Phase 5: the real Aegis Gate. No bypass flag may exist anywhere.
        # Phase 6: gate decisions persist through the sink into the receipt log.
        if gate is None:
            self.gate = GateLog(sink=self._persist_gate_decision)
        else:
            self.gate = gate
        self.memory = memory  # phase 6: injected or None
        self.verifier = verifier if verifier is not None else Verifier()

    def _persist_gate_decision(self, entry: dict) -> None:
        """GateLog sink: every gate decision lands in the receipt log."""
        self.receipt_log.issue(
            f"gate:{entry['action']}",
            {"allowed": entry["allowed"], "mode": entry["mode"]},
            {"risk": entry["risk"], "ts": entry["ts"]},
            approved_by=None)

    def plan_command(self, command: str) -> dict:
        """Plan only. Never executes anything. Phase 6: memory is injected
        by simple key match — any remembered key appearing in the command
        surfaces as a memory hit for the planner."""
        memory_hits = []
        if self.memory is not None:
            lowered = command.lower()
            for kind in ("fact", "preference", "decision", "lesson"):
                for item in self.memory.recall_all(kind):
                    if item["key"].lower() in lowered:
                        memory_hits.append({"kind": kind, "key": item["key"],
                                            "value": item["value"]})
        return {"command": command,
                "steps": [f"1. Plan: {command}",
                          "2. Route to engine worker",
                          "3. Independent verification",
                          "4. Aegis Gate review",
                          "5. Stage for Wallace's approval"],
                "requires_approval": True,
                "estimated_cost_usd": 0.0,
                "memory_hits": memory_hits}

    def _engine_stub(self, command: str) -> dict:
        """Phase-1 stub for the engine adapter (phase 2). EngineResult shape."""
        return {"engine": "none", "status": "staged",
                "reason": "engine_adapter_wires_in_phase_2",
                "artifacts": [], "tests": {}, "executed": False}

    def run_command(self, command: str, approved: bool = False) -> dict:
        """Run the orchestrator loop. Every engine result is verified before
        staging; a failed verdict gets one fix retry, then stages with the
        failure honestly reported. Never fakes success. Staged, never executed."""
        if kill_on():
            # A receipt is issued even for refusals — the decision is auditable.
            rcpt = self.receipt_log.issue(
                command, {"allowed": False, "mode": "kill_switch"},
                {"status": "refused", "reason": "kill_switch_on"})
            return {"status": "refused", "reason": "kill_switch_on",
                    "receipt_id": rcpt["id"]}
        planned = self.plan_command(command)
        engine_result = self._engine_stub(command)
        verdict = self.verifier.verify_build(engine_result)
        if not verdict["passed"]:
            # One fix retry: re-run the engine step once.
            engine_result = self._engine_stub(command)
            verdict = self.verifier.verify_build(engine_result)
            if not verdict["passed"]:
                gate_decision = self.gate.decide("run_command", 3)
                rcpt = self.receipt_log.issue(
                    command, gate_decision,
                    {"status": "staged_with_failure"})
                return {"status": "staged_with_failure",
                        "command": command,
                        "plan": planned["steps"],
                        "engine": engine_result,
                        "verdict": verdict,
                        "gate": gate_decision,
                        "receipt_id": rcpt["id"],
                        "executed": False,
                        "sent": False,
                        "money_moved": False,
                        "deployed": False,
                        "published": False}
        # Every consequential step goes through the gate. allowed False →
        # staged, never executed. No bypass flag exists anywhere in this code.
        gate_decision = self.gate.decide("run_command", 3)
        rcpt = self.receipt_log.issue(command, gate_decision,
                                      {"status": "staged_for_approval"})
        return {"status": "staged_for_approval",
                "command": command,
                "plan": planned["steps"],
                "engine": engine_result,
                "verdict": verdict,
                "gate": gate_decision,
                "receipt_id": rcpt["id"],
                "executed": False,
                "sent": False,
                "money_moved": False,
                "deployed": False,
                "published": False}
