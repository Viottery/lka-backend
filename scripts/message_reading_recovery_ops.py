"""Native-install recovery operations; counters only, never print chat or keys.

snapshot is a SQLite backup (including WAL); state is read-only; replay uses the
authenticated backend API and never bypasses configured budgets or permissions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def state(root: Path) -> dict:
    db = root / "data/runtime/lka.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        result = []
        for policy in conn.execute("SELECT * FROM message_history_policies WHERE record_enabled=1 AND analysis_enabled=1"):
            key = policy["conversation_key"]
            conversation = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)).fetchone()
            watermark = max(conversation["generation_published_seq"], conversation["reading_covered_seq"] or 0,
                            conversation["covered_seq"], conversation["analysis_baseline_floor_seq"] or 0)
            schedule = conn.execute("SELECT * FROM message_reading_schedules WHERE conversation_key=?", (key,)).fetchone()
            schedule = dict(schedule) if schedule else None
            job = conn.execute("SELECT job_id,status,error_class,payload_json,updated_at FROM background_jobs WHERE kind='message_analysis' AND scope_id=? ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
            result.append({"conversation_id": policy["conversation_id"], "conversation_key": key,
                "revision": policy["revision"], "raw_seq": conversation["next_seq"] - 1,
                "watermark": watermark, "pending": conversation["next_seq"] - 1 - watermark,
                "replay_through_seq": schedule.get("replay_through_seq") if schedule else None,
                "job": {k: job[k] for k in ("job_id", "status", "error_class", "updated_at")} if job else None,
                "range": [json.loads(job["payload_json"]).get(k) for k in ("start_seq", "end_seq")] if job else None})
        return {"checked_at": datetime.now(UTC).isoformat(), "conversations": result}


def snapshot(root: Path) -> dict:
    destination = root / "data/deployment_backups" / datetime.now(UTC).strftime("message-recovery-%Y%m%dT%H%M%S%fZ")
    destination.mkdir(parents=True, exist_ok=False)
    db = root / "data/runtime/lka.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as old, sqlite3.connect(destination / "backend.sqlite3") as new:
        old.backup(new)
    before = state(root)
    (destination / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"backup_path": str(destination), **before}


def replay(root: Path, groups: list[str], url: str) -> dict:
    from dotenv import dotenv_values
    token = (os.environ.get("LKA_MESSAGES_CONTROL_TOKEN") or os.environ.get("LKA_MESSAGES_API_TOKEN")
             or dotenv_values(root / ".env").get("LKA_MESSAGES_API_TOKEN"))
    if not token:
        raise ValueError("message_management_credential_missing")
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        with http.open(urllib.request.Request(url.rstrip("/") + path, data=body, headers=headers), timeout=30) as response:
            return json.load(response)
    policies = request("/messages/policies")["policies"]
    selected = [p for p in policies if p["platform"] == "qq" and p["conversation_type"] == "group"
                and p["conversation_id"] in groups and p["record_enabled"] and p["analysis_enabled"]]
    if {p["conversation_id"] for p in selected} != set(groups):
        raise ValueError("replay_groups_not_authorized")
    result = []
    for policy in selected:
        value = request(f"/messages/conversations/{policy['conversation_key']}/replay", {"expected_revision": policy["revision"]})
        result.append({"conversation_id": policy["conversation_id"],
                       **{k: value[k] for k in ("through_seq", "analysis_watermark_seq", "status")}})
    return {"replay": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("state", "snapshot", "replay"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--groups", nargs="+")
    parser.add_argument("--url", choices=("http://127.0.0.1:8765",), default="http://127.0.0.1:8765")
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "app/core/runtime.py").is_file() or not (root / "config/local.toml").is_file():
        parser.error("Established native backend installation required.")
    if args.action == "replay" and not args.groups:
        parser.error("Replay needs an explicit authorized group list.")
    try:
        value = state(root) if args.action == "state" else snapshot(root) if args.action == "snapshot" else replay(root, args.groups, args.url)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - operation boundary must not echo private exception data.
        # Even HTTP errors must not echo a request, credential or message body.
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
