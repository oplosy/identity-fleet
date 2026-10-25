"""M0 acceptance tests (docs/design/M0-walking-skeleton.md §11)."""

import asyncio
import json
from datetime import timedelta

import pytest
from mcp import Client

from identity_fleet.domain import IdfError
from identity_fleet.mcp_server import build_server
from identity_fleet.sim import SimDirectory


def leaver(core, eid="E1001", by="selin.k"):
    return core.plan_leaver(eid, reason="Resigned", on_behalf_of=by, ticket="HR-881")


def code(excinfo) -> str:
    return excinfo.value.code


def test_resolve_person_is_accent_insensitive_and_reports_ambiguity(core):
    found = core.resolve_person("ayse yilmaz")
    assert {c["employee_id"] for c in found} == {"E1001", "E1002"}
    assert core.resolve_person("ŞULE")[0]["employee_id"] == "E1004"


def test_1_leaver_ops_are_ordered_disable_sessions_groups(core):
    plan = leaver(core, "E1004")
    assert [o.op for o in plan.ops] == [
        "disable_account",
        "terminate_session",
        "terminate_session",
        "remove_group",
        "remove_group",
    ]
    assert plan.risk == "high" and plan.approvals_required == 1 and plan.status == "pending_approval"


def test_plans_are_idempotent(core):
    first, second = leaver(core), leaver(core)
    assert first.plan_id == second.plan_id
    assert [e["event"] for e in core.audit.entries()].count("plan.created") == 1


def test_2_apply_without_approval_is_refused(core):
    plan = leaver(core)
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_NOT_APPROVED"
    assert "idf plan approve" in e.value.next_step


def test_3_requester_cannot_approve_own_request(core, approve):
    plan = leaver(core, by="selin.k")
    with pytest.raises(IdfError) as e:
        approve(plan.plan_id, "selin.k")
    assert code(e) == "APPROVAL_REFUSED"


def test_4_approve_apply_verify_and_audit(core, approve, clock):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    clock.t += timedelta(minutes=3)
    run = core.apply(plan.plan_id)

    assert run.status == "verified" and not run.residual
    assert all(r.outcome == "applied" for r in run.results)
    assert run.time_to_revoke_seconds == 180
    identity = core.get_identity("E1001")
    assert identity["person"]["status"] == "left"
    assert identity["accounts"][0]["enabled"] is False and identity["accounts"][0]["groups"] == []
    assert identity["sessions"] == []
    assert core.audit.verify() == (True, None)


def test_5_reapply_skips_everything(core, approve):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    core.apply(plan.plan_id)
    again = core.apply(plan.plan_id)
    assert again.status == "verified"
    assert {r.outcome for r in again.results} == {"already_satisfied"}


def test_6_unapproved_drift_is_reported_not_executed(core, approve):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    SimDirectory(core.store).add_group("dir:ayse.yilmaz", "GG-Payroll")  # out-of-band change

    run = core.apply(plan.plan_id)
    assert run.status == "verified_with_residual"
    assert [(o.op, o.group) for o in run.residual] == [("remove_group", "GG-Payroll")]
    assert "GG-Payroll" in core.get_identity("E1001")["accounts"][0]["groups"]


def test_7_tampered_plan_is_refused(core, approve):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    stored = json.loads(core.store.conn.execute("SELECT doc FROM plans").fetchone()[0])
    stored["ops"] = stored["ops"][:1]  # drop everything but disabling the account
    core.store.put("plans", plan.plan_id, stored)
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_TAMPERED"


def test_7b_status_tampering_does_not_bypass_approval(core):
    plan = leaver(core)
    plan.status = "approved"
    core.store.put_plan(plan)
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_NOT_APPROVED"


def test_8_tier0_needs_two_distinct_approvers(core, approve):
    plan = leaver(core, "E1003")
    assert plan.risk == "critical" and plan.approvals_required == 2
    approve(plan.plan_id, "emre.d")
    with pytest.raises(IdfError) as e:
        approve(plan.plan_id, "emre.d")
    assert code(e) == "APPROVAL_REFUSED"
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_NOT_APPROVED"
    approve(plan.plan_id, "deniz.a")
    assert core.apply(plan.plan_id).status == "verified"


def test_8b_break_glass_is_denied(core):
    with pytest.raises(IdfError) as e:
        leaver(core, "E0001")
    assert code(e) == "POLICY_DENIED"


def test_9_edited_audit_line_breaks_the_chain(core, approve):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    core.apply(plan.plan_id)
    lines = core.audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "someone-else"
    lines[1] = json.dumps(entry, ensure_ascii=False)
    core.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert core.audit.verify() == (False, entry["seq"])


def test_expired_plan_cannot_be_applied(core, approve, clock):
    plan = leaver(core)
    approve(plan.plan_id, "emre.d")
    clock.t += timedelta(hours=25)
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_EXPIRED"


def test_rejected_plan_cannot_be_applied(core, keys):
    from identity_fleet import approvals

    plan = leaver(core)
    core.reject(plan.plan_id, approvals.sign("reject", plan.plan_id, keys["emre.d"]))
    with pytest.raises(IdfError) as e:
        core.apply(plan.plan_id)
    assert code(e) == "PLAN_REJECTED"


def test_10_mcp_end_to_end_without_approve_tool(core, approve):
    async def flow():
        async with Client(build_server(core)) as client:
            tools = [t.name for t in (await client.list_tools()).tools]
            assert tools == ["resolve_person", "get_identity", "plan_leaver", "get_plan", "apply_plan"]

            found = await client.call_tool("resolve_person", {"query": "Şule"})
            eid = found.structured_content["candidates"][0]["employee_id"]
            planned = await client.call_tool(
                "plan_leaver", {"employee_id": eid, "reason": "Resigned", "on_behalf_of": "selin.k"}
            )
            plan_id = planned.structured_content["plan_id"]

            refused = await client.call_tool("apply_plan", {"plan_id": plan_id})
            assert refused.is_error
            # The SDK prefixes "Error executing tool <name>: "; the payload after it is our JSON error.
            error = json.loads(refused.content[0].text.split(": ", 1)[1])
            assert error["code"] == "PLAN_NOT_APPROVED" and error["next_step"]

            approve(plan_id, "emre.d")  # out of band, as a human would via the CLI
            applied = await client.call_tool("apply_plan", {"plan_id": plan_id})
            assert applied.structured_content["status"] == "verified"

    asyncio.run(flow())
