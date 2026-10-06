"""Freeze an authorized group text snapshot using the database's native Python.

No LLM, production mutations, evaluation grant or media bytes. On Windows run
this with native Python instead of accessing a live Windows SQLite WAL in WSL.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.domains.message_reading_replay import create_snapshot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True, help="New folder name under data/message_replay")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--groups", nargs="+", required=True)
    parser.add_argument("--until", required=True, help="Timezone-aware ISO cutoff")
    args = parser.parse_args()
    try:
        if Path(args.output).name != args.output or args.output in (".", ".."):
            raise ValueError("snapshot_folder_name_required")
        cutoff = datetime.fromisoformat(args.until)
        if cutoff.tzinfo is None:
            raise ValueError("snapshot_cutoff_timezone_required")
        result = create_snapshot(
            Path(args.source), ROOT / "data/message_replay" / args.output,
            platform=args.platform, account_id=args.account, group_ids=args.groups,
            until=int(cutoff.timestamp()), consent_at=datetime.now(UTC).isoformat(),
        )
        print(json.dumps({key: result[key] for key in ("snapshot_id", "count", "conversations", "until")}))
    except Exception as exc:  # noqa: BLE001 - no exception-carried chat, paths or secrets
        print(json.dumps({"state": "stopped", "error_class": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
