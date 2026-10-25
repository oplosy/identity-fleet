import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from identity_fleet import approvals
from identity_fleet.config import open_core
from identity_fleet.sim import load_fixture

APPROVERS = ("selin.k", "emre.d", "deniz.a")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, Path]:
    keys, lines = {}, []
    for name in APPROVERS:
        key = tmp_path / f"{name}.key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        algo, blob = (tmp_path / f"{name}.key.pub").read_text().split()[:2]
        lines.append(f"{name} {algo} {blob}")
        keys[name] = key
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "allowed_signers").write_text("\n".join(lines) + "\n")
    return keys


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def core(tmp_path: Path, keys, clock):
    c = open_core(actor="agent:test", home=tmp_path / "home")
    c.now = clock
    load_fixture(c.store)
    return c


@pytest.fixture
def approve(core, keys):
    def _approve(plan_id: str, who: str):
        return core.approve(plan_id, approvals.sign("approve", plan_id, keys[who]))

    return _approve
