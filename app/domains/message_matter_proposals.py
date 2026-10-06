"""Evidence-bound human decisions and atomic message-to-matter application.

This service has no model, network, or worker write capability. Its principal is
supplied by the authenticated control transport, never by a decision payload.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.domains.matters import MatterCreateInput, MatterService, MatterSourceLinkInput
from app.domains.message_history import _now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return sha256(_json(value).encode()).hexdigest()


class ReviewedFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=8000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    due_at: str | None = Field(default=None, max_length=80)
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=30
    )

    @field_validator("title")
    @classmethod
    def title_nonblank(cls, value):
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()

    @field_validator("due_at")
    @classmethod
    def date_timezone(cls, value):
        if value is not None:
            date = datetime.fromisoformat(value)
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError("due_at requires an explicit timezone")
        return value

    @model_validator(mode="after")
    def normalized_saved_fields(self):
        self.summary = self.summary.strip() or self.title
        seen = set()
        tags = []
        for tag in self.tags:
            clean = tag.strip()
            if not clean:
                raise ValueError("tags must not be blank")
            if clean.casefold() not in seen:
                tags.append(clean)
                seen.add(clean.casefold())
        self.tags = tags
        return self


class ProposalInput(ReviewedFields):
    action_key: str = Field(min_length=1, max_length=256)
    evidence_message_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=1, max_length=50
    )
    certainty: Literal["explicit", "inferred", "needs_review"] = "needs_review"
    due_provenance: Literal["explicit", "relative_resolved", "inferred", "unknown"] = "unknown"
    time_text: str | None = Field(default=None, max_length=500)
    topic_id: str | None = Field(default=None, max_length=256)
    insight_id: str | None = Field(default=None, max_length=256)


class _DecisionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_key: str = Field(min_length=1, max_length=128)


class CreateDecision(_DecisionBase):
    action: Literal["create"]
    reviewed_fields: ReviewedFields


class LinkExistingDecision(_DecisionBase):
    action: Literal["link_existing"]
    target_matter_id: str = Field(min_length=1, max_length=256)
    expected_target_revision: int = Field(ge=1)
    source_reason: str = Field(default="", max_length=2000)


class RejectDecision(_DecisionBase):
    action: Literal["reject"]
    reason: str = Field(default="", max_length=2000)


ProposalDecision = Annotated[
    CreateDecision | LinkExistingDecision | RejectDecision, Field(discriminator="action")
]
DECISION_ADAPTER = TypeAdapter(ProposalDecision)


@dataclass(frozen=True)
class HumanControlPrincipal:
    principal_id: str
    kind: str = "human_control"


class ProposalConflict(ValueError):
    pass


class MessageMatterProposalService:
    def __init__(self, message_history, matter_service: MatterService, conn_factory=None):
        self.message_history = message_history
        self.matters = matter_service
        self._conn_factory = conn_factory or message_history._connect
        self.ensure_schema()

    @contextmanager
    def _transaction(self, conn=None):
        owns = conn is None
        if owns:
            conn = self._conn_factory()
            conn.execute("BEGIN IMMEDIATE")
        elif not conn.in_transaction:
            raise ValueError("Proposal caller connection requires an active transaction")
        try:
            yield conn
            if owns:
                conn.commit()
        except BaseException:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                conn.close()

    def ensure_schema(self):
        with self._transaction() as conn:
            for sql in (
                """CREATE TABLE IF NOT EXISTS message_matter_proposals (
                    proposal_id TEXT NOT NULL, revision INTEGER NOT NULL, conversation_key TEXT NOT NULL,
                    action_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                    ('pending','accepted','rejected','superseded','revoked')),
                    content_json TEXT NOT NULL, evidence_json TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                    fences_json TEXT NOT NULL, preview_digest TEXT NOT NULL, content_digest TEXT NOT NULL,
                    frozen_reason TEXT, related_proposal_id TEXT, matter_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(proposal_id,revision))""",
                """CREATE INDEX IF NOT EXISTS idx_message_proposals_feed ON
                    message_matter_proposals(state,updated_at,proposal_id,revision)""",
                """CREATE TABLE IF NOT EXISTS message_proposal_decisions (
                    decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL, payload_json TEXT NOT NULL, principal_id TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS message_proposal_applications (
                    proposal_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE, action TEXT NOT NULL,
                    matter_id TEXT NOT NULL, before_revision INTEGER, after_revision INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            ):
                conn.execute(sql)

    @staticmethod
    def _human(principal):
        if (
            not isinstance(principal, HumanControlPrincipal)
            or principal.kind != "human_control"
            or not isinstance(principal.principal_id, str)
            or not principal.principal_id.strip()
        ):
            raise PermissionError("human_control_identity_required")

    def _evidence(self, conn, ids, *, allowed_sources=None, allowed_accounts=None):
        evidence = []
        for message_id in sorted(set(ids)):
            message = conn.execute(
                "SELECT * FROM message_history_messages WHERE internal_message_id=?", (message_id,)
            ).fetchone()
            if message is None:
                raise PermissionError("message_history_not_allowed")
            policies = self.message_history._read_scope(
                conn, message["conversation_key"], allowed_sources, allowed_accounts
            )
            if not policies:
                raise PermissionError("message_history_not_allowed")
            policy = policies[0]
            evidence.append(
                {
                    "source_type": "message_history_message",
                    "source_id": policy["source_id"],
                    "account_scope_id": policy["account_scope_id"],
                    **self.message_history._resolved_message(conn, message),
                }
            )
        if len({item["conversation_key"] for item in evidence}) != 1:
            raise ValueError("proposal evidence must belong to one conversation")
        return evidence

    @staticmethod
    def _policy(conn, key):
        return conn.execute(
            "SELECT * FROM message_history_policies WHERE conversation_key=?", (key,)
        ).fetchone()

    @staticmethod
    def _fences(policy):
        return {
            name: policy[name]
            for name in (
                "capture_epoch",
                "analysis_epoch",
                "proposals_epoch",
                "processing_revision",
            )
        }

    def _head(self, conn, proposal_id):
        row = conn.execute(
            "SELECT * FROM message_matter_proposals WHERE proposal_id=? ORDER BY revision DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise KeyError("matter_proposal_not_found")
        return row

    @staticmethod
    def _preview_digest(proposal_id, revision, content, evidence_digest, fences):
        return _digest(
            {
                "proposal_id": proposal_id,
                "revision": revision,
                "content": content,
                "evidence_digest": evidence_digest,
                "fences": fences,
            }
        )

    def publish_proposal(self, payload: ProposalInput | dict, *, conn=None, expected_fences=None):
        data = (
            payload if isinstance(payload, ProposalInput) else ProposalInput.model_validate(payload)
        )
        content = data.model_dump(mode="json", exclude={"evidence_message_ids"})
        # Inferred/ambiguous dates never become a definite matter deadline by default.
        if data.due_provenance in {"inferred", "unknown"}:
            content["due_at"] = None
        with self._transaction(conn) as transaction:
            evidence = self._evidence(transaction, data.evidence_message_ids)
            key = evidence[0]["conversation_key"]
            policy = self._policy(transaction, key)
            fences = self._fences(policy)
            if not policy["analysis_enabled"] or not policy["proposals_enabled"]:
                raise PermissionError("matter_proposals_not_enabled")
            if expected_fences is not None and any(
                expected_fences.get(name) != value for name, value in fences.items()
            ):
                raise ProposalConflict("proposal_generation_fenced")
            evidence_digest = _digest(evidence)
            proposal_id = "message_proposal_" + _digest([key, data.action_key])[:32]
            old = transaction.execute(
                "SELECT * FROM message_matter_proposals WHERE proposal_id=? ORDER BY revision DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            related = None
            if old and old["state"] in {"accepted", "rejected", "revoked"}:
                if old["evidence_digest"] == evidence_digest:
                    return self._preview(transaction, old)
                related = proposal_id
                proposal_id += "_" + evidence_digest[:16]
                old = transaction.execute(
                    "SELECT * FROM message_matter_proposals WHERE proposal_id=? ORDER BY revision DESC LIMIT 1",
                    (proposal_id,),
                ).fetchone()
            content_digest = _digest(content)
            if (
                old
                and old["content_digest"] == content_digest
                and old["evidence_digest"] == evidence_digest
                and json.loads(old["fences_json"]) == fences
            ):
                return self._preview(transaction, old)
            if old and old["state"] != "pending":
                return self._preview(transaction, old)
            revision = old["revision"] + 1 if old else 1
            if old:
                transaction.execute(
                    "UPDATE message_matter_proposals SET state='superseded',updated_at=? WHERE proposal_id=? AND revision=?",
                    (_now(), proposal_id, old["revision"]),
                )
            self._insert(
                transaction, proposal_id, revision, key, content, evidence, fences, related
            )
            return self._preview(transaction, self._head(transaction, proposal_id))

    def _insert(self, conn, proposal_id, revision, key, content, evidence, fences, related=None):
        evidence_digest = _digest(evidence)
        now = _now()
        conn.execute(
            """INSERT INTO message_matter_proposals(proposal_id,revision,conversation_key,action_key,
            state,content_json,evidence_json,evidence_digest,fences_json,preview_digest,content_digest,
            related_proposal_id,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id,
                revision,
                key,
                content["action_key"],
                _json(content),
                _json(evidence),
                evidence_digest,
                _json(fences),
                self._preview_digest(proposal_id, revision, content, evidence_digest, fences),
                _digest(content),
                related,
                now,
                now,
            ),
        )

    def _refresh_state(self, conn, row):
        policy = self._policy(conn, row["conversation_key"])
        reason = row["frozen_reason"] if row["state"] == "revoked" else None
        state = row["state"]
        if policy is None or not policy["record_enabled"]:
            if state in {"pending", "revoked"}:
                state = "revoked"
            reason = "record_revoked"
        elif row["state"] == "pending":
            if not policy["analysis_enabled"]:
                reason = "analysis_disabled"
            elif not policy["proposals_enabled"]:
                reason = "proposals_disabled"
            elif self._fences(policy) != json.loads(row["fences_json"]):
                reason = "permission_generation_changed"
        if state != row["state"] or reason != row["frozen_reason"]:
            conn.execute(
                "UPDATE message_matter_proposals SET state=?,frozen_reason=? WHERE proposal_id=? AND revision=?",
                (state, reason, row["proposal_id"], row["revision"]),
            )
            row = self._head(conn, row["proposal_id"])
        return row

    def _visible(self, conn, row, allowed_sources, allowed_accounts):
        policy = self._policy(conn, row["conversation_key"])
        if (
            policy is None
            or (allowed_sources is not None and policy["source_id"] not in allowed_sources)
            or (allowed_accounts is not None and policy["account_scope_id"] not in allowed_accounts)
        ):
            raise PermissionError("message_history_not_allowed")

    def _preview(self, conn, row, allowed_sources=None, allowed_accounts=None):
        self._visible(conn, row, allowed_sources, allowed_accounts)
        row = self._refresh_state(conn, row)
        base = {
            key: row[key]
            for key in (
                "proposal_id",
                "revision",
                "state",
                "frozen_reason",
                "matter_id",
                "created_at",
                "updated_at",
                "related_proposal_id",
            )
        }
        if row["state"] == "revoked" or row["frozen_reason"] == "record_revoked":
            return base
        content = json.loads(row["content_json"])
        fields = {key: content[key] for key in ReviewedFields.model_fields}
        matches = conn.execute(
            "SELECT m.matter_id,m.title,m.summary,m.revision,m.status,m.priority,m.due_at "
            "FROM matters_fts JOIN matters m ON m.matter_id=matters_fts.matter_id "
            "WHERE matters_fts MATCH ? ORDER BY rank,m.updated_at DESC,m.matter_id LIMIT 10",
            (self.matters._fts_query(content["title"]),),
        ).fetchall()
        suggestions = [
            dict(m)
            for m in matches
            if self._target_in_scope(conn, m["matter_id"], allowed_sources, allowed_accounts)
        ][:5]
        policy = self._policy(conn, row["conversation_key"])
        return {
            **base,
            "conversation_key": row["conversation_key"],
            "conversation_display_name": policy["display_name"],
            "topic_id": content["topic_id"],
            "insight_id": content["insight_id"],
            "action_key": row["action_key"],
            "preview_digest": row["preview_digest"],
            "evidence_digest": row["evidence_digest"],
            "proposed_fields": fields,
            "certainty": content["certainty"],
            "due_provenance": content["due_provenance"],
            "time_text": content["time_text"],
            "evidence": json.loads(row["evidence_json"]),
            "suggested_matters": suggestions,
        }

    def get_proposal(self, proposal_id, *, allowed_sources=None, allowed_accounts=None):
        with self._transaction() as conn:
            return self._preview(
                conn, self._head(conn, proposal_id), allowed_sources, allowed_accounts
            )

    def list_proposals(
        self,
        *,
        conversation_key=None,
        allowed_sources=None,
        allowed_accounts=None,
        state=None,
        limit=50,
        cursor=None,
        since=None,
        until=None,
    ):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("invalid proposal page limit")
        if state is not None and state not in {
            "pending",
            "accepted",
            "rejected",
            "superseded",
            "revoked",
        }:
            raise ValueError("invalid proposal state")
        if conversation_key is not None and (
            not isinstance(conversation_key, str)
            or not conversation_key.strip()
            or len(conversation_key) > 256
        ):
            raise ValueError("invalid proposal conversation")
        times = {}
        for name, value in (("since", since), ("until", until)):
            if value is not None:
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError("invalid proposal time filter")
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("proposal time filters require timezone")
                times[name] = parsed.astimezone(UTC).isoformat(timespec="microseconds")
        if len(times) == 2 and times["since"] > times["until"]:
            raise ValueError("invalid proposal time range")
        filters = {
            "conversation_key": conversation_key,
            "sources": sorted(allowed_sources) if allowed_sources is not None else None,
            "accounts": sorted(allowed_accounts) if allowed_accounts is not None else None,
            "state": state,
            **times,
        }
        terms, params = [], []
        if conversation_key is not None:
            terms.append("q.conversation_key=?")
            params.append(conversation_key)
        for name, value in times.items():
            terms.append("q.created_at" + (">=?" if name == "since" else "<=?"))
            params.append(value)
        for name, values in (
            ("source_id", allowed_sources),
            ("account_scope_id", allowed_accounts),
        ):
            if values is not None:
                terms.append(
                    "p." + name + " IN (" + ",".join("?" for _ in values) + ")" if values else "0"
                )
                params.extend(values)
        if state:
            terms.append("q.state=?")
            params.append(state)
        if cursor:
            try:
                page = json.loads(cursor)
                if not isinstance(page, dict) or set(page) != {
                    "filters",
                    "created_at",
                    "proposal_id",
                }:
                    raise ValueError("invalid proposal cursor")
                if any(not isinstance(page[key], str) for key in page):
                    raise ValueError("invalid proposal cursor")
                if page["filters"] != _digest(filters):
                    raise ValueError("proposal_cursor_scope_changed")
                if len(cursor) > 2048 or not page["proposal_id"].startswith("message_proposal_"):
                    raise ValueError("invalid proposal cursor")
                if datetime.fromisoformat(page["created_at"]).tzinfo is None:
                    raise ValueError("invalid proposal cursor")
            except (TypeError, json.JSONDecodeError, OverflowError) as exc:
                raise ValueError("invalid proposal cursor") from exc
            terms.append("(q.created_at,q.proposal_id) < (?,?)")
            params.extend([page["created_at"], page["proposal_id"]])
        with self._transaction() as conn:
            # Refresh revoked pending state before SQL filtering and pagination.
            conn.execute("""UPDATE message_matter_proposals SET
                state=CASE WHEN state='pending' THEN 'revoked' ELSE state END,frozen_reason='record_revoked'
                WHERE conversation_key IN (SELECT conversation_key FROM message_history_policies WHERE record_enabled=0)""")
            where = " AND " + " AND ".join(terms) if terms else ""
            rows = conn.execute(
                """SELECT q.* FROM message_matter_proposals q
                JOIN message_history_policies p ON p.conversation_key=q.conversation_key
                WHERE q.revision=(SELECT max(revision) FROM message_matter_proposals h WHERE h.proposal_id=q.proposal_id)"""
                + where
                + " ORDER BY q.created_at DESC,q.proposal_id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            more = len(rows) > limit
            items = [
                self._preview(conn, row, allowed_sources, allowed_accounts) for row in rows[:limit]
            ]
            last = rows[limit - 1] if more else None
            return {
                "proposals": items,
                "has_more": more,
                "next_cursor": _json(
                    {
                        "created_at": last["created_at"],
                        "proposal_id": last["proposal_id"],
                        "filters": _digest(filters),
                    }
                )
                if last
                else None,
            }

    def _target_in_scope(self, conn, matter_id, allowed_sources, allowed_accounts):
        links = conn.execute(
            "SELECT source_type,source_id FROM matter_source_links WHERE matter_id=?", (matter_id,)
        ).fetchall()
        if allowed_sources is None and allowed_accounts is None:
            # Even a root control action cannot attach currently revoked message evidence.
            for link in links:
                if link["source_type"].strip().lower() == "message_history_message":
                    try:
                        self._evidence(conn, [link["source_id"]])
                    except PermissionError:
                        return False
            return True
        if not links:
            return False
        for link in links:
            if link["source_type"].strip().lower() == "message_history_message":
                try:
                    self._evidence(
                        conn,
                        [link["source_id"]],
                        allowed_sources=allowed_sources,
                        allowed_accounts=allowed_accounts,
                    )
                except PermissionError:
                    return False
            else:
                try:
                    self.matters.validate_source_links_scope(
                        source_links=[
                            MatterSourceLinkInput(
                                source_type=link["source_type"], source_id=link["source_id"]
                            )
                        ],
                        source_ids=tuple(allowed_sources) if allowed_sources is not None else None,
                        account_ids=tuple(allowed_accounts)
                        if allowed_accounts is not None
                        else None,
                        allow_unattributed=False,
                        conn=conn,
                    )
                except PermissionError:
                    return False
        return True

    def decide(
        self,
        proposal_id,
        decision: ProposalDecision | dict,
        *,
        principal: HumanControlPrincipal,
        allowed_sources=None,
        allowed_accounts=None,
    ):
        self._human(principal)
        data = DECISION_ADAPTER.validate_python(decision)
        payload = data.model_dump(mode="json")
        payload_digest = _digest(
            {
                "proposal_id": proposal_id,
                "principal_id": principal.principal_id,
                "decision": payload,
            }
        )
        with self._transaction() as conn:
            row = self._head(conn, proposal_id)
            self._visible(conn, row, allowed_sources, allowed_accounts)
            row = self._refresh_state(conn, row)
            if row["state"] == "revoked" or row["frozen_reason"] == "record_revoked":
                # Persist revocation independently of the rejected action.
                conn.commit()
                raise PermissionError("proposal_record_revoked")
            evidence = self._evidence(
                conn,
                [item["message_id"] for item in json.loads(row["evidence_json"])],
                allowed_sources=allowed_sources,
                allowed_accounts=allowed_accounts,
            )
            if (
                _digest(evidence) != data.evidence_digest
                or row["evidence_digest"] != data.evidence_digest
            ):
                raise ProposalConflict("proposal_evidence_changed")
            if (
                row["revision"] != data.expected_revision
                or row["preview_digest"] != data.preview_digest
            ):
                raise ProposalConflict("proposal_preview_changed")
            policy = self._policy(conn, row["conversation_key"])
            if data.action != "reject" and (
                row["frozen_reason"]
                or not policy["analysis_enabled"]
                or not policy["proposals_enabled"]
                or self._fences(policy) != json.loads(row["fences_json"])
            ):
                raise PermissionError("proposal_acceptance_frozen")
            prior = conn.execute(
                "SELECT * FROM message_proposal_decisions WHERE decision_id=?", (data.decision_key,)
            ).fetchone()
            target = None
            if data.action == "link_existing":
                target = self.matters.get_matter(matter_id=data.target_matter_id, conn=conn)
                if not self._target_in_scope(
                    conn, target.matter_id, allowed_sources, allowed_accounts
                ):
                    raise PermissionError("proposal_target_outside_scope")
                expected = data.expected_target_revision
                if prior:
                    application = conn.execute(
                        "SELECT * FROM message_proposal_applications WHERE decision_id=?",
                        (data.decision_key,),
                    ).fetchone()
                    expected = application["after_revision"] if application else expected
                if target.revision != expected:
                    raise ProposalConflict("proposal_target_revision_changed")
            if prior:
                if prior["payload_digest"] != payload_digest:
                    raise ProposalConflict("proposal_decision_key_reused")
                application = conn.execute(
                    "SELECT * FROM message_proposal_applications WHERE decision_id=?",
                    (data.decision_key,),
                ).fetchone()
                if application:
                    current = self.matters.get_matter(matter_id=application["matter_id"], conn=conn)
                    if not self._target_in_scope(
                        conn, current.matter_id, allowed_sources, allowed_accounts
                    ):
                        raise PermissionError("proposal_target_outside_scope")
                    if current.revision != application["after_revision"]:
                        raise ProposalConflict("proposal_target_revision_changed")
                return (
                    json.loads(application["receipt_json"])
                    if application
                    else {
                        "proposal_id": proposal_id,
                        "state": "rejected",
                        "decision_id": data.decision_key,
                    }
                )
            if row["state"] != "pending":
                raise ProposalConflict("proposal_already_decided")
            conn.execute(
                "INSERT INTO message_proposal_decisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    data.decision_key,
                    proposal_id,
                    row["revision"],
                    payload_digest,
                    _json(payload),
                    principal.principal_id,
                    data.action,
                    _now(),
                ),
            )
            if data.action == "reject":
                conn.execute(
                    "UPDATE message_matter_proposals SET state='rejected',updated_at=? WHERE proposal_id=? AND revision=?",
                    (_now(), proposal_id, row["revision"]),
                )
                return {
                    "proposal_id": proposal_id,
                    "state": "rejected",
                    "decision_id": data.decision_key,
                }
            before = target.revision if target else None
            links = [
                MatterSourceLinkInput(
                    source_type="message_history_message",
                    source_id=item["message_id"],
                    reason=data.source_reason
                    if isinstance(data, LinkExistingDecision)
                    else "Human-approved message proposal",
                )
                for item in evidence
            ]
            if isinstance(data, CreateDecision):
                create = MatterCreateInput(**data.reviewed_fields.model_dump(), source_links=links)
                capability = self.matters._issue_message_receipt(
                    conn, action="create", matter_id=None, payload=create
                )
                matter = self.matters.create_matter(create, conn=conn, _receipt=capability)
            else:
                for link in links:
                    capability = self.matters._issue_message_receipt(
                        conn, action="link", matter_id=target.matter_id, payload=link
                    )
                    matter = self.matters.link_source(
                        matter_id=target.matter_id, source_link=link, conn=conn, _receipt=capability
                    )
            receipt = {
                "proposal_id": proposal_id,
                "proposal_revision": row["revision"],
                "decision_id": data.decision_key,
                "state": "accepted",
                "action": data.action,
                "matter_id": matter.matter_id,
                "before_revision": before,
                "after_revision": matter.revision,
            }
            conn.execute(
                "INSERT INTO message_proposal_applications VALUES(?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    data.decision_key,
                    data.action,
                    matter.matter_id,
                    before,
                    matter.revision,
                    _json(receipt),
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE message_matter_proposals SET state='accepted',matter_id=?,updated_at=? WHERE proposal_id=? AND revision=?",
                (matter.matter_id, _now(), proposal_id, row["revision"]),
            )
            return receipt

    def revalidate(
        self,
        proposal_id,
        *,
        expected_revision,
        principal,
        allowed_sources=None,
        allowed_accounts=None,
    ):
        self._human(principal)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("invalid proposal revision")
        with self._transaction() as conn:
            old = self._head(conn, proposal_id)
            self._visible(conn, old, allowed_sources, allowed_accounts)
            old = self._refresh_state(conn, old)
            if old["revision"] != expected_revision or old["state"] != "pending":
                raise ProposalConflict("proposal_preview_changed")
            policy = self._policy(conn, old["conversation_key"])
            if (
                not policy["record_enabled"]
                or not policy["analysis_enabled"]
                or not policy["proposals_enabled"]
            ):
                raise PermissionError("matter_proposals_not_enabled")
            evidence = self._evidence(
                conn,
                [e["message_id"] for e in json.loads(old["evidence_json"])],
                allowed_sources=allowed_sources,
                allowed_accounts=allowed_accounts,
            )
            conn.execute(
                "UPDATE message_matter_proposals SET state='superseded',updated_at=? WHERE proposal_id=? AND revision=?",
                (_now(), proposal_id, old["revision"]),
            )
            self._insert(
                conn,
                proposal_id,
                old["revision"] + 1,
                old["conversation_key"],
                json.loads(old["content_json"]),
                evidence,
                self._fences(policy),
                old["related_proposal_id"],
            )
            return self._preview(
                conn, self._head(conn, proposal_id), allowed_sources, allowed_accounts
            )
