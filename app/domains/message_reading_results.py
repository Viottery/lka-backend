"""Deterministic reading projections, provenance and independent user attention."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ReadingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    self_ids: dict[str, list[str]] = Field(default_factory=dict, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    critical_keywords: list[str] = Field(default_factory=list, max_length=100)
    tracked_topics: list[str] = Field(default_factory=list, max_length=100)
    important_contacts: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounded_items(self):
        items = [
            *self.aliases,
            *self.keywords,
            *self.critical_keywords,
            *self.tracked_topics,
            *self.important_contacts,
            *self.exclusions,
        ]
        for platform, ids in self.self_ids.items():
            if not platform.strip() or len(platform) > 80 or len(ids) > 50:
                raise ValueError("invalid_profile_identity")
            items.extend(ids)
        if any(not item.strip() or len(item) > 512 for item in items):
            raise ValueError("invalid_profile_item")
        return self


class ReadingRevisionConflict(ValueError):
    """A local-control compare-and-swap lost its expected revision."""


class TopicUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    existing_topic_id: str | None = Field(default=None, max_length=120)
    batch_local_key: str | None = Field(default=None, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    source_message_ids: list[str] = Field(min_length=1, max_length=50)
    member_message_ids: list[str] | None = Field(default=None, max_length=10000)
    conclusions: list[str] = Field(default_factory=list, max_length=10)
    disagreements: list[str] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def bounded_topic(self):
        if bool(self.existing_topic_id) == bool(self.batch_local_key):
            raise ValueError("topic_requires_one_identity")
        if not self.title.strip() or not self.summary.strip():
            raise ValueError("blank_topic")
        if self.member_message_ids is not None and not set(self.source_message_ids) <= set(self.member_message_ids):
            raise ValueError("topic_representatives_must_be_members")
        if any(
            not item.strip() or len(item) > 512
            for item in [*self.conclusions, *self.disagreements, *self.open_questions]
        ):
            raise ValueError("invalid_topic_statement")
        return self


class ReadingFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["useful", "interesting", "decision", "question", "importance", "correction"] = (
        "importance"
    )
    text: str = Field(min_length=1, max_length=2000)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)
    existing_insight_id: str | None = Field(default=None, max_length=120)
    existing_topic_id: str | None = Field(default=None, max_length=120)
    batch_local_key: str | None = Field(default=None, max_length=120)
    action_key: str | None = Field(default=None, min_length=1, max_length=200)
    importance: Literal["critical", "important", "possible", "ordinary"] = "possible"
    certainty: Literal["explicit", "inferred", "needs_review"] = "needs_review"
    reason_codes: list[
        Literal[
            "direct_mention",
            "alias_mention",
            "reply_to_self",
            "group_mention",
            "action_requested",
            "deadline",
            "material_change",
            "tracked_topic",
            "important_contact",
            "worth_reading",
        ]
    ] = Field(default_factory=list, max_length=10)
    directed_to: Literal["self", "group", "other", "unknown"] = "unknown"
    time_text: str | None = Field(default=None, max_length=512)
    due_at: str | None = Field(default=None, max_length=80)
    due_provenance: Literal["explicit", "relative_resolved", "inferred", "unknown"] = "unknown"
    timezone: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def bounded_finding(self):
        if not self.text.strip() or (self.existing_topic_id and self.batch_local_key):
            raise ValueError("invalid_finding")
        if self.existing_insight_id and (self.kind != "correction" or self.certainty != "explicit"):
            raise ValueError("correction_requires_explicit_evidence")
        if self.due_at:
            value = datetime.fromisoformat(self.due_at)
            if value.tzinfo is None or self.due_provenance not in ("explicit", "relative_resolved"):
                raise ValueError("deadline_requires_grounded_timezone")
        return self


class MessageReadingResultsMixin:
    def _ensure_reading_results_schema(self, conn):
        for sql in (
            "CREATE TABLE IF NOT EXISTS message_reading_profiles(scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,profile_json TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS message_reading_topics(topic_id TEXT PRIMARY KEY,conversation_key TEXT NOT NULL,revision INTEGER NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,details_json TEXT NOT NULL,first_seen INTEGER NOT NULL,last_seen INTEGER NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_reading_topics ON message_reading_topics(conversation_key,last_seen DESC,topic_id)",
            "CREATE TABLE IF NOT EXISTS message_reading_topic_revisions(topic_id TEXT NOT NULL,revision INTEGER NOT NULL,batch_job_id TEXT NOT NULL,content_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(topic_id,revision))",
            "CREATE TABLE IF NOT EXISTS message_reading_insights(insight_id TEXT PRIMARY KEY,conversation_key TEXT NOT NULL,topic_id TEXT,revision INTEGER NOT NULL,kind TEXT NOT NULL,importance TEXT NOT NULL,certainty TEXT NOT NULL,text TEXT NOT NULL,content_json TEXT NOT NULL,dedup_key TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(conversation_key,dedup_key))",
            "CREATE INDEX IF NOT EXISTS idx_reading_insights ON message_reading_insights(conversation_key,importance,updated_at DESC,insight_id)",
            "CREATE TABLE IF NOT EXISTS message_reading_insight_revisions(insight_id TEXT NOT NULL,revision INTEGER NOT NULL,content_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(insight_id,revision))",
            "CREATE TABLE IF NOT EXISTS message_reading_detector_evidence(insight_id TEXT NOT NULL,detector TEXT NOT NULL,input_digest TEXT NOT NULL,explanation_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(insight_id,detector,input_digest))",
            "CREATE TABLE IF NOT EXISTS message_reading_sources(object_kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,message_id TEXT NOT NULL,conversation_key TEXT NOT NULL,batch_job_id TEXT,purpose TEXT NOT NULL,PRIMARY KEY(object_kind,object_id,revision,message_id))",
            "CREATE INDEX IF NOT EXISTS idx_reading_sources_message ON message_reading_sources(message_id,object_kind,object_id)",
            "CREATE TABLE IF NOT EXISTS message_reading_attention(user_id TEXT NOT NULL,insight_id TEXT NOT NULL,revision INTEGER NOT NULL,viewed_revision INTEGER NOT NULL DEFAULT 0,dismissed_revision INTEGER NOT NULL DEFAULT 0,snoozed_until TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,insight_id))",
            "CREATE TABLE IF NOT EXISTS message_reading_digests(conversation_key TEXT PRIMARY KEY,revision INTEGER NOT NULL,generated_at TEXT NOT NULL,covered_seq INTEGER NOT NULL,input_digest TEXT NOT NULL,content_json TEXT NOT NULL,stale INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS message_reading_batch_outputs(job_id TEXT PRIMARY KEY,output_json TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS message_reading_manifests(job_id TEXT PRIMARY KEY,conversation_key TEXT NOT NULL,capture_epoch INTEGER NOT NULL,end_seq INTEGER NOT NULL,content_json TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_reading_manifests_scope ON message_reading_manifests(conversation_key,end_seq DESC)",
        ):
            conn.execute(sql)
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(message_history_conversations)")
        }
        for name, decl in {
            "reading_covered_seq": "INTEGER",
            "reading_baseline_start_seq": "INTEGER",
            "digest_covered_seq": "INTEGER NOT NULL DEFAULT 0",
            # NULL means no from-now cutover was requested. Zero is a valid
            # cutover floor for an empty conversation and must remain distinct.
            "analysis_baseline_floor_seq": "INTEGER",
            "analysis_baseline_initialized": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE message_history_conversations ADD COLUMN {name} {decl}")
        # Existing enabled policies have already begun analysis and cannot use
        # a later start_from_now request to jump over unfinished paid work.
        conn.execute("UPDATE message_history_conversations SET analysis_baseline_initialized=1 WHERE conversation_key IN (SELECT conversation_key FROM message_history_policies WHERE analysis_enabled=1 OR analysis_epoch>1)")
        conn.execute(
            "INSERT OR IGNORE INTO message_reading_profiles VALUES('global',0,?,?)",
            (_json(ReadingProfile().model_dump()), self._reading_now()),
        )

    def _profile(self, conn, scope="global"):
        row = conn.execute(
            "SELECT * FROM message_reading_profiles WHERE scope=?", (scope,)
        ).fetchone()
        return {
            "scope": scope,
            "revision": row["revision"] if row else 0,
            "updated_at": row["updated_at"] if row else None,
            **ReadingProfile().model_dump(),
            **(json.loads(row["profile_json"]) if row else {}),
        }

    def get_reading_profile(self, scope="global"):
        with self._connection() as conn:
            if scope != "global":
                self._read_scope(conn, scope, None, None)
            return self._profile(conn, scope)

    def set_reading_profile(self, profile, expected_revision, scope="global"):
        value = ReadingProfile.model_validate(profile).model_dump()
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_profile_revision")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if scope != "global":
                self._read_scope(conn, scope, None, None)
            old = self._profile(conn, scope)
            if old["revision"] != expected_revision:
                raise ReadingRevisionConflict("profile_revision_conflict")
            now = self._reading_now()
            conn.execute(
                "INSERT INTO message_reading_profiles VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                (scope, expected_revision + 1, _json(value), now),
            )
            # A profile change changes semantic input; retain consumed family budget.
            keys = [
                r[0]
                for r in conn.execute(
                    "SELECT conversation_key FROM message_history_policies WHERE record_enabled=1"
                    + (" AND conversation_key=?" if scope != "global" else ""),
                    (scope,) if scope != "global" else (),
                )
            ]
            for key in keys:
                conn.execute(
                    "UPDATE message_history_policies SET processing_revision=processing_revision+1,revision=revision+1,updated_at=? WHERE conversation_key=?",
                    (now, key),
                )
                conn.execute(
                    "UPDATE background_jobs SET status='cancelled',finished_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL,lease_epoch=lease_epoch+1 WHERE kind='message_analysis' AND scope_id=? AND status IN ('queued','running','retry_wait')",
                    (now, now, key),
                )
                conn.execute(
                    "DELETE FROM message_reading_checkpoints WHERE family_id IN (SELECT family_id FROM message_reading_families WHERE conversation_key=?)",
                    (key,),
                )
            conn.commit()
            return {"scope": scope, "revision": expected_revision + 1, "updated_at": now, **value}

    def _reading_input_context(self, conn, key):
        now = int(datetime.fromisoformat(self._reading_now()).timestamp())
        rows = conn.execute(
            "SELECT * FROM message_reading_topics WHERE conversation_key=? AND last_seen>=? ORDER BY last_seen DESC,topic_id LIMIT 21",
            (key, now - 86400),
        ).fetchall()
        insights = conn.execute(
            "SELECT insight_id,revision,kind,text FROM message_reading_insights WHERE conversation_key=? ORDER BY updated_at DESC,insight_id LIMIT 20",
            (key,),
        ).fetchall()
        return {
            "known_topics": [
                {
                    "topic_id": r["topic_id"],
                    "revision": r["revision"],
                    "title": r["title"],
                    "summary": r["summary"][:500],
                }
                for r in rows[:20]
            ],
            "topic_candidates_limited": len(rows) > 20,
            "known_insights": [{**dict(r), "text": r["text"][:200]} for r in insights],
            "reading_profile": self._profile(conn),
            "conversation_profile": self._profile(conn, key),
        }

    def scan_local_signals(self, conversation_key=None, *, limit=200):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_scan_limit")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policies = conn.execute(
                "SELECT p.* FROM message_history_policies p JOIN message_history_conversations c ON c.conversation_key=p.conversation_key WHERE p.record_enabled=1 AND p.local_signals_enabled=1 AND c.next_seq-1>c.local_signal_seq"
                + (" AND p.conversation_key=?" if conversation_key else "")
                + " ORDER BY c.local_signal_seq,p.conversation_key LIMIT 32",
                (conversation_key,) if conversation_key else (),
            ).fetchall()
            scanned = 0
            for policy in policies:
                scanned += self._scan_local_signals(conn, policy, limit)
            conn.commit()
            return {"scanned": scanned}

    def _scan_local_signals(self, conn, policy, limit=1000):
        key = policy["conversation_key"]
        if not policy["record_enabled"] or not policy["local_signals_enabled"]:
            return 0
        state = conn.execute(
            "SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)
        ).fetchone()
        start = state["local_signal_seq"] + 1
        profile, local = self._profile(conn), self._profile(conn, key)
        keywords = [
            *profile["keywords"],
            *profile["tracked_topics"],
            *local["keywords"],
            *local["tracked_topics"],
        ]
        contacts = [*profile["important_contacts"], *local["important_contacts"]]
        critical_keywords = [*profile["critical_keywords"], *local["critical_keywords"]]
        aliases = [*profile["aliases"], *local["aliases"]]
        exclusions = [*profile["exclusions"], *local["exclusions"]]
        self_ids = set(
            profile["self_ids"].get(policy["platform"], [])
            + local["self_ids"].get(policy["platform"], [])
        )
        rows = conn.execute(
            "SELECT * FROM message_history_messages WHERE conversation_key=? AND seq>=? ORDER BY seq LIMIT ?",
            (key, start, limit),
        ).fetchall()
        for row in rows:
            message = self._message(row)
            text = message["text"]
            reasons = []
            mentions = (
                message["mentions"]
                if message["metadata_capabilities"].get("mentions") == "supported"
                else []
            )
            direct = any(m["kind"] == "user" and m.get("user_id") in self_ids for m in mentions)
            group = any(m["kind"] == "all" for m in mentions)
            if direct:
                reasons.append("direct_mention")
            if group:
                reasons.append("group_mention")
            resolved = self._resolved_message(conn, row)
            reply_self = False
            if resolved["reply_to_internal_message_id"]:
                target = conn.execute(
                    "SELECT sender_id FROM message_history_messages WHERE internal_message_id=? AND conversation_key=?",
                    (resolved["reply_to_internal_message_id"], key),
                ).fetchone()
                reply_self = bool(target and target[0] in self_ids)
                if reply_self:
                    reasons.append("reply_to_self")
            excluded = any(word.casefold() in text.casefold() for word in exclusions)
            critical = False
            if not excluded:
                critical = any(word.casefold() in text.casefold() for word in critical_keywords)
                if critical:
                    reasons.append("tracked_topic")
                if any(
                    re.search(r"@" + re.escape(alias) + r"(?!\w)", text, re.IGNORECASE)
                    for alias in aliases
                ):
                    reasons.append("alias_mention")
                if any(word.casefold() in text.casefold() for word in keywords):
                    reasons.append("tracked_topic")
                if message["sender_id"] in contacts:
                    reasons.append("important_contact")
                if re.search(r"请|需要|务必|please|must|action required", text, re.IGNORECASE):
                    reasons.append("action_requested")
                if re.search(r"截止|期限|之前|deadline|due\b", text, re.IGNORECASE):
                    reasons.append("deadline")
                if re.search(
                    r"更正|改为|取消|correction|cancelled|changed to", text, re.IGNORECASE
                ):
                    reasons.append("material_change")
            if reasons:
                finding = ReadingFinding(
                    text=text[:2000] or "Native mention in a non-text message",
                    source_message_ids=[row["internal_message_id"]],
                    reason_codes=reasons,
                    importance="critical" if critical else "possible",
                    directed_to="self" if direct or reply_self else "group" if group else "unknown",
                )
                self._upsert_insight(
                    conn, key, finding, None, None, self._reading_now(), "local_rule"
                )
        if rows:
            conn.execute(
                "UPDATE message_history_conversations SET local_signal_start_seq=COALESCE(local_signal_start_seq,?),local_signal_seq=? WHERE conversation_key=?",
                (start, rows[-1]["seq"], key),
            )
        return len(rows)

    def _source_rows(self, conn, kind, object_id, revision, key, ids, job_id, purpose):
        for message_id in set(ids):
            conn.execute(
                "INSERT OR IGNORE INTO message_reading_sources VALUES(?,?,?,?,?,?,?)",
                (kind, object_id, revision, message_id, key, job_id, purpose),
            )

    def _upsert_insight(self, conn, key, finding, topic_id, job_id, now, detector, bucket=None):
        bucket = bucket or ("importance" if finding.kind in ("importance", "correction") else "highlight")
        primary = min(set(finding.source_message_ids))
        # Explicit action keys distinguish different actions in the same message.
        dedup = _hash(
            [
                primary,
                finding.action_key
                or ("importance" if bucket == "importance" else finding.kind),
            ]
        )
        old = conn.execute(
            "SELECT * FROM message_reading_insights WHERE "
            + (
                "insight_id=? AND conversation_key=?"
                if finding.existing_insight_id
                else "conversation_key=? AND dedup_key=?"
            ),
            (finding.existing_insight_id, key) if finding.existing_insight_id else (key, dedup),
        ).fetchone()
        insight_id = old["insight_id"] if old else "message_insight_" + _hash([key, dedup])
        content = finding.model_dump(mode="json")
        content["reading_bucket"] = bucket
        content.pop("existing_insight_id")
        content["importance"] = (
            "important"
            if finding.importance == "critical" and detector == "model"
            else finding.importance
        )
        content["detectors"] = [detector]
        explanation = {
            "text": finding.text,
            "reason_codes": finding.reason_codes,
            "certainty": finding.certainty,
            "source_message_ids": finding.source_message_ids,
        }
        content["detector_explanations"] = {detector: explanation}
        conn.execute(
            "INSERT OR IGNORE INTO message_reading_detector_evidence VALUES(?,?,?,?,?)",
            (insight_id, detector, _hash(explanation), _json(explanation), now),
        )
        if old:
            prior = json.loads(old["content_json"])
            if prior.get("reading_bucket") == "importance":
                content["reading_bucket"] = "importance"
            if prior["importance"] == "critical" and detector == "model":
                content["importance"] = "critical"
            content["detectors"] = sorted(set(prior.get("detectors", []) + [detector]))
            content["detector_explanations"] = {
                **prior.get("detector_explanations", {}),
                detector: explanation,
            }
            native_reasons = [
                r for r in prior["reason_codes"] if r in ("direct_mention", "group_mention")
            ]
            content["reason_codes"] = sorted(set(content["reason_codes"] + native_reasons))
            if "direct_mention" in native_reasons:
                content["directed_to"] = "self"
            # Current cards stay bounded; all previous evidence remains in the
            # immutable revision/source ledger and is available through paging.
            content["source_message_ids"] = sorted(set(content["source_message_ids"]))
            if _json(content) == _json(prior) and (topic_id is None or topic_id == old["topic_id"]):
                return insight_id

            def material(value):
                return {
                    "text": re.sub(r"[\W_]+", "", value["text"]).casefold(),
                    "sources": sorted(value["source_message_ids"]),
                    "due_at": value["due_at"],
                    "time_text": value["time_text"],
                    "directed_to": value["directed_to"],
                }

            if material(content) == material(prior):
                conn.execute(
                    "UPDATE message_reading_insights SET content_json=?,text=?,importance=?,certainty=?,topic_id=COALESCE(?,topic_id),updated_at=? WHERE insight_id=?",
                    (
                        _json(content),
                        finding.text,
                        content["importance"],
                        finding.certainty,
                        topic_id,
                        now,
                        insight_id,
                    ),
                )
                return insight_id
        revision = old["revision"] + 1 if old else 1
        conn.execute(
            "INSERT INTO message_reading_insights VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(insight_id) DO UPDATE SET topic_id=COALESCE(excluded.topic_id,topic_id),revision=excluded.revision,kind=excluded.kind,importance=excluded.importance,certainty=excluded.certainty,text=excluded.text,content_json=excluded.content_json,updated_at=excluded.updated_at",
            (
                insight_id,
                key,
                topic_id,
                revision,
                finding.kind,
                content["importance"],
                finding.certainty,
                finding.text,
                _json(content),
                old["dedup_key"] if old else dedup,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO message_reading_insight_revisions VALUES(?,?,?,?)",
            (insight_id, revision, _json(content), now),
        )
        self._source_rows(
            conn,
            "insight",
            insight_id,
            revision,
            key,
            content["source_message_ids"],
            job_id,
            "evidence",
        )
        if finding.kind == "correction" or "material_change" in finding.reason_codes:
            conn.execute(
                "UPDATE message_reading_digests SET stale=1 WHERE conversation_key=?", (key,)
            )
        return insight_id

    def _publish_reading_results(self, conn, job, analysis, policy, state, now):
        if analysis.schema_version not in (2, 3):
            return
        if (
            analysis.schema_version == 2 and state["reading_covered_seq"] is not None
            and state["reading_covered_seq"] != job["payload"]["start_seq"] - 1
        ):
            raise ValueError("reading_generation_coverage_gap")
        key = policy["conversation_key"]
        granted = getattr(analysis, "reading_snapshot", None) or {}
        known = {t["topic_id"]: t["revision"] for t in granted.get("known_topics", [])}
        known_insights = {t["insight_id"]: t["revision"] for t in granted.get("known_insights", [])}
        valid_ids = {
            r[0]
            for r in conn.execute(
                "SELECT internal_message_id FROM message_history_messages WHERE conversation_key=? AND seq BETWEEN ? AND ?",
                (key, job["payload"]["start_seq"], job["payload"]["end_seq"]),
            )
        }
        for finding in [
            *analysis.topic_updates,
            *analysis.highlights,
            *analysis.importance_findings,
        ]:
            if not set(finding.source_message_ids).issubset(valid_ids):
                raise ValueError("reading_source_outside_batch")
            members = getattr(finding, "member_message_ids", None)
            if members is not None and (analysis.schema_version != 3 or not set(members) <= valid_ids):
                raise ValueError("reading_member_outside_batch")
            if finding.existing_topic_id:
                row = conn.execute(
                    "SELECT revision FROM message_reading_topics WHERE topic_id=? AND conversation_key=?",
                    (finding.existing_topic_id, key),
                ).fetchone()
                if (
                    finding.existing_topic_id not in known
                    or not row
                    or row[0] != known[finding.existing_topic_id]
                ):
                    raise ValueError("reading_topic_revision_conflict")
            if getattr(finding, "existing_insight_id", None):
                row = conn.execute(
                    "SELECT revision FROM message_reading_insights WHERE insight_id=? AND conversation_key=?",
                    (finding.existing_insight_id, key),
                ).fetchone()
                if (
                    finding.existing_insight_id not in known_insights
                    or not row
                    or row[0] != known_insights[finding.existing_insight_id]
                ):
                    raise ValueError("reading_insight_revision_conflict")
            if getattr(finding, "due_provenance", None) == "relative_resolved" and finding.due_at:
                timestamps = conn.execute(
                    "SELECT sent_at FROM message_history_messages WHERE internal_message_id IN ("
                    + ",".join("?" for _ in finding.source_message_ids)
                    + ")",
                    finding.source_message_ids,
                ).fetchall()
                if any(r[0] is None for r in timestamps):
                    raise ValueError("reading_relative_date_without_sent_at")
        local_keys = {t.batch_local_key for t in analysis.topic_updates if t.batch_local_key}
        for finding in [*analysis.highlights, *analysis.importance_findings]:
            if finding.batch_local_key and finding.batch_local_key not in local_keys:
                raise ValueError("reading_unknown_local_topic")
        mapping = {local: "message_topic_" + _hash([job["job_id"], local]) for local in local_keys}
        conn.execute(
            "INSERT INTO message_reading_batch_outputs VALUES(?,?,?)",
            (job["job_id"], _json(analysis.model_dump(mode="json")), now),
        )
        if analysis.reading_manifest:
            conn.execute("INSERT INTO message_reading_manifests VALUES(?,?,?,?,?,?)",
                         (job["job_id"], key, policy["capture_epoch"], job["payload"]["end_seq"],
                          _json({**analysis.reading_manifest, "published_range": {
                              "start_seq": job["payload"]["start_seq"], "end_seq": job["payload"]["end_seq"]}}), now))
        for update in analysis.topic_updates:
            topic_id = update.existing_topic_id or mapping[update.batch_local_key]
            old = conn.execute(
                "SELECT * FROM message_reading_topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            times = conn.execute(
                "SELECT COALESCE(sent_at,received_at) FROM message_history_messages WHERE internal_message_id IN ("
                + ",".join("?" for _ in update.source_message_ids)
                + ")",
                update.source_message_ids,
            ).fetchall()
            first, last = min(r[0] for r in times), max(r[0] for r in times)
            revision = old["revision"] + 1 if old else 1
            details = update.model_dump(mode="json")
            conn.execute(
                "INSERT INTO message_reading_topics VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET revision=excluded.revision,title=excluded.title,summary=excluded.summary,details_json=excluded.details_json,first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen),updated_at=excluded.updated_at",
                (
                    topic_id,
                    key,
                    revision,
                    update.title,
                    update.summary,
                    _json(details),
                    first,
                    last,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO message_reading_topic_revisions VALUES(?,?,?,?,?)",
                (topic_id, revision, job["job_id"], _json(details), now),
            )
            self._source_rows(
                conn,
                "topic",
                topic_id,
                revision,
                key,
                update.source_message_ids,
                job["job_id"],
                "discussion",
            )
            if update.member_message_ids:
                self._source_rows(conn, "topic", topic_id, revision, key,
                                  update.member_message_ids, job["job_id"], "discussion")
        for finding in [*analysis.highlights, *analysis.importance_findings]:
            topic_id = finding.existing_topic_id or mapping.get(finding.batch_local_key)
            insight_id = self._upsert_insight(
                conn,
                key,
                finding,
                topic_id,
                job["job_id"],
                now,
                "model",
                bucket="importance" if any(finding is item for item in analysis.importance_findings) else "highlight",
            )
            proposals = getattr(self, "matter_proposals", None)
            if (
                proposals
                and policy["proposals_enabled"]
                and policy["analysis_enabled"]
                and set(finding.reason_codes) & {"action_requested", "deadline"}
            ):
                proposals.publish_proposal(
                    {
                        "action_key": finding.action_key or insight_id,
                        "evidence_message_ids": finding.source_message_ids,
                        "title": finding.text[:500],
                        "summary": finding.text,
                        "priority": "high"
                        if finding.importance in ("important", "critical")
                        else "normal",
                        "due_at": finding.due_at
                        if finding.due_provenance in ("explicit", "relative_resolved")
                        else None,
                        "due_provenance": finding.due_provenance,
                        "time_text": finding.time_text[:500] if finding.time_text else None,
                        "certainty": finding.certainty,
                        "topic_id": topic_id,
                        "insight_id": insight_id,
                    },
                    conn=conn,
                    expected_fences={
                        name: policy[name]
                        for name in (
                            "capture_epoch",
                            "analysis_epoch",
                            "proposals_epoch",
                            "processing_revision",
                        )
                    },
                )
        baseline = state["reading_baseline_start_seq"] or job["payload"]["start_seq"]
        full_text = not analysis.reading_manifest or analysis.reading_manifest.get("coverage_mode") == "full_text"
        conn.execute(
            "UPDATE message_history_conversations SET pipeline_version=?,reading_baseline_start_seq=?,baseline_start_seq=?,reading_covered_seq=? WHERE conversation_key=?",
            ("message-reading-v3" if analysis.schema_version == 3 else "message-reading-v2",
             baseline, baseline, job["payload"]["end_seq"] if full_text else state["reading_covered_seq"], key),
        )

    def _reading_coverage(self, conn, key):
        row = conn.execute(
            "SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)
        ).fetchone()
        latest = conn.execute("SELECT m.content_json FROM message_reading_manifests m JOIN message_history_policies p ON p.conversation_key=m.conversation_key AND p.capture_epoch=m.capture_epoch AND p.record_enabled=1 WHERE m.conversation_key=? ORDER BY m.end_seq DESC,m.created_at DESC LIMIT 1", (key,)).fetchone()
        manifest = json.loads(latest[0]) if latest else {}
        decisions = manifest.get("selection_manifest", [])
        seen = {span["message_id"] for span in manifest.get("model_seen_spans", [])}
        return {
            "conversation_key": key,
            "pipeline_version": row["pipeline_version"],
            "baseline_start_seq": row["reading_baseline_start_seq"],
            "legacy_covered_seq": row["covered_seq"],
            "legacy_only": row["reading_covered_seq"] is None,
            "latest_seq": row["next_seq"] - 1,
            "local_signal_start_seq": row["local_signal_start_seq"],
            "local_signal_seq": row["local_signal_seq"],
            "analysis_covered_seq": row["reading_covered_seq"],
            "analysis_watermark_seq": self._reading_watermark(row),
            "analysis_baseline_floor_seq": row["analysis_baseline_floor_seq"],
            "excluded_history_through_seq": row["analysis_baseline_floor_seq"],
            "excluded_history_count": (
                conn.execute(
                    "SELECT COUNT(*) FROM message_history_messages WHERE conversation_key=? AND seq<=?",
                    (key, row["analysis_baseline_floor_seq"]),
                ).fetchone()[0]
                if row["analysis_baseline_floor_seq"] is not None else 0
            ),
            "generation_published_seq": row["generation_published_seq"],
            "screened_seq": manifest.get("screened_seq"),
            "coverage_mode": manifest.get("coverage_mode", "full_text" if row["covered_seq"] else "unknown"),
            "model_seen_count": len(seen),
            "selected_out_count": sum(item.get("decision") in ("sampled_out", "folded")
                                      and item.get("id") not in seen for item in decisions),
            "selection_count": len(decisions),
            "published_range": manifest.get("published_range"),
            "selection_manifest": decisions,
            "model_seen_spans": manifest.get("model_seen_spans", []),
            "processing_versions": manifest.get("versions", {}),
            "digest_covered_seq": row["digest_covered_seq"],
            "pending_messages": row["next_seq"] - 1 - self._reading_watermark(row),
            "capture_mode": "inbound_only",
            "capture_gaps": "unknown",
            "complete_for_platform": False,
        }

    def _reading_scope_sql(
        self, conn, conversation_key, allowed_sources, allowed_accounts, alias="o"
    ):
        params = []
        where = f"EXISTS(SELECT 1 FROM message_history_policies p WHERE p.conversation_key={alias}.conversation_key AND p.record_enabled=1"
        if conversation_key is not None:
            self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            where += " AND p.conversation_key=?"
            params.append(conversation_key)
        for field, values in (
            ("source_id", allowed_sources),
            ("account_scope_id", allowed_accounts),
        ):
            if values is not None:
                where += (
                    " AND p."
                    + field
                    + " IN ("
                    + (",".join("?" for _ in values) if values else "NULL")
                    + ")"
                )
                params.extend(values)
        return where + ")", params

    @staticmethod
    def _reading_page(limit, maximum=100):
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError("invalid_reading_limit")
        return limit

    def _cursor(self, cursor, scope, version, sort_type=None):
        if not cursor:
            return None
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (
                value["scope"] != scope
                or value["version"] != version
                or not isinstance(value["last"], list)
                or set(value) != {"scope", "version", "last"}
            ):
                raise ValueError()
            last = value["last"]
            arity = 1 if type(version) is int else 2
            expected_type = sort_type or int
            if len(last) != arity or type(last[0]) is not expected_type:
                raise ValueError()
            if arity == 2 and (not isinstance(last[1], str) or not 1 <= len(last[1]) <= 120):
                raise ValueError()
            return value["last"]
        except Exception as exc:
            raise ValueError("reading_cursor_invalid_or_stale") from exc

    @staticmethod
    def _next_cursor(scope, version, last):
        return base64.urlsafe_b64encode(
            _json({"scope": scope, "version": version, "last": last}).encode()
        ).decode()

    def _topic(self, conn, row):
        now = int(datetime.fromisoformat(self._reading_now()).timestamp())
        stats = conn.execute(
            "SELECT COUNT(*) raw,COUNT(DISTINCT m.sender_id) participants,COUNT(DISTINCT CASE WHEN COALESCE(json_array_length(json_extract(m.metadata_json,'$.mentions')),0)>0 OR m.text LIKE '%截止%' OR m.text LIKE '%更正%' OR lower(m.text) LIKE '%deadline%' OR lower(m.text) LIKE '%correction%' THEN m.internal_message_id ELSE m.text END) folded FROM message_history_messages m WHERE m.conversation_key=? AND COALESCE(m.sent_at,m.received_at)>=? AND EXISTS(SELECT 1 FROM message_reading_sources s WHERE s.object_kind='topic' AND s.object_id=? AND s.message_id=m.internal_message_id)",
            (row["conversation_key"], now - 86400, row["topic_id"]),
        ).fetchone()
        capped = (
            conn.execute(
                "SELECT SUM(MIN(n,5)) FROM (SELECT COUNT(*) n FROM message_history_messages m WHERE m.conversation_key=? AND COALESCE(m.sent_at,m.received_at)>=? AND EXISTS(SELECT 1 FROM message_reading_sources s WHERE s.object_kind='topic' AND s.object_id=? AND s.message_id=m.internal_message_id) GROUP BY sender_id)",
                (row["conversation_key"], now - 86400, row["topic_id"]),
            ).fetchone()[0]
            or 0
        )
        freshness = min(1.0, max(0.0, 1 - (now - row["last_seen"]) / 86400))
        burst = conn.execute(
            "SELECT COALESCE(SUM(MIN(recent,5)),0) recent,COALESCE(SUM(MIN(prior,5)),0) prior FROM (SELECT SUM(CASE WHEN COALESCE(m.sent_at,m.received_at)>=? THEN 1 ELSE 0 END) recent,SUM(CASE WHEN COALESCE(m.sent_at,m.received_at)<? THEN 1 ELSE 0 END) prior FROM message_history_messages m WHERE m.conversation_key=? AND COALESCE(m.sent_at,m.received_at)>=? AND EXISTS(SELECT 1 FROM message_reading_sources s WHERE s.object_kind='topic' AND s.object_id=? AND s.message_id=m.internal_message_id) GROUP BY m.sender_id)",
            (now - 3600, now - 3600, row["conversation_key"], now - 86400, row["topic_id"]),
        ).fetchone()
        comparable = burst["prior"] >= 20
        baseline = burst["prior"] / 23
        burst_score = (
            min(1.0, max(0.0, (burst["recent"] - baseline) / max(1.0, baseline)))
            if comparable
            else 0.0
        )
        heat = min(
            1.0,
            freshness
            * (
                0.45 * min(1, stats["participants"] / 10)
                + 0.35 * min(1, math.log1p(min(stats["folded"], capped)) / math.log(31))
                + 0.20 * burst_score
            ),
        )
        details = json.loads(row["details_json"])
        return {
            "topic_id": row["topic_id"],
            "conversation_key": row["conversation_key"],
            "revision": row["revision"],
            "title": row["title"],
            "summary": row["summary"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "updated_at": row["updated_at"],
            "status": "active" if row["last_seen"] >= now - 86400 else "cooled",
            "conclusions": details["conclusions"],
            "disagreements": details["disagreements"],
            "open_questions": details["open_questions"],
            "heat": {
                "score": round(heat, 6),
                "score_version": "observed-v1",
                "window_seconds": 86400,
                "raw_message_count": stats["raw"],
                "unique_message_count": stats["folded"],
                "participant_count": stats["participants"],
                "capped_message_count": capped,
                "burst_comparable": comparable,
                "burst_score": round(burst_score, 6) if comparable else None,
                "burst_baseline_messages": burst["prior"],
                "burst_recent_messages": burst["recent"],
            },
            "sources_paginated": True,
            "untrusted_data": True,
        }

    def _attention(self, conn, insight, user_id):
        row = conn.execute(
            "SELECT * FROM message_reading_attention WHERE user_id=? AND insight_id=?",
            (user_id, insight["insight_id"]),
        ).fetchone()
        value = {
            "revision": row["revision"] if row else 0,
            "viewed_revision": row["viewed_revision"] if row else 0,
            "dismissed_revision": row["dismissed_revision"] if row else 0,
            "snoozed_until": row["snoozed_until"] if row else None,
        }
        value["state"] = (
            "dismissed"
            if value["dismissed_revision"] >= insight["revision"]
            else "seen"
            if value["viewed_revision"] >= insight["revision"]
            else "unseen"
        )
        value["has_new_changes"] = (
            0 < max(value["viewed_revision"], value["dismissed_revision"]) < insight["revision"]
        )
        value["snoozed"] = bool(
            value["snoozed_until"] and value["snoozed_until"] > self._reading_now()
        )
        return value

    def _insight(self, conn, row, user_id="local"):
        content = json.loads(row["content_json"])
        profile = self._profile(conn)
        reasons = [
            w
            for w in [*profile["keywords"], *profile["tracked_topics"]]
            if w.casefold() in row["text"].casefold()
        ]
        return {
            **content,
            "insight_id": row["insight_id"],
            "conversation_key": row["conversation_key"],
            "topic_id": row["topic_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "detector": "+".join(content["detectors"]),
            "interest_reason": reasons,
            "interest_label": "tracked" if reasons else "worth_reading",
            "attention": self._attention(conn, row, user_id),
            "sources_paginated": True,
            "source_count": conn.execute(
                "SELECT COUNT(DISTINCT message_id) FROM message_reading_sources WHERE object_kind='insight' AND object_id=?",
                (row["insight_id"],),
            ).fetchone()[0],
            "untrusted_data": True,
        }

    def _reading_feed(
        self,
        feed_kind,
        *,
        conversation_key=None,
        allowed_sources=None,
        allowed_accounts=None,
        limit=50,
        cursor=None,
        importance=None,
        unseen=False,
        user_id="local",
        kind=None,
        since=None,
        until=None,
    ):
        size = self._reading_page(limit)
        table, id_column, sort = (
            ("message_reading_topics", "topic_id", "last_seen")
            if feed_kind == "topics"
            else ("message_reading_insights", "insight_id", "updated_at")
        )
        if kind not in (None, "highlight", "importance") or (kind and feed_kind != "insights"):
            raise ValueError("invalid_insight_kind")
        times = {}
        for name, value in (("since", since), ("until", until)):
            if value is not None:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError("reading_filter_requires_timezone")
                times[name] = (
                    int(parsed.timestamp())
                    if feed_kind == "topics"
                    else parsed.astimezone(UTC).isoformat(timespec="microseconds")
                )
        if len(times) == 2 and times["since"] > times["until"]:
            raise ValueError("invalid_reading_time_range")
        if importance is not None and importance not in (
            "critical",
            "important",
            "possible",
            "ordinary",
        ):
            raise ValueError("invalid_importance")
        with self._connection() as conn:
            where, params = self._reading_scope_sql(
                conn, conversation_key, allowed_sources, allowed_accounts
            )
            for name, value in times.items():
                where += f" AND o.{sort}" + (">=?" if name == "since" else "<=?")
                params.append(value)
            if kind:
                where += " AND COALESCE(json_extract(o.content_json,'$.reading_bucket'),CASE WHEN o.kind IN ('importance','correction') THEN 'importance' ELSE 'highlight' END)=?"
                params.append(kind)
            if importance:
                where += " AND o.importance=?"
                params.append(importance)
            if unseen:
                where += " AND NOT EXISTS(SELECT 1 FROM message_reading_attention a WHERE a.insight_id=o.insight_id AND a.user_id=? AND (a.viewed_revision>=o.revision OR a.dismissed_revision>=o.revision OR a.snoozed_until>?))"
                params.extend([user_id, self._reading_now()])
            scope = _hash(
                [
                    feed_kind,
                    conversation_key,
                    allowed_sources,
                    allowed_accounts,
                    importance,
                    unseen,
                    user_id,
                    kind,
                    since,
                    until,
                ]
            )
            version = list(
                conn.execute(
                    f"SELECT COUNT(*),COALESCE(SUM(revision),0),COALESCE(MAX(updated_at),'') FROM {table} o WHERE {where}",
                    params,
                ).fetchone()
            )
            last = self._cursor(cursor, scope, version, int if feed_kind == "topics" else str)
            if last:
                where += f" AND (o.{sort}<? OR (o.{sort}=? AND o.{id_column}>?))"
                params.extend([last[0], last[0], last[1]])
            rows = conn.execute(
                f"SELECT o.* FROM {table} o WHERE {where} ORDER BY o.{sort} DESC,o.{id_column} LIMIT ?",
                [*params, size + 1],
            ).fetchall()
            values = [
                self._topic(conn, r) if feed_kind == "topics" else self._insight(conn, r, user_id)
                for r in rows[:size]
            ]
            keys = {r["conversation_key"] for r in rows[:size]}
            return {
                feed_kind: values,
                "next_cursor": self._next_cursor(
                    scope, version, [rows[size - 1][sort], rows[size - 1][id_column]]
                )
                if len(rows) > size
                else None,
                "has_more": len(rows) > size,
                "coverage": [self._reading_coverage(conn, k) for k in sorted(keys)],
                "untrusted_data": True,
            }

    def list_topics(self, **kwargs):
        return self._reading_feed("topics", **kwargs)

    def list_insights(self, **kwargs):
        return self._reading_feed("insights", **kwargs)

    def _get_derived(self, conn, kind, object_id, allowed_sources, allowed_accounts):
        table, column = (
            ("message_reading_topics", "topic_id")
            if kind == "topic"
            else ("message_reading_insights", "insight_id")
        )
        row = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (object_id,)).fetchone()
        if row is None:
            raise PermissionError("message_history_not_allowed")
        if not self._read_scope(conn, row["conversation_key"], allowed_sources, allowed_accounts):
            raise PermissionError("message_history_not_allowed")
        return row

    def get_topic(self, topic_id, *, allowed_sources=None, allowed_accounts=None):
        with self._connection() as conn:
            return self._topic(
                conn, self._get_derived(conn, "topic", topic_id, allowed_sources, allowed_accounts)
            )

    def get_insight(
        self, insight_id, *, allowed_sources=None, allowed_accounts=None, user_id="local"
    ):
        with self._connection() as conn:
            return self._insight(
                conn,
                self._get_derived(conn, "insight", insight_id, allowed_sources, allowed_accounts),
                user_id,
            )

    def derived_sources(
        self, kind, object_id, *, allowed_sources=None, allowed_accounts=None, limit=50, cursor=None
    ):
        if kind not in ("topic", "insight"):
            raise ValueError("invalid_derived_kind")
        size = self._reading_page(limit, 50)
        with self._connection() as conn:
            obj = self._get_derived(conn, kind, object_id, allowed_sources, allowed_accounts)
            scope = _hash([kind, object_id, allowed_sources, allowed_accounts])
            version = obj["revision"]
            last = self._cursor(cursor, scope, version)
            rows = conn.execute(
                "SELECT m.*,MAX(s.revision) source_revision FROM message_history_messages m JOIN message_reading_sources s ON s.message_id=m.internal_message_id WHERE s.object_kind=? AND s.object_id=? AND m.conversation_key=?"
                + (" AND m.seq>?" if last else "")
                + " GROUP BY m.internal_message_id ORDER BY m.seq LIMIT ?",
                [kind, object_id, obj["conversation_key"], *([last[0]] if last else []), size + 1],
            ).fetchall()
            return {
                "sources": [
                    {**self._resolved_message(conn, r), "source_revision": r["source_revision"]}
                    for r in rows[:size]
                ],
                "next_cursor": self._next_cursor(scope, version, [rows[size - 1]["seq"]])
                if len(rows) > size
                else None,
                "has_more": len(rows) > size,
                "coverage": self._reading_coverage(conn, obj["conversation_key"]),
                "untrusted_data": True,
            }

    def topic_sources(self, topic_id, **kwargs):
        return self.derived_sources("topic", topic_id, **kwargs)

    def set_attention(
        self,
        insight_id,
        expected_revision,
        *,
        viewed_revision=None,
        dismissed_revision=None,
        snoozed_until=None,
        user_id="local",
        allowed_sources=None,
        allowed_accounts=None,
    ):
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not user_id
            or len(user_id) > 120
        ):
            raise ValueError("invalid_attention")
        if snoozed_until:
            value = datetime.fromisoformat(snoozed_until)
            if value.tzinfo is None:
                raise ValueError("invalid_snooze_time")
            snoozed_until = value.astimezone(UTC).isoformat(timespec="microseconds")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            insight = self._get_derived(
                conn, "insight", insight_id, allowed_sources, allowed_accounts
            )
            old = self._attention(conn, insight, user_id)
            if old["revision"] != expected_revision:
                raise ReadingRevisionConflict("attention_revision_conflict")
            for value in (viewed_revision, dismissed_revision):
                if value is not None and (
                    type(value) is not int or not 0 <= value <= insight["revision"]
                ):
                    raise ValueError("invalid_attention_content_revision")
            conn.execute(
                "INSERT INTO message_reading_attention VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,insight_id) DO UPDATE SET revision=excluded.revision,viewed_revision=excluded.viewed_revision,dismissed_revision=excluded.dismissed_revision,snoozed_until=excluded.snoozed_until,updated_at=excluded.updated_at",
                (
                    user_id,
                    insight_id,
                    expected_revision + 1,
                    old["viewed_revision"] if viewed_revision is None else viewed_revision,
                    old["dismissed_revision"] if dismissed_revision is None else dismissed_revision,
                    snoozed_until,
                    self._reading_now(),
                ),
            )
            conn.commit()
            return self._attention(conn, insight, user_id)

    def reading_overview(
        self, *, conversation_key=None, allowed_sources=None, allowed_accounts=None
    ):
        topics = self.list_topics(
            conversation_key=conversation_key,
            allowed_sources=allowed_sources,
            allowed_accounts=allowed_accounts,
            limit=10,
        )
        insights = self.list_insights(
            conversation_key=conversation_key,
            allowed_sources=allowed_sources,
            allowed_accounts=allowed_accounts,
            limit=10,
        )
        with self._connection() as conn:
            where, params = self._reading_scope_sql(
                conn, conversation_key, allowed_sources, allowed_accounts, alias="o"
            )
            policies = conn.execute(
                f"SELECT o.conversation_key FROM message_history_policies o WHERE {where} ORDER BY o.conversation_key LIMIT 201",
                params,
            ).fetchall()
            counts = {
                kind: conn.execute(
                    f"SELECT COUNT(*) FROM message_reading_{kind} o WHERE {where}", params
                ).fetchone()[0]
                for kind in ("topics", "insights")
            }
            unseen_count = conn.execute(
                f"SELECT COUNT(*) FROM message_reading_insights o WHERE {where} AND o.importance!='ordinary' "
                "AND COALESCE(json_extract(o.content_json,'$.reading_bucket'),CASE WHEN o.kind IN ('importance','correction') THEN 'importance' ELSE 'highlight' END)='importance' "
                "AND NOT EXISTS(SELECT 1 FROM message_reading_attention a WHERE a.insight_id=o.insight_id AND a.user_id='local' "
                "AND (a.viewed_revision>=o.revision OR a.dismissed_revision>=o.revision OR a.snoozed_until>?))",
                [*params,self._reading_now()],
            ).fetchone()[0]
            return {
                "coverage": [self._reading_coverage(conn, r[0]) for r in policies[:200]],
                "coverage_truncated": len(policies) > 200,
                "topics": topics["topics"],
                "insights": insights["insights"],
                "counts": counts,
                "unseen_count": unseen_count,
                "untrusted_data": True,
            }

    def reading_digest(self, conversation_key, *, allowed_sources=None, allowed_accounts=None):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts):
                raise PermissionError("message_history_not_allowed")
            coverage = self._reading_coverage(conn, conversation_key)
            topics = conn.execute(
                "SELECT topic_id,revision,title,summary FROM message_reading_topics WHERE conversation_key=? ORDER BY last_seen DESC,topic_id LIMIT 10",
                (conversation_key,),
            ).fetchall()
            insights = conn.execute(
                "SELECT insight_id,revision,text,importance FROM message_reading_insights WHERE conversation_key=? ORDER BY CASE importance WHEN 'critical' THEN 0 WHEN 'important' THEN 1 WHEN 'possible' THEN 2 ELSE 3 END,updated_at DESC,insight_id LIMIT 10",
                (conversation_key,),
            ).fetchall()
            content = {"topics": [dict(r) for r in topics], "insights": [dict(r) for r in insights]}
            signature = _hash([content, coverage["analysis_covered_seq"]])
            old = conn.execute(
                "SELECT * FROM message_reading_digests WHERE conversation_key=?",
                (conversation_key,),
            ).fetchone()
            now = self._reading_now()
            if old and (
                old["input_digest"] == signature
                or datetime.fromisoformat(old["generated_at"]) + timedelta(hours=1)
                > datetime.fromisoformat(now)
            ):
                return {
                    "revision": old["revision"],
                    "generated_at": old["generated_at"],
                    "covered_seq": old["covered_seq"],
                    "stale": bool(old["stale"] or old["input_digest"] != signature),
                    **json.loads(old["content_json"]),
                    "coverage": coverage,
                    "untrusted_data": True,
                }
            revision = old["revision"] + 1 if old else 1
            covered = coverage["analysis_covered_seq"] or 0
            conn.execute(
                "INSERT INTO message_reading_digests VALUES(?,?,?,?,?,?,0) ON CONFLICT(conversation_key) DO UPDATE SET revision=excluded.revision,generated_at=excluded.generated_at,covered_seq=excluded.covered_seq,input_digest=excluded.input_digest,content_json=excluded.content_json,stale=0",
                (conversation_key, revision, now, covered, signature, _json(content)),
            )
            conn.execute(
                "UPDATE message_history_conversations SET digest_covered_seq=? WHERE conversation_key=?",
                (covered, conversation_key),
            )
            conn.commit()
            coverage["digest_covered_seq"] = covered
            return {
                "revision": revision,
                "generated_at": now,
                "covered_seq": covered,
                "stale": False,
                **content,
                "coverage": coverage,
                "untrusted_data": True,
            }
