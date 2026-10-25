# M1 — Real directory, joiner and mover

Status: proposed · 2026-10-25 · Related: [PLAN.md](../PLAN.md) §7,
[M0 design](M0-walking-skeleton.md)

**M1 is done when:**

1. One contract test suite passes against **both** the simulator and Samba AD, locally
   and in GitHub Actions.
2. Joiner, mover and leaver all run through the M0 write path (plan → policy →
   approve → apply → verify), on both backends.
3. The LDAP adapter runs under a **delegated service account**, not Administrator.

Everything here is a hypothesis. §12 lists the spikes that may change it.

---

## 1. One planner, three intents

M0 derived only the "left" state. M1 derives the active state too, so all three intents
become patches to the person record. The diff engine stays the same:

| Intent | Patch | Typical ops |
|---|---|---|
| joiner | new person record | create account → add groups → enable |
| mover | `department: A → B` | remove old groups → add new groups |
| leaver | `status: active → left` | disable → end sessions → remove groups |

`apply_intent(person | None, intent) → Person` is the only place where intents differ.
The planner calls it to build the target person, and `apply` calls it to commit the
patch.

### One phase order for every op

| Phase | Op | Why here |
|---|---|---|
| 1 | `disable_account` | Block new logins first |
| 2 | `terminate_session` | Then end live sessions so the user cannot reconnect |
| 3 | `remove_group` | Remove before adding, so a move never passes through a state with both old and new access |
| 4 | `create_account` | |
| 5 | `add_group` | |
| 6 | `enable_account` | Last, so an account is never usable with only part of its access |

Every intent uses the same order. There are no per-intent scripts.

## 2. Desired state for an active person

```
desired_groups = catalog.base ∪ catalog.departments[person.department]
managed        = catalog.base ∪ all department groups
```

| Observed | Ops |
|---|---|
| no account | `create_account`, `add_group` × desired, `enable_account` |
| account disabled | `enable_account` (escalated, see §5) |
| `g ∈ managed`, `g ∉ desired` | `remove_group(g)` |
| `g ∈ desired`, `g ∉ account.groups` | `add_group(g)` |
| `g ∉ managed` | **untouched**, listed in the plan's `unmanaged` field |

**Managed vs unmanaged groups.** IdentityFleet changes only the groups its catalog
knows. A group someone added by hand outside the catalog is never silently removed by a
mover. It does show up in the plan as `unmanaged`, so the reviewer sees it. A leaver
still removes every group, as in M0.

More than one account per person (for example a separate admin account) is out of
scope. The planner refuses with `MULTIPLE_ACCOUNTS` instead of guessing.

## 3. Role catalog

The catalog is data, not code. It is loaded from `catalog.json` (in the fixture for
now; later a versioned file in Git):

```json
{
  "base": ["GG-AllStaff"],
  "departments": {
    "Marketing": ["GG-Marketing"],
    "Finance":   ["GG-Finance", "GG-AP-Approve"],
    "Treasury":  ["GG-Treasury", "GG-AP-Pay"]
  },
  "sod_conflicts": [["GG-AP-Approve", "GG-AP-Pay"], ["GG-AP-Approve", "GG-Vendor-Admin"]],
  "tier0_groups": ["Domain Admins", "Enterprise Admins", "Schema Admins"]
}
```

Loading validates the catalog and rejects it if:

- any department group is a Tier 0 group. **Lifecycle never grants Tier 0.**
- any single department (including base groups) grants both sides of a SoD pair.

Validation alone cannot catch every conflict at plan time. A person's **final** state is
`desired ∪ unmanaged`, and a hand-added unmanaged group (such as `GG-Vendor-Admin`
above) can conflict with a department's groups. That is what `sod.deny` checks.

## 4. Joiner details

### Login names

The login is chosen **at plan time** and written into the `create_account` op. It is
therefore part of the plan hash and visible to the approver.

1. Fold the name: Turkish dotless i, then strip diacritics (`Şule Çelik` → `sule.celik`).
2. Keep `[a-z0-9.]` only.
3. Fit the 20-character `sAMAccountName` limit.
4. On collision, append `2`, `3`, … and truncate the base so the result still fits.

If someone takes the same login between plan and apply, the re-diff at apply time picks a
different login. That op was not approved, so it is reported as residual and nothing is
created. This is the M0 invariant at work.

### The initial password: agents never see secrets

The account is created with a random password that **no one** sees. It comes from
`secrets` and is never stored, logged or returned. First-time credential issuance (a
service-desk reset, a temporary access pass, a certificate) is a separate, later
concern.

This adds a project-wide invariant, **D9: no tool output, audit entry or error ever
contains a secret.** A test scans every MCP tool result and audit line in the suite for
password-like fields.

### Input validation

`plan_joiner` takes `employee_id`, `given_name`, `surname` and `department`.

- An unknown department returns `UNKNOWN_DEPARTMENT`, with the valid list in `details`.
- An existing `employee_id` returns `PERSON_EXISTS`.
- Empty or whitespace-only names are rejected.

## 5. Policy additions

| Rule | Effect |
|---|---|
| `joiner.requires_approval` | 1 approval |
| `mover.requires_approval` | 1 approval |
| `sod.deny` | Final groups (`desired ∪ unmanaged`) contain a conflicting pair → deny |
| `reenable.escalate` | Plan enables a disabled account → 2 approvals. Re-enabling is a classic way back in |
| `tier0.manual` | Every op on an account holding a Tier 0 group is **manual** (see below) |

Risk per op: Tier 0 subject → `critical`; create, enable or disable account → `high`;
add group or terminate session → `medium`; remove group → `low`. A plan's risk is
the highest of its ops.

### Tier 0 subjects: IdentityFleet plans and verifies, a human executes

A delegated service account cannot change Tier 0 accounts. AD's AdminSDHolder resets
their permissions, and that protection is correct. So for Tier 0 subjects:

- Ops are emitted with `executor: "manual"`. The field is part of the op, so it is
  covered by the plan hash.
- `apply` does not attempt manual ops. It reports them as `manual_required`, with the
  exact action needed, and the run status becomes `awaiting_manual`.
- After a Tier 0 admin performs them, calling `apply` again re-runs the diff. The
  manual ops now show up as `already_satisfied`, and the run becomes `verified`.

Re-running apply is the verification. No new mechanism is needed. This replaces M0's
behaviour, where the simulator executed Tier 0 ops directly.

## 6. Directory port v2

```python
class Directory(Protocol):
    def find_accounts(self, employee_id: str) -> list[Account]: ...
    def login_exists(self, login: str) -> bool: ...
    def create_account(self, spec: NewAccount) -> None: ...   # created disabled
    def enable_account(self, account_id: str) -> None: ...
    def disable_account(self, account_id: str) -> None: ...
    def add_group(self, account_id: str, group: str) -> None: ...
    def remove_group(self, account_id: str, group: str) -> None: ...
```

- `account_id` stays `dir:<login>` on every backend.
- The anchor is the `employeeID` attribute, never the name.
- Every write is idempotent.
- `TARGET_MISSING` is raised for an unknown account, `GROUP_MISSING` for an unknown
  group.

Renames are out of scope for M1.

## 7. LDAP adapter (Samba AD now, Windows AD in M4)

| Port call | LDAP |
|---|---|
| `find_accounts` | search `(&(objectClass=user)(employeeID=<id>))` across the domain, so accounts created outside our OU are still found |
| `login_exists` | search `(sAMAccountName=<login>)` |
| `create_account` | `add` under `OU=Users,OU=IdentityFleet`, `userAccountControl=0x202` (disabled), then set `unicodePwd` to a random password |
| `enable` / `disable_account` | read `userAccountControl`, flip bit `0x2` |
| `add` / `remove_group` | modify the **group's** `member` attribute (`memberOf` is a read-only back-link) |

Directory facts the adapter has to respect:

- **Primary group.** "Domain Users" membership comes from `primaryGroupID` and never
  appears in `memberOf`, so the planner never sees or touches it. That is correct
  behaviour, and it needs a test.
- **Direct membership only.** Nested groups appear in `explain_access` later, not in
  the diff.
- **Transport.** LDAPS only, with the server certificate validated against the
  directory's CA. In CI the CA comes from the container. Never `ssl verify = none`.
  Samba also refuses simple binds over plain LDAP by default, which is good.
- **Credentials.** The service account's bind credentials come from the environment
  (`IDF_LDAP_URL`, `IDF_LDAP_BIND_DN`, `IDF_LDAP_PASSWORD`, `IDF_LDAP_CA`). They are
  never written to the state store. A Kerberos/gMSA bind is a later improvement.
- **Effective access.** Group changes take effect at the user's next logon or ticket
  refresh, because of the Kerberos PAC. M2 measures this. M1 only proves the directory
  state.

### Least privilege (D7)

The bind account `svc-identityfleet` gets delegated rights on `OU=IdentityFleet` only:

- create and modify user objects,
- write `userAccountControl` and reset passwords,
- write `member` on groups in `OU=IdentityFleet,OU=Groups`.

It is not a member of any privileged group. The CI setup script creates this
delegation, so every CI run proves the adapter works without admin rights.

## 8. Contract tests

```
tests/
  test_m0.py              existing, unchanged except the Tier 0 test (§5)
  test_m1.py              joiner/mover/catalog/login/policy tests on the simulator
  contract/
    conftest.py           `directory` fixture parametrised: sim, ldap
    test_directory.py     the port contract
    test_lifecycle.py     joiner → mover → leaver through Core, on each backend
```

- The `ldap` parameter runs only when `IDF_LDAP_URL` is set. Otherwise it is
  **skipped and reported as skipped**, never silently passed.
- **Isolation.** Each test uses unique employee IDs and logins (a random suffix), and
  cleans up the objects it created. Tests never depend on each other or on run order.
- **What the contract covers:** find by anchor; create (account starts disabled);
  enable/disable idempotent; add/remove group idempotent; unknown account →
  `TARGET_MISSING`; unknown group → `GROUP_MISSING`; `login_exists`; the primary
  group is invisible.

This suite is how we prove the simulator is faithful. Whenever Samba behaves
differently, we fix the simulator, not the test.

## 9. Running Samba AD

- **Local:** Docker Desktop (installed on the dev machine, currently stopped), using
  `scripts/samba-up` to start the container, wait for provisioning, create the OU tree,
  groups, delegation and service account, and export the CA certificate.
- **CI:** a GitHub Actions job runs the same script, then
  `uv run pytest tests/contract`. The simulator-only tests run in a separate job, so a
  Samba problem never hides a core regression.

## 10. MCP surface

Two new tools:

| Tool | Annotations |
|---|---|
| `plan_joiner(employee_id, given_name, surname, department, reason, on_behalf_of, ticket?)` | not destructive, idempotent |
| `plan_mover(employee_id, new_department, reason, on_behalf_of, ticket?)` | not destructive, idempotent |

Plans gain an `unmanaged` field. Runs gain `manual_required` results and the
`awaiting_manual` status.

New error codes: `UNKNOWN_DEPARTMENT`, `PERSON_EXISTS`, `MULTIPLE_ACCOUNTS`,
`GROUP_MISSING`, `TARGET_MISSING`.

## 11. Acceptance tests

1. Contract suite green on the simulator and on Samba AD (CI job).
2. Joiner: the plan shows the login, groups and the enable op last. After apply, the
   account exists, is enabled and has exactly the desired groups.
3. Login collision: a second "Ayşe Yılmaz" gets `ayse.yilmaz2`. A name longer than 20
   characters is truncated correctly.
4. Mover: old managed groups are removed and new ones added, in that order. A
   hand-added unmanaged group survives and is listed as `unmanaged`.
5. A mover into Finance is denied (`sod.deny`) when the person holds a hand-added
   `GG-Vendor-Admin`. A catalog in which one department grants both sides of a SoD pair
   fails to load.
6. Re-enabling a disabled account needs 2 approvals.
7. Tier 0 leaver → `awaiting_manual`. After a simulated manual change, re-apply →
   `verified`.
8. Login taken between plan and apply → nothing is created, residual is reported.
9. No MCP tool result or audit line contains a secret.
10. The full lifecycle on Samba AD runs under `svc-identityfleet`, not Administrator.

## 12. Spikes (time-boxed; each ends in a note in this file)

| Spike | Question | Fallback |
|---|---|---|
| Samba image | `quay.io/samba.org/samba-ad-server` (needs `--privileged`, sambacc config) vs a community image with env-var setup | Whichever provisions reliably in under 60 s in Actions |
| Delegation on Samba | Can `samba-tool dsacl set` express the §7 rights cleanly? | Narrower rights plus a documented gap. Never fall back to Administrator silently |
| LDAP library | ldap3 (pure Python, Windows-friendly; 2.9.1 stable, 2.10 in RC) | `msldap`/`badldap` behind the same port |
| Windows dev loop | Does Docker Desktop's `--privileged` work for Samba's xattrs on WSL2? | Run the contract job only in CI |

## 13. Not in M1

Hosts and sessions on real machines (M2), scheduled leavers, renames, admin accounts,
nested group reasoning, `explain_access`, Windows AD itself (M4), and credential
issuance.

## 14. Dependencies

- `ldap3`: new runtime dependency (needs approval).
- Docker: for local Samba runs only. CI uses the GitHub runner's Docker.
