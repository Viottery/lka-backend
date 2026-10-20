"""Offline contracts for benchmark preparation; no dataset/provider access."""

import hashlib
import io
import json
import sys
import types

import pytest

from evals.lka_evals import swe_dataset as dataset


def row(instance_id="owner__project-1"):
    return {
        "instance_id": instance_id,
        "repo": "owner/project",
        "base_commit": "a" * 40,
        "problem_statement": "Repair the reported behavior.",
        "version": "1.0",
        "patch": "GOLD",
        "test_patch": "SECRET TESTS",
        "FAIL_TO_PASS": '["test_regression"]',
        "PASS_TO_PASS": ["test_existing"],
        "hints_text": "SECRET HINT",
        "image": "SECRET IMAGE",
    }


def write_rows(tmp_path, rows):
    path = tmp_path / "dataset.jsonl"
    path.write_text("\n".join(json.dumps(value) for value in rows), encoding="utf-8")
    return path


def test_load_normalizes_without_mutation_and_public_projection(tmp_path):
    original = row()
    path = write_rows(tmp_path, [original])
    loaded = dataset.load_dataset(path)
    assert loaded == dataset.load_dataset(tmp_path)
    assert loaded[0]["FAIL_TO_PASS"] == ["test_regression"]
    assert original["FAIL_TO_PASS"] == '["test_regression"]'
    public = dataset.public_task(loaded[0])
    assert set(public) == {"instance_id", "repo", "base_commit", "problem_statement", "version"}
    assert "SECRET" not in json.dumps(public)
    assert "GOLD" not in json.dumps(public)


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", "../../escape"),
        ("instance_id", "invalid"),
        ("repo", "../owner/project"),
        ("repo", "owner/project/extra"),
        ("base_commit", "main"),
        ("problem_statement", "  "),
        ("patch", None),
        ("test_patch", []),
        ("FAIL_TO_PASS", "not json"),
        ("FAIL_TO_PASS", "[1]"),
        ("PASS_TO_PASS", {}),
        ("version", 1),
    ],
)
def test_load_rejects_invalid_records(tmp_path, field, value):
    record = row()
    record[field] = value
    with pytest.raises(ValueError):
        dataset.load_dataset(write_rows(tmp_path, [record]))


def test_load_rejects_duplicate_ids_and_bad_json(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        dataset.load_dataset(write_rows(tmp_path, [row(), row()]))
    path = tmp_path / "bad.jsonl"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        dataset.load_dataset(path)


def test_selection_is_exact_and_seeded_independent_of_input_order():
    rows = [row(f"owner__project-{i}") for i in range(20)]
    selected = dataset.select_cases(rows, [], limit=5, seed=17)
    assert selected == dataset.select_cases(list(reversed(rows)), [], limit=5, seed=17)
    assert selected != dataset.select_cases(rows, [], limit=5, seed=18)
    ids = [rows[3]["instance_id"], rows[1]["instance_id"]]
    assert dataset.select_cases(rows, ids) == [rows[3], rows[1]]
    assert dataset.select_cases(rows, ids, limit=1) == [rows[3]]
    for invalid_ids in [["missing--id"], [ids[0], ids[0]]]:
        with pytest.raises(ValueError):
            dataset.select_cases(rows, invalid_ids)
    for invalid_limit in [0, -1, True, 21]:
        with pytest.raises(ValueError):
            dataset.select_cases(rows, [], limit=invalid_limit)


def install_fake_parquet(monkeypatch, rows):
    module = types.ModuleType("pyarrow")
    parquet = types.ModuleType("pyarrow.parquet")
    parquet.read_table = lambda path: types.SimpleNamespace(to_pylist=lambda: rows)
    module.parquet = parquet
    monkeypatch.setitem(sys.modules, "pyarrow", module)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", parquet)


def test_fetch_uses_pinned_sources_and_retains_hashes(tmp_path, monkeypatch):
    rows = [row(f"owner__project-{i}") for i in range(500)]
    install_fake_parquet(monkeypatch, rows)
    requests = []

    def fake_urlopen(url, timeout):
        requests.append((url, timeout))
        return io.BytesIO(b"mock-source")

    monkeypatch.setattr(dataset, "urlopen", fake_urlopen)
    destination = tmp_path / "new"
    manifest = dataset.fetch_dataset(destination)
    assert len(dataset.load_dataset(destination)) == 500
    assert manifest == json.loads((destination / "manifest.json").read_text())
    assert manifest["source"]["revision"] == "78f471bf655a3137b2e8a75af1501690ec009ec3"
    assert all(dataset.DEFAULT_SOURCE["revision"] in url for url, _ in requests)
    assert len(requests) == 2
    assert all(timeout == 30 for _, timeout in requests)
    assert manifest["files"][0]["sha256"] == hashlib.sha256(b"mock-source").hexdigest()
    assert (
        manifest["dataset_sha256"]
        == hashlib.sha256((destination / "dataset.jsonl").read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        dataset.fetch_dataset(destination)
    assert len(requests) == 2


def test_fetch_rejects_count_mismatch_without_success_manifest(tmp_path, monkeypatch):
    install_fake_parquet(monkeypatch, [row()])
    monkeypatch.setattr(dataset, "urlopen", lambda *args, **kwargs: io.BytesIO(b"source"))
    destination = tmp_path / "incomplete"
    with pytest.raises(ValueError, match="Expected 500"):
        dataset.fetch_dataset(destination)
    assert not (destination / "manifest.json").exists()


def test_missing_optional_dependency_is_actionable_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    destination = tmp_path / "missing"
    with pytest.raises(RuntimeError, match="uv run --no-project --with pyarrow==19.0.1"):
        dataset.fetch_dataset(destination)
    assert not destination.exists()
