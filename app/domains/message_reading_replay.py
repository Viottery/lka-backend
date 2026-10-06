"""Local, explicitly authorized message replay datasets; never calls an LLM."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _write_private(path: Path, value: Any) -> None:
    """Dataset writes are new artifacts, never overwrites of an existing snapshot."""
    with path.open("x", encoding="utf-8") as output:
        output.write(canonical_json(value) + "\n")
    path.chmod(0o600)


def redact_text(text: str, identities: dict[str, str]) -> str:
    for identity in sorted(identities, key=len, reverse=True):
        if identity:
            text = re.sub(r"(?<!\d)" + re.escape(identity) + r"(?!\d)", identities[identity], text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone]", text)
    # Media/private URLs are never necessary for text interpretation. Keep a
    # marker so link novelty remains visible without forwarding signed URLs.
    return re.sub(r"https?://[^\s<>\[\]]+", "[link]", text)


def create_snapshot(
    source: Path,
    output: Path,
    *,
    platform: str,
    account_id: str,
    group_ids: list[str],
    until: int,
    consent_at: str,
) -> dict[str, Any]:
    if not group_ids or len(set(group_ids)) != len(group_ids) or len(group_ids) > 10:
        raise ValueError("invalid_group_scope")
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("snapshot_destination_must_be_new")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        selected = []
        scopes = []
        for group in sorted(group_ids):
            policy = conn.execute(
                "SELECT * FROM message_history_policies WHERE platform=? AND account_id=? "
                "AND conversation_type='group' AND conversation_id=?",
                (platform, account_id, group),
            ).fetchone()
            if policy is None or not policy["record_enabled"]:
                raise ValueError("source_recording_not_authorized")
            rows = conn.execute(
                "SELECT * FROM message_history_messages WHERE conversation_key=? "
                "AND received_at<=? ORDER BY seq",
                (policy["conversation_key"], until),
            ).fetchall()
            selected.append([dict(row) for row in rows])
            scopes.append(
                {
                    "conversation_key": policy["conversation_key"],
                    "group_id": group,
                    "capture_epoch": policy["capture_epoch"],
                    "count": len(rows),
                    "end_seq": rows[-1]["seq"] if rows else 0,
                }
            )
    original = {"scopes": scopes, "messages": selected}
    snapshot_id = digest(original)
    projected = {}
    mappings = {}
    for index, rows in enumerate(selected, 1):
        conversation = f"c{index}"
        messages = {
            row["internal_message_id"]: f"{conversation}m{i:05d}" for i, row in enumerate(rows, 1)
        }
        providers = {
            row["provider_message_id"]: messages[row["internal_message_id"]] for row in rows
        }
        metadata = {
            row["internal_message_id"]: json.loads(row.get("metadata_json") or "{}") for row in rows
        }
        identities = {account_id, *group_ids}
        for row in rows:
            if row["sender_id"]:
                identities.add(row["sender_id"])
            identities.update(
                item["user_id"]
                for item in metadata[row["internal_message_id"]].get("mentions", [])
                if item.get("kind") == "user" and item.get("user_id")
            )
        users = {uid: f"{conversation}p{i:04d}" for i, uid in enumerate(sorted(identities), 1)}
        threads = {
            value: f"{conversation}t{i:04d}"
            for i, value in enumerate(
                sorted({meta["thread_id"] for meta in metadata.values() if meta.get("thread_id")}),
                1,
            )
        }
        projection = []
        for row in rows:
            meta = metadata[row["internal_message_id"]]
            native_mentions = [
                "all" if item.get("kind") == "all" else users.get(item.get("user_id"), "unknown")
                for item in meta.get("mentions", [])
            ]
            reply = meta.get("reply_to_message_id")
            parts = []
            for part in meta.get("content_parts", []):
                kind = part.get("kind")
                if kind == "text":
                    parts.append(
                        {"kind": "text", "text": redact_text(part.get("text") or "", users)}
                    )
                elif kind == "mention":
                    mention = part.get("mention") or {}
                    parts.append(
                        {
                            "kind": "mention",
                            "target": "all"
                            if mention.get("kind") == "all"
                            else users.get(mention.get("user_id"), "unknown"),
                        }
                    )
                elif kind == "reply":
                    parts.append(
                        {
                            "kind": "reply",
                            "target": providers.get(part.get("message_id"), "unresolved"),
                        }
                    )
                else:
                    parts.append({"kind": "unsupported"})
            projection.append(
                {
                    "id": messages[row["internal_message_id"]],
                    "sender": users.get(row["sender_id"], ""),
                    "seq": row["seq"],
                    "sent_at": row["sent_at"],
                    "received_at": row["received_at"],
                    "text": redact_text(row["text"], users),
                    "kind": row["content_kind"],
                    "mentions": native_mentions,
                    "reply": providers.get(reply, "unresolved") if reply else None,
                    "timestamp_quality": row["timestamp_quality"],
                    "thread": threads.get(meta.get("thread_id")),
                    "capabilities": meta.get(
                        "metadata_capabilities",
                        {
                            "mentions": "unknown",
                            "reply": "unknown",
                            "thread": "unknown",
                            "content_parts": "unknown",
                        },
                    ),
                    "parts": parts,
                }
            )
        projected[conversation] = projection
        # Display names are local presentation metadata, never provider input.
        # Alias identities alone are insufficient for stable living documents:
        # consumers reverse-map actual sender IDs locally before persistence.
        display_names = {}
        for row in rows:
            if row["sender_id"] and row.get("sender_name"):
                display_names[users[row["sender_id"]]] = row["sender_name"]
        mappings[conversation] = {"messages": messages, "users": users,
                                  "display_names": display_names, "source": scopes[index - 1]}
    public_manifest = {
        "snapshot_id": snapshot_id,
        "until": until,
        "consent_at": consent_at,
        "created_at": datetime.now(UTC).isoformat(),
        "capture_mode": "inbound_only",
        "capture_gaps": "unknown",
        "projection_version": "replay-projection-v1",
        "projection_digest": digest(projected),
        "mapping_digest": digest(mappings),
        "count": sum(len(rows) for rows in selected),
        "conversations": {
            key: {
                "count": len(rows),
                "v2": sum(row["capabilities"].get("mentions") == "supported" for row in rows),
            }
            for key, rows in projected.items()
        },
        "redaction_limits": "Sender names omitted; known IDs, phone/email/links replaced; free-text names may remain.",
    }
    _write_private(output / "manifest.json", public_manifest)
    _write_private(output / "messages.json", projected)
    _write_private(output / "identity_mapping.json", mappings)
    return public_manifest


def load_snapshot(directory: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    messages = json.loads((directory / "messages.json").read_text(encoding="utf-8"))
    mapping = json.loads((directory / "identity_mapping.json").read_text(encoding="utf-8"))
    if digest(messages) != manifest["projection_digest"]:
        raise ValueError("snapshot_digest_mismatch")
    if digest(mapping) != manifest.get("mapping_digest"):
        raise ValueError("snapshot_mapping_digest_mismatch")
    return manifest, messages


def build_windows(
    messages: dict[str, list[dict[str, Any]]], *, size: int = 24, max_per_group: int = 8
) -> list[dict[str, Any]]:
    if not 4 <= size <= 100 or not 1 <= max_per_group <= 32:
        raise ValueError("invalid_window_limits")
    windows = []
    for conversation, rows in sorted(messages.items()):
        if not rows:
            continue
        # Disjoint contiguous blocks, extending boundaries rather than splitting
        # direct reply edges across development/holdout. Long chains remain one
        # block and token admission, not truncation, decides remote eligibility.
        groups: list[list[dict[str, Any]]] = []
        current = []
        for row in rows:
            current.append(row)
            if len(current) >= size:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        owner = {row["id"]: i for i, group in enumerate(groups) for row in group}
        edges = []
        for i, group in enumerate(groups):
            for row in group:
                target = owner.get(row["reply"])
                if target is not None and target != i:
                    edges.append((min(i, target), max(i, target)))
        thread_owner = {}
        for i, group in enumerate(groups):
            for row in group:
                thread = row.get("thread")
                if thread:
                    first = thread_owner.setdefault(thread, i)
                    if first != i:
                        edges.append((first, i))
        components = []
        start = end = 0
        for i in range(len(groups)):
            end = max(end, i, *(right for left, right in edges if left <= i <= right))
            if i == end:
                components.append([row for group in groups[start : end + 1] for row in group])
                start = i + 1
        chosen = sorted(
            {
                round(i * (len(components) - 1) / max(1, min(max_per_group, len(components)) - 1))
                for i in range(min(max_per_group, len(components)))
            }
        )
        for number, pos in enumerate(chosen):
            block = components[pos]
            windows.append(
                {
                    "window_id": f"{conversation}w{number + 1:02d}",
                    "conversation": conversation,
                    "split": "holdout" if number % 4 == 2 else "development",
                    "messages": block,
                    "range": [block[0]["seq"], block[-1]["seq"]],
                    "digest": digest(block),
                    "gold_status": "unlabelled",
                }
            )
    # Protocol upgrades are chronological: a suffix-only holdout would exclude
    # all native-v2 cases from development. Split reply-connected units across
    # time strata independently of their text/labels before model evaluation.
    target = round(len(windows) / 3)
    held = sum(window["split"] == "holdout" for window in windows)
    for window in windows:
        if held >= target:
            break
        if window["split"] == "development" and window["window_id"].endswith("w02"):
            window["split"] = "holdout"
            held += 1
    return windows


def validate_grant(
    grant: dict[str, Any],
    manifest: dict[str, Any],
    provider_hash: str,
    current_policies: list[dict[str, Any]],
    mapping: dict[str, Any],
    *,
    now: int,
) -> None:
    if (
        grant.get("active") is not True
        or grant.get("snapshot_id") != manifest["snapshot_id"]
        or grant.get("provider_hash") != provider_hash
        or now >= grant.get("expires_at", 0)
        or grant.get("manifest_digest") != digest(manifest)
        or grant.get("mapping_digest") != digest(mapping)
    ):
        raise ValueError("evaluation_grant_revoked_or_mismatched")
    expected = {
        value["source"]["conversation_key"]: value["source"]["capture_epoch"]
        for value in mapping.values()
    }
    actual = {p["conversation_key"]: p for p in current_policies}
    if set(grant.get("conversation_keys", [])) != set(expected):
        raise ValueError("evaluation_scope_mismatch")
    for key, epoch in expected.items():
        row = actual.get(key)
        if row is None or not row.get("record_enabled") or row.get("capture_epoch") != epoch:
            raise ValueError("evaluation_source_permission_changed")


def validate_windows(
    windows: list[dict], messages: dict[str, list[dict]], expected_digest: str
) -> None:
    if digest(windows) != expected_digest:
        raise ValueError("window_definitions_changed")
    seen = set()
    window_ids = set()
    for window in windows:
        rows = window["messages"]
        authorized = {row["id"]: row for row in messages.get(window["conversation"], [])}
        ids = [row["id"] for row in rows]
        seqs = [row["seq"] for row in rows]
        if (
            not rows
            or window["window_id"] in window_ids
            or len(ids) != len(set(ids))
            or seen.intersection(ids)
            or seqs != sorted(set(seqs))
            or window["range"] != [seqs[0], seqs[-1]]
            or digest(rows) != window["digest"]
            or window["split"] not in {"development", "holdout"}
            or any(authorized.get(row["id"]) != row for row in rows)
        ):
            raise ValueError("invalid_window_definition")
        seen.update(ids)
        window_ids.add(window["window_id"])
