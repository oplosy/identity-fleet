"""Wiring: where state lives and which adapters the core uses."""

import os
from pathlib import Path

from .audit import AuditLog
from .core import Core
from .sim import SimDirectory, SimHosts
from .store import Store


def idf_home() -> Path:
    home = Path(os.environ.get("IDF_HOME", ".idf"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def open_core(actor: str, home: Path | None = None) -> Core:
    home = home or idf_home()
    store = Store(home / "state.db")
    # M0 has only the simulator; real adapters plug in here.
    return Core(
        store=store,
        directory=SimDirectory(store),
        hosts=SimHosts(store),
        audit=AuditLog(home / "audit.jsonl"),
        allowed_signers=home / "allowed_signers",
        actor=actor,
    )
