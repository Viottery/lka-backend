"""Isolated native diagnostic grading, explicitly separate from SWE-bench scoring."""

from __future__ import annotations

import ast
import json
import re
import shlex
import shutil
from pathlib import Path, PurePosixPath

from evals.lka_evals.swe_sandbox import Sandbox

PROTOCOL = "native_diagnostic_not_official"
_SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (.+)$")


def _relative_path(value: str, *, python_file: bool = False) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in ("..", ".git") or part.startswith("-") for part in path.parts)
        or value != path.as_posix()
        or value.startswith("-")
        or (python_file and path.suffix != ".py")
    ):
        raise ValueError(f"Unsafe benchmark path: {value!r}")
    return value


def _expected(row: dict) -> list[str]:
    names = []
    for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        values = row[field]
        if isinstance(values, str):
            values = json.loads(values)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{field} must contain test node IDs")
        for value in values:
            if not value or "\n" in value or "\r" in value:
                raise ValueError("Invalid test node ID")
            if not (row.get("repo") == "sympy/sympy" and value.isidentifier()):
                _relative_path(value.split("::", 1)[0], python_file=True)
            if value not in names:
                names.append(value)
    if not names:
        raise ValueError("No expected test node IDs")
    return names


def _resolve_nodes(expected: list[str], workspace: Path, heldout: list[str]) -> dict[str, str]:
    """Resolve upstream SymPy labels from source structure, never test outcomes."""
    aliases = [name for name in expected if name.isidentifier()]
    candidates = {name: [] for name in aliases}

    def functions(nodes, classes=()):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                yield from functions(node.body, (*classes, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node.name, "::".join((*classes, node.name))

    if aliases:
        for name in heldout:
            path = workspace / name
            if path.suffix != ".py" or not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
            for function_name, qualified in functions(tree.body):
                if function_name in candidates:
                    candidates[function_name].append(f"{name}::{qualified}")
    result = {}
    for name in expected:
        if name not in candidates:
            result[name] = name
        elif len(candidates[name]) != 1:
            raise ValueError(
                f"Native test alias {name!r} requires exactly one heldout function; found {len(candidates[name])}"
            )
        else:
            result[name] = candidates[name][0]
    return result


def _heldout_paths(patch: str) -> list[str]:
    paths = []
    in_header = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            parts = shlex.split(line[len("diff --git ") :])
            if len(parts) != 2:
                raise ValueError("Malformed heldout diff header")
            for part, prefix in zip(parts, ("a/", "b/"), strict=True):
                if not part.startswith(prefix):
                    raise ValueError("Malformed heldout diff path")
                paths.append(_relative_path(part[2:]))
        elif in_header and line.startswith(("--- ", "+++ ")):
            part = line[4:].split("\t", 1)[0]
            if part == "/dev/null":
                continue
            if part.startswith('"'):
                part = json.loads(part)
            if not part.startswith(("a/", "b/")):
                raise ValueError("Malformed heldout diff path")
            paths.append(_relative_path(part[2:]))
        elif line.startswith("@@"):
            in_header = False
    if patch.strip() and not paths:
        raise ValueError("Heldout patch has no supported diff headers")
    return list(dict.fromkeys(paths))


def _check_symlinks(workspace: Path) -> None:
    for item in workspace.rglob("*"):
        if item.is_symlink() and not item.resolve().is_relative_to(workspace.resolve()):
            raise ValueError(f"Symlink leaves grading workspace: {item.relative_to(workspace)}")


def _runner_snapshot(workspace: Path) -> dict:
    """Freeze runner/config files separately from code the issue asks to repair."""
    snapshot = {}
    for item in workspace.rglob("*"):
        relative = item.relative_to(workspace)
        if not (
            item.name in {"conftest.py", "pytest.py", "sitecustomize.py", "usercustomize.py"}
            or "_pytest" in relative.parts
            or (item.name == "pytest" and item.is_symlink())
            or relative.as_posix().endswith("pytest/__init__.py")
            or relative.as_posix() in {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}
            or relative.as_posix() in {"bin/test", "sympy/utilities/runtests.py"}
        ):
            continue
        if item.is_symlink():
            value = ("symlink", item.readlink().as_posix())
        elif item.is_dir():
            value = ("directory",)
        else:
            value = ("file", item.read_bytes())
        snapshot[relative.as_posix()] = value
    return snapshot


def _restore(pristine: Path, workspace: Path, paths: list[str]) -> None:
    for name in paths:
        source, target = pristine / name, workspace / name
        # Never follow candidate-controlled parent links during host-side restoration.
        for parent in target.parents:
            if parent == workspace:
                break
            if parent.is_symlink():
                raise ValueError("Candidate replaced a heldout test parent with a symlink")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        if source.is_file() or source.is_symlink():
            if not source.resolve().is_relative_to(pristine):
                raise ValueError("Heldout source symlink leaves pristine checkout")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        elif source.exists():
            raise ValueError("Heldout patch path names a directory")


def _parse(stdout: str, expected: list[str]) -> dict[str, str]:
    results = {}
    for line in stdout.splitlines():
        match = _SUMMARY.fullmatch(line.strip())
        if match is None:
            continue
        status, tail = match.groups()
        # Match whole node IDs first: parameter values can themselves contain ' - '.
        matched = [name for name in expected if tail == name or tail.startswith(name + " - ")]
        name = max(matched, key=len) if matched else tail.split(" - ", 1)[0]
        # Conflicting duplicate observations cannot turn a failure into a pass.
        if name in results and results[name] != status:
            results[name] = "CONFLICT"
        else:
            results[name] = status
    return results


def _parse_sympy(stdout: str, nodes: dict[str, str]) -> dict[str, str]:
    """Parse the native verbose reporter's per-function tokens, never totals."""
    results = {}
    active_file = None
    for line in stdout.splitlines():
        line = line.strip()
        header = re.match(r"^(.+\.py)\[\d+\]", line)
        if header:
            active_file = header.group(1)
            continue
        match = re.fullmatch(r"([^\W\d]\w*)\s+(.+?)\s*(?:\[(?:OK|FAIL)\])?", line)
        if match is None:
            continue
        name, token = match.groups()
        token = token.strip()
        aliases = [
            alias
            for alias, node in nodes.items()
            if node.rsplit("::", 1)[-1] == name
            and (active_file is None or node.split("::", 1)[0] == active_file)
        ]
        if len(aliases) != 1:
            continue
        # E is a caught exception in a test body, not a runner infrastructure error.
        status = {
            "ok": "PASSED",
            "F": "FAILED",
            "E": "FAILED",
            "T": "ERROR",
            "f": "XFAIL",
            "X": "XPASS",
        }.get(token)
        if status is None and (token in {"s", "w", "K"} or re.search(r"\s[sTwK]$", token)):
            status = "SKIPPED"
        if status is not None:
            alias = aliases[0]
            results[alias] = (
                status if alias not in results or results[alias] == status else "CONFLICT"
            )
    return results


def grade(
    row: dict,
    pristine: Path,
    prediction: str,
    output: Path,
    python: Path,
    timeout: int = 180,
) -> dict:
    """Grade in a fresh isolated copy, withholding all oracle data during execution."""
    python = Path(python).absolute()
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError(f"Grading output already exists: {output}")
    pristine = Path(pristine).resolve(strict=True)
    if output.resolve().is_relative_to(pristine):
        raise ValueError("Grading output must be outside pristine checkout")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "grading_protocol": PROTOCOL,
        "status": "environment_error",
        "resolved": None,
        "counts": {"expected": 0, "observed": 0, "passed": 0},
        "missing": [],
        "not_passed": [],
        "test_results": {},
        "commands": [],
    }

    def finish(status, error=None):
        report["status"] = status
        report["resolved"] = status == "resolved" if status in {"resolved", "unresolved"} else None
        if error:
            report["error"] = str(error)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report

    def run(stage, command):
        result = sandbox.run(command, timeout=timeout)
        report["commands"].append({"stage": stage, "command": command, **result})
        (output / f"{stage}.stdout.txt").write_text(result["stdout"], encoding="utf-8")
        (output / f"{stage}.stderr.txt").write_text(result["stderr"], encoding="utf-8")
        (output / f"{stage}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result

    try:
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        expected = _expected(row)
        report["counts"]["expected"] = len(expected)
        heldout = _heldout_paths(row["test_patch"])
        workspace = output / "workspace"
        shutil.copytree(pristine, workspace, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _check_symlinks(workspace)
        original_runner = _runner_snapshot(workspace)
        sandbox = Sandbox(workspace, python)
        if prediction.strip():
            # Store only the candidate patch inside the exposed worktree.
            patch_file = workspace / ".lka-prediction.patch"
            if patch_file.exists() or patch_file.is_symlink():
                raise ValueError("Checkout contains reserved prediction patch path")
            patch_file.write_text(prediction, encoding="utf-8")
            outcome = run(
                "prediction_apply", "git apply --whitespace=nowarn - < .lka-prediction.patch"
            )
            patch_file.unlink(missing_ok=True)
            if outcome["timed_out"]:
                return finish("timeout", "Candidate patch application timed out")
            if outcome["exit_code"] != 0:
                return finish("patch_error", "Candidate patch did not apply")
        if _runner_snapshot(workspace) != original_runner:
            return finish(
                "patch_error",
                "Candidate modified or shadowed the trusted pytest runner/configuration",
            )
        _check_symlinks(workspace)
        expected_files = [name.split("::", 1)[0] for name in expected if not name.isidentifier()]
        _restore(pristine, workspace, list(dict.fromkeys([*heldout, *expected_files])))
        _check_symlinks(workspace)
        if row["test_patch"].strip():
            # The patch is removed before pytest; only its resulting files are exposed.
            patch_file = workspace / ".lka-heldout.patch"
            if patch_file.exists() or patch_file.is_symlink():
                raise ValueError("Checkout contains reserved heldout patch path")
            patch_file.write_text(row["test_patch"], encoding="utf-8")
            outcome = run(
                "heldout_check", "git apply --check --whitespace=nowarn - < .lka-heldout.patch"
            )
            if not outcome["timed_out"] and outcome["exit_code"] == 0:
                outcome = run(
                    "heldout_apply", "git apply --whitespace=nowarn - < .lka-heldout.patch"
                )
            patch_file.unlink(missing_ok=True)
            if outcome["timed_out"]:
                return finish("timeout", "Heldout patch application timed out")
            if outcome["exit_code"] != 0:
                return finish(
                    "patch_error", "Heldout patch did not apply after pristine restoration"
                )
        _check_symlinks(workspace)
        resolved_nodes = _resolve_nodes(expected, workspace, heldout)
        report["test_nodeids"] = resolved_nodes
        sympy_native = row.get("repo") == "sympy/sympy"
        if sympy_native:
            files = list(dict.fromkeys(node.split("::", 1)[0] for node in resolved_nodes.values()))
            command = "PYTHONWARNINGS=ignore::UserWarning,ignore::SyntaxWarning " + shlex.join(
                [str(python), "bin/test", "-C", "--verbose", "--no-colors", *files]
            )
        else:
            command = shlex.join(
                [
                    str(python),
                    "-m",
                    "pytest",
                    "-rA",
                    "-v",
                    "--tb=short",
                    "--",
                    *resolved_nodes.values(),
                ]
            )
        report["test_runner"] = "sympy_native" if sympy_native else "pytest"
        outcome = run("sympy" if sympy_native else "pytest", command)
        if sympy_native:
            results = _parse_sympy(outcome["stdout"], resolved_nodes)
        else:
            observed = _parse(outcome["stdout"], list(resolved_nodes.values()))
            results = {
                alias: observed[node] for alias, node in resolved_nodes.items() if node in observed
            }
        missing = [name for name in expected if name not in results]
        report.update(
            test_results=results,
            missing=missing,
            not_passed=[name for name in expected if results.get(name) != "PASSED"],
            counts={
                "expected": len(expected),
                "observed": sum(name in results for name in expected),
                "passed": sum(results.get(name) == "PASSED" for name in expected),
            },
        )
        if outcome["timed_out"]:
            return finish("timeout")
        if outcome["exit_code"] not in (0, 1) or missing:
            return finish(
                "environment_error",
                "Test infrastructure failure or missing required test identities",
            )
        if outcome["exit_code"] != 0 or report["not_passed"]:
            return finish("unresolved")
        return finish("resolved")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        return finish("environment_error", exc)
