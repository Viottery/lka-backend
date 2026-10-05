"""Select quality challenges and print statistics/replay plans; never execute tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex

from evals.lka_evals.quality_catalog import (
    DEFAULT_CATALOG,
    GROUPS,
    PROFILES,
    STATUSES,
    load_catalog,
    replay_plan,
    repository_inventory,
    select_cases,
    statistics,
    validate_references,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="focus")
    parser.add_argument("--group", choices=GROUPS, action="append")
    parser.add_argument("--status", choices=STATUSES, action="append")
    parser.add_argument("--case", action="append", help="logical catalog ID, not a raw runner case")
    parser.add_argument("--inventory", action="store_true", help="also count checked-in suites/datasets")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog()
        validate_references(catalog)
        selected = select_cases(catalog, profile=args.profile,
                                groups=set(args.group) if args.group else None,
                                statuses=set(args.status) if args.status else None,
                                case_ids=set(args.case) if args.case else None)
        if not selected:
            parser.error("Selection is empty; not a successful evaluation")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    result = {"catalog_id": catalog["catalog_id"], "as_of": catalog["as_of"],
              "catalog_sha256": hashlib.sha256(DEFAULT_CATALOG.read_bytes()).hexdigest(),
              "profile": args.profile, "catalog_statistics": statistics(catalog["cases"]),
              "profile_counts": {profile: len(select_cases(catalog, profile=profile)) for profile in PROFILES},
              "selection_statistics": statistics(selected),
              "selected": [{key: c[key] for key in ("id", "title", "group", "tier", "status", "exposure")}
                           for c in selected], "plan": replay_plan(selected)}
    if args.inventory:
        result["repository_inventory"] = repository_inventory()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"profile={args.profile} selected={len(selected)}/{len(catalog['cases'])} executed=false")
        for case in selected:
            print(f"{case['id']} [{case['group']}/{case['status']}] {case['title']}")
        if command := result["plan"]["pytest_command"]:
            print("Offline regression plan: " + shlex.join(command))
        for job in result["plan"]["replay_jobs"]:
            print("Replay plan (not executed): " + shlex.join(job["command_without_authorization"]))
            print(f"  gates={job['required_explicit_flags']} private_snapshot={job['requires_private_snapshot']}")
        if args.inventory:
            print(json.dumps(result["repository_inventory"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
