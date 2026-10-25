"""Domain model: people, accounts, sessions, operations, plans and runs."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

PersonStatus = Literal["active", "left"]
OpKind = Literal["disable_account", "terminate_session", "remove_group"]
Risk = Literal["low", "medium", "high", "critical"]
PlanStatus = Literal[
    "noop",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "verified",
    "verified_with_residual",
    "failed",
]
RunStatus = Literal["verified", "verified_with_residual", "failed"]
OpOutcome = Literal["applied", "already_satisfied", "target_missing", "failed"]


class Person(BaseModel):
    employee_id: str
    given_name: str
    surname: str
    department: str
    status: PersonStatus = "active"
    tags: list[str] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return f"{self.given_name} {self.surname}"


class Account(BaseModel):
    account_id: str
    login: str
    owner: str
    enabled: bool = True
    groups: list[str] = Field(default_factory=list)


class Session(BaseModel):
    session_id: str
    host: str
    account_id: str
    started_at: str


class Op(BaseModel):
    op: OpKind
    target: str
    group: str | None = None
    phase: int

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.op, self.target, self.group)


class Intent(BaseModel):
    kind: Literal["leaver"]
    employee_id: str
    reason: str
    ticket: str | None = None


class Decision(BaseModel):
    rule: str
    effect: Literal["require_approval", "deny"]
    approvals: int = 0
    reason: str


class Approval(BaseModel):
    approver: str
    at: str


class Plan(BaseModel):
    plan_id: str
    intent: Intent
    ops: list[Op]
    risk: Risk
    approvals_required: int
    policy: list[Decision]
    requested: dict[str, str]
    status: PlanStatus
    created_at: str
    expires_at: str


class OpResult(BaseModel):
    op: Op
    outcome: OpOutcome
    detail: str | None = None


class Run(BaseModel):
    run_id: str
    plan_id: str
    status: RunStatus
    results: list[OpResult]
    residual: list[Op]
    started_at: str
    verified_at: str
    time_to_revoke_seconds: float


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_plan_id(intent: Intent, ops: list[Op]) -> str:
    payload = {"intent": intent.model_dump(mode="json"), "ops": [o.model_dump(mode="json") for o in ops]}
    return "pln_" + hashlib.sha256(canonical(payload)).hexdigest()[:16]


class IdfError(Exception):
    """An anticipated failure with a machine-readable code and a hint for the caller."""

    def __init__(self, code: str, message: str, next_step: str | None = None, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "next_step": self.next_step, "details": self.details}
