"""Native diagnostic grading contracts with a mocked isolation executor."""

import json
import shlex
from pathlib import Path

import pytest

from evals.lka_evals import swe_native


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "pristine"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_demo.py").write_text("ORIGINAL TEST\n")
    (root / ".git").mkdir()
    (root / ".git" / "secret").write_text("private git metadata")
    return root


def case():
    return {
        "FAIL_TO_PASS": ["tests/test_demo.py::test_fix"],
        "PASS_TO_PASS": '["tests/test_demo.py::test_keep"]',
        "test_patch": (
            "diff --git a/tests/test_demo.py b/tests/test_demo.py\n"
            "--- a/tests/test_demo.py\n+++ b/tests/test_demo.py\n"
            "@@ -1 +1 @@\n-ORIGINAL TEST\n+HELDOUT TEST\n"
        ),
    }


def install_sandbox(monkeypatch, *, stdout, exit_code=0, timed_out=False, candidate=None):
    calls = []

    class FakeSandbox:
        def __init__(self, workspace, python):
            self.workspace = workspace
            assert not (workspace / ".git").exists()

        def run(self, command, timeout):
            calls.append(command)
            if command.startswith("git apply"):
                if "prediction" in command and candidate:
                    candidate(self.workspace)
                if "heldout" in command:
                    assert (self.workspace / "tests/test_demo.py").read_text() == "ORIGINAL TEST\n"
                return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}
            assert not (self.workspace / ".lka-heldout.patch").exists()
            assert not (self.workspace / ".lka-prediction.patch").exists()
            assert timeout == 180
            return {
                "stdout": stdout,
                "stderr": "pytest stderr",
                "exit_code": exit_code,
                "timed_out": timed_out,
            }

    monkeypatch.setattr(swe_native, "Sandbox", FakeSandbox)
    return calls


def grade(checkout, tmp_path, row=None, prediction=""):
    return swe_native.grade(
        row or case(),
        checkout,
        prediction,
        tmp_path / "grade",
        Path("/venv/bin/python"),
    )


def test_pass_requires_both_sets_and_restores_candidate_test_changes(
    checkout, tmp_path, monkeypatch
):
    stdout = "PASSED tests/test_demo.py::test_fix\nPASSED tests/test_demo.py::test_keep\n"

    def candidate(workspace):
        (workspace / "tests/test_demo.py").unlink()

    calls = install_sandbox(monkeypatch, stdout=stdout, candidate=candidate)
    result = grade(checkout, tmp_path, prediction="candidate patch")
    assert result["status"] == "resolved"
    assert result["resolved"] is True
    assert result["grading_protocol"] == "native_diagnostic_not_official"
    assert result["counts"] == {"expected": 2, "observed": 2, "passed": 2}
    assert len(calls) == 4
    command = shlex.split(calls[-1])
    assert command == [
        "/venv/bin/python",
        "-m",
        "pytest",
        "-rA",
        "-v",
        "--tb=short",
        "--",
        "tests/test_demo.py::test_fix",
        "tests/test_demo.py::test_keep",
    ]
    assert (checkout / "tests/test_demo.py").read_text() == "ORIGINAL TEST\n"
    assert json.loads((tmp_path / "grade/report.json").read_text()) == result
    assert (tmp_path / "grade/pytest.stdout.txt").read_text() == stdout
    assert (tmp_path / "grade/pytest.stderr.txt").read_text() == "pytest stderr"


@pytest.mark.parametrize("status", ["FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS"])
def test_regressions_and_skips_never_resolve(checkout, tmp_path, monkeypatch, status):
    stdout = (
        f"PASSED tests/test_demo.py::test_fix\n{status} tests/test_demo.py::test_keep - detail\n"
    )
    install_sandbox(monkeypatch, stdout=stdout, exit_code=1 if status == "FAILED" else 0)
    result = grade(checkout, tmp_path)
    assert result["status"] == "unresolved"
    assert result["resolved"] is False
    assert result["not_passed"] == ["tests/test_demo.py::test_keep"]


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3, 4, 5])
def test_missing_exact_node_ids_are_environment_errors(checkout, tmp_path, monkeypatch, exit_code):
    install_sandbox(
        monkeypatch, stdout="PASSED tests/test_demo.py::test_fix_wrong\n", exit_code=exit_code
    )
    result = grade(checkout, tmp_path)
    assert result["status"] == "environment_error"
    assert result["resolved"] is None
    assert len(result["missing"]) == 2


def test_infrastructure_exit_dominates_passing_summary(checkout, tmp_path, monkeypatch):
    install_sandbox(
        monkeypatch,
        stdout="PASSED tests/test_demo.py::test_fix\nPASSED tests/test_demo.py::test_keep",
        exit_code=2,
    )
    assert grade(checkout, tmp_path)["status"] == "environment_error"


def test_timeout_is_not_a_failure_or_success_score(checkout, tmp_path, monkeypatch):
    install_sandbox(monkeypatch, stdout="", timed_out=True, exit_code=-9)
    result = grade(checkout, tmp_path)
    assert result["status"] == "timeout"
    assert result["resolved"] is None


@pytest.mark.parametrize(
    "name",
    [
        "/tmp/test.py::test",
        "../test.py::test",
        "-test.py::test",
        "tests/test.txt::test",
        "tests/../test.py::test",
        "tests/test.py\nmalice",
    ],
)
def test_unsafe_test_paths_fail_before_execution(checkout, tmp_path, monkeypatch, name):
    calls = install_sandbox(monkeypatch, stdout="")
    row = case()
    row["FAIL_TO_PASS"] = [name]
    assert grade(checkout, tmp_path, row)["status"] == "environment_error"
    assert calls == []


def test_heldout_paths_cannot_escape(checkout, tmp_path, monkeypatch):
    calls = install_sandbox(monkeypatch, stdout="")
    row = case()
    row["test_patch"] = row["test_patch"].replace("tests/test_demo.py", "../outside.py")
    assert grade(checkout, tmp_path, row)["status"] == "environment_error"
    assert not calls


def test_escaping_symlink_rejected_without_sandbox_execution(checkout, tmp_path, monkeypatch):
    calls = install_sandbox(monkeypatch, stdout="")
    (checkout / "leak").symlink_to(tmp_path)
    assert grade(checkout, tmp_path)["status"] == "environment_error"
    assert not calls


def test_internal_symlink_preserved(checkout, tmp_path, monkeypatch):
    install_sandbox(
        monkeypatch,
        stdout="PASSED tests/test_demo.py::test_fix\nPASSED tests/test_demo.py::test_keep\n",
    )
    (checkout / "test_link").symlink_to("tests/test_demo.py")
    assert grade(checkout, tmp_path)["resolved"] is True
    assert (tmp_path / "grade/workspace/test_link").is_symlink()


def test_patch_failure_and_existing_output(checkout, tmp_path, monkeypatch):
    class BrokenSandbox:
        def __init__(self, *args):
            pass

        def run(self, *args, **kwargs):
            return {"stdout": "", "stderr": "bad patch", "exit_code": 1, "timed_out": False}

    monkeypatch.setattr(swe_native, "Sandbox", BrokenSandbox)
    assert grade(checkout, tmp_path, prediction="bad patch")["status"] == "patch_error"
    with pytest.raises(FileExistsError):
        grade(checkout, tmp_path)


def test_exact_parametrized_ids_and_conflicting_summaries():
    expected = ["tests/test_demo.py::test_value[a - b]"]
    parsed = swe_native._parse("PASSED " + expected[0], expected)
    assert parsed == {expected[0]: "PASSED"}
    parsed = swe_native._parse(
        "PASSED " + expected[0] + "\nFAILED " + expected[0] + " - reason", expected
    )
    assert parsed == {expected[0]: "CONFLICT"}


def test_all_expected_test_files_restored_even_without_heldout_changes(
    checkout, tmp_path, monkeypatch
):
    regression = checkout / "tests/test_existing.py"
    regression.write_text("ORIGINAL REGRESSION\n")
    row = case()
    row["PASS_TO_PASS"] = ["tests/test_existing.py::test_regression"]

    def candidate(workspace):
        (workspace / "tests/test_existing.py").write_text("FAKE PASS\n")
        (workspace / "tests/test_candidate_only.py").write_text("new test\n")

    calls = install_sandbox(
        monkeypatch,
        stdout="PASSED tests/test_demo.py::test_fix\nPASSED tests/test_existing.py::test_regression\n",
        candidate=candidate,
    )
    assert grade(checkout, tmp_path, row, prediction="candidate patch")["resolved"] is True
    workspace = tmp_path / "grade/workspace"
    assert (workspace / "tests/test_existing.py").read_text() == "ORIGINAL REGRESSION\n"
    assert (workspace / "tests/test_candidate_only.py").exists()
    command = shlex.split(calls[-1])
    assert command[command.index("--") + 1 :] == [
        "tests/test_demo.py::test_fix",
        "tests/test_existing.py::test_regression",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "pytest.py",
        "tests/conftest.py",
        "pytest/__init__.py",
        "src/_pytest/runner.py",
        "sitecustomize.py",
        "usercustomize.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "bin/test",
        "sympy/utilities/runtests.py",
    ],
)
def test_candidate_runner_tampering_is_rejected_before_tests(checkout, tmp_path, monkeypatch, name):
    # Exercise both existing trusted configuration and newly introduced shadow modules.
    if name == "pyproject.toml":
        (checkout / name).write_text("original configuration\n")

    def candidate(workspace):
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("candidate-controlled runner\n")

    calls = install_sandbox(monkeypatch, stdout="", candidate=candidate)
    result = grade(checkout, tmp_path, prediction="runner modification")
    assert result["status"] == "patch_error"
    assert result["resolved"] is None
    assert "trusted pytest runner" in result["error"]
    assert len(calls) == 1


def test_sympy_aliases_resolve_from_applied_heldout_source_and_preserve_upstream_keys(
    checkout, tmp_path, monkeypatch
):
    before = "def test_existing():\n    pass\n"
    after = before + "\ndef test_new_issue():\n    pass\n"
    (checkout / "tests/test_demo.py").write_text(before)
    row = {
        "repo": "sympy/sympy",
        "FAIL_TO_PASS": ["test_new_issue"],
        "PASS_TO_PASS": ["test_existing"],
        "test_patch": "diff --git a/tests/test_demo.py b/tests/test_demo.py\n--- a/tests/test_demo.py\n+++ b/tests/test_demo.py\n@@ -1,2 +1,5 @@\n def test_existing():\n     pass\n+\n+def test_new_issue():\n+    pass\n",
    }
    commands = []

    class FakeSandbox:
        def __init__(self, workspace, python):
            self.workspace = workspace

        def run(self, command, timeout):
            commands.append(command)
            if command.startswith("git apply --whitespace"):
                (self.workspace / "tests/test_demo.py").write_text(after)
            stdout = (
                ""
                if command.startswith("git apply")
                else "tests/test_demo.py[2] \ntest_new_issue ok\ntest_existing ok [OK]\n"
            )
            return {"stdout": stdout, "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(swe_native, "Sandbox", FakeSandbox)
    result = grade(checkout, tmp_path, row)
    assert result["resolved"] is True
    assert result["test_results"] == {"test_new_issue": "PASSED", "test_existing": "PASSED"}
    assert result["test_nodeids"]["test_new_issue"] == "tests/test_demo.py::test_new_issue"
    command = shlex.split(commands[-1])
    assert command[1:] == [
        "/venv/bin/python",
        "bin/test",
        "-C",
        "--verbose",
        "--no-colors",
        "tests/test_demo.py",
    ]
    assert result["test_runner"] == "sympy_native"


@pytest.mark.parametrize("count", [0, 2])
def test_sympy_missing_or_ambiguous_alias_does_not_run_tests(
    checkout, tmp_path, monkeypatch, count
):
    (checkout / "tests/test_demo.py").write_text("def test_duplicate():\n    pass\n" * count)
    calls = []

    class FakeSandbox:
        def __init__(self, *args):
            pass

        def run(self, command, timeout):
            calls.append(command)
            assert command.startswith("git apply")
            return {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}

    row = case()
    row.update(repo="sympy/sympy", FAIL_TO_PASS=["test_duplicate"], PASS_TO_PASS=[])
    monkeypatch.setattr(swe_native, "Sandbox", FakeSandbox)
    result = grade(checkout, tmp_path, row)
    assert result["status"] == "environment_error"
    assert f"found {count}" in result["error"]
    assert len(calls) == 2


def test_relative_python_is_made_absolute_without_resolving_venv_link(
    checkout, tmp_path, monkeypatch
):
    calls = install_sandbox(
        monkeypatch,
        stdout="PASSED tests/test_demo.py::test_fix\nPASSED tests/test_demo.py::test_keep\n",
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "venv/bin").mkdir(parents=True)
    (tmp_path / "venv/bin/python").symlink_to("/usr/bin/python3")
    result = swe_native.grade(case(), checkout, "", tmp_path / "grade", Path("venv/bin/python"))
    assert result["resolved"] is True
    assert shlex.split(calls[-1])[0] == str(tmp_path / "venv/bin/python")


def test_native_sympy_reporter_exact_tokens_and_skip_messages():
    # SymPy's entering_filename/entering_test and test_* methods emit these tokens.
    stdout = """sympy/tests/test_one.py[9]
test_ok ok
test_failure F
test_error E
test_xfail f
test_xpass X
test_skip  - s
test_slow Slow w
test_timeout Timeout T
test_interrupt KeyboardInterrupt K [FAIL]
"""
    names = [
        "test_ok",
        "test_failure",
        "test_error",
        "test_xfail",
        "test_xpass",
        "test_skip",
        "test_slow",
        "test_timeout",
        "test_interrupt",
    ]
    nodes = {name: "sympy/tests/test_one.py::" + name for name in names}
    assert swe_native._parse_sympy(stdout, nodes) == dict(
        zip(
            names,
            [
                "PASSED",
                "FAILED",
                "FAILED",
                "XFAIL",
                "XPASS",
                "SKIPPED",
                "SKIPPED",
                "SKIPPED",
                "SKIPPED",
            ],
            strict=True,
        )
    )


def test_native_sympy_parser_requires_function_outcome_and_matching_source_file():
    nodes = {"test_expected": "sympy/tests/test_one.py::test_expected"}
    assert swe_native._parse_sympy("1 tests passed\ntest_expected\n[OK]\n", nodes) == {}
    assert swe_native._parse_sympy("sympy/tests/test_wrong.py[1]\ntest_expected ok\n", nodes) == {}
    assert swe_native._parse_sympy(
        "sympy/tests/test_one.py[1]\ntest_expected ok [OK]\n", nodes
    ) == {"test_expected": "PASSED"}
