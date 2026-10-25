# IdentityFleet — Plan

> **This is a compass, not a script.** It fixes the goal, the few decisions that are
> expensive to reverse, and the questions we must answer. Everything else is a
> starting hypothesis. When we find a better way, we change this document instead of
> defending it.
>
> Guiding question at every step: **can we do better than this?**

Status: design phase · Last reviewed: 2026-10-25

---

## 1. Purpose

Make employee identity and device lifecycle (**joiner → mover → leaver**) across
Windows and Linux **correct, provable and safe to hand to an AI agent**.

Three claims the project must be able to prove, with tests and not slides:

1. **Correct:** after any lifecycle event, the actual state of AD, Linux hosts,
   certificates and sessions matches the desired state.
2. **Provable:** every change has an audit trail (who asked, which agent planned it,
   who approved it, what changed) and every leaver has a measured time-to-revoke.
3. **Agent-safe:** an LLM agent can operate the system through MCP without being able
   to exceed its policy, even under prompt injection.

## 2. Who we serve

| Persona | Need | What they get |
|---|---|---|
| IT/sysadmin team (on-prem AD + Linux, no budget for SailPoint/Saviynt) | Reliable JML without a heavy IGA suite | Small, self-hostable service with a lab to try it |
| Platform/security team adopting AI agents | Let agents act on identity infrastructure without handing them Domain Admin | Governed MCP server: plan → policy → approve → apply |
| Auditors / compliance (ISO 27001, CIS, KVKK) | Evidence that access is granted and removed correctly and on time | Append-only audit log, time-to-revoke reports, control mapping |
| Hiring managers / reviewers | Proof of real systems and agent-safety skill | One-command lab, CI on a real directory, published evals |

The last row is honest: early on, most readers will be reviewers. We still design for
the first three, because that is what makes the project credible to reviewers.

## 3. Landscape and where we can do better

What exists (October 2026):

| Category | Examples | Gap we can fill |
|---|---|---|
| IGA suites | SailPoint, Saviynt, Omada, One Identity, Entra ID Governance | Heavy, expensive; hide the last-mile edge cases (SSSD cache, sessions, CRL) |
| Open-source IGA / directory | midPoint, Apache Syncope, FreeIPA | Mature but heavy to learn; not designed as agent tool surfaces |
| Vendor MCP servers | Okta (managed + open-source MCP server, OAuth-scope bound), Microsoft MCP Server for Enterprise (Graph, **read-only**, preview) | Reads are covered; **governed writes** are not: no plan/approve/apply, no risk tiers, no post-change verification |
| Agent governance middleware | Microsoft Agent Governance Toolkit (YAML/Rego/Cedar policies, community preview), RSA Agent ID | Generic; knows nothing about identity semantics (toxic combos, Tier 0 groups, leaver SLAs) |

Where we aim to be better, concretely:

1. **Governed writes over MCP.** Every mutation is a plan with a diff, a risk tier and
   a server-minted `plan_id`; only an approved, unchanged plan can be applied.
2. **Closed loop.** After `apply`, the system verifies the effect (login actually
   fails, session actually gone, cert actually revoked) and reports time-to-revoke.
3. **Approval outside the agent channel.** The agent can request approval but can
   never deliver it. Approvals come from an authenticated human on a separate path.
4. **Identity-aware policy.** Policies speak the domain: Tier 0 groups, separation of
   duties, leaver deadlines, blast-radius budgets (for example max N leavers per hour
   without a second approver).
5. **Fewer revocations needed.** Prefer short-lived credentials (SSH certificates,
   short-lived X.509) so offboarding depends less on CRLs and cache expiry.
6. **`explain_access`.** Answer "why does this person have this access?" as a path
   (person → role → group → resource). Useful to agents, admins and auditors alike.
7. **Simulator with proven fidelity.** An in-memory backend that passes the same
   contract test suite as the real directory, so agents and CI run in seconds.
8. **Published evals.** Scenario suite (correct, ambiguous, unauthorized, injected)
   with results in the repo.

If we find that an existing tool already does one of these well, we integrate with it
instead of rebuilding it.

## 4. Decisions

We split decisions by how expensive they are to reverse.

### 4.1 One-way doors (decided now, change only with an ADR)

| # | Decision | Why |
|---|---|---|
| D1 | **The product is the core + MCP server, not the agent.** The agent is a reference client. | Any MCP client (Claude, others) should be able to use it; value must not depend on one model. |
| D2 | **All writes go through `resolve → plan → policy → approve → apply → verify`.** No other write path exists, for humans or agents. | One path means one place to secure, test and audit. |
| D3 | **Ports and adapters.** The core talks to directories, hosts and PKI only through interfaces. | Lets us swap AD/Samba/FreeIPA/sim, and isolates library risk. |
| D4 | **Desired state + reconciliation**, not fire-and-forget commands. | Detects drift and makes retries and partial failures tractable. |
| D5 | **Agents get typed, intent-level tools. Reads may be flexible (allow-listed LDAP queries, JEA, diagnostic commands); writes are never free-form.** | Keeps policy meaningful and limits the blast radius of prompt injection. |
| D6 | **Audit log is append-only and records requester, agent, approver and plan hash.** | Required for both compliance and agent accountability. |
| D7 | **The system is treated as a Tier 0 asset.** Least-privilege delegated service accounts, never Domain Admin. | It can change who has access to everything. |
| D8 | **Stable identity anchor is an immutable `employeeId`**, never a name. | Name collisions and renames are normal. |

### 4.2 Two-way doors (current best guess, revisit freely)

| Topic | Starting choice | Alternatives to keep open | Revisit when |
|---|---|---|---|
| Core language | Python 3.13 | C#/.NET (native AD + PowerShell), Go (single binary) | Python blocks us on AD features or performance |
| Package/tooling | uv, ruff, pyright, pytest | — | — |
| MCP | Official Python SDK v2 (targets spec 2026-07-28) | FastMCP 4 | SDK gaps appear |
| Directory access | LDAP(S) from Python behind the directory port | Windows-side PowerShell via JEA for Windows-only ops | ldap3 maintenance risk materialises (single maintainer, last release 2.9.1) |
| Windows-only actions (logoff, LAPS) | WinRM to a **JEA** endpoint | — | — |
| Linux actions | SSH with `ForceCommand` / sudoers allow-lists | Ansible for request-time actions | Ansible proves simpler for us |
| Baseline config (device onboarding) | Ansible (Linux), GPO + PowerShell DSC (Windows) | — | — |
| State store | PostgreSQL | SQLite for the earliest prototype | — |
| Durable jobs | Plain state machine on Postgres | DBOS (Postgres-backed library), Temporal | Retries/timers get hard to get right by hand |
| Policy engine | Small typed rule layer behind a `PolicyPort` | Cedar (typed, analysable), OPA/Rego | Rules outgrow hand-written code |
| PKI | step-ca (short-lived certs, SSH CA) | AD CS | We need Windows autoenrollment in the demo |
| Inbound HR feed | CSV/JSON import | SCIM 2.0 endpoint | We want to plug into real HR systems |
| Lab | Hyper-V + Packer images | Vagrant, Proxmox | Setup takes more than one command |
| CI directory | Samba AD DC in a container | — | Samba differs from AD in a way that matters |
| Approval UX | CLI | Slack/Teams buttons, MCP URL-mode elicitation to a web page | First real demo |
| Agent eval harness | pytest-based scenario runner | Inspect AI, promptfoo | We need richer reporting |
| Reference agent | Claude Agent SDK | Any MCP-capable client | — |
| Licence | Undecided | Apache-2.0 (patent grant), MIT | Before first public release |

### 4.3 Open questions (we do not know yet)

- How fast does a disabled AD account actually stop working against existing
  Kerberos tickets in our lab? (Measure, don't assume.)
- What is the right SSSD cache configuration (`offline_credentials_expiration`,
  `account_cache_expiration`) for a defensible leaver SLA?
- Can we model "approve" with MCP's multi round-trip `input_required` flow while still
  keeping approval out of the agent's hands? Or is a separate web/CLI path cleaner?
- Should agent identity follow emerging OAuth patterns (actor vs. subject, as in the
  transaction-tokens-for-agents drafts) from day one, or start with signed requests?
- Which ISO 27001 / CIS controls can we evidence automatically vs. only document?
- How much of AD is worth supporting beyond users, groups, OUs and computers?

## 5. Architecture (hypothesis)

```
 Ticket / chat / alert (untrusted text)
            │
            ▼
      ┌───────────┐        any MCP client works; this one is the reference
      │  Agent    │  own identity, acts "on behalf of" the requester
      └─────┬─────┘
            │ MCP (stateless, 2026-07-28)      CLI        HTTP/SCIM
            ▼                                  ▼              ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ Core                                                             │
 │  resolve → plan → policy → approve → apply → verify   reconcile  │
 │  desired state · jobs · explain_access · blast-radius budgets    │
 └──────┬───────────────┬──────────────┬──────────────┬─────────────┘
        │ DirectoryPort │ HostPort     │ PkiPort      │ AuditPort
        ▼               ▼              ▼              ▼
   AD / Samba / sim   Linux (SSH)    step-ca / AD CS  append-only log
                      Windows (JEA)                   + OpenTelemetry
        ▲
  Approvals arrive here from an authenticated human, never via the agent.
```

### MCP surface (first cut, expect it to change)

- **Reads:** `resolve_person`, `get_identity`, `explain_access`, `query_directory`
  (allow-listed attributes, size/time limits), `diagnose_host` (allow-listed commands),
  `get_job`, `check_policy`.
- **Plans:** `plan_joiner`, `plan_mover`, `plan_leaver`, `plan_device_enrol`,
  `plan_grant_temporary_access`. Each returns diff, risk tier and `plan_id`.
- **Execution:** `apply(plan_id)`; long-running work returns a task handle (MCP tasks
  extension) or `job_id`.
- **Resources:** role catalogue, policy documents, runbooks.

Design rules for this surface:

- 10–15 tools, intent-level, deterministic order (helps prompt caching).
- Tool annotations (`readOnlyHint`, `destructiveHint`) set honestly.
- Errors are machine-readable with a next step (`USER_AMBIGUOUS` + candidates,
  `HOST_UNREACHABLE` + `retry_after`, `POLICY_DENIED` + rule id).
- Idempotency keys on every plan and apply.
- OpenTelemetry trace context from MCP `_meta` flows into the audit log.

## 6. Codebase shape (hypothesis)

```
identity-fleet/
  src/identity_fleet/
    domain/        entities, desired state, diff        (no I/O)
    engine/        plan, apply, verify, reconcile, jobs
    policy/        rules, risk tiers, budgets
    ports/         Directory, Host, Pki, Audit, Clock
    adapters/      sim, ldap, linux_ssh, windows_jea, step_ca
    interfaces/    mcp, cli, http
  agent/           reference agent (separate package)
  evals/           agent scenarios incl. prompt injection
  lab/             images, baselines, one-command bring-up
  tests/           unit · contract (same suite for sim and Samba AD) · e2e
  docs/            this plan, ADRs, threat model, edge-case notes
```

Testing pyramid:

1. **Unit:** domain and engine against the simulator.
2. **Contract:** one suite run against every directory adapter. This is how we prove
   the simulator is faithful.
3. **E2E (lab):** real Windows DC, Windows client, Linux host; measures time-to-revoke.
4. **Agent evals:** scenarios scored automatically, results committed.

## 7. Milestones as outcomes

Each milestone says what must be **true**, not which tasks to do. Order may change.

| Milestone | True when | Questions it answers |
|---|---|---|
| **M0 Walking skeleton** | One leaver flows end to end on the simulator through plan → approve → apply → verify, from both CLI and MCP, with an audit record | Does the core contract feel right? Is the tool surface usable by an agent? |
| **M1 Real directory** | Contract tests pass on Samba AD in CI; joiner, mover and leaver work on it | Is the simulator faithful? Is the LDAP library adequate? |
| **M2 Linux and sessions** | A domain-joined Linux host loses access and sessions within a measured SLA, cache included | What SLA can we honestly claim? |
| **M3 Agent** | Reference agent passes the eval suite, including injection cases, with published results | Is the governance model enough under adversarial input? |
| **M4 Windows, devices, PKI** | Windows lab, device enrolment, short-lived certs and SSH CA in the leaver path | Do short-lived credentials simplify offboarding as hoped? |
| **M5 Drift** | Out-of-band changes are detected and reported or reverted | How noisy is reconciliation in practice? |

Explicitly out of scope until proven necessary: DNS/DHCP management, patch
deployment, asset inventory UI, a large web dashboard.

## 8. How this plan stays open

- **Better-idea protocol.** Anyone (including an agent) who finds a better approach
  writes a short ADR: current way, proposed way, evidence, cost of switching. If it is
  better, we switch and update this plan. "It's not in the plan" is never a reason to
  reject it.
- **Spikes.** Uncertain choices get a time-boxed spike (hours, not days) that ends in
  an ADR, not in production code.
- **Milestone review.** At the end of each milestone we ask:
  1. Can we do better than what we just built?
  2. Did something we assumed turn out false?
  3. Is any two-way door now worth closing, or any one-way door worth reopening?
  4. Has an existing tool appeared that we should use instead?
- **Kill criteria.** If a component costs more than it proves, we cut it and say so in
  the README.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Scope creep (eight subsystems in one project) | Milestones as outcomes, explicit out-of-scope list |
| Windows lab is heavy (eval licence 180 days, 16 GB+ RAM) | Simulator and Samba AD carry most testing; Windows lab for E2E only |
| LLM behaviour is non-deterministic | Governance does not rely on the model behaving; evals measure it anyway |
| Library risk (ldap3 single maintainer) | Directory port; contract tests make swapping cheap |
| Security of the system itself | Tier 0 treatment, threat model before M1, least privilege |
| Fast-moving standards (MCP, agent OAuth drafts) | Thin MCP adapter; track spec changes at milestone reviews |

## 10. References

- MCP specification 2026-07-28 changelog — https://modelcontextprotocol.io/specification/2026-07-28/changelog
- MCP Python SDK v2 — https://pydantic.dev/articles/mcp-python-sdk-v2-beta
- Okta Managed MCP Server — https://help.okta.com/mcp/en-us/content/topics/mcpserver/mcpserver.htm
- Microsoft MCP Server for Enterprise — https://learn.microsoft.com/graph/mcp-server/overview
- Microsoft Agent Governance Toolkit — https://helpnetsecurity.com/2026/04/03/microsoft-ai-agent-governance-toolkit
- Entra Agent ID on-behalf-of flow — https://learn.microsoft.com/en-us/entra/agent-id/agent-on-behalf-of-oauth-flow
- IETF AI agent auth draft — https://datatracker.ietf.org/doc/draft-klrc-aiagent-auth/02/
- Transaction tokens for agents draft — https://www.ietf.org/archive/id/draft-araut-oauth-transaction-tokens-for-agents-00.txt
- OWASP Top 10 for Agentic Applications (overview) — https://www.promptfoo.dev/docs/red-team/owasp-agentic-ai/
- Cedar vs OPA — https://permit.io/blog/opa-vs-cedar
- DBOS vs Temporal — https://docs.dbos.dev/explanations/comparing-temporal
- Samba AD container — https://github.com/samba-in-kubernetes/samba-container/
- SSSD offline credential caching — https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/6/html/deployment_guide/sssd-cache-cred
- ldap3 package health — https://security.snyk.io/package/pip/ldap3
