"""Durable node suggestions; these records grant no memory or execution authority."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.domains.shared_sync import NodeProposal, SyncConflictError, canonical
from app.storage.db import connect


class NodeProposalOutbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.path)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS node_proposal_outbox(
                proposal_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                submitted INTEGER NOT NULL DEFAULT 0, receipt TEXT)""")

    def enqueue(self, payload: dict) -> dict:
        item = NodeProposal.model_validate({"proposal_id": "proposal_" + uuid4().hex, **payload})
        encoded = canonical(item.model_dump())
        with closing(connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT payload FROM node_proposal_outbox WHERE proposal_id=?",
                               (item.proposal_id,)).fetchone()
            if old and old[0] != encoded:
                raise SyncConflictError("proposal identity already has different content")
            conn.execute("INSERT OR IGNORE INTO node_proposal_outbox(proposal_id,payload) VALUES(?,?)",
                         (item.proposal_id, encoded))
        return {"proposal_id": item.proposal_id, "status": "queued"}

    def pending(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid proposal limit")
        with closing(connect(self.path)) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM node_proposal_outbox WHERE submitted=0 ORDER BY rowid LIMIT ?",
                (limit,),
            )]

    def acknowledge(self, proposal: dict, receipt: dict) -> None:
        if (not isinstance(receipt, dict) or receipt.get("proposal_id") != proposal["proposal_id"]
                or receipt.get("status") not in {"pending", "reviewed", "rejected"}
                or not isinstance(receipt.get("node_id"), str) or not receipt["node_id"]):
            raise SyncConflictError("invalid proposal receipt")
        with closing(connect(self.path)) as conn, conn:
            changed = conn.execute(
                "UPDATE node_proposal_outbox SET submitted=1,receipt=? WHERE proposal_id=? AND payload=?",
                (canonical(receipt), proposal["proposal_id"], canonical(proposal)),
            ).rowcount
            if not changed:
                raise SyncConflictError("proposal changed before receipt")

    def status(self) -> dict:
        with closing(connect(self.path)) as conn:
            pending, submitted = conn.execute(
                "SELECT coalesce(sum(submitted=0),0),coalesce(sum(submitted=1),0) "
                "FROM node_proposal_outbox",
            ).fetchone()
        return {"pending": pending, "submitted": submitted}
