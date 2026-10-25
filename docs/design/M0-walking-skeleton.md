# M0 — Walking skeleton

Status: proposed · 2026-10-25 · Related: [PLAN.md](../PLAN.md) §7

**M0 is done when** one leaver flows end to end on the simulator through
`resolve → plan → policy → approve → apply → verify`, driven from both MCP and the
CLI, and leaves a verifiable audit trail.

Everything here is a hypothesis. If building it shows a better shape, change this
document first, then the code.

---

## 1. The core idea: intents are patches, plans are diffs

We do not write a "leaver script". IdentityFleet keeps a **person record** (the
desired-state input) and derives what every system *should* look like from it:

```
desired = derive(person_record, role_catalog)
plan    = diff(desired, observed)          # observed = what adapters report
```

A lifecycle intent is just a patch to the person record:

| Intent | Patch | Notes |
|---|---|---|
| leaver | `status: active → left` | **M0** |
| mover | `department: A → B` | M1 |
| joiner | new record | M1 |
| reconcile | no patch | M5, same diff engine |

So joiner, mover, leaver and drift detection share **one** planner. M0 builds that
planner and exercises it with the leaver patch only.

The patch is committed to the person store only when the plan is applied, so a
rejected plan changes nothing.

## 2. Domain model

```
Person        employee_id (immutable anchor), given_name, surname, department,
              status: active | left, tags: {break_glass, ...}

Account       account_id, system ("directory"), login, enabled,
              groups: set[str], owner: employee_id

Session       session_id, host, account_id, started_at

RoleCatalog   department → set[group]          (+ base groups for everyone)
              tier0_groups: set[group]         (e.g. Domain Admins)
```

Desired state for a person:

| status | account | groups | sessions |
|---|---|---|---|
| active | enabled | `base ∪ catalog[department]` | any |
| left | disabled | ∅ | none |

M0 leaves out devices and certificates. They come back as new op types in M2–M4.

### Operations

A plan is an ordered list of typed, idempotent operations. Each op has a
**precondition** (still needed?) and a **postcondition** (used by verify).

| Op | Phase | Postcondition |
|---|---|---|
| `disable_account(account)` | 1 | `account.enabled == false` |
| `terminate_session(session)` | 2 | session absent |
| `remove_group(account, group)` | 3 | `group ∉ account.groups` |

The order matters: block new logins first, then kill live sessions so the user
cannot reconnect, then strip memberships. Removed memberships are recorded in the
audit log so access can be restored later if the leaver is reversed.

## 3. Plan contract

```json
{
  "plan_id": "pln_3f9a51c2e0b7",
  "intent": { "kind": "leaver", "employee_id": "E1042",
              "reason": "Resigned", "ticket": "HR-881" },
  "ops": [
    { "op": "disable_account",   "target": "dir:ayse.yilmaz",           "phase": 1 },
    { "op": "terminate_session", "target": "lnx-03/4211",                "phase": 2 },
    { "op": "remove_group",      "target": "dir:ayse.yilmaz", "group": "GG-Marketing", "phase": 3 }
  ],
  "risk": "high",
  "approvals_required": 1,
  "policy": [ { "rule": "leaver.requires_approval", "effect": "require_approval",
                "approvals": 1, "reason": "Disables an account" } ],
  "requested": { "on_behalf_of": "hr:selin.k", "agent": "agent:reference" },
  "status": "pending_approval",
  "created_at": "2026-10-25T10:00:00Z",
  "expires_at": "2026-10-26T10:00:00Z"
}
```

### Content-addressed plans

`plan_id` is a hash of the canonical JSON of `intent` + `ops`. This gives us three
things with no extra machinery:

- **Idempotency.** The same request against the same state returns the same plan, so
  an agent that retries does not create duplicates. No idempotency keys needed.
- **Approve exactly this.** An approval is bound to the hash. If the stored plan is
  altered, its hash no longer matches and it is refused (`PLAN_TAMPERED`).
- **Readable audit.** Every audit event refers to one immutable plan.

### Status machine

```
pending_approval ──approve(n)──▶ approved ──apply──▶ applying ──▶ verified
       │                                                   ├──▶ verified_with_residual
       ├──reject──▶ rejected                               └──▶ failed
       └──timeout─▶ expired
```

Plans that need zero approvals go straight to `approved`. A leaver never needs zero.

### Apply rule: re-diff, never exceed the approval

The world can change between approval and apply. Instead of a strict version lock
(which would make every unrelated change invalidate the plan), `apply` re-runs the
diff and then:

| Case | Action |
|---|---|
| Approved op still needed | execute |
| Approved op already satisfied | skip (`already_satisfied`) |
| Approved op no longer possible (target gone) | skip (`target_missing`) |
| New op needed that was **not** approved | **never executed**; reported as residual, status `verified_with_residual`, suggest a new plan |

The invariant: **apply never performs an operation that a human did not approve.**

### Verify

After the ops run, verify re-reads every target through the adapter and checks each
op's postcondition. The run records `time_to_revoke = verified_at − approved_at`.
Any failed postcondition makes the run `failed`, with the failing op named.

## 4. Policy (M0 rules)

Rules are plain functions `(plan, context) → Decision(rule, effect, reason)`. The
strictest effect wins.

| Rule | Effect |
|---|---|
| `leaver.requires_approval` | require 1 approval |
| `tier0.escalate`: subject holds a Tier 0 group | risk `critical`, require 2 approvals |
| `break_glass.deny`: subject is tagged `break_glass` | deny (`POLICY_DENIED`) |
| `approval.not_self`: approver ≠ `on_behalf_of`, approvers distinct | enforced at approval time |

Blast-radius budgets come in M3, when an agent can generate volume.

## 5. Approval is out of band

There is **no approve tool on the MCP server.** Approval happens only through the
CLI:

```
idf plan show pln_3f9a51c2e0b7
idf plan approve pln_3f9a51c2e0b7
```

Proposed for M0: **signed approvals with SSH keys**, using tooling already on every
Windows 11 and Linux machine (OpenSSH, no new dependency):

- The CLI signs the `plan_id` with `ssh-keygen -Y sign -n identity-fleet-approval`.
- The core verifies it with `ssh-keygen -Y verify` against an `allowed_signers` file
  that maps keys to approver identities.
- Recommended keys are FIDO (`ed25519-sk`, needs a touch) or passphrase-protected,
  so an agent running as the same OS user cannot sign silently.

Fallback if this turns out too heavy for M0: record the OS user as the approver, and
document the gap in the threat notes.

## 6. Identity of the caller (M0 honesty)

- The MCP server has a configured `agent_id`. Every event records it.
- `on_behalf_of` is a tool argument, so in M0 it is a **claim**, not proof. The audit
  log stores it as `claimed`.
- Binding the human behind the agent properly (OAuth on-behalf-of, MCP
  Enterprise-Managed Authorization) is planned for M3.

## 7. MCP tools (M0)

| Tool | Annotations | Returns |
|---|---|---|
| `resolve_person(query)` | read-only | candidates: `employee_id`, name, department, status, match reason |
| `get_identity(employee_id)` | read-only | person + accounts + groups + sessions |
| `plan_leaver(employee_id, reason, ticket?, on_behalf_of)` | not destructive, idempotent | plan (§3) |
| `get_plan(plan_id)` | read-only | plan + approvals + latest run |
| `apply_plan(plan_id)` | destructive, idempotent | run: per-op result, residual, `time_to_revoke` |

Rules:

- Write tools take `employee_id` only, never a name. `resolve_person` is the only
  fuzzy entry point.
- Tools are listed in a fixed order.
- An empty diff is not an error. `plan_leaver` returns a plan with no ops and status
  `noop`.

### Errors

Every error has the same shape and tells the agent what to do next:

```json
{ "code": "PLAN_NOT_APPROVED",
  "message": "Plan pln_3f9a51c2e0b7 needs 1 more approval.",
  "next_step": "Ask a human approver to run: idf plan approve pln_3f9a51c2e0b7",
  "details": { "approvals_required": 1, "approvals": 0 } }
```

M0 codes: `PERSON_NOT_FOUND`, `PLAN_NOT_FOUND`, `PLAN_NOT_APPROVED`, `PLAN_EXPIRED`,
`PLAN_TAMPERED`, `POLICY_DENIED`.

## 8. Audit log

- Append-only JSONL file. Each line includes `seq`, `ts`, `event`, `actor`,
  `plan_id`, `data`, `prev_hash` and `hash`, forming a **hash chain**.
- `idf audit verify` walks the chain and reports the first broken link.
- Events: `plan.created`, `plan.approved`, `plan.rejected`, `run.started`,
  `op.applied`, `op.skipped`, `op.failed`, `run.verified`, `run.failed`.
- SIEM forwarding and OpenTelemetry come later. The JSONL format already suits both.

## 9. Ports and the simulator

```python
class Directory(Protocol):
    def find_accounts(self, employee_id: str) -> list[Account]: ...
    def disable_account(self, account_id: str) -> None: ...
    def remove_group(self, account_id: str, group: str) -> None: ...

class Hosts(Protocol):
    def list_sessions(self, account_id: str) -> list[Session]: ...
    def terminate_session(self, session_id: str) -> None: ...
```

The simulator implements both ports on top of the same SQLite file the core uses.
That way the MCP server and the CLI, running as separate processes, see the same
world. Its fixture (`fixtures/sim.json`) seeds:

- two people named Ayşe Yılmaz (exercises `resolve_person` ambiguity),
- one Domain Admins member (Tier 0 escalation),
- one break-glass account (deny),
- one person with sessions on two hosts,
- names with Turkish characters.

Adapter methods are idempotent, matching the real directory semantics we will test
in M1.

## 10. Code layout (M0 subset)

```
src/identity_fleet/
  domain.py        Person, Account, Session, Op, Plan, Run (pydantic)
  planner.py       derive(), diff(), plan_leaver()
  policy.py        rules + evaluate()
  executor.py      apply(), verify()
  approvals.py     ssh-keygen sign/verify
  audit.py         hash-chained JSONL
  store.py         SQLite: persons, plans, approvals, runs
  ports.py         Directory, Hosts
  sim.py           simulator adapter
  mcp_server.py    tools from §7
  cli.py           idf: plan show/approve/reject, apply, audit verify, sim reset
tests/
fixtures/sim.json
```

Flat modules on purpose. They are split into the packages from the plan when a module
grows beyond one concern.

**Dependencies:** `mcp` (Python SDK v2; brings pydantic), plus `pytest` for
development. Everything else uses the standard library (`sqlite3`, `argparse`,
`hashlib`, `json`, `subprocess` for `ssh-keygen`).

## 11. Acceptance tests

1. A leaver plan orders its ops as disable → sessions → groups.
2. `apply_plan` without approval returns `PLAN_NOT_APPROVED`.
3. An approval by the `on_behalf_of` person is refused.
4. Approve then apply returns `verified`, and `idf audit verify` passes.
5. Applying the same plan again skips every op as `already_satisfied`.
6. A group added out of band after approval is **not** removed and is reported as
   residual.
7. An edited stored plan is refused with `PLAN_TAMPERED`.
8. A Tier 0 subject needs 2 distinct approvers. A break-glass subject is denied.
9. Editing one audit line makes `idf audit verify` fail at that line.
10. Over an in-memory MCP client: the tool list has no approve tool, and the full
    leaver flow works end to end.

## 12. Deliberately not in M0

Scheduled (future-dated) leavers, joiner and mover, devices, certificates, async
jobs, blast-radius budgets, `explain_access`, real authentication of the caller, and
any real directory.

## 13. Open questions for M0

- Is `ssh-keygen -Y` signing smooth enough on Windows for approvers, or should M0 use
  the OS-user fallback?
- Should `plan_leaver` also accept an `effective_at` now, so the contract does not
  change when scheduled leavers arrive? (Current answer: no. Add it when we
  build the scheduler.)
- Should the audit log live in SQLite for atomicity with plan state, or stay JSONL for
  readability? (Current answer: JSONL, written after the state commit. If a crash
  happens in between, recovery must re-emit the event. Revisit if that proves fragile.)
