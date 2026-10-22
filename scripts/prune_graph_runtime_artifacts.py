"""Remove superseded LangGraph runtime snapshots from finished runs.

Operator script, not an Agent tool. Default is a read-only preview; pass
--apply to delete. For every finished run (completed, failed, cancelled,
timed_out) it keeps the one `agent_graph_runtime` artifact that the run's
latest checkpoint references, and deletes the older per-step copies.
Unfinished runs are never touched. Back up the database first and run it while
the backend is idle; VACUUM afterwards (--vacuum) returns the space to disk.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.session_admission import TERMINAL_RUN_STATUSES


def latest_runtime_refs(checkpoint_db: Path) -> dict[str, str]:
    """thread (run) id -> runtime artifact id referenced by its newest root checkpoint."""
    serializer = JsonPlusSerializer()
    latest: dict[str, tuple[str, str]] = {}
    with sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT thread_id, checkpoint_id, type, checkpoint FROM checkpoints WHERE checkpoint_ns = ''"
        )
        for thread_id, checkpoint_id, kind, blob in rows:
            values = serializer.loads_typed((kind, blob)).get("channel_values") or {}
            ref = values.get("runtime_artifact_ref")
            if not isinstance(ref, dict) or not isinstance(ref.get("artifact_id"), str):
                continue
            if thread_id not in latest or checkpoint_id > latest[thread_id][0]:
                latest[thread_id] = (checkpoint_id, ref["artifact_id"])
    return {thread: artifact for thread, (_, artifact) in latest.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data/runtime"))
    parser.add_argument("--apply", action="store_true", help="delete; without it only report")
    parser.add_argument("--vacuum", action="store_true", help="VACUUM after deleting")
    args = parser.parse_args()
    main_db, checkpoint_db = args.data_dir / "lka.sqlite3", args.data_dir / "agent_checkpoints.sqlite3"
    keep = latest_runtime_refs(checkpoint_db)
    with sqlite3.connect(main_db, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        statuses = dict(conn.execute("SELECT run_id, status FROM agent_runs"))
        rows = conn.execute(
            "SELECT artifact_id, run_id, length(payload) FROM agent_run_artifacts WHERE kind = 'agent_graph_runtime'"
        ).fetchall()
        doomed = [
            (artifact_id, size) for artifact_id, run_id, size in rows
            if statuses.get(run_id) in TERMINAL_RUN_STATUSES
            and run_id in keep and artifact_id != keep[run_id]
        ]
        skipped = sum(1 for _, run_id, _ in rows if statuses.get(run_id) in TERMINAL_RUN_STATUSES and run_id not in keep)
        print(f"runtime snapshots: {len(rows)}; superseded in finished runs: {len(doomed)} "
              f"({sum(size for _, size in doomed) / 1e6:.0f} MB); finished runs without a checkpoint ref kept: {skipped}")
        if not args.apply:
            conn.rollback()
            print("preview only; rerun with --apply to delete")
            return 0
        conn.executemany("DELETE FROM agent_run_artifacts WHERE artifact_id = ?", [(a,) for a, _ in doomed])
        conn.commit()
        print(f"deleted {len(doomed)} snapshots")
    if args.vacuum:
        with sqlite3.connect(main_db, timeout=30) as conn:
            conn.execute("VACUUM")
        print(f"vacuumed; database is now {main_db.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
