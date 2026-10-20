"""Repository repair evaluation: explicit fetch/setup, isolated runs, separate grading.

Listing is offline. No private configuration or runtime is imported until run.
Native results are diagnostics; official SWE-bench grading is a separate stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import urlopen

from evals.lka_evals.swe_dataset import (
    DEFAULT_SOURCE,
    fetch_dataset,
    load_dataset,
    public_task,
    select_cases,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "evals/fixtures/software_engineering.json"
DEFAULT_DATASET = ROOT / "data/benchmarks/swe-verified" / DEFAULT_SOURCE["revision"]


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(path: Path) -> str:
    validate_tree(path)
    result = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if ".git" in item.relative_to(path).parts or (item.is_dir() and not item.is_symlink()):
            continue
        result.update(item.relative_to(path).as_posix().encode() + b"\0")
        result.update(
            b"link:" + os.readlink(item).encode() if item.is_symlink() else item.read_bytes()
        )
        result.update(b"\0" + str(item.lstat().st_mode & 0o777).encode() + b"\0")
    return result.hexdigest()


def environment_identity(python: Path) -> dict:
    python = python.absolute()
    manifest = python.parent.parent / "environment.json"
    if not manifest.is_file():
        raise ValueError("Use a dedicated environment prepared by the env subcommand")
    return {"python": str(python), "environment_sha256": digest(manifest)}


def catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def ignored_output(path: Path, *, fresh: bool = True) -> Path:
    """Keep public benchmark answers, repositories and raw traces out of Git."""
    path = path.expanduser().resolve()
    if not path.is_relative_to(ROOT / "data"):
        raise ValueError("Evaluation output must be inside the ignored repository data/ directory")
    if fresh and path.exists():
        raise FileExistsError(f"Output already exists: {path}")
    return path


def source_file(path: Path) -> Path:
    return path / "dataset.jsonl" if path.is_dir() else path


def dataset_provenance(path: Path) -> dict:
    source = source_file(path)
    manifest = source.parent / "manifest.json"
    if manifest.is_file():
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if record.get("dataset_sha256") == digest(source) and isinstance(
            record.get("source"), dict
        ):
            return record["source"]
    return {"dataset": "local_jsonl", "path": str(source.resolve()), "sha256": digest(source)}


def separate_output(path: Path, bundle: Path, python: Path) -> Path:
    output = ignored_output(path)
    if output.is_relative_to(bundle) or output.is_relative_to(python.absolute().parent.parent):
        raise ValueError("Output must be outside prepared bundle and Python environment")
    return output


def selection(args) -> list[dict]:
    ids = catalog()["pilot"] if args.pilot else args.case
    if args.pilot and args.case:
        raise ValueError("Use --pilot or --case, not both")
    return select_cases(load_dataset(args.dataset), ids, args.sample, args.seed)


def selected_bundle(args) -> tuple[Path, dict, dict[str, dict]]:
    bundle, manifest, rows = load_bundle(args.prepared)
    selected = getattr(args, "case", [])
    if selected:
        chosen = select_cases(list(rows.values()), selected)
        manifest = {
            **manifest,
            "prepared_selection": manifest["selection"],
            "selection": [r["instance_id"] for r in chosen],
        }
        rows = {r["instance_id"]: r for r in chosen}
    return bundle, manifest, rows


def download_repository(row: dict, archive: Path, pristine: Path) -> str:
    """Fetch only the base snapshot: no remote, later history, PR or gold commit."""
    url = f"https://codeload.github.com/{row['repo']}/tar.gz/{row['base_commit']}"
    deadline = time.monotonic() + 180
    size = 0
    with urlopen(url, timeout=30) as response, archive.open("xb") as stream:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > 128 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("Repository download exceeded 128 MiB / 180 seconds")
            stream.write(block)
    extraction = pristine.parent / "extracting"
    extraction.mkdir()
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("Repository expanded size exceeds 512 MiB")
        if any(".git" in Path(m.name).parts for m in members):
            raise ValueError("Repository archive contains Git metadata")
        bundle.extractall(extraction, filter="data")
    roots = list(extraction.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Expected one repository archive root")
    roots[0].rename(pristine)
    extraction.rmdir()
    validate_tree(pristine)
    return digest(archive)


def validate_tree(tree: Path) -> None:
    for path in tree.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(tree.resolve()):
            raise ValueError(f"Repository symlink leaves workspace: {path.relative_to(tree)}")


def git_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def initialize_git(workspace: Path) -> None:
    for command in (
        ["init", "-q"],
        ["add", "-A", "-f"],
        [
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Benchmark base snapshot",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(workspace), *command],
            env=git_env(),
            check=True,
            capture_output=True,
            timeout=60,
        )


def prepare(args, rows: list[dict]) -> dict:
    output = ignored_output(args.output)
    output.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "dataset": str(source_file(args.dataset).resolve()),
        "dataset_sha256": digest(source_file(args.dataset)),
        "selection": [r["instance_id"] for r in rows],
        "cases": [],
        "source": dataset_provenance(args.dataset),
        "provider_calls": 0,
    }
    save(output / "manifest.json", manifest)
    for row in rows:
        directory = output / row["instance_id"]
        directory.mkdir()
        task = public_task(row)
        save(directory / "task.json", task)
        entry = {"instance_id": row["instance_id"], "status": "preparing"}
        manifest["cases"].append(entry)
        try:
            entry["archive_sha256"] = download_repository(
                row, directory / "source.tar.gz", directory / "pristine"
            )
            entry["pristine_sha256"] = tree_digest(directory / "pristine")
            shutil.copytree(directory / "pristine", directory / "workspace", symlinks=True)
            initialize_git(directory / "workspace")
            entry["status"] = "prepared"
        except (
            OSError,
            ValueError,
            RuntimeError,
            tarfile.TarError,
            subprocess.SubprocessError,
        ) as exc:
            entry.update(status="environment_error", error=f"{type(exc).__name__}: {exc}")
        save(output / "manifest.json", manifest)
        print(json.dumps(entry), flush=True)
    return manifest


def load_bundle(path: Path) -> tuple[Path, dict, dict[str, dict]]:
    path = ignored_output(path, fresh=False)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    dataset = Path(manifest["dataset"])
    if digest(dataset) != manifest["dataset_sha256"]:
        raise ValueError("Prepared dataset hash changed")
    rows = {r["instance_id"]: r for r in select_cases(load_dataset(dataset), manifest["selection"])}
    for entry in manifest["cases"]:
        case = path / entry["instance_id"]
        if entry["status"] != "prepared":
            raise ValueError(f"Case was not prepared: {entry['instance_id']}")
        if json.loads((case / "task.json").read_text()) != public_task(rows[entry["instance_id"]]):
            raise ValueError("Prepared task differs from the frozen dataset")
        if digest(case / "source.tar.gz") != entry["archive_sha256"]:
            raise ValueError("Repository archive hash changed")
        if tree_digest(case / "pristine") != entry["pristine_sha256"]:
            raise ValueError("Pristine repository changed; prepare a new bundle")
    return path, manifest, rows


def create_environment(args) -> dict:
    output = ignored_output(args.output)
    profile = catalog()["native_profiles"][args.profile]
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("uv is required to prepare the isolated Python environment")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            uv,
            "venv",
            "--no-project",
            "--managed-python",
            "--python",
            profile["python"],
            str(output),
        ],
        check=True,
        timeout=300,
    )
    python = output / "bin/python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), *profile["packages"]],
        check=True,
        timeout=300,
    )
    frozen = subprocess.run(
        [uv, "pip", "freeze", "--python", str(python)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = {
        "profile": args.profile,
        **profile,
        "freeze": frozen.stdout,
        "python_executable": str(python),
        "protocol": "native_diagnostic_not_official",
    }
    save(output / "environment.json", result)
    return result


def calibration(args) -> dict:
    from evals.lka_evals.swe_native import grade

    bundle, manifest, rows = load_bundle(args.prepared)
    output = separate_output(args.output, bundle, args.python)
    output.mkdir(parents=True)
    report = {
        "protocol": "native_diagnostic_not_official",
        "provider_calls": 0,
        "dataset_sha256": manifest["dataset_sha256"],
        "cases": [],
        "prepared_manifest_sha256": digest(bundle / "manifest.json"),
        **environment_identity(args.python),
    }
    for name in manifest["selection"]:
        row, case = rows[name], bundle / name
        blank = grade(
            row, case / "pristine", "", output / name / "blank", args.python, args.timeout
        )
        gold = grade(
            row, case / "pristine", row["patch"], output / name / "gold", args.python, args.timeout
        )
        blank_failures = all(
            blank.get("test_results", {}).get(t) == "FAILED" for t in row["FAIL_TO_PASS"]
        )
        # A negative control also needs all regression tests present and passing.
        blank_regressions = all(
            blank.get("test_results", {}).get(t) == "PASSED" for t in row["PASS_TO_PASS"]
        )
        valid = (
            bool(row["FAIL_TO_PASS"])
            and blank.get("status") == "unresolved"
            and blank_failures
            and blank_regressions
            and gold.get("resolved") is True
        )
        report["cases"].append(
            {"instance_id": name, "environment_validated": valid, "blank": blank, "gold": gold}
        )
        save(output / "report.json", report)
    report["environment_validated"] = all(c["environment_validated"] for c in report["cases"])
    save(output / "report.json", report)
    return report


def validate_calibration(path: Path, bundle: Path, manifest: dict, python: Path) -> None:
    report = json.loads(path.read_text(encoding="utf-8"))
    valid = {c["instance_id"] for c in report["cases"] if c["environment_validated"]}
    if (
        report["dataset_sha256"] != manifest["dataset_sha256"]
        or report["prepared_manifest_sha256"] != digest(bundle / "manifest.json")
        or any(report.get(k) != v for k, v in environment_identity(python).items())
        or not set(manifest["selection"]).issubset(valid)
    ):
        raise ValueError(
            "Every case needs successful blank/gold calibration with this bundle and environment"
        )


def capture_patch(pristine: Path, workspace: Path, output: Path) -> str:
    """Diff a fresh index; agent-controlled .git/config and ignored files cannot hide edits."""
    validate_tree(workspace)
    comparison = output / "comparison"
    shutil.copytree(pristine, comparison, symlinks=True)
    initialize_git(comparison)
    for path in comparison.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    shutil.copytree(
        workspace,
        comparison,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=lambda directory, names: [
            n for n in names if n in {".git", "__pycache__", ".pytest_cache", ".hypothesis"}
        ],
    )
    # Force-add intentional new files even if the candidate changed .gitignore.
    subprocess.run(
        ["git", "-C", str(comparison), "add", "-A", "-f"],
        env=git_env(),
        check=True,
        capture_output=True,
        timeout=60,
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(comparison),
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
        ],
        env=git_env(),
        check=True,
        capture_output=True,
        timeout=60,
    )
    return result.stdout.decode("utf-8")


def run_cases(args) -> dict:
    bundle, manifest, rows = selected_bundle(args)
    output = separate_output(args.output, bundle, args.python)
    validate_calibration(args.calibration, bundle, manifest, args.python)
    if not args.python.is_file():
        raise ValueError("Dedicated benchmark --python is missing")
    from evals.lka_evals.swe_sandbox import Sandbox

    for name in manifest["selection"]:
        status = Sandbox(bundle / name / "workspace", args.python).check()
        if not status["available"]:
            raise RuntimeError(status["error"])
    output.mkdir(parents=True)
    ledger = ignored_output(args.ledger or output / "budget.sqlite3", fresh=False)
    report = {
        "protocol": "native_diagnostic_not_official",
        "selected": manifest["selection"],
        "unselected_prepared": [
            n for n in manifest.get("prepared_selection", []) if n not in manifest["selection"]
        ],
        "dataset_sha256": manifest["dataset_sha256"],
        "ledger": str(args.ledger or output / "budget.sqlite3"),
        "cases": [],
        "limits": {
            "usd": args.usd_limit,
            "calls": args.call_limit,
            "case_seconds": args.timeout,
            "decision_steps": args.max_steps,
        },
    }
    predictions = []
    for name in manifest["selection"]:
        directory = output / name
        directory.mkdir()
        case = bundle / name
        # Fresh workspace per attempt: never resume a candidate or reuse gold controls.
        workspace = directory / "workspace"
        shutil.copytree(case / "pristine", workspace, symlinks=True)
        initialize_git(workspace)
        job = {
            "task": public_task(rows[name]),
            "workspace": str(workspace),
            "python": str(args.python.absolute()),
            "output": str(directory),
            "ledger": str(ledger),
            "local_config": str(args.local_config.resolve()),
            "env": str(args.env.resolve()),
            "usd_limit": args.usd_limit,
            "call_limit": args.call_limit,
            "max_steps": args.max_steps,
            "multi_agent": args.multi_agent,
            "safety_review_mode": args.safety_review,
        }
        save(directory / "job.json", job)
        started = time.monotonic()
        with (directory / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "evals.lka_evals.swe_runtime", str(directory / "job.json")],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=args.timeout)
                status = "finished" if exit_code == 0 else "runtime_error"
            except subprocess.TimeoutExpired:
                status, exit_code = "timeout", None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        runtime_report = directory / "runtime.json"
        entry = {
            "instance_id": name,
            "execution_status": status,
            "exit_code": exit_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "resolved": None,
        }
        if runtime_report.is_file():
            entry["runtime"] = json.loads(runtime_report.read_text(encoding="utf-8"))
        try:
            patch = capture_patch(case / "pristine", workspace, directory)
            (directory / "prediction.patch").write_text(patch, encoding="utf-8")
            predictions.append(
                {
                    "instance_id": name,
                    "model_name_or_path": entry.get("runtime", {}).get("model", "lka-agent"),
                    "model_patch": patch,
                }
            )
            entry["patch_bytes"] = len(patch.encode())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            entry.update(execution_status="artifact_error", error=f"{type(exc).__name__}: {exc}")
        report["cases"].append(entry)
        (output / "predictions.jsonl").write_text(
            "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in predictions), encoding="utf-8"
        )
        save(output / "report.json", report)
        # A budget refusal/transport failure should not become another full retry.
        if (
            entry["execution_status"] != "finished"
            or entry.get("runtime", {}).get("status") != "completed"
        ):
            break
    report["not_run"] = [
        n for n in manifest["selection"] if n not in {c["instance_id"] for c in report["cases"]}
    ]
    if ledger.is_file():
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as conn:
            values = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(charged),0),COALESCE(SUM(usage_known),0),"
                "COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) "
                "FROM calls WHERE kind='llm'"
            ).fetchone()
        report["budget_usage"] = dict(
            zip(
                (
                    "requests",
                    "charged_or_reserved_usd",
                    "known_usage_requests",
                    "input_tokens",
                    "output_tokens",
                ),
                values,
                strict=True,
            )
        )
    save(output / "report.json", report)
    return report


def grade_predictions(args) -> dict:
    from evals.lka_evals.swe_native import grade

    bundle, manifest, rows = selected_bundle(args)
    validate_calibration(args.calibration, bundle, manifest, args.python)
    predictions = [
        json.loads(s)
        for s in args.predictions.read_text(encoding="utf-8").splitlines()
        if s.strip()
    ]
    names = [p["instance_id"] for p in predictions]
    if len(names) != len(set(names)) or not set(names).issubset(rows):
        raise ValueError("Predictions contain duplicate or unselected IDs")
    output = separate_output(args.output, bundle, args.python)
    output.mkdir(parents=True)
    report = {
        "protocol": "native_diagnostic_not_official",
        "selected": manifest["selection"],
        "unselected_prepared": [
            n for n in manifest.get("prepared_selection", []) if n not in manifest["selection"]
        ],
        "predictions_sha256": digest(args.predictions),
        "cases": [],
    }
    for prediction in predictions:
        name = prediction["instance_id"]
        outcome = grade(
            rows[name],
            bundle / name / "pristine",
            prediction["model_patch"],
            output / name,
            args.python,
            args.timeout,
        )
        report["cases"].append({"instance_id": name, **outcome})
        save(output / "report.json", report)
    report["missing_predictions"] = sorted(set(manifest["selection"]) - set(names))
    report["resolved_count"] = sum(c.get("resolved") is True for c in report["cases"])
    report["selected_count"] = len(manifest["selection"])
    report["resolved_fraction_of_selected"] = report["resolved_count"] / len(manifest["selection"])
    save(output / "report.json", report)
    return report


def official_plan(args) -> dict:
    """Produce a pinned official grader command without installing/starting Docker."""
    bundle, manifest, _ = load_bundle(args.prepared)
    run_id = "lka-" + digest(args.predictions)[:12] + "-" + str(time.time_ns())
    command = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "swebench==5.0.2",
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        manifest["dataset"],
        "--predictions_path",
        str(args.predictions.resolve()),
        "--max_workers",
        "1",
        "--run_id",
        run_id,
        "--timeout",
        "1800",
    ]
    return {
        "protocol": "official_swebench",
        "execution": "plan_only",
        "provider_calls": 0,
        "requires": "Docker daemon and official harness dependencies; separate from LKA backend",
        "working_directory": str(bundle),
        "command": shlex.join(command),
        "run_id": run_id,
        "selected": manifest["selection"],
        "note": "Use a fresh run_id for every prediction hash; do not reuse cached grades.",
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="Explicit network download, no model calls")
    fetch.add_argument("--output", type=Path, default=DEFAULT_DATASET)
    for name in ("list", "prepare"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        cmd.add_argument("--case", action="append", default=[])
        cmd.add_argument("--pilot", action="store_true")
        cmd.add_argument("--sample", type=int)
        cmd.add_argument("--seed", type=int, default=20261020)
        if name == "prepare":
            cmd.add_argument("--output", type=Path, required=True)
    env = sub.add_parser("env", help="Install pinned dependencies into a dedicated environment")
    env.add_argument("--profile", choices=tuple(catalog()["native_profiles"]), required=True)
    env.add_argument("--output", type=Path, required=True)
    official = sub.add_parser(
        "official-plan", help="Print pinned official Docker grader command; no execution"
    )
    official.add_argument("--prepared", type=Path, required=True)
    official.add_argument("--predictions", type=Path, required=True)
    for name in ("calibrate", "run", "grade"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--prepared", type=Path, required=True)
        cmd.add_argument("--output", type=Path, required=True)
        cmd.add_argument("--python", type=Path, required=True)
        cmd.add_argument("--timeout", type=int, default=900 if name == "run" else 180)
        if name in {"run", "grade"}:
            cmd.add_argument("--case", action="append", default=[])
        if name == "grade":
            cmd.add_argument("--predictions", type=Path, required=True)
            cmd.add_argument("--calibration", type=Path, required=True)
        if name == "run":
            cmd.add_argument("--calibration", type=Path, required=True)
            cmd.add_argument("--allow-paid", action="store_true")
            cmd.add_argument("--local-config", type=Path, default=ROOT / "config/local.toml")
            cmd.add_argument("--env", type=Path, default=ROOT / ".env")
            cmd.add_argument("--ledger", type=Path)
            cmd.add_argument("--usd-limit", type=float, default=2)
            cmd.add_argument("--call-limit", type=int, default=120)
            cmd.add_argument("--max-steps", type=int, default=40)
            cmd.add_argument("--multi-agent", action="store_true")
            cmd.add_argument("--safety-review", choices=("skip", "llm", "manual"))
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    try:
        if hasattr(args, "timeout") and args.timeout < 1:
            raise ValueError("timeout must be positive")
        if args.command == "fetch":
            result = fetch_dataset(ignored_output(args.output))
        elif args.command in {"list", "prepare"}:
            rows = selection(args)
            if args.command == "list":
                result = {
                    "total": len(load_dataset(args.dataset)),
                    "selected": len(rows),
                    "cases": [
                        {
                            "instance_id": r["instance_id"],
                            "repo": r["repo"],
                            "version": r["version"],
                            "difficulty": r.get("difficulty"),
                        }
                        for r in rows
                    ],
                    "provider_calls": 0,
                }
            else:
                if not (args.pilot or args.case or args.sample):
                    raise ValueError("prepare requires --pilot, --case, or bounded --sample")
                result = prepare(args, rows)
        elif args.command == "env":
            result = create_environment(args)
        elif args.command == "official-plan":
            result = official_plan(args)
        elif args.command == "calibrate":
            result = calibration(args)
        elif args.command == "run":
            if not args.allow_paid:
                raise ValueError("Paid dispatch requires explicit --allow-paid")
            if (
                args.call_limit < 1
                or not math.isfinite(args.usd_limit)
                or args.usd_limit <= 0
                or args.max_steps < 1
            ):
                raise ValueError("Budgets and decision step limit must be positive")
            result = run_cases(args)
        else:
            result = grade_predictions(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "calibrate":
            return int(not result["environment_validated"])
        if args.command == "prepare":
            return int(any(c["status"] != "prepared" for c in result["cases"]))
        if args.command == "grade":
            return int(result["resolved_count"] != result["selected_count"])
        if args.command == "run":
            return int(
                bool(result["not_run"])
                or any(
                    c["execution_status"] != "finished"
                    or c.get("runtime", {}).get("status") != "completed"
                    for c in result["cases"]
                )
            )
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
