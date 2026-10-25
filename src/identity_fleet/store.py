"""SQLite state shared by the CLI and MCP server processes."""

import json
import sqlite3
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .domain import Approval, IdfError, Person, Plan, Run, compute_plan_id

M = TypeVar("M", bound=BaseModel)

_TABLES = ("persons", "accounts", "sessions", "plans", "runs", "meta")


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        with self.conn:
            for table in _TABLES:
                self.conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, doc TEXT NOT NULL)")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS approvals ("
                "plan_id TEXT, approver TEXT, signature TEXT, at TEXT, PRIMARY KEY (plan_id, approver))"
            )

    def reset(self) -> None:
        with self.conn:
            for table in (*_TABLES, "approvals"):
                self.conn.execute(f"DELETE FROM {table}")

    # Generic JSON documents. Table names come from code, never from input.
    def put(self, table: str, key: str, doc: BaseModel | Any) -> None:
        text = doc.model_dump_json() if isinstance(doc, BaseModel) else json.dumps(doc)
        with self.conn:
            self.conn.execute(f"INSERT OR REPLACE INTO {table} (id, doc) VALUES (?, ?)", (key, text))

    def get(self, table: str, key: str, model: type[M]) -> M | None:
        row = self.conn.execute(f"SELECT doc FROM {table} WHERE id = ?", (key,)).fetchone()
        return model.model_validate_json(row[0]) if row else None

    def all(self, table: str, model: type[M]) -> list[M]:
        rows = self.conn.execute(f"SELECT doc FROM {table} ORDER BY id").fetchall()
        return [model.model_validate_json(r[0]) for r in rows]

    def delete(self, table: str, key: str) -> None:
        with self.conn:
            self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (key,))

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT doc FROM meta WHERE id = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # Persons (desired-state input)
    def get_person(self, employee_id: str) -> Person | None:
        return self.get("persons", employee_id, Person)

    def persons(self) -> list[Person]:
        return self.all("persons", Person)

    # Plans
    def put_plan(self, plan: Plan) -> None:
        self.put("plans", plan.plan_id, plan)

    def get_plan(self, plan_id: str) -> Plan:
        plan = self.get("plans", plan_id, Plan)
        if plan is None:
            raise IdfError("PLAN_NOT_FOUND", f"No plan with id {plan_id}.", "Create one with plan_leaver.")
        if plan.plan_id != plan_id or compute_plan_id(plan.intent, plan.ops) != plan_id:
            raise IdfError(
                "PLAN_TAMPERED",
                f"Stored plan {plan_id} does not match its id; it was modified after creation.",
                "Do not apply it. Investigate the state store and create a new plan.",
            )
        return plan

    # Approvals
    def add_approval(self, plan_id: str, approver: str, signature: str, at: str) -> bool:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO approvals (plan_id, approver, signature, at) VALUES (?, ?, ?, ?)",
                    (plan_id, approver, signature, at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def approvals(self, plan_id: str) -> list[Approval]:
        rows = self.conn.execute(
            "SELECT approver, at FROM approvals WHERE plan_id = ? ORDER BY at", (plan_id,)
        ).fetchall()
        return [Approval(approver=r[0], at=r[1]) for r in rows]

    def approval_signatures(self, plan_id: str) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT approver, signature FROM approvals WHERE plan_id = ?", (plan_id,)
        ).fetchall()

    def clear_approvals(self, plan_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM approvals WHERE plan_id = ?", (plan_id,))

    # Runs
    def put_run(self, run: Run) -> None:
        self.put("runs", run.run_id, run)

    def latest_run(self, plan_id: str) -> Run | None:
        runs = [r for r in self.all("runs", Run) if r.plan_id == plan_id]
        return max(runs, key=lambda r: r.started_at) if runs else None
