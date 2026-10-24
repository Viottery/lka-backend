"""Create/verify a private, offline data snapshot without merging or restoring databases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

FORMAT = 1
SQLITE_HEADER = b"SQLite format 3\0"
TABLES = ("agent_sessions", "agent_session_messages", "memory_entries", "memory_sources",
          "agent_runs", "background_jobs", "manager_controllers", "manager_executions",
          "accounts", "devices", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inventory(root):
    """Do not follow links, including links to private files outside the dataset."""
    result = {}
    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError("Unsupported special file in data directory")
            result[path.relative_to(root).as_posix()] = (
                info.st_mode, info.st_size, info.st_mtime_ns, os.readlink(path) if path.is_symlink() else None)
    return result


def database_summary(path):
    result = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result["rows"] = {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                          for name in TABLES if name in tables}
        for name in ("agent_runs", "background_jobs"):
            if name in tables:
                result[name + "_states"] = dict(conn.execute(f'SELECT status,count(*) FROM "{name}" GROUP BY status'))
    return result


def copy_database(source, destination):
    deadline = time.monotonic() + 60

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise ValueError("Database backup timed out; stop all writers before retrying")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256, progress=progress)
        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Snapshot database integrity check failed")
        # Destination is independent of source WAL/SHM files.
        dst.execute("PRAGMA journal_mode=DELETE")


def create(data_dir, output, *, offline=False, env_file=None, config_file=None):
    source, output = Path(data_dir).resolve(), Path(output).resolve()
    if not offline:
        raise ValueError("Stop all writers and explicitly pass --offline")
    if not source.is_dir() or not (source / "lka.sqlite3").is_file():
        raise ValueError("Source must be an existing backend data directory")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Snapshot and source directories must not contain one another")
    summary = database_summary(source / "lka.sqlite3")
    if any(summary.get("agent_runs_states", {}).get(state, 0) for state in ("queued", "running")) or summary.get("background_jobs_states", {}).get("running", 0):
        raise ValueError("Source has active runs/jobs; reach an idle point before stopping writers")
    before = inventory(source)
    databases = set()
    for name, entry in before.items():
        if stat.S_ISREG(entry[0]):
            with (source / name).open("rb") as stream:
                if stream.read(16) == SQLITE_HEADER:
                    databases.add(name)
    sidecars = {name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = {}
    for name, entry in sorted(before.items()):
        if name in sidecars:
            continue
        target = output / "data" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(entry[0]):
            target.symlink_to(entry[3], target_is_directory=(source / name).is_dir())
            records["data/" + name] = {"kind": "symlink", "target": entry[3]}
        elif stat.S_ISDIR(entry[0]):
            target.mkdir(exist_ok=True)
            records["data/" + name] = {"kind": "directory"}
        else:
            if name in databases:
                copy_database(source / name, target)
            else:
                shutil.copy2(source / name, target)
            records["data/" + name] = {"kind": "file", "size": target.stat().st_size, "sha256": digest(target)}
    for label, path in (("backend.env", env_file), ("local.toml", config_file)):
        if path is None:
            continue
        path = Path(path).resolve(strict=True)
        target = output / "private" / label
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(path, target)
        records["private/" + label] = {"kind": "file", "size": target.stat().st_size, "sha256": digest(target)}
    # SQLite readers can update SHM locks or create an empty WAL on Windows.
    # Neither contains a new committed frame. Keep checking every nonempty WAL.
    def relevant(items):
        return {key: (value[0], None, None, None) if stat.S_ISDIR(value[0]) else value
                for key, value in items.items()
                if not (key in sidecars and (key.endswith("-shm")
                        or key.endswith("-wal") and value[1] == 0))}
    if relevant(inventory(source)) != relevant(before):
        raise ValueError("Source changed during snapshot; incomplete output has no manifest, retry after stopping writers")
    manifest = {"format": FORMAT, "created_at": datetime.now(UTC).isoformat(),
                "source_data_dir": str(source), "offline_asserted": True, "database_summary": summary,
                "databases": sorted(databases), "records": records}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    verify(output)
    return {"snapshot": str(output), "entries": len(records), "databases": len(databases),
            "bytes": sum(item.get("size", 0) for item in records.values()), "verified": True}


def verify(output):
    output = Path(output).resolve()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("records"), dict):
        raise ValueError("Unsupported snapshot manifest")
    expected = set(manifest["records"])
    for name, record in manifest["records"].items():
        parts = name.split("/")
        if not parts or parts[0] not in {"data", "private"} or any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
            raise ValueError("Invalid snapshot-relative path")
        target = output.joinpath(*parts)
        # Never traverse an ancestor symlink while verifying untrusted manifests.
        if any(parent.is_symlink() for parent in target.parents if parent != output and parent.is_relative_to(output)):
            raise ValueError("Snapshot record traverses a symlink")
        kind = record.get("kind")
        if kind == "symlink":
            valid = target.is_symlink() and os.readlink(target) == record.get("target")
        elif kind == "directory":
            valid = target.is_dir() and not target.is_symlink()
        else:
            valid = (kind == "file" and target.is_file() and not target.is_symlink()
                     and target.stat().st_size == record.get("size") and digest(target) == record.get("sha256"))
        if not valid:
            raise ValueError("Snapshot content mismatch")
    actual = set(inventory(output)) - {"manifest.json", "data", "private"}
    if actual != expected:
        raise ValueError("Snapshot contains missing or unexpected entries")
    return {"verified": True, "entries": len(expected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--data-dir", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.add_argument("--offline", action="store_true")
    create_parser.add_argument("--env-file", type=Path)
    create_parser.add_argument("--config-file", type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (create(args.data_dir, args.output, offline=args.offline, env_file=args.env_file,
                         config_file=args.config_file) if args.command == "create" else verify(args.snapshot))
    except (OSError, ValueError, sqlite3.Error) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"Snapshot failed: {detail}; keep incomplete snapshots private.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
