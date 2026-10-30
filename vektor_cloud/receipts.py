"""Receipts — a receipt for every consequential decision (Phase 6).

Local-first: append-only JSONL file under ~/.vektor/. No cloud, no sync.
Append-only: no update/delete API exists, by design.
"""
import json
import os
import re
import secrets
import time


def redact(text: str) -> str:
    """Replace key material with [REDACTED]. Never let a secret reach a log."""
    text = re.sub(r"LLM_[A-Za-z0-9]+", "[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9]+", "[REDACTED]", text)
    text = re.sub(r"([A-Za-z_]*_API_KEY[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
                  r"\1[REDACTED]", text)
    return text


class ReceiptLog:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.expanduser(
            "~/.vektor/vektorcode_receipts.jsonl")
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def issue(self, action: str, gate: dict, result: dict,
              approved_by: str | None = None) -> dict:
        entry = {"id": "rcpt-" + secrets.token_hex(4),
                 "ts": time.time(),
                 "action": redact(action),
                 "allowed": gate["allowed"],
                 "mode": gate["mode"],
                 "result_summary": redact(str(result))[:500],
                 "approved_by": approved_by,
                 "money_moved": False,
                 "sent": False}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
