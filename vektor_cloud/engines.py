"""Engine adapter — coding engines are replaceable workers (Phase 2).

One interface; engines are swappable. Vektor-native works today. The Codex
integration is HELD and is never activated without Wallace's explicit
approval. OpenAI Codex CLI is banned by NO-OPENAI.md — see
HeldCodexEngine.available() for the documented check (default deny).

EngineResult contract (plain dict):
  {"engine": str, "status": "completed"|"failed"|"staged",
   "artifacts": [str, ...], "tests": {...}, "executed": bool,
   "reason": str|None}
"""
import os
import shutil
from abc import ABC, abstractmethod

from vektor_cloud.vektor_coder import build_artifact
from vektor_cloud.sandbox import Sandbox
from vektor_cloud import codex_integration


class EngineAdapter(ABC):
    """Abstract worker. Contract: writes ONLY inside workdir; no network
    except through an injected model client (None in this build); never
    sends, deploys, or publishes; never auto-merges."""

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def build(self, task: str, workdir: str) -> dict:
        """Return an EngineResult dict."""
        ...


class VektorNativeEngine(EngineAdapter):
    """Vektor himself codes: plan -> write -> test -> fix. Always available."""

    def name(self) -> str:
        return "vektor-native"

    def available(self) -> bool:
        return True

    def build(self, task: str, workdir: str) -> dict:
        # One Sandbox per build: validates the workdir root exists and jails
        # all engine file/subprocess activity inside it (phase 3).
        Sandbox(workdir)
        root = os.path.realpath(workdir)
        result = build_artifact(task, workdir)
        # Contract enforcement: every artifact must stay inside workdir.
        for art in result.get("artifacts", []):
            if os.path.commonpath([root, os.path.realpath(art)]) != root:
                return {"engine": self.name(), "status": "failed",
                        "artifacts": [], "tests": {},
                        "executed": False,
                        "reason": "artifact_escaped_workdir"}
        return result


# Markers of OpenAI's Codex CLI installation. If the resolved binary path
# contains one of these, the binary is treated as OpenAI's and refused.
# This is a conservative heuristic — when in doubt, deny (default deny).
_OPENAI_CODEX_MARKERS = ("@openai/codex", "openai-codex", "openai/codex")


def _looks_like_openai_codex(bin_path: str) -> bool:
    return any(m in os.path.realpath(bin_path).lower()
               for m in _OPENAI_CODEX_MARKERS)


class HeldCodexEngine(EngineAdapter):
    """Wraps codex_integration.run_codex. HELD — never activated without
    Wallace's explicit approval.

    available() is True ONLY when ALL hold:
      1. CODEX_ENABLED=1 in the environment (Wallace's explicit enablement),
      2. CODEX_BIN is set (or `codex` resolves on PATH),
      3. the resolved binary is NOT OpenAI's Codex CLI (banned by
         NO-OPENAI.md; conservative marker check, default deny).
    """

    def name(self) -> str:
        return "codex-held"

    def available(self) -> bool:
        if os.environ.get("CODEX_ENABLED") != "1":
            return False
        bin_path = os.environ.get("CODEX_BIN") or shutil.which("codex")
        if not bin_path:
            return False
        if _looks_like_openai_codex(bin_path):
            return False
        return True

    def build(self, task: str, workdir: str) -> dict:
        if not self.available():
            return {"engine": self.name(), "status": "staged",
                    "reason": "held_per_no_openai_rule",
                    "artifacts": [], "tests": {}, "executed": False}
        r = codex_integration.run_codex(task, workdir)
        return {"engine": self.name(),
                "status": r.get("status", "failed"),
                "artifacts": [], "tests": {},
                "executed": bool(r.get("executed", False)),
                "reason": r.get("reason")}


ENGINE_REGISTRY = {"vektor-native": VektorNativeEngine,
                   "codex-held": HeldCodexEngine}


def select_engine(task: str, prefer: str = "vektor-native") -> EngineAdapter:
    """Return the preferred engine if available, else the first available
    engine, else raise RuntimeError."""
    preferred_cls = ENGINE_REGISTRY.get(prefer)
    if preferred_cls is not None:
        engine = preferred_cls()
        if engine.available():
            return engine
    for name, cls in ENGINE_REGISTRY.items():
        if name == prefer:
            continue
        engine = cls()
        if engine.available():
            return engine
    raise RuntimeError("no engine available")
