"""Record and verify deployed code content, without copying private config/data.

Release identity includes dirty and untracked application files, not just Git
HEAD. A deployment still needs a backend restart to load the verified snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def fingerprint(root):
    root = Path(root).resolve()
    files = [p for p in (root / "app").rglob("*.py") if "__pycache__" not in p.parts]
    files.extend(root / name for name in ("pyproject.toml", "uv.lock", "scripts/start_backend.py"))
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def release_id(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(target):
    target = Path(target).resolve()
    # Only an established application install is accepted, never a broad root.
    if not (target / "app/core/runtime.py").is_file() or not (target / "config/local.toml").is_file():
        raise ValueError("Not an established backend installation")
    backup = target / "data/deployment_backups" / datetime.now(UTC).strftime("code-%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, exist_ok=False)
    for name in ("app", "scripts", "config"):
        shutil.copytree(target / name, backup / name, ignore=shutil.ignore_patterns("__pycache__", "cache"))
    for name in ("pyproject.toml", "uv.lock", ".env"):
        if (target / name).is_file():
            shutil.copy2(target / name, backup / name)
    files = fingerprint(target)
    manifest = {"created_at": datetime.now(UTC).isoformat(), "release_id": release_id(files), "files": files}
    (backup / "previous_code_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"backup_path": str(backup), "previous_release_id": manifest["release_id"]}


def preflight(target):
    db = Path(target).resolve() / "data/runtime/lka.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        runs = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_confirmation','waiting_user')").fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status='running'").fetchone()[0]
    if runs or jobs:
        raise ValueError(f"Wait for an idle point: active_runs={runs}, running_jobs={jobs}")
    return {"active_runs": runs, "running_jobs": jobs}


def verify(source, target, *, record=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    expected, actual = fingerprint(source), fingerprint(target)
    mismatches = sorted(name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name))
    if mismatches:
        raise ValueError(f"Production code mismatch: {mismatches}")
    identifier = release_id(expected)
    result = {"release_id": identifier, "file_count": len(expected), "identical": True}
    if record:
        git = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
        manifest = {**result, "created_at": datetime.now(UTC).isoformat(),
                    "source_git_head": git.stdout.strip() if git.returncode == 0 else None,
                    "source": str(source), "production": str(target), "files": expected}
        path = target / "data/runtime/production_release.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify", "record", "preflight"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "snapshot":
        result = snapshot(args.target)
    elif args.action == "preflight":
        result = preflight(args.target)
    else:
        result = verify(args.source, args.target, record=args.action == "record")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
