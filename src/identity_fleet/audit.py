"""Append-only, hash-chained JSONL audit log."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import canonical

GENESIS = "0" * 64


def _hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def _last(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        last = None
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last) if last else None

    def append(self, event: str, actor: str, plan_id: str | None = None, **data: Any) -> dict[str, Any]:
        # shortcut: no file lock; two processes appending at the same instant can fork the chain. Add a lock before multi-writer use.
        last = self._last()
        entry = {
            "seq": last["seq"] + 1 if last else 1,
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "actor": actor,
            "plan_id": plan_id,
            "data": data,
            "prev_hash": last["hash"] if last else GENESIS,
        }
        entry["hash"] = _hash(entry)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def verify(self) -> tuple[bool, int | None]:
        """Return (ok, seq of the first broken entry)."""
        prev = GENESIS
        for i, entry in enumerate(self.entries(), start=1):
            if entry.get("seq") != i or entry.get("prev_hash") != prev or entry.get("hash") != _hash(entry):
                return False, entry.get("seq", i)
            prev = entry["hash"]
        return True, None
