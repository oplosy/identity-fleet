"""`idf` command line. The only place where humans approve or reject plans."""

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import approvals
from .config import open_core
from .domain import IdfError
from .sim import DEFAULT_FIXTURE, load_fixture


def _print(data: Any) -> None:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="idf", description="IdentityFleet CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("sim", help="simulator").add_subparsers(dest="sim_cmd", required=True)
    reset = sim.add_parser("reset", help="wipe state and load the fixture")
    reset.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)

    plan = sub.add_parser("plan", help="plans").add_subparsers(dest="plan_cmd", required=True)
    leaver = plan.add_parser("leaver", help="plan offboarding")
    leaver.add_argument("employee_id")
    leaver.add_argument("--reason", required=True)
    leaver.add_argument("--on-behalf-of", required=True)
    leaver.add_argument("--ticket")
    show = plan.add_parser("show")
    show.add_argument("plan_id")
    for action in ("approve", "reject"):
        p = plan.add_parser(action, help=f"{action} with your SSH key")
        p.add_argument("plan_id")
        p.add_argument("--key", type=Path, required=True, help="private key listed in allowed_signers")

    apply = sub.add_parser("apply", help="apply an approved plan")
    apply.add_argument("plan_id")

    audit = sub.add_parser("audit").add_subparsers(dest="audit_cmd", required=True)
    audit.add_parser("verify", help="check the hash chain")

    sub.add_parser("mcp", help="run the MCP server on stdio")

    args = parser.parse_args(argv)

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    core = open_core(actor=f"cli:{getpass.getuser()}")
    try:
        if args.cmd == "sim":
            load_fixture(core.store, args.fixture)
            core.audit.append("sim.reset", core.actor, None, fixture=str(args.fixture))
            print(f"Simulator loaded from {args.fixture}")
        elif args.cmd == "plan" and args.plan_cmd == "leaver":
            _print(core.plan_leaver(args.employee_id, args.reason, args.on_behalf_of, args.ticket))
        elif args.cmd == "plan" and args.plan_cmd == "show":
            _print(core.get_plan(args.plan_id))
        elif args.cmd == "plan":
            signature = approvals.sign(args.plan_cmd, args.plan_id, args.key)
            fn = core.approve if args.plan_cmd == "approve" else core.reject
            _print(fn(args.plan_id, signature))
        elif args.cmd == "apply":
            _print(core.apply(args.plan_id))
        elif args.cmd == "audit":
            ok, bad = core.audit.verify()
            print("audit chain OK" if ok else f"audit chain BROKEN at seq {bad}")
            return 0 if ok else 1
    except IdfError as e:
        print(json.dumps(e.to_dict(), indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
