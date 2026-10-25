"""Policy rules. Each returns a decision or None; the strictest decision wins."""

from .domain import Account, Decision, Intent, Op, Person, Risk


def evaluate(
    intent: Intent, ops: list[Op], person: Person, accounts: list[Account], tier0_groups: set[str]
) -> list[Decision]:
    decisions: list[Decision] = []
    if not ops:
        return decisions
    if "break_glass" in person.tags:
        decisions.append(
            Decision(rule="break_glass.deny", effect="deny", reason="Break-glass accounts are managed manually.")
        )
    if intent.kind == "leaver":
        decisions.append(
            Decision(
                rule="leaver.requires_approval", effect="require_approval", approvals=1, reason="Disables an account"
            )
        )
    held = sorted({g for a in accounts for g in a.groups} & tier0_groups)
    if held:
        decisions.append(
            Decision(
                rule="tier0.escalate",
                effect="require_approval",
                approvals=2,
                reason=f"Subject holds Tier 0 groups: {', '.join(held)}",
            )
        )
    return decisions


def risk_of(ops: list[Op], decisions: list[Decision]) -> Risk:
    if any(d.rule == "tier0.escalate" for d in decisions):
        return "critical"
    kinds = {o.op for o in ops}
    if "disable_account" in kinds:
        return "high"
    if "terminate_session" in kinds:
        return "medium"
    return "low"


def approvals_required(decisions: list[Decision]) -> int:
    return max((d.approvals for d in decisions), default=0)
