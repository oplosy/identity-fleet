"""Approvals signed with SSH keys (ssh-keygen -Y), so the approver's identity comes from a key, not a claim."""

import subprocess
import tempfile
from pathlib import Path

from .domain import IdfError

NAMESPACE = "identity-fleet-approval"


def message(action: str, plan_id: str) -> bytes:
    # The action is part of the signed data so an approval cannot be replayed as a rejection, or vice versa.
    return f"{action}:{plan_id}".encode()


def sign(action: str, plan_id: str, key_path: Path) -> str:
    result = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-q", "-f", str(key_path), "-n", NAMESPACE],
        input=message(action, plan_id),
        capture_output=True,
    )
    if result.returncode != 0:
        raise IdfError("SIGNING_FAILED", f"ssh-keygen could not sign: {result.stderr.decode().strip()}")
    return result.stdout.decode()


def verify(action: str, plan_id: str, signature: str, allowed_signers: Path) -> str:
    """Return the principal that produced a valid signature, or raise APPROVAL_INVALID."""
    if not allowed_signers.exists():
        raise IdfError("APPROVAL_INVALID", f"No allowed_signers file at {allowed_signers}.")
    with tempfile.TemporaryDirectory() as tmp:
        sig_path = Path(tmp) / "approval.sig"
        sig_path.write_text(signature)
        found = subprocess.run(
            ["ssh-keygen", "-Y", "find-principals", "-s", str(sig_path), "-f", str(allowed_signers)],
            capture_output=True,
        )
        principals = found.stdout.decode().split()
        if found.returncode != 0 or len(principals) != 1:
            raise IdfError("APPROVAL_INVALID", "Signature key is not listed exactly once in allowed_signers.")
        principal = principals[0]
        checked = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", principal, "-n", NAMESPACE,
             "-s", str(sig_path)],
            input=message(action, plan_id),
            capture_output=True,
        )
        if checked.returncode != 0:
            raise IdfError("APPROVAL_INVALID", f"Signature does not verify for {action} of {plan_id}.")
    return principal
