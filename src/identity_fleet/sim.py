"""In-memory-style simulator for the Directory and Hosts ports, persisted in the shared store."""

import json
from pathlib import Path

from .domain import Account, IdfError, Person, Session
from .store import Store

DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "sim.json"


class SimDirectory:
    def __init__(self, store: Store):
        self.store = store

    def find_accounts(self, employee_id: str) -> list[Account]:
        return [a for a in self.store.all("accounts", Account) if a.owner == employee_id]

    def _account(self, account_id: str) -> Account:
        account = self.store.get("accounts", account_id, Account)
        if account is None:
            raise IdfError("TARGET_MISSING", f"Account {account_id} does not exist.")
        return account

    def disable_account(self, account_id: str) -> None:
        account = self._account(account_id)
        account.enabled = False
        self.store.put("accounts", account_id, account)

    def remove_group(self, account_id: str, group: str) -> None:
        account = self._account(account_id)
        account.groups = [g for g in account.groups if g != group]
        self.store.put("accounts", account_id, account)

    def add_group(self, account_id: str, group: str) -> None:
        """Out-of-band change; not part of the port. Used to simulate drift."""
        account = self._account(account_id)
        if group not in account.groups:
            account.groups.append(group)
        self.store.put("accounts", account_id, account)


class SimHosts:
    def __init__(self, store: Store):
        self.store = store

    def list_sessions(self, account_id: str) -> list[Session]:
        return [s for s in self.store.all("sessions", Session) if s.account_id == account_id]

    def terminate_session(self, session_id: str) -> None:
        self.store.delete("sessions", session_id)


def load_fixture(store: Store, path: Path = DEFAULT_FIXTURE) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    store.reset()
    for p in data["persons"]:
        person = Person.model_validate(p)
        store.put("persons", person.employee_id, person)
    for a in data["accounts"]:
        account = Account.model_validate(a)
        store.put("accounts", account.account_id, account)
    for s in data["sessions"]:
        session = Session.model_validate(s)
        store.put("sessions", session.session_id, session)
    store.put("meta", "tier0_groups", data["tier0_groups"])
