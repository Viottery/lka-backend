"""Pinned SWE-bench data preparation; importing this module performs no I/O."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from pathlib import Path
from urllib.request import urlopen

DEFAULT_SOURCE = {
    "dataset": "SWE-bench/SWE-bench_Verified",
    "revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
    "split": "test",
    "parquet_path": "data/test-00000-of-00001.parquet",
    "expected_instances": 500,
    "base_url": "https://huggingface.co/datasets",
}
_INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*__[A-Za-z0-9][A-Za-z0-9_-]*")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")


def _normalize(rows: list[dict]) -> list[dict]:
    normalized, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each dataset row must be an object")  # noqa: TRY004 - invalid serialized data
        row = dict(row)
        for field, pattern in (
            ("instance_id", _INSTANCE),
            ("repo", _REPO),
            ("base_commit", _COMMIT),
        ):
            value = row.get(field)
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"Invalid {field} in dataset row")
        instance_id = row["instance_id"]
        if instance_id in seen:
            raise ValueError(f"Duplicate instance_id: {instance_id}")
        seen.add(instance_id)
        statement = row.get("problem_statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"Missing problem_statement: {instance_id}")
        for field in ("patch", "test_patch"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"Invalid {field}: {instance_id}")  # noqa: TRY004 - invalid serialized data
        for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            value = row.get(field)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise ValueError(f"Invalid {field}: {instance_id}") from exc
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{field} must be a string list: {instance_id}")
            row[field] = value.copy()
        row.setdefault("version", "")
        if not isinstance(row["version"], str):
            raise ValueError(f"Invalid version: {instance_id}")  # noqa: TRY004 - invalid serialized data
        normalized.append(row)
    return normalized


def load_dataset(path: Path) -> list[dict]:
    """Load and validate normalized JSONL; gold fields remain evaluator-only."""
    path = Path(path)
    if path.is_dir():
        path = path / "dataset.jsonl"
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from exc
    return _normalize(rows)


def select_cases(
    rows: list[dict], ids: list[str], limit: int | None = None, seed: int = 0
) -> list[dict]:
    """Select exact IDs or a reproducible sample independent of source ordering."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate selected instance IDs")
    index = {row["instance_id"]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError("Duplicate dataset instance IDs")
    unknown = set(ids) - index.keys()
    if unknown:
        raise ValueError(f"Unknown instance IDs: {', '.join(sorted(unknown))}")
    selected = [index[key] for key in ids] if ids else [index[key] for key in sorted(index)]
    if limit is not None:
        if limit > len(selected):
            raise ValueError("limit exceeds selected dataset size")
        selected = selected[:limit] if ids else random.Random(seed).sample(selected, limit)
    return selected


def public_task(row: dict) -> dict:
    """An explicit allowlist prevents hints, gold patches and tests leaking."""
    return {field: row.get(field, "") for field in _PUBLIC_FIELDS}


def _download(url: str, destination: Path) -> dict:
    deadline = time.monotonic() + 120
    digest, size = hashlib.sha256(), 0
    with urlopen(url, timeout=30) as response, destination.open("xb") as handle:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Dataset download exceeded 120 seconds")
            block = response.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > 64 * 1024 * 1024:
                raise ValueError("Dataset source exceeds 64 MiB download ceiling")
            digest.update(block)
            handle.write(block)
    return {"url": url, "sha256": digest.hexdigest(), "bytes": size, "file": destination.name}


def fetch_dataset(output_dir: Path) -> dict:
    """Fetch one immutable public snapshot; existing output is never overwritten."""
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Output directory already exists: {output_dir}")
    try:
        from pyarrow import parquet
    except ImportError as exc:
        raise RuntimeError(
            "Dataset conversion requires pyarrow. Re-run the fetch command with "
            "uv run --no-project --with pyarrow==19.0.1 python ..."
        ) from exc
    output_dir.mkdir(parents=True, exist_ok=False)
    source = dict(DEFAULT_SOURCE)
    prefix = f"{source['base_url']}/{source['dataset']}/resolve/{source['revision']}"
    files = [
        _download(f"{prefix}/{source['parquet_path']}", output_dir / "source.parquet"),
        _download(f"{prefix}/README.md", output_dir / "README.md"),
    ]
    rows = _normalize(parquet.read_table(output_dir / "source.parquet").to_pylist())
    if len(rows) != source["expected_instances"]:
        raise ValueError(f"Expected {source['expected_instances']} instances, received {len(rows)}")
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    encoded = payload.encode("utf-8")
    (output_dir / "dataset.jsonl").write_bytes(encoded)
    manifest = {
        "schema_version": 1,
        "source": source,
        "instance_count": len(rows),
        "files": files,
        "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
