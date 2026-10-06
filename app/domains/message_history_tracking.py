"""Deterministic source accounting; topic membership comes only from the model."""

from __future__ import annotations

from app.domains.message_reading_replay import canonical_json, digest

APPLICATION_CODEC = "history-explicit-v2"


def analysis_projection(row: dict) -> dict:
    """Minimal lossless text-analysis projection; complete originals stay local."""
    return {
        key: row.get(key)
        for key in (
            "id",
            "sender",
            "sent_at",
            "text",
            "kind",
            "mentions",
            "reply",
            "parts",
            "thread",
            "timestamp_quality",
            "capabilities",
        )
    }


def encode_history(rows: list[dict]) -> list[dict]:
    encoded = []
    for row in rows:
        item = {key: row[key] for key in ("id", "sender", "sent_at", "text")}
        for field in ("kind", "mentions", "reply", "thread", "timestamp_quality"):
            value = row.get(field)
            default = {
                "kind": "text",
                "mentions": [],
                "reply": None,
                "thread": None,
                "timestamp_quality": "provider",
            }[field]
            if value != default:
                item[field] = value
        parts = row.get("parts")
        if parts == [{"kind": "text", "text": row["text"]}]:
            item["text_part_is_body"] = True
        elif parts:
            item["parts"] = parts
        item["metadata_support"] = row.get("capabilities", {})
        encoded.append(item)
    return encoded


def decode_history(value: list[dict]) -> list[dict]:
    rows = []
    for item in value:
        row = {
            "kind": "text",
            "mentions": [],
            "reply": None,
            "parts": [],
            "thread": None,
            "timestamp_quality": "provider",
        }
        row.update(item)
        row["capabilities"] = row.pop("metadata_support")
        if row.pop("text_part_is_body", False):
            row["parts"] = [{"kind": "text", "text": row["text"]}]
        rows.append(row)
    return rows


class HistoryTracker:
    def __init__(self, conversation: str, messages: list[dict]):
        self.conversation = conversation
        self.messages = {row["id"]: row for row in messages}
        self.topics: dict[str, dict] = {}
        self.findings: dict[str, list[dict]] = {"highlights": [], "important": []}
        self.summaries: list[dict] = []
        self.completed: set[str] = set()
        self.warnings: list[str] = []

    def prior(self, counter, limit: int) -> list[dict]:
        selected = []
        for key, topic in reversed(list(self.topics.items())):
            source_ids = topic["source_ids"][-2:]
            item = {
                "key": key,
                "title": topic["title"],
                "summary": topic["summary"],
                "source_ids": source_ids,
                "evidence": [
                    {"id": mid, "text": self.messages[mid]["text"][:240]} for mid in source_ids
                ],
            }
            if counter.count_text(canonical_json([*selected, item])).count > limit:
                continue
            selected.append(item)
            if len(selected) >= 12:
                break
        return selected

    def observe(self, chunk: dict, result: dict) -> None:
        self.completed.update(row["id"] for row in chunk["messages"])
        for item in result["topics"]:
            key = (
                item["existing_key"]
                or self.conversation + "t" + digest([chunk["chunk_id"], item["local_key"]])[:12]
            )
            old = self.topics.get(key)
            if item["existing_key"] and old is None:
                raise ValueError("unknown_stable_topic")
            if old is None:
                old = {
                    "key": key,
                    "title": item["title"],
                    "summary": item["summary"],
                    "source_ids": [],
                    "member_ids": [],
                    "changes": [],
                }
                self.topics[key] = old
            old["changes"].append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "summary": item["summary"],
                    "source_ids": item["source_ids"],
                    "continuity_source_id": item["continuity_source_id"],
                }
            )
            old["title"], old["summary"] = item["title"], item["summary"]
            for field in ("source_ids", "member_ids"):
                old[field] = list(dict.fromkeys([*old[field], *item[field]]))
            # Reinsert recently discussed topics so bounded prior favors recency.
            self.topics.pop(key)
            self.topics[key] = old
        for kind in self.findings:
            self.findings[kind].extend(
                {**item, "chunk_id": chunk["chunk_id"]} for item in result[kind]
            )
        self.summaries.append({"chunk_id": chunk["chunk_id"], "summary": result["group_summary"]})
        self.warnings.extend(result["warnings"])

    def report(self) -> dict:
        topics = []
        assigned = set()
        for item in self.topics.values():
            rows = [self.messages[mid] for mid in item["member_ids"]]
            assigned.update(item["member_ids"])
            known_times = [row["sent_at"] for row in rows if row["sent_at"] is not None]
            authors = {row["sender"] for row in rows}
            buckets = {value // 1800 for value in known_times}
            activity_score = (
                min(len(rows), 50) * 0.25 + min(len(authors), 30) * 2 + min(len(buckets), 20) * 1.5
            )
            topics.append(
                {
                    **item,
                    "message_count": len(rows),
                    "participant_count": len(authors),
                    "active_half_hour_buckets": len(buckets),
                    "activity_score": activity_score,
                    "activity_score_version": "diversity-time-volume-v1",
                    "senders": sorted({row["sender"] for row in rows}),
                    "sent_time_span": [min(known_times), max(known_times)] if known_times else None,
                    "unknown_sent_time_count": len(rows) - len(known_times),
                }
            )
        topics.sort(key=lambda item: (-item["activity_score"], item["key"]))
        return {
            "conversation": self.conversation,
            "message_count": len(self.messages),
            "completed_messages": len(self.completed),
            "unprocessed_ids": sorted(self.messages.keys() - self.completed),
            "unassigned_completed_ids": sorted(self.completed - assigned),
            "topics_by_activity": topics,
            **self.findings,
            "batch_group_summaries": self.summaries,
            "warnings": list(dict.fromkeys(self.warnings)),
            "topic_continuity": "model_source_backed_bounded_prior_may_fragment",
            "observations_not_verified_facts": True,
        }
