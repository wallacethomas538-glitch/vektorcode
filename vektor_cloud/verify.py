"""Independent verifier — builder never verifies own work (Phase 4).

The Verifier is owned by VektorCore, not by any engine. Criteria come from
the task spec, never from the engine's own output.

Independence rule: this module imports NOTHING from vektor_cloud.engines.
The EngineResult contract is a plain dict, so no shared logic is needed.
"""
import os
import pathlib
import re
import sys

from vektor_cloud.sandbox import MAX_WRITE_CHARS, DEFAULT_TIMEOUT

DEFAULT_BANNED_STRINGS = ["sk-", "openai"]
# Key-like shapes: never allow anything that looks like key material in
# an artifact, even if it dodges the plain substring list.
DEFAULT_BANNED_PATTERNS = [r"LLM_[A-Za-z0-9]{8,}", r"sk-[A-Za-z0-9]{8,}"]

_REQUIRED_RESULT_KEYS = ("engine", "status", "artifacts", "tests", "executed")
_HONEST_STATUSES = ("completed", "failed", "staged")


class Verifier:
    def verify_artifact(self, path: str, criteria: dict | None = None) -> dict:
        """Check one artifact file. Returns {"passed", "checks": [...]}."""
        criteria = criteria or {}
        banned = criteria.get("banned_strings", DEFAULT_BANNED_STRINGS)
        max_size = criteria.get("max_size_chars", MAX_WRITE_CHARS)
        checks = []

        p = pathlib.Path(path)
        exists = p.is_file()
        checks.append({"name": "exists", "passed": exists,
                       "detail": f"{path} is a file" if exists
                       else f"{path} missing"})
        if not exists:
            return {"passed": False, "checks": checks}

        content = p.read_text(encoding="utf-8", errors="replace")
        checks.append({"name": "non_empty",
                       "passed": len(content.strip()) > 0,
                       "detail": f"{len(content)} chars"})
        checks.append({"name": "size_within_cap",
                       "passed": len(content) <= max_size,
                       "detail": f"{len(content)} <= {max_size}"})

        lowered = content.lower()
        found = [b for b in banned if b.lower() in lowered]
        for pat in DEFAULT_BANNED_PATTERNS:
            if re.search(pat, content):
                found.append(f"pattern:{pat}")
        checks.append({"name": "no_banned_strings",
                       "passed": not found,
                       "detail": "clean" if not found
                       else f"banned material found: {found}"})
        return {"passed": all(c["passed"] for c in checks), "checks": checks}

    def verify_tests(self, sandbox) -> dict:
        """Run the test suite inside the sandbox. The TESTS are the criteria,
        written independently of the engine's code."""
        r = sandbox.run([sys.executable, "-m", "pytest", "-q"],
                        timeout=DEFAULT_TIMEOUT)
        passed = r["returncode"] == 0 and not r["timed_out"]
        detail = (r["stdout"] + r["stderr"])[-500:]
        return {"passed": passed,
                "checks": [{"name": "pytest_green", "passed": passed,
                            "detail": detail}]}

    def verify_build(self, result: dict, sandbox=None) -> dict:
        """Full check on an EngineResult dict: shape honest, artifacts
        present / inside the sandbox / free of secret material, tests honest.
        """
        checks = []

        missing = [k for k in _REQUIRED_RESULT_KEYS if k not in result]
        checks.append({"name": "result_shape", "passed": not missing,
                       "detail": "ok" if not missing
                       else f"missing keys: {missing}"})

        status = result.get("status")
        checks.append({"name": "status_honest",
                       "passed": status in _HONEST_STATUSES,
                       "detail": f"status={status!r}"})

        artifacts = result.get("artifacts", []) or []
        for art in artifacts:
            av = self.verify_artifact(art, {})
            checks.append({"name": f"artifact:{art}",
                           "passed": av["passed"],
                           "detail": "; ".join(
                               f"{c['name']}={'ok' if c['passed'] else 'FAIL'}"
                               for c in av["checks"])})
            if sandbox is not None:
                inside = os.path.commonpath(
                    [os.path.realpath(sandbox.root),
                     os.path.realpath(art)]) == os.path.realpath(sandbox.root)
                checks.append({"name": f"artifact_inside_sandbox:{art}",
                               "passed": inside,
                               "detail": "inside root" if inside
                               else "ESCAPED sandbox root"})

        tests = result.get("tests", {})
        if isinstance(tests, dict) and "passed" in tests:
            tpass = tests["passed"] is True
            checks.append({"name": "tests_passed", "passed": tpass,
                           "detail": "engine tests green" if tpass
                           else "engine tests failed"})
        else:
            checks.append({"name": "tests_passed", "passed": True,
                           "detail": "no test verdict to check"})

        return {"passed": all(c["passed"] for c in checks), "checks": checks}
