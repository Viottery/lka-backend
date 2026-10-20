"""Public synthetic management catalog. Importing/listing never loads runtime/config."""

from __future__ import annotations

import argparse
import copy
import json
import secrets
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "management_delegates.json"


def load_catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def materialize_case(case):
    value = copy.deepcopy(case)
    if "source_id" in value["expected"] and value["expected"]["source_id"] == "RANDOM_AT_RUN":
        source_id = "VENUE-" + secrets.token_hex(8).upper()
        value["expected"]["source_id"] = source_id
        value["files"]["venue.json"]["source_id"] = source_id
    return value


def select_cases(names=None):
    catalog = load_catalog()
    available = {**catalog["cases"], **catalog["offline_regressions"]}
    names = names or list(catalog["cases"])
    if len(names) != len(set(names)) or any(n not in available for n in names):
        raise ValueError("Select unique exact case IDs from --list")
    return names


def plan(names):
    catalog = load_catalog()
    return [
        {
            "case_id": name,
            "kind": "real_multiturn" if name in catalog["cases"] else "offline_contract",
            "status": catalog["cases"].get(name, {}).get("historical_status", "unvalidated"),
            "nodeids": catalog["offline_regressions"].get(name, []),
        }
        for name in names
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true", help="Output is always JSON")
    parser.add_argument("--case", action="append")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--llm-mode", choices=["offline", "real"], default="offline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--local-config", type=Path, default=Path("config/local.toml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--call-limit", type=int, default=120)
    parser.add_argument("--usd-limit", type=float, default=2.0)
    parser.add_argument("--max-steps", type=int, default=80)
    args = parser.parse_args(argv)
    try:
        names = select_cases(args.case)
    except ValueError as exc:
        parser.error(str(exc))
    if args.list:
        names = list(load_catalog()["cases"]) + list(load_catalog()["offline_regressions"])
    selection = plan(names)
    if not args.run or args.list:
        print(
            json.dumps(
                {
                    "run": False,
                    "cases": selection,
                    "search_limit": 0,
                    "call_limit": args.call_limit,
                    "usd_limit": args.usd_limit,
                }
            )
        )
        return 0
    if not args.case:
        parser.error("--run requires explicit --case selection")
    if args.output is None or args.output.exists():
        parser.error("--output must be a new directory; preserve prior results and ledgers")
    if (
        not 1 <= args.max_steps <= 200
        or not 1 <= args.call_limit <= 1000
        or not 0 < args.usd_limit <= 20
    ):
        parser.error("Invalid bounded steps/calls/USD limit")
    live = [c for c in selection if c["kind"] == "real_multiturn"]
    if live and args.llm_mode != "real":
        parser.error("Persistent model scenarios require both --run and --llm-mode real")
    if live and len(live) != len(selection):
        parser.error("Run offline contracts and real-model scenarios separately")
    if live:
        from evals.lka_evals.management_runtime import run_live

        return run_live(args, names)
    import subprocess
    import sys

    args.output.mkdir(parents=True)
    nodeids = list(dict.fromkeys(n for c in selection for n in c["nodeids"]))
    command = [sys.executable, "-m", "pytest", "-q", *nodeids]
    try:
        result = subprocess.run(command, timeout=180, capture_output=True, text=True, check=False)
        report = {
            "status": "passed" if result.returncode == 0 else "failed",
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "quality_scope": "offline contract only; no real-model semantic claim",
        }
    except subprocess.TimeoutExpired:
        report = {"status": "timeout", "command": command}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
