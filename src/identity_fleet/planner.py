"""Desired state vs observed state. The diff is the plan; an op's postcondition is "no longer in the diff"."""

from dataclasses import dataclass

from .domain import Account, Op, Person, Session
from .ports import Directory, Hosts


@dataclass
class Observed:
    accounts: list[Account]
    sessions: list[Session]

    def has_target(self, op: Op) -> bool:
        ids = {a.account_id for a in self.accounts} | {s.session_id for s in self.sessions}
        return op.target in ids


def observe(person: Person, directory: Directory, hosts: Hosts) -> Observed:
    accounts = directory.find_accounts(person.employee_id)
    sessions = [s for a in accounts for s in hosts.list_sessions(a.account_id)]
    return Observed(accounts, sessions)


def diff(person: Person, observed: Observed) -> list[Op]:
    """Ops needed to move observed state to the person's desired state.

    Order: block new logins, then end live sessions so the user cannot reconnect, then strip access.
    """
    # shortcut: only the "left" state is derived; active-person desired groups (role catalog) arrive with mover/joiner in M1.
    if person.status != "left":
        return []
    ops = [Op(op="disable_account", target=a.account_id, phase=1) for a in observed.accounts if a.enabled]
    ops += [Op(op="terminate_session", target=s.session_id, phase=2) for s in observed.sessions]
    ops += [
        Op(op="remove_group", target=a.account_id, group=g, phase=3)
        for a in observed.accounts
        for g in a.groups
    ]
    return sorted(ops, key=lambda o: (o.phase, o.target, o.group or ""))
