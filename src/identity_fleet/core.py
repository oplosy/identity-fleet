"""The single write path: resolve → plan → policy → approve → apply → verify."""

import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import approvals
from .audit import AuditLog
from .domain import IdfError, Intent, Op, OpResult, Person, Plan, Run, compute_plan_id
from .planner import diff, observe
from .policy import approvals_required, evaluate, risk_of
from .ports import Directory, Hosts
from .store import Store

PLAN_TTL = timedelta(hours=24)
# A finished plan whose diff reappears needs fresh approval; failed plans stay retryable.
REOPENABLE = {"expired", "verified", "verified_with_residual"}


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form, aware of Turkish dotless i."""
    text = text.replace("ı", "i").replace("İ", "i")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


class Core:
    def __init__(
        self,
        store: Store,
        directory: Directory,
        hosts: Hosts,
        audit: AuditLog,
        allowed_signers: Path,
        actor: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.directory = directory
        self.hosts = hosts
        self.audit = audit
        self.allowed_signers = allowed_signers
        self.actor = actor
        self.now = now

    # Reads

    def resolve_person(self, query: str) -> list[dict[str, Any]]:
        q = fold(query)
        candidates = []
        for p in self.store.persons():
            if p.employee_id.casefold() == query.strip().casefold():
                reason = "employee_id"
            elif q and q in fold(p.display_name):
                reason = "name"
            else:
                continue
            candidates.append(
                {
                    "employee_id": p.employee_id,
                    "display_name": p.display_name,
                    "department": p.department,
                    "status": p.status,
                    "match_reason": reason,
                }
            )
        return candidates

    def get_identity(self, employee_id: str) -> dict[str, Any]:
        person = self._person(employee_id)
        observed = observe(person, self.directory, self.hosts)
        return {
            "person": person.model_dump(mode="json"),
            "accounts": [a.model_dump(mode="json") for a in observed.accounts],
            "sessions": [s.model_dump(mode="json") for s in observed.sessions],
        }

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._refresh(self.store.get_plan(plan_id))
        run = self.store.latest_run(plan_id)
        return {
            "plan": plan.model_dump(mode="json"),
            "approvals": [a.model_dump(mode="json") for a in self.store.approvals(plan_id)],
            "latest_run": run.model_dump(mode="json") if run else None,
        }

    # Plan

    def plan_leaver(self, employee_id: str, reason: str, on_behalf_of: str, ticket: str | None = None) -> Plan:
        person = self._person(employee_id)
        intent = Intent(kind="leaver", employee_id=employee_id, reason=reason, ticket=ticket)
        target = person.model_copy(update={"status": "left"})
        observed = observe(target, self.directory, self.hosts)
        ops = diff(target, observed)
        decisions = evaluate(intent, ops, person, observed.accounts, set(self.store.get_meta("tier0_groups", [])))

        denied = [d for d in decisions if d.effect == "deny"]
        if denied:
            self.audit.append("plan.denied", self.actor, None, intent=intent.model_dump(), rules=[d.rule for d in denied])
            raise IdfError(
                "POLICY_DENIED",
                denied[0].reason,
                "This change cannot be made through IdentityFleet.",
                rules=[d.rule for d in denied],
            )

        plan_id = compute_plan_id(intent, ops)
        event = "plan.created"
        if self.store.get("plans", plan_id, Plan) is not None:
            existing = self._refresh(self.store.get_plan(plan_id))
            if existing.status not in REOPENABLE:
                return existing  # same request, same state: same plan
            self.store.clear_approvals(plan_id)
            event = "plan.reopened"

        now = self.now()
        required = approvals_required(decisions)
        plan = Plan(
            plan_id=plan_id,
            intent=intent,
            ops=ops,
            risk=risk_of(ops, decisions),
            approvals_required=required,
            policy=decisions,
            requested={"on_behalf_of": on_behalf_of, "agent": self.actor},
            status="noop" if not ops else ("approved" if required == 0 else "pending_approval"),
            created_at=now.isoformat(),
            expires_at=(now + PLAN_TTL).isoformat(),
        )
        self.store.put_plan(plan)
        self.audit.append(
            event, self.actor, plan_id, on_behalf_of_claimed=on_behalf_of, risk=plan.risk, ops=len(ops)
        )
        return plan

    # Approve / reject (CLI only; there is deliberately no MCP tool for these)

    def approve(self, plan_id: str, signature: str) -> Plan:
        plan = self._refresh(self.store.get_plan(plan_id))
        if plan.status != "pending_approval":
            raise IdfError("PLAN_NOT_PENDING", f"Plan {plan_id} is {plan.status}; it cannot be approved.")
        principal = approvals.verify("approve", plan_id, signature, self.allowed_signers)
        if principal == plan.requested["on_behalf_of"]:
            self.audit.append("approval.refused", principal, plan_id, reason="self_approval")
            raise IdfError("APPROVAL_REFUSED", "The requester cannot approve their own request.")
        if not self.store.add_approval(plan_id, principal, signature, self.now().isoformat()):
            raise IdfError("APPROVAL_REFUSED", f"{principal} has already approved {plan_id}.")
        self.audit.append("plan.approved", principal, plan_id)
        if len(self.store.approvals(plan_id)) >= plan.approvals_required:
            plan.status = "approved"
            self.store.put_plan(plan)
        return plan

    def reject(self, plan_id: str, signature: str) -> Plan:
        plan = self._refresh(self.store.get_plan(plan_id))
        if plan.status not in ("pending_approval", "approved"):
            raise IdfError("PLAN_NOT_PENDING", f"Plan {plan_id} is {plan.status}; it cannot be rejected.")
        principal = approvals.verify("reject", plan_id, signature, self.allowed_signers)
        plan.status = "rejected"
        self.store.put_plan(plan)
        self.audit.append("plan.rejected", principal, plan_id)
        return plan

    # Apply + verify

    def apply(self, plan_id: str) -> Run:
        plan = self._refresh(self.store.get_plan(plan_id))
        if plan.status == "rejected":
            raise IdfError("PLAN_REJECTED", f"Plan {plan_id} was rejected.", "Do not retry; ask the requester.")
        if plan.status == "expired" or self.now() > datetime.fromisoformat(plan.expires_at):
            raise IdfError("PLAN_EXPIRED", f"Plan {plan_id} has expired.", "Create a new plan with plan_leaver.")
        # Count signatures, not the status field: status is not covered by the plan hash.
        valid = self._valid_approvers(plan)
        if len(valid) < plan.approvals_required:
            raise IdfError(
                "PLAN_NOT_APPROVED",
                f"Plan {plan_id} needs {plan.approvals_required - len(valid)} more approval(s).",
                f"Ask a human approver to run: idf plan approve {plan_id} --key <their ssh key>",
                approvals_required=plan.approvals_required,
                approvals=len(valid),
            )

        person = self._person(plan.intent.employee_id)
        if person.status != "left":
            person.status = "left"
            self.store.put("persons", person.employee_id, person)
            self.audit.append("person.updated", self.actor, plan_id, status="left")

        started_at = self.now()
        self.audit.append("run.started", self.actor, plan_id)
        observed = observe(person, self.directory, self.hosts)
        needed = {o.key for o in diff(person, observed)}

        results: list[OpResult] = []
        for op in plan.ops:
            if op.key not in needed:
                # A missing session is exactly what terminate_session wants.
                satisfied = op.op == "terminate_session" or observed.has_target(op)
                outcome = "already_satisfied" if satisfied else "target_missing"
                results.append(OpResult(op=op, outcome=outcome))
                self.audit.append("op.skipped", self.actor, plan_id, op=op.model_dump(), outcome=outcome)
                continue
            try:
                self._execute(op)
                results.append(OpResult(op=op, outcome="applied"))
                self.audit.append("op.applied", self.actor, plan_id, op=op.model_dump())
            except Exception as e:  # one failing system must not hide the outcome of the others
                results.append(OpResult(op=op, outcome="failed", detail=str(e)))
                self.audit.append("op.failed", self.actor, plan_id, op=op.model_dump(), error=str(e))

        # Verify: an op's postcondition holds when it no longer appears in the diff.
        after = diff(person, observe(person, self.directory, self.hosts))
        after_keys = {o.key for o in after}
        for r in results:
            if r.outcome in ("applied", "already_satisfied") and r.op.key in after_keys:
                r.outcome, r.detail = "failed", "postcondition not met after apply"
        approved_keys = {o.key for o in plan.ops}
        residual = [o for o in after if o.key not in approved_keys]  # never executed: not approved

        status = (
            "failed"
            if any(r.outcome == "failed" for r in results)
            else "verified_with_residual" if residual else "verified"
        )
        verified_at = self.now()
        approved_at = max(
            (datetime.fromisoformat(a.at) for a in self.store.approvals(plan_id)),
            default=datetime.fromisoformat(plan.created_at),
        )
        run = Run(
            run_id="run_" + uuid.uuid4().hex[:12],
            plan_id=plan_id,
            status=status,
            results=results,
            residual=residual,
            started_at=started_at.isoformat(),
            verified_at=verified_at.isoformat(),
            time_to_revoke_seconds=(verified_at - approved_at).total_seconds(),
        )
        self.store.put_run(run)
        plan.status = status
        self.store.put_plan(plan)
        self.audit.append(
            f"run.{status}", self.actor, plan_id, run_id=run.run_id, residual=[o.model_dump() for o in residual]
        )
        return run

    # Internals

    def _person(self, employee_id: str) -> Person:
        person = self.store.get_person(employee_id)
        if person is None:
            raise IdfError(
                "PERSON_NOT_FOUND",
                f"No person with employee_id {employee_id}.",
                "Use resolve_person to find the employee_id.",
            )
        return person

    def _refresh(self, plan: Plan) -> Plan:
        if plan.status in ("pending_approval", "approved") and self.now() > datetime.fromisoformat(plan.expires_at):
            plan.status = "expired"
            self.store.put_plan(plan)
            self.audit.append("plan.expired", self.actor, plan.plan_id)
        return plan

    def _valid_approvers(self, plan: Plan) -> set[str]:
        valid = set()
        for approver, signature in self.store.approval_signatures(plan.plan_id):
            try:
                principal = approvals.verify("approve", plan.plan_id, signature, self.allowed_signers)
            except IdfError:
                continue
            if principal == approver and principal != plan.requested["on_behalf_of"]:
                valid.add(principal)
        return valid

    def _execute(self, op: Op) -> None:
        if op.op == "disable_account":
            self.directory.disable_account(op.target)
        elif op.op == "terminate_session":
            self.hosts.terminate_session(op.target)
        elif op.op == "remove_group":
            assert op.group is not None
            self.directory.remove_group(op.target, op.group)
