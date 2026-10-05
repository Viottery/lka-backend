"""Curated quality issues, deterministic selection, and non-executing replay plans.

This is evaluator-only metadata. Symptoms, labels and acceptance criteria must
never be used as model-visible hints. No runtime, provider or private log imports.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "evals/fixtures/linux_quality_cases_2026-10-05.json"
GROUPS = ("files_bash_code", "mail", "web", "rag", "multi_agent", "memory",
          "background", "context_tools", "watches")
STATUSES = ("baseline_passed", "fixed_live", "fixed_offline", "open", "inconclusive")
TIERS = ("foundation", "regression", "challenge")
PROFILES = ("focus", "regression", "foundation", "historical_holdout", "all")
_RUNNERS = {
    "realworld": ("scripts/eval_realworld.py", ("--remote",)),
    "web": ("scripts/eval_runtime_web_quality.py", ("--remote", "--root-go")),
    "parallel": ("scripts/eval_runtime_parallel_quality.py", ("--remote", "--root-go")),
    "background": ("scripts/eval_runtime_background_quality.py", ("--remote", "--root-go")),
    "background_followup": ("scripts/eval_runtime_background_followup_quality.py", ("--remote", "--root-go")),
    "memory": ("scripts/eval_runtime_memory_quality.py", ("--remote",)),
    "watch": ("scripts/eval_runtime_watch_quality.py", ("--remote",)),
    "public_rag": ("scripts/eval_runtime_public_rag_quality.py", ("--remote",)),
    "public_retrieval": ("-m evals.lka_evals.public_retrieval", ()),
}
_ID = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_CASE_KEYS = {"id", "title", "group", "tier", "status", "symptom", "cause", "optimization",
              "acceptance", "evidence", "regression_tests", "replay", "tags", "exposure", "data_class"}


def _strings(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (nonempty and not value)
        or any(not isinstance(x, str) or not x.strip() for x in value)
        or len(set(value)) != len(value)):
        raise ValueError(f"{where}: expected unique nonempty strings")
    return value


def validate_catalog(payload: Any) -> dict:
    if (not isinstance(payload, dict)
        or set(payload) != {"schema_version", "catalog_id", "as_of", "description", "cases"}
        or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or not isinstance(payload["cases"], list) or not payload["cases"]):
        raise ValueError("Invalid quality catalog root/version or empty cases")
    for key in ("catalog_id", "as_of", "description"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f"Invalid catalog {key}")
    seen = set()
    for case in payload["cases"]:
        if not isinstance(case, dict) or set(case) != _CASE_KEYS:
            raise ValueError("Invalid quality case fields")
        for key in ("id", "title", "symptom", "cause", "optimization"):
            if not isinstance(case[key], str) or not case[key].strip():
                raise ValueError(f"Invalid quality case {key}")
        if not _ID.fullmatch(case["id"]) or case["id"] in seen:
            raise ValueError(f"Invalid/duplicate quality case id: {case['id']}")
        seen.add(case["id"])
        for key, choices in (("group", GROUPS), ("status", STATUSES), ("tier", TIERS),
                             ("exposure", ("seen", "unseen")),
                             ("data_class", ("synthetic", "public", "private_snapshot"))):
            if case[key] not in choices:
                raise ValueError(f"{case['id']}: invalid {key}")
        if (case["status"] == "baseline_passed") != (case["tier"] == "foundation"):
            raise ValueError("Foundation is a baseline, not a repaired historical failure")
        for key in ("acceptance", "evidence", "regression_tests", "tags"):
            _strings(case[key], f"{case['id']}.{key}", nonempty=key in {"acceptance", "evidence"})
        for target in case["regression_tests"]:
            path = PurePosixPath(target.split("::", 1)[0])
            if (path.is_absolute() or ".." in path.parts or "\\" in target
                or not str(path).startswith(("tests/test_", "evals/reproductions/")) or path.suffix != ".py"):
                raise ValueError(f"Invalid regression target: {target}")
        replay = case["replay"]
        if replay is not None:
            if (not isinstance(replay, dict) or set(replay) != {"runner", "case", "args"}
                or not isinstance(replay["runner"], str) or replay["runner"] not in _RUNNERS
                or not isinstance(replay["args"], list)
                or any(not isinstance(x, str) or not x for x in replay["args"])
                or (replay["case"] is not None and not isinstance(replay["case"], str))):
                raise ValueError(f"Invalid replay for {case['id']}")
            # argparse accepts long-option abbreviations by default. Metadata
            # may specify parameters, never an abbreviation/equals form of GO.
            if any(x.startswith("--") and any(flag.startswith(x.split("=", 1)[0])
                   for flag in ("--remote", "--root-go")) for x in replay["args"]):
                raise ValueError("Replay metadata cannot grant remote dispatch")
            if replay["runner"] in {"realworld", "web"}:
                if not replay["case"]:
                    raise ValueError("Case-selecting runner requires an explicit case")
            elif replay["case"] is not None:
                raise ValueError("This runner does not accept --case")
    return payload


def load_catalog(path: Path = DEFAULT_CATALOG) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Quality catalog exceeds 2 MiB")
    return validate_catalog(json.loads(path.read_text(encoding="utf-8")))


def select_cases(catalog: dict, *, profile: str = "focus", groups: set[str] | None = None,
                 statuses: set[str] | None = None, case_ids: set[str] | None = None) -> list[dict]:
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    for label, values, allowed in (("group", groups, set(GROUPS)),
                                   ("status", statuses, set(STATUSES)),
                                   ("case", case_ids, {c["id"] for c in catalog["cases"]})):
        if values is not None and (missing := values - allowed):
            raise ValueError(f"Unknown {label}: {', '.join(sorted(missing))}")
    def in_profile(case):
        if profile == "focus":
            return case["tier"] != "foundation" and case["status"] in {"open", "fixed_offline", "inconclusive"}
        if profile == "regression":
            return case["tier"] == "regression" or case["status"] in {"fixed_live", "fixed_offline"}
        if profile == "foundation":
            return case["tier"] == "foundation"
        if profile == "historical_holdout":
            return case["exposure"] == "seen" and "heldout" in case["tags"]
        return True
    return sorted((c for c in catalog["cases"] if in_profile(c)
                   and (groups is None or c["group"] in groups)
                   and (statuses is None or c["status"] in statuses)
                   and (case_ids is None or c["id"] in case_ids)), key=lambda c: c["id"])


def statistics(cases: list[dict]) -> dict:
    return {
        "logical_cases": len(cases),
        "by_status": {key: sum(c["status"] == key for c in cases) for key in STATUSES},
        "by_tier": {key: sum(c["tier"] == key for c in cases) for key in TIERS},
        "groups": {group: {"total": sum(c["group"] == group for c in cases),
                            **{status: sum(c["group"] == group and c["status"] == status for c in cases)
                               for status in STATUSES}} for group in GROUPS},
        "unseen_cases": sum(c["exposure"] == "unseen" for c in cases),
        "with_regression_targets": sum(bool(c["regression_tests"]) for c in cases),
        "with_replay_entry": sum(c["replay"] is not None for c in cases),
        "label": "Curated logical issues/baselines, NOT run count or current model accuracy",
    }


def replay_plan(cases: list[dict]) -> dict:
    tests = sorted({t for c in cases for t in c["regression_tests"]})
    # Do not repeat a node selection if its whole module is already selected.
    tests = [t for t in tests if "::" not in t or t.split("::", 1)[0] not in tests]
    jobs, manual = {}, []
    for case in cases:
        replay = case["replay"]
        if replay is None:
            manual.append(case["id"])
            continue
        entry, gates = _RUNNERS[replay["runner"]]
        command = [".venv/bin/python", *entry.split(" ")]
        if replay["case"] is not None:
            command.extend(["--case", replay["case"]])
        command.extend(replay["args"])
        blocked = case["data_class"] == "private_snapshot"
        key = (tuple(command), blocked)
        job = jobs.setdefault(key, {"command_without_authorization": command,
            "required_explicit_flags": list(gates), "logical_case_ids": [],
            "requires_private_snapshot": blocked})
        job["logical_case_ids"].append(case["id"])
    return {"executed": False, "paid_calls": 0,
            "pytest_command": [".venv/bin/pytest", "-q", *tests] if tests else None,
            "unique_regression_targets": len(tests), "replay_jobs": list(jobs.values()),
            "manual_replay_cases": manual,
            "note": "Planning only. No commands executed; existing budget/GO gates remain. Private mail requires an explicit authorized --mail-db snapshot. Opt-in unresolved reproductions may intentionally fail; they are not xfailed or collected by default tests."}


def goal_only_case_ids(root: Path = ROOT) -> list[str]:
    """Read literal fixture IDs, without importing the real-model/runtime runner."""
    tree = ast.parse((root / "scripts/eval_realworld.py").read_text(encoding="utf-8"))
    definition = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "CASES" for t in n.targets))
    return [key.value for key in definition.keys]


def _web_case_ids(root: Path) -> set[str]:
    """Resolve only literal constant/tuple names; do not execute runner source."""
    tree = ast.parse((root / _RUNNERS["web"][0]).read_text(encoding="utf-8"))
    constants = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            resolved = value.value
        elif isinstance(value, (ast.Tuple, ast.List)):
            resolved = []
            for item in value.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    resolved.append(item.value)
                elif isinstance(item, ast.Name) and isinstance(constants.get(item.id), str):
                    resolved.append(constants[item.id])
                else:
                    break
            else:
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = resolved
                continue
            continue
        else:
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = resolved
    if not isinstance(constants.get("APPROVED_CASES"), list):
        raise ValueError("Cannot resolve bounded web case selection")  # noqa: TRY004 - reference validation uses ValueError.
    return set(constants["APPROVED_CASES"])


def validate_references(catalog: dict, root: Path = ROOT) -> None:
    """Fail stale replay IDs/paths before suggesting an unusable selection."""
    available = set(goal_only_case_ids(root))
    web_available = _web_case_ids(root)
    for case in catalog["cases"]:
        for target in case["regression_tests"]:
            filename, *nodes = target.split("::")
            path = (root / filename).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"Missing/outside regression target: {target}")
            if nodes:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                body = tree.body
                for index, node in enumerate(nodes):
                    name = node.split("[", 1)[0]
                    definition = next((n for n in body if isinstance(
                        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name), None)
                    if definition is None or (index < len(nodes) - 1 and not isinstance(definition, ast.ClassDef)):
                        raise ValueError(f"Missing regression node hierarchy: {target}")
                    body = definition.body
        replay = case["replay"]
        if replay is None:
            continue
        entry, _ = _RUNNERS[replay["runner"]]
        if not entry.startswith("-m ") and not (root / entry).is_file():
            raise ValueError(f"Missing replay runner: {entry}")
        if replay["runner"] == "realworld" and replay["case"] not in available:
            raise ValueError(f"Unknown realworld replay case: {replay['case']}")
        if replay["runner"] == "web" and replay["case"] not in web_available:
            raise ValueError(f"Unknown web replay case: {replay['case']}")


def repository_inventory(root: Path = ROOT) -> dict:
    suites, ids = [], Counter()
    for path in sorted((root / "evals/suites").rglob("*.yaml")):
        suite = json.loads(path.read_text(encoding="utf-8"))
        cases = suite["cases"]
        names = [c["case_id"] for c in cases]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate case IDs within {path}")
        ids.update(names)
        suites.append({"path": path.relative_to(root).as_posix(), "cases": len(cases)})
    realworld_ids = goal_only_case_ids(root)
    datasets = []
    manifest_path = root / "evals/datasets/public_multihop/manifest.json"
    for dataset in json.loads(manifest_path.read_text(encoding="utf-8"))["datasets"]:
        relative = PurePosixPath(dataset["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl":
            raise ValueError("Dataset manifest path must stay within dataset directory")
        path = manifest_path.parent / str(relative)
        counts, digest = Counter(), hashlib.sha256()
        with path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                if line.strip():
                    kind = json.loads(line).get("kind")
                    if kind not in {"query", "document"}:
                        raise ValueError(f"Unknown record kind in {path}")
                    counts[kind] += 1
        datasets.append({"dataset": dataset["dataset"], "queries": counts["query"],
                         "documents": counts["document"], "sha256": digest.hexdigest()})
    return {"suite_files": len(suites), "suite_case_definitions": sum(s["cases"] for s in suites),
            "unique_suite_case_ids": len(ids), "repeated_suite_case_ids": sorted(k for k, v in ids.items() if v > 1),
            "suites": suites, "goal_only_scenarios": len(realworld_ids), "goal_only_case_ids": realworld_ids,
            "public_datasets": datasets, "public_queries": sum(d["queries"] for d in datasets),
            "note": "Separate inventories; overlapping fixtures/replays are not added into a single accuracy denominator. No pytest collection, model loading or private-log scanning."}
