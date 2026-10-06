"""Bounded local participant/focus projections, fenced by live capture permissions.

Only publication and explicit controls write. Read methods never ingest historical
messages, infer claims, or advance cursors. The per-conversation snapshot is saved
inside the same writer transaction as the authorized generation that produced it.
"""
from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta
from itertools import chain

from app.domains.message_participant_profiles import (
    ConversationFocusTracker,
    ParticipantProfileIndex,
    _Participant,
)
from app.domains.message_reading_results import _hash, _json


class MessageReadingIntelligenceMixin:
    def _ensure_reading_intelligence_schema(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS message_reading_intelligence (conversation_key TEXT PRIMARY KEY,capture_epoch INTEGER NOT NULL,revision INTEGER NOT NULL,state_json TEXT NOT NULL,updated_at TEXT NOT NULL)")

    def _intelligence_now(self):
        return int(datetime.fromisoformat(self._reading_now()).timestamp())

    def _intelligence_load(self, conn, policy):
        config = self._reading_config
        index = ParticipantProfileIndex(policy["conversation_key"], int(config.get("participant_pool_capacity", 30)),
            pinned_capacity=int(config.get("participant_pinned_capacity", 10)),
            cold_days=int(config.get("profile_cold_days", 14)), retention_days=int(config.get("profile_retention_days", 30)))
        focus = ConversationFocusTracker(policy["conversation_key"])
        row = conn.execute("SELECT * FROM message_reading_intelligence WHERE conversation_key=? AND capture_epoch=?",
                           (policy["conversation_key"], policy["capture_epoch"])).fetchone()
        revision = 0
        if row:
            state = json.loads(row["state_json"])
            for sender, value in state["people"].items():
                value["buckets"] = {int(key): timestamp for key, timestamp in value["buckets"].items()}
                index._people[sender] = _Participant(**value)
            index._seen = {key: tuple(value) for key, value in state["seen"].items()}
            index._pool = set(state["pool"])
            index._promotion = state["promotion"]
            index._last_recompute = state["last_recompute"]
            index.observed_seq = state.get("observed_seq", 0)
            index._deferred = state.get("deferred", {})
            for name, value in state["focus"].items():
                setattr(focus, name, value)
            revision = row["revision"]
        else:
            old = conn.execute("SELECT state_json,revision FROM message_reading_intelligence WHERE conversation_key=?", (policy["conversation_key"],)).fetchone()
            if old:
                state = json.loads(old["state_json"])
                for sender, value in state["people"].items():
                    if value["suppressed"] or value["hidden"] or value["pinned"] or value["correction"] is not None:
                        index._people[sender] = _Participant(revision=value["revision"], pinned=value["pinned"],
                            hidden=value["hidden"], suppressed=value["suppressed"], correction=value["correction"])
                if state["focus"]["mode"] == "manual":
                    focus.mode = "manual"
                    focus._labels = state["focus"]["_labels"]
                focus.revision = state["focus"]["revision"]
                revision = old["revision"]
        return index, focus, revision

    def _intelligence_save(self, conn, policy, index, focus, revision):
        timestamps = [min(chain(person.buckets.values(), person.topics.values(),
                               (claim["created_at"] for claim in person.claims)), default=None)
                      for person in index._people.values()]
        timestamps = [value for value in timestamps if value is not None]
        if index._seen:
            timestamps.append(min(value[1] for value in index._seen.values()))
        deadlines = [min(timestamps) + index.retention_days * 86400 + 1] if timestamps else []
        focus_times = [message["sent_at"] if message["sent_at"] is not None else message["received_at"]
                       for evidence in focus._evidence.values() for message in evidence.values()]
        if focus_times:
            deadlines.append(min(focus_times) + 30 * 86400 + 1)
        state = {"people": {key: asdict(value) for key, value in index._people.items()},
                 "seen": index._seen, "pool": sorted(index._pool), "promotion": index._promotion,
                 "last_recompute": index._last_recompute, "observed_seq": index.observed_seq, "deferred": index._deferred,
                 "cleanup_after": min(deadlines) if deadlines else None,
                 "focus": {name: getattr(focus, name) for name in ("revision", "mode", "_evidence", "_labels", "_last_merge")}}
        conn.execute("INSERT INTO message_reading_intelligence VALUES(?,?,?,?,?) ON CONFLICT(conversation_key) DO UPDATE SET capture_epoch=excluded.capture_epoch,revision=excluded.revision,state_json=excluded.state_json,updated_at=excluded.updated_at",
                     (policy["conversation_key"], policy["capture_epoch"], revision + 1, _json(state), self._reading_now()))

    def _intelligence_policy(self, conn, conversation_key, source_ids=None, account_ids=None,
                             allowed_sources=None, allowed_accounts=None):
        where = "conversation_key=? AND record_enabled=1"
        params = [conversation_key]
        for column, groups in (("source_id", (source_ids, allowed_sources)),
                               ("account_scope_id", (account_ids, allowed_accounts))):
            for values in groups:
                if values is not None:
                    if len(values) > 200:
                        raise ValueError("invalid_intelligence_scope")
                    where += f" AND {column} IN (" + (",".join("?" for _ in values) or "NULL") + ")"
                    params.extend(values)
        row = conn.execute("SELECT * FROM message_history_policies WHERE " + where, params).fetchone()
        if row is None:
            raise PermissionError("message_history_not_allowed")
        return row

    def _publish_reading_intelligence(self, conn, job, messages, result):
        payload = job["payload"]
        policy = self._intelligence_policy(conn, payload["conversation_id"])
        index, focus, revision = self._intelligence_load(conn, policy)
        now = self._intelligence_now()
        manifest = result.reading_manifest or {}
        selected_ids = None
        if manifest.get("coverage_mode") == "selected_text":
            selected_ids = {value["message_id"] for value in manifest.get("model_seen_spans", [])}
        canonical = []
        for raw in messages:
            row = dict(raw)
            if row.get("capture_epoch") != policy["capture_epoch"]:
                continue
            metadata = json.loads(row.get("metadata_json", "{}"))
            canonical.append({"id": row["internal_message_id"], "sender": row["sender_id"],
                "seq": row["seq"], "sent_at": row["sent_at"], "received_at": row["received_at"],
                "text": row["text"], "kind": row["content_kind"], "reply": metadata.get("reply_to_message_id"),
                "parts": metadata.get("content_parts", [])})
        index.observe([{key: value for key, value in row.items() if key not in ("reply", "parts")} for row in canonical], now)
        topics = []
        for value in result.topic_updates:
            topic = value.model_dump()
            if not topic.get("existing_topic_id"):
                topic["existing_topic_id"] = "message_topic_" + _hash([job.get("job_id"), topic.get("batch_local_key")])
            topics.append(topic)
        index.observe_topics(topics, canonical, now)
        evidence = canonical if selected_ids is None else [m for m in canonical if m["id"] in selected_ids]
        intelligence = result.reading_intelligence or {}
        claims = intelligence.get("participant_claim_candidates", [])
        candidates = intelligence.get("focus_candidates", [])
        if not isinstance(claims, list) or len(claims) > 100 or not isinstance(candidates, list) or len(candidates) > 40:
            raise ValueError("invalid_reading_intelligence")
        for claim in claims:
            index.add_claim(claim, evidence, now)
        for candidate in candidates:
            try:
                focus.observe([candidate], evidence, now)
            except (ValueError, TypeError):
                continue
        index.hot_profiles(now)
        focus.merge(now)
        self._intelligence_save(conn, policy, index, focus, revision)

    def _observe_reading_activity(self, conn, conversation_key, limit=1000):
        """Bounded cursor catch-up; called in the successful import transaction."""
        policy = self._intelligence_policy(conn, conversation_key)
        index, focus, revision = self._intelligence_load(conn, policy)
        rows = conn.execute("SELECT internal_message_id,sender_id,seq,sent_at,received_at,text,content_kind,capture_epoch FROM message_history_messages WHERE conversation_key=? AND seq>? ORDER BY seq LIMIT ?",
                            (conversation_key, index.observed_seq, min(limit, 1000))).fetchall()
        now = self._intelligence_now()
        due = sorted(key for key, timestamp in index._deferred.items() if timestamp <= now)[:200]
        replay = conn.execute("SELECT internal_message_id,sender_id,seq,sent_at,received_at,text,content_kind,capture_epoch FROM message_history_messages WHERE conversation_key=? AND capture_epoch=? AND internal_message_id IN (" + (",".join("?" for _ in due) or "NULL") + ")", [conversation_key, policy["capture_epoch"], *due]).fetchall()
        for key in due:
            index._deferred.pop(key, None)
        for row in rows:
            timestamp = max(row["received_at"], row["sent_at"] or 0)
            if timestamp > now and row["capture_epoch"] == policy["capture_epoch"]:
                index._deferred[row["internal_message_id"]] = timestamp
        index._deferred = dict(sorted(index._deferred.items(), key=lambda item: (item[1], item[0]))[:1000])
        canonical = [{"id": row["internal_message_id"], "sender": row["sender_id"], "seq": row["seq"],
            "sent_at": row["sent_at"], "received_at": row["received_at"], "text": row["text"], "kind": row["content_kind"]}
            for row in [*rows, *replay] if row["capture_epoch"] == policy["capture_epoch"]]
        # Activity dedup deliberately uses only immutable identity/time/body digest.
        # Publication evidence retains richer attribution; it does not observe again.
        index.observe(canonical, self._intelligence_now())
        if rows:
            index.observed_seq = max(index.observed_seq, rows[-1]["seq"])
        index.hot_profiles(self._intelligence_now())
        focus.cleanup(self._intelligence_now())
        self._intelligence_save(conn, policy, index, focus, revision)

    def refresh_participant_activity(self, conversation_key, *, limit=1000, **scope):
        """Explicit bounded local catch-up for a scheduler or migration worker."""
        self._reading_page(limit, 1000)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            self._observe_reading_activity(conn, conversation_key, limit)
            index, _, revision = self._intelligence_load(conn, policy)
            return {"activity_observed_seq": index.observed_seq, "revision": revision,
                    "deferred_messages": len(index._deferred), "untrusted_data": True}

    def _catch_up_participant_activity(self, conn, conversation_key=None):
        now = self._intelligence_now()
        cutoff = (datetime.fromisoformat(self._reading_now()) - timedelta(days=int(self._reading_config.get("profile_retention_days", 30)))).isoformat()
        cursor_sql = "CASE WHEN i.capture_epoch=p.capture_epoch THEN COALESCE(json_extract(i.state_json,'$.observed_seq'),0) ELSE 0 END"
        where = "p.record_enabled=1 AND (c.next_seq-1>" + cursor_sql + " OR (i.capture_epoch=p.capture_epoch AND (EXISTS(SELECT 1 FROM json_each(i.state_json,'$.deferred') d WHERE d.value<=?) OR json_extract(i.state_json,'$.cleanup_after')<=? OR (json_type(i.state_json,'$.cleanup_after') IS NULL AND i.updated_at<? AND EXISTS(SELECT 1 FROM json_each(i.state_json,'$.people'))))))"
        params = [now, now, cutoff]
        if conversation_key is not None:
            where += " AND p.conversation_key=?"
            params.append(conversation_key)
        rows = conn.execute("SELECT p.conversation_key FROM message_history_policies p JOIN message_history_conversations c USING(conversation_key) LEFT JOIN message_reading_intelligence i USING(conversation_key) WHERE " + where + " ORDER BY (c.next_seq-1>" + cursor_sql + ") DESC,COALESCE(i.updated_at,''),p.conversation_key LIMIT ?",
                            [*params, min(int(self._reading_config.get("due_scan_limit", 32)), 200)]).fetchall()
        for row in rows:
            self._observe_reading_activity(conn, row["conversation_key"])

    def _reading_intelligence_context(self, conn, conversation_key):
        policy = self._intelligence_policy(conn, conversation_key)
        index, focus, _ = self._intelligence_load(conn, policy)
        now = self._intelligence_now()
        index.cleanup(now)
        profiles = index.hot_profiles(now)
        return {"participants": [{**value, "claims": value["claims"][-3:]} for value in profiles[:8]], "focus": focus.snapshot(now),
                "version": "participant-focus-v3", "untrusted_data": True}

    def snapshot_context(self, conversation_key, upto_seq, *, _conn=None):
        if type(upto_seq) is not int or upto_seq < 0:
            raise ValueError("invalid_intelligence_snapshot_sequence")
        with (nullcontext(_conn) if _conn is not None else self._connection()) as conn:
            policy = self._intelligence_policy(conn, conversation_key)
            index, focus, _ = self._intelligence_load(conn, policy)
            index.cleanup(self._intelligence_now())
            protected_senders = sorted(sender for sender, person in index._people.items()
                                       if person.pinned and not person.hidden and not person.suppressed)
            selected = {sender: person for sender, person in sorted(index._people.items(), key=lambda item: (not item[1].pinned, item[0]))
                        if not person.hidden and not person.suppressed and (person.pinned or person.claims or person.correction is not None)}
            index._people = dict(list(selected.items())[:8])
            # Suppress any derived fact whose evidence includes a future sequence.
            ids = {source for person in index._people.values() for claim in person.claims for source in claim["source_ids"]}
            allowed = {row[0] for row in conn.execute("SELECT internal_message_id FROM message_history_messages WHERE conversation_key=? AND capture_epoch=? AND seq<=? AND internal_message_id IN (" + (",".join("?" for _ in ids) or "NULL") + ")", [conversation_key, policy["capture_epoch"], upto_seq, *ids])}
            for person in index._people.values():
                person.claims = [claim for claim in person.claims if set(claim["source_ids"]).issubset(allowed)]
            index._people = {sender: person for sender, person in index._people.items()
                             if person.pinned or person.correction is not None or person.claims}
            now = self._intelligence_now()
            profiles = [index.profile(sender, now) for sender in sorted(index._people)]
            for evidence in focus._evidence.values():
                for key, message in list(evidence.items()):
                    if message["seq"] > upto_seq:
                        del evidence[key]
            if focus.mode == "auto":
                focus._labels = [label for label in focus._labels if focus._support(label, now)["score"] >= 1.5]
            return {"participants": [{"sender": value["sender"], "revision": value["revision"],
                    "claims": value["claims"][-3:], "summary": value["summary"],
                    "pinned": index._people[value["sender"]].pinned,
                    "status": value["status"] if index.observed_seq <= upto_seq or index._people[value["sender"]].pinned else "activity_only"} for value in profiles],
                    "focus": focus.snapshot(now), "protected_senders": protected_senders,
                    "upto_seq": upto_seq,
                    "version": "participant-focus-v3", "untrusted_data": True}

    def _participant_value(self, index, sender, now):
        value = index.profile(sender, now)
        value.update(conversation_key=index.conversation_id, sender_id=sender,
                     sources_paginated=True, untrusted_data=True)
        if value["status"] in ("hidden", "suppressed"):
            value["claims"] = []
        return value

    def _intelligence_cursor(self, cursor, scope, version):
        if not cursor:
            return None
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor))
            if set(value) != {"scope", "version", "last"} or value["scope"] != scope or value["version"] != version:
                raise ValueError()
            last = value["last"]
            if not isinstance(last, list) or len(last) != 2 or any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in last):
                raise ValueError()
            return last
        except Exception as exc:
            raise ValueError("reading_cursor_invalid_or_stale") from exc

    def get_participant(self, conversation_key, sender_id, **scope):
        with self._connection() as conn:
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            index, _, _ = self._intelligence_load(conn, policy)
            now = self._intelligence_now()
            index.cleanup(now)
            if sender_id not in index._people:
                raise PermissionError("message_history_not_allowed")
            return self._participant_value(index, sender_id, now)

    def list_participants(self, conversation_key=None, *, limit=50, cursor=None, source_ids=None,
                          account_ids=None, allowed_sources=None, allowed_accounts=None):
        size = self._reading_page(limit)
        scope = _hash([conversation_key, source_ids, account_ids, allowed_sources, allowed_accounts])
        with self._connection() as conn:
            if conversation_key is not None:
                self._intelligence_policy(conn, conversation_key, source_ids, account_ids, allowed_sources, allowed_accounts)
            where = "p.record_enabled=1 AND p.capture_epoch=i.capture_epoch"
            params = []
            if conversation_key:
                where += " AND p.conversation_key=?"
                params.append(conversation_key)
            for column, groups in (("source_id", (source_ids, allowed_sources)), ("account_scope_id", (account_ids, allowed_accounts))):
                for values in groups:
                    if values is not None:
                        if len(values) > 200:
                            raise ValueError("invalid_intelligence_scope")
                        where += f" AND p.{column} IN (" + (",".join("?" for _ in values) or "NULL") + ")"
                        params.extend(values)
            version = list(conn.execute("SELECT COUNT(*),COALESCE(SUM(i.revision),0),COALESCE(SUM(p.capture_epoch),0) FROM message_reading_intelligence i JOIN message_history_policies p USING(conversation_key) WHERE " + where, params).fetchone())
            last = self._intelligence_cursor(cursor, scope, version)
            now = self._intelligence_now()
            hot_where = " AND (COALESCE(json_extract(people.value,'$.pinned'),0)=1 OR (EXISTS(SELECT 1 FROM json_each(i.state_json,'$.pool') pool WHERE pool.value=people.key) AND EXISTS(SELECT 1 FROM json_each(people.value,'$.buckets') bucket WHERE bucket.value>?)))"
            # JSON extraction keeps scope filtering and pagination in SQLite before aggregation.
            after = " AND (p.conversation_key>? OR (p.conversation_key=? AND people.key>?))" if last else ""
            rows = conn.execute("SELECT p.*,people.key sender FROM message_reading_intelligence i JOIN message_history_policies p USING(conversation_key),json_each(i.state_json,'$.people') people WHERE " + where + " AND COALESCE(json_extract(people.value,'$.hidden'),0)=0 AND COALESCE(json_extract(people.value,'$.suppressed'),0)=0" + hot_where + after + " ORDER BY p.conversation_key,people.key LIMIT ?",
                [*params, now - int(self._reading_config.get("profile_cold_days", 14)) * 86400,
                 *([last[0], last[0], last[1]] if last else []), size + 1]).fetchall()
            loaded = {}
            values = []
            for row in rows[:size]:
                if row["conversation_key"] not in loaded:
                    loaded[row["conversation_key"]] = self._intelligence_load(conn, row)[0]
                index = loaded[row["conversation_key"]]
                index.cleanup(now)
                if row["sender"] not in index._people:
                    continue
                values.append(self._participant_value(index, row["sender"], now))
            return {"participants": values, "has_more": len(rows) > size,
                "next_cursor": self._next_cursor(scope, version, [rows[size - 1]["conversation_key"], rows[size - 1]["sender"]]) if len(rows) > size else None,
                "untrusted_data": True}

    def update_participant(self, conversation_key, sender_id, expected_revision, action, summary=None, **scope):
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(sender_id, str) or not sender_id or len(sender_id) > 512:
            raise ValueError("invalid_participant_control")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            index, focus, revision = self._intelligence_load(conn, policy)
            if sender_id not in index._people:
                raise PermissionError("message_history_not_allowed")
            if action in ("pin", "unpin"):
                index.pin(sender_id, action == "pin", expected_revision=expected_revision)
            elif action in ("hide", "unhide"):
                index.hide(sender_id, action == "hide", expected_revision=expected_revision)
            elif action == "delete":
                index.delete(sender_id, expected_revision=expected_revision)
            elif action == "correct":
                if index._people[sender_id].suppressed:
                    raise ValueError("profile_suppressed")
                index.correct(sender_id, summary, expected_revision=expected_revision)
            else:
                raise ValueError("invalid_participant_action")
            self._intelligence_save(conn, policy, index, focus, revision)
            return self._participant_value(index, sender_id, self._intelligence_now())

    def participant_sources(self, conversation_key, sender_id, *, limit=50, cursor=None, **scope):
        size = self._reading_page(limit, 50)
        with self._connection() as conn:
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            index, _, revision = self._intelligence_load(conn, policy)
            person = index._people.get(sender_id)
            if person is None or person.hidden or person.suppressed:
                raise PermissionError("message_history_not_allowed")
            index.cleanup(self._intelligence_now())
            ids = sorted({source for claim in person.claims for source in claim["source_ids"]})
            signature = _hash([conversation_key, sender_id, scope])
            last = self._cursor(cursor, signature, revision)
            rows = conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? AND capture_epoch=? AND sender_id=? AND internal_message_id IN (" + (",".join("?" for _ in ids) or "NULL") + ")" + (" AND seq>?" if last else "") + " ORDER BY seq LIMIT ?",
                [conversation_key, policy["capture_epoch"], sender_id, *ids, *([last[0]] if last else []), size + 1]).fetchall()
            return {"sources": [self._resolved_message(conn, row) for row in rows[:size]],
                "has_more": len(rows) > size,
                "next_cursor": self._next_cursor(signature, revision, [rows[size - 1]["seq"]]) if len(rows) > size else None,
                "untrusted_data": True}

    def get_conversation_focus(self, conversation_key, **scope):
        with self._connection() as conn:
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            _, focus, _ = self._intelligence_load(conn, policy)
            now = self._intelligence_now()
            focus.cleanup(now)
            revision = focus.revision
            focus.merge(now)  # Pure projection; never writes during a read.
            focus.revision = revision
            return {**focus.snapshot(now), "conversation_key": conversation_key, "untrusted_data": True}

    def set_conversation_focus(self, conversation_key, expected_revision, mode, labels=None, **scope):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_focus_revision")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = self._intelligence_policy(conn, conversation_key, **scope)
            index, focus, revision = self._intelligence_load(conn, policy)
            if mode == "manual":
                focus.set_manual(labels or [], expected_revision=expected_revision)
            elif mode == "auto":
                focus.resume_auto(expected_revision=expected_revision)
            else:
                raise ValueError("invalid_focus_mode")
            self._intelligence_save(conn, policy, index, focus, revision)
            return {**focus.snapshot(self._intelligence_now()), "conversation_key": conversation_key, "untrusted_data": True}
