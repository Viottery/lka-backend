"""Inspect, poll or queue suggestions for the explicitly configured cloud hub."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from app.core.config import Settings
from app.domains.node_outbox import NodeProposalOutbox
from app.integrations.cloud_sync import CloudReplicaSynchronizer
from app.storage.shared_replica import SharedReplicaStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument("--max-pages", type=int, default=8, choices=range(1, 101), metavar="1..100")
    queue = commands.add_parser("queue")
    queue.add_argument("--file", type=Path, required=True, help="Bounded JSON node proposal")
    args = parser.parse_args(argv)
    load_dotenv(override=False)
    settings = Settings()
    config = settings.load_local_config().sync
    if not config.enabled or config.role != "replica":
        parser.error("Enable [sync] role=replica with an HTTPS hub_url and pinned hub_id")
    path = settings.data_dir / "shared-replica.sqlite3"
    replica = SharedReplicaStore(path, config.hub_id)
    outbox = NodeProposalOutbox(path)
    worker = CloudReplicaSynchronizer(config, replica, outbox)
    if args.command == "queue":
        with args.file.open("rb") as source:
            raw = source.read(32769)
        if len(raw) > 32768:
            parser.error("Proposal exceeds 32 KiB")
        result = outbox.enqueue(json.loads(raw))
    elif args.command == "run":
        try:
            result = worker.run_once(max_pages=args.max_pages)
        except Exception:  # noqa: BLE001 - report only redacted status, never tokens or source text.
            print(json.dumps(worker.status(), ensure_ascii=False, indent=2))
            return 1
    else:
        result = worker.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
