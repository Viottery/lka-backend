"""Live keyword retrieval over currently enabled captured message history."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

from app.domains.knowledge import (
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeSearchItem,
    KnowledgeSourceRecord,
    PrivacyGateway,
)


def _id(kind: str, value: str) -> str:
    encoded = urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    return f"chat_message_{kind}_{encoded}"


class MessageKnowledgeAdapter:
    """Expose live MessageHistoryService rows without mirroring or embedding them."""

    source_type = "chat_message"
    origin_constraint = "message_evidence_human_matter"

    def __init__(self, service: Any) -> None:
        self.service = service
        self._privacy = PrivacyGateway()

    @staticmethod
    def handles_id(value: str) -> bool:
        return value.startswith("chat_message_")

    def list_sources(self, *, source_ids: list[str] | None = None,
                     account_ids: list[str] | None = None) -> list[KnowledgeSourceRecord]:
        if source_ids == [] or account_ids == []:
            return []
        rows = self.service.source_inventory()
        if source_ids is not None:
            rows = [row for row in rows if row["source_id"] in source_ids]
        # Inventory is filtered through conversations to preserve independent grants.
        available = self._all_conversations(source_ids, account_ids)
        visible_ids = {row["source_id"] for row in available}
        by_id = {row["source_id"]: row for row in rows if row["source_id"] in visible_ids}
        counts: dict[str, int] = {}
        for conversation in available:
            source_id = conversation.get("source_id")
            if source_id:
                counts[source_id] = counts.get(source_id, 0) + int(conversation.get("message_count", 0))
        metadata_rows = {row["source_id"]: row for row in available}
        return [KnowledgeSourceRecord(source_id=key, source_type=self.source_type,
                    display_name=metadata_rows.get(key, {}).get("display_name", row["display_name"]), uri=f"chat-message://source/{key}",
                document_count=int(metadata_rows.get(key, {}).get("current_message_count", counts.get(key, 0))),
                    metadata={"platform": row["platform"], "conversation_type": row["conversation_type"],
                              "platform_name": metadata_rows.get(key, {}).get("platform_name"),
                              "user_alias": metadata_rows.get(key, {}).get("user_alias"),
                              "source_policy": {"constraint_id": self.origin_constraint,
                                                "matter_application": "dedicated_human_proposal_only"}})
                for key, row in by_id.items()
                if source_ids is None or key in source_ids]

    def search(self, *, query: str, limit: int, source_ids: list[str] | None,
               account_ids: list[str] | None, max_snippet_chars: int) -> list[KnowledgeSearchItem]:
        if source_ids == [] or account_ids == [] or not query.strip() or len(query) > 256:
            return []
        # MessageHistoryService applies current record_enabled and capture_epoch fencing.
        page = self.service.search(query, limit=min(200, max(1, limit * 8)),
                                   allowed_sources=source_ids, allowed_accounts=account_ids,
                                   current_capture_only=True)
        result: list[KnowledgeSearchItem] = []
        policies = {row["conversation_key"]: row for row in self._all_conversations(source_ids, account_ids)}
        for message in page["messages"]:
            key = str(message["conversation_key"])
            policy = policies.get(key)
            # Re-enable creates a new capture epoch; old captured rows remain local
            # history but are not in the currently authorized searchable corpus.
            if policy is None or int(message.get("capture_epoch", 0)) != int(policy["capture_epoch"]):
                continue
            identity = str(message["message_id"])
            title = str(message.get("sender_name") or message.get("sender_id") or "Message")
            source_id = str(policy["source_id"])
            if source_ids is not None and source_id not in source_ids:
                continue
            source_ref = f"chat-message://{key}/{identity}"
            snippet = str(message.get("text") or "")[:max_snippet_chars]
            decision = self._privacy.decide(sensitivity="personal", remote_policy="redact", text=snippet)
            metadata = self._metadata(message)
            result.append(KnowledgeSearchItem(
                chunk_id=_id("chunk", identity), document_id=_id("doc", f"{key}|{policy['capture_epoch']}"), source_id=source_id,
                title=title, source_type=self.source_type, uri=source_ref, chunk_index=int(message.get("seq", 0)),
                snippet=decision.text, source_ref=source_ref, sensitivity="personal", remote_policy="redact",
                policy_decision=decision.policy_decision, retrieval_channels=["keyword"],
                retrieval_score=1.0 / (1 + len(result)),
                metadata={**metadata, "group_name": policy.get("display_name")
                          if policy.get("conversation_type") == "group" else None,
                          "conversation_type": policy.get("conversation_type"),
                          "conversation_id": policy.get("conversation_id"),
                          "group_id": policy.get("group_id"), "account_id": policy.get("account_id"),
                          "capture_epoch": policy.get("capture_epoch"),
                          "retrieval_mode": "keyword", "remote_retrieval": False,
                          "source_policy": {"constraint_id": self.origin_constraint,
                                            "matter_application": "dedicated_human_proposal_only"}},
            ))
            if len(result) >= limit:
                break
        return result

    def load_chunks(self, *, chunk_ids: list[str], max_chars_per_chunk: int, offset: int,
                    source_ids: list[str] | None, account_ids: list[str] | None) -> list[KnowledgeChunkRecord]:
        results: list[KnowledgeChunkRecord] = []
        current_policies = {row["conversation_key"]: row for row in self._all_conversations(source_ids, account_ids)}
        for chunk_id in chunk_ids:
            internal_id = self._resolve_id("chunk", chunk_id)
            if internal_id is None:
                continue
            try:
                getter = getattr(self.service, "get_message", self.service.resolve_message_source)
                row = getter(internal_id, allowed_sources=source_ids,
                             allowed_accounts=account_ids)
            except (PermissionError, ValueError):
                continue
            current = current_policies.get(row["conversation_key"])
            if current is None or int(row.get("capture_epoch", 0)) != int(current["capture_epoch"]):
                continue
            conversation_metadata = row.get("conversation_metadata") or current
            text = str(row.get("text") or "")
            decision = self._privacy.decide(sensitivity="personal", remote_policy="redact", text=text)
            text = decision.text
            part = text[offset:offset + max_chars_per_chunk]
            key = str(row["conversation_key"])
            ref = f"chat-message://{key}/{internal_id}"
            results.append(KnowledgeChunkRecord(
                chunk_id=chunk_id, document_id=_id("doc", f"{key}|{current['capture_epoch']}"), source_id=current["source_id"],
                title=str(row.get("sender_name") or row.get("sender_id") or "Message"),
                source_type=self.source_type, uri=ref, chunk_index=int(row.get("seq", 0)), text=part,
                char_count=len(part), token_estimate=max(1, len(part) // 4), source_ref=ref,
                sensitivity="personal", remote_policy="redact", policy_decision=decision.policy_decision,
                total_chars=len(text), truncated=offset + len(part) < len(text), offset=offset,
                next_offset=offset + len(part) if offset + len(part) < len(text) else None,
                metadata={**self._metadata(row), "conversation_type": current.get("conversation_type"),
                          "group_name": conversation_metadata.get("display_name") if current.get("conversation_type") == "group" else None,
                          "conversation_id": current.get("conversation_id"),
                          "group_id": current.get("group_id"), "account_id": current.get("account_id"),
                          "retrieval_mode": "keyword", "remote_retrieval": False,
                          "user_alias": conversation_metadata.get("user_alias"),
                          "source_policy": {"constraint_id": self.origin_constraint,
                                            "matter_application": "dedicated_human_proposal_only"}},
            ))
        return results

    def load_document(self, *, document_id: str, include_text: bool, max_chars: int,
                      source_ids: list[str] | None, account_ids: list[str] | None) -> KnowledgeDocumentRecord | None:
        identity = self._resolve_id("doc", document_id)
        if identity is None or source_ids == [] or account_ids == []:
            return None
        key, epoch_text = identity.rsplit("|", 1) if "|" in identity else (identity, "")
        try:
            page = self.service.recent(key, limit=20, allowed_sources=source_ids,
                                       allowed_accounts=account_ids, current_capture_only=True)
        except (PermissionError, ValueError):
            return None
        rows = list(reversed(page["messages"]))
        if not rows:
            return None
        policy = next((item for item in self._all_conversations(source_ids, account_ids)
                       if item["conversation_key"] == key), None)
        if policy is None or epoch_text != str(policy["capture_epoch"]):
            return None
        rows = [row for row in rows if int(row.get("capture_epoch", 0)) == int(policy["capture_epoch"])]
        if not rows:
            return None
        policy = self._conversation_metadata(policy, source_ids, account_ids)
        source_id = str(policy.get("source_id") or self._source_id(key))
        text = "\n".join(str(row.get("text") or "") for row in rows)[:max_chars] if include_text else None
        if text is not None:
            text = self._privacy.redact(text)
        return KnowledgeDocumentRecord(
            document_id=document_id, source_id=source_id, source_type=self.source_type,
            title=str(policy.get("display_name") or "Message conversation"),
            uri=f"chat-message://conversation/{key}", checksum="", mime_type="text/plain", status="active",
            sensitivity="personal", remote_policy="redact", source_ref=f"chat-message://conversation/{key}",
            metadata={"conversation_key": key, "message_count": policy.get("current_message_count", 0),
                      "capture_epoch": policy.get("capture_epoch"), "page_only": True,
                      "complete_for_platform_history": False,
                      "conversation_type": policy.get("conversation_type"),
                      "conversation_id": policy.get("conversation_id"), "group_id": policy.get("group_id"),
                      "account_id": policy.get("account_id"),
                      "group_name": policy.get("display_name") if policy.get("conversation_type") == "group" else None,
                      "user_alias": policy.get("user_alias"), "platform_name": policy.get("platform_name"),
                      "has_more": bool(page.get("has_more")), "next_offset": page.get("next_offset"),
                      "coverage": {"kind": "latest_message_page", "page_only": True,
                                   "complete_for_platform_history": False,
                                   "first_seq": min((int(row.get("seq", 0)) for row in rows), default=None),
                                   "last_seq": max((int(row.get("seq", 0)) for row in rows), default=None),
                                   "capture_epoch": policy.get("capture_epoch"),
                                   "has_more": bool(page.get("has_more"))},
                      "source_policy": {"constraint_id": self.origin_constraint,
                                        "matter_application": "dedicated_human_proposal_only"}},
            chunk_ids=[_id("chunk", str(row["message_id"])) for row in rows], text=text,
            truncated=bool(page.get("has_more") or (include_text and sum(
                len(str(r.get("text") or "")) for r in rows
            ) > max_chars)),
            policy_decision="redacted",
        )

    def resolve_origin_constraints(self, *, input: dict[str, Any], context: Any = None) -> list[str]:
        source_types = input.get("source_types")
        if source_types and self.source_type not in source_types:
            return []
        scope = getattr(context, "tool_view", None)
        if scope is not None and scope.child_run_id is not None and not scope.allowed_account_ids:
            return []
        granted_sources = list(scope.allowed_source_ids) if scope is not None and scope.allowed_source_ids else None
        account_ids = list(scope.allowed_account_ids) if scope is not None and scope.allowed_account_ids else None
        if scope is not None and scope.child_run_id is not None and not scope.full_data_authority and not granted_sources:
            return []
        visible = {row.source_id for row in self.list_sources(source_ids=granted_sources,
                                                               account_ids=account_ids)}
        explicit_ids = [str(value) for value in input.get("source_ids", [])]
        explicit_ids.extend(str(value) for value in input.get("chunk_ids", []))
        if input.get("document_id"):
            explicit_ids.append(str(input["document_id"]))
        if explicit_ids:
            if visible.intersection(explicit_ids):
                return [self.origin_constraint]
            for value in explicit_ids:
                if value.startswith("chat_message_chunk_"):
                    message_id = self._resolve_id("chunk", value)
                    if message_id:
                        try:
                            self.service.get_message(message_id, allowed_sources=granted_sources,
                                                     allowed_accounts=account_ids)
                            return [self.origin_constraint]
                        except (AttributeError, PermissionError, ValueError):
                            continue
                elif value.startswith("chat_message_doc_"):
                    identity = self._resolve_id("doc", value)
                    if identity:
                        key, _, epoch = identity.rpartition("|")
                        policy = next((item for item in self._all_conversations(granted_sources, account_ids)
                                       if item["conversation_key"] == key), None)
                        if policy is not None and str(policy["capture_epoch"]) == epoch:
                            return [self.origin_constraint]
            return []
        # Generic search may include every currently granted provider unless its
        # source type was narrowed above, so protect the origin before retrieval.
        return [self.origin_constraint] if visible else []

    def _source_id(self, conversation_key: str) -> str:
        # Source identity is the policy's source_inventory ID; find it through a guarded lookup.
        for source in self.service.source_inventory():
            try:
                self.service.coverage(conversation_key, allowed_sources=[source["source_id"]])
            except PermissionError:
                continue
            return str(source["source_id"])
        return ""

    def _all_conversations(self, source_ids: list[str] | None,
                           account_ids: list[str] | None) -> list[dict[str, Any]]:
        if source_ids == [] or account_ids == []:
            return []
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.service.list_conversations(allowed_sources=source_ids,
                allowed_accounts=account_ids, limit=200, offset=offset)
            rows.extend(page["conversations"])
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            offset = next_offset
        return [self._conversation_metadata(row, source_ids, account_ids) for row in rows]

    def _conversation_metadata(self, row: dict[str, Any], source_ids: list[str] | None,
                               account_ids: list[str] | None) -> dict[str, Any]:
        getter = getattr(self.service, "get_conversation_metadata", None)
        if getter is None:
            return row
        try:
            metadata = getter(row["conversation_key"], allowed_sources=source_ids,
                              allowed_accounts=account_ids)
        except (PermissionError, ValueError, TypeError):
            return row
        return {**row, **metadata}

    @staticmethod
    def _metadata(message: dict[str, Any]) -> dict[str, Any]:
        return {key: message.get(key) for key in (
            "sender_id", "sender_name", "platform", "conversation_key", "sent_at",
            "received_at", "capture_epoch", "seq",
        ) if message.get(key) is not None}

    @staticmethod
    def _resolve_id(kind: str, value: str) -> str | None:
        prefix = f"chat_message_{kind}_"
        if not value.startswith(prefix):
            return None
        encoded = value[len(prefix):]
        try:
            return urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
