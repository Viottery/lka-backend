"""Durable deterministic scheduling and protected continuation state."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

_RECOVERABLE_READING_ERRORS = frozenset({
    "model_output_invalid", "checkpoint_input_changed", "checkpoint_cursor_invalid",
    "model_recovery_exhausted", "evidence_recovery_exhausted",
})


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MessageReadingRuntimeMixin:
    def configure_reading(self, config: dict, range_planner=None):
        self._reading_config = dict(config)
        self._range_planner = range_planner

    def _reading_now(self):
        from app.domains.message_history import _now
        return _now()

    def _ensure_reading_schema(self, conn):
        conversation_columns = {r["name"] for r in conn.execute("PRAGMA table_info(message_history_conversations)")}
        if "generation_published_seq" not in conversation_columns:
            conn.execute("ALTER TABLE message_history_conversations ADD COLUMN generation_published_seq INTEGER NOT NULL DEFAULT 0")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(message_history_policies)")}
        for name, declaration in {"min_interval_seconds": "INTEGER NOT NULL DEFAULT 300",
                "max_wait_seconds": "INTEGER NOT NULL DEFAULT 900", "max_batch_messages": "INTEGER NOT NULL DEFAULT 200",
                "auto_analyze": "INTEGER NOT NULL DEFAULT 1"}.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE message_history_policies ADD COLUMN {name} {declaration}")
        for sql in (
            "CREATE TABLE IF NOT EXISTS message_reading_control(service TEXT PRIMARY KEY,paused INTEGER NOT NULL,service_epoch INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS message_reading_schedules(conversation_key TEXT PRIMARY KEY,pending_since TEXT,last_dispatch_at TEXT,next_due_at TEXT,active_work_id TEXT,revision INTEGER NOT NULL DEFAULT 1)",
            "CREATE INDEX IF NOT EXISTS idx_message_reading_due ON message_reading_schedules(next_due_at,conversation_key)",
            "CREATE TABLE IF NOT EXISTS message_reading_families(family_id TEXT PRIMARY KEY,conversation_key TEXT NOT NULL,start_seq INTEGER NOT NULL,end_seq INTEGER NOT NULL,max_tokens INTEGER NOT NULL,max_calls INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,UNIQUE(conversation_key,start_seq))",
            "CREATE TABLE IF NOT EXISTS message_reading_checkpoints(family_id TEXT PRIMARY KEY,cursor_json TEXT NOT NULL,checkpoint_json TEXT NOT NULL,input_digest TEXT NOT NULL,semantic_digest TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS message_reading_fragments(family_id TEXT NOT NULL,cursor INTEGER NOT NULL,input_digest TEXT NOT NULL,semantic_digest TEXT NOT NULL,output_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(family_id,cursor,input_digest))",
            "CREATE TABLE IF NOT EXISTS message_reading_control_events(control_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(control_id,revision))",
            "CREATE TABLE IF NOT EXISTS message_reading_checkpoint_archives(family_id TEXT NOT NULL,recovery_count INTEGER NOT NULL,reason TEXT NOT NULL,checkpoint_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(family_id,recovery_count))",
        ):
            conn.execute(sql)
        conn.execute("INSERT OR IGNORE INTO message_reading_control VALUES('message_reading',0,1,1,?)", (self._reading_now(),))
        schedule_columns = {r["name"] for r in conn.execute("PRAGMA table_info(message_reading_schedules)")}
        if "last_scan_at" not in schedule_columns:
            conn.execute("ALTER TABLE message_reading_schedules ADD COLUMN last_scan_at TEXT")
        if "replay_through_seq" not in schedule_columns:
            conn.execute("ALTER TABLE message_reading_schedules ADD COLUMN replay_through_seq INTEGER NOT NULL DEFAULT 0")
        family_columns = {r["name"] for r in conn.execute("PRAGMA table_info(message_reading_families)")}
        if "recovery_count" not in family_columns:
            conn.execute("ALTER TABLE message_reading_families ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
        for row in conn.execute("SELECT conversation_key FROM message_history_policies").fetchall():
            self._refresh_reading_schedule(conn, row[0])

    def reading_status(self):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM message_reading_control WHERE service='message_reading'").fetchone()
            schedules = conn.execute("SELECT * FROM message_reading_schedules ORDER BY conversation_key LIMIT 201").fetchall()
            result = []
            for schedule in schedules[:200]:
                value = dict(schedule)
                state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (schedule["conversation_key"],)).fetchone()
                watermark = self._reading_watermark(state) if state else 0
                value.update(analysis_watermark_seq=watermark,
                    pending_messages=max(0, state["next_seq"] - 1 - watermark) if state else 0,
                    replay_pending=max(0, schedule["replay_through_seq"] - watermark))
                result.append(value)
            return {"paused": bool(row["paused"]), "service_epoch": row["service_epoch"], "revision": row["revision"],
                    "schedules": result, "schedules_truncated": len(schedules) > 200}

    def request_reading_replay(self, conversation_key, expected_revision):
        """Capture an incremental replay target without changing permission/budget."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("invalid_replay_revision")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (conversation_key,)).fetchone()
            if not policy or not policy["record_enabled"] or not policy["analysis_enabled"]:
                raise PermissionError("message_history_not_allowed")
            if policy["revision"] != expected_revision:
                raise ValueError("policy_revision_conflict")
            state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (conversation_key,)).fetchone()
            self._refresh_reading_schedule(conn, conversation_key)
            target = state["next_seq"] - 1
            conn.execute("UPDATE message_reading_schedules SET replay_through_seq=MAX(replay_through_seq,?),revision=revision+1 WHERE conversation_key=?", (target, conversation_key))
            job = self._enqueue_reading(conn, policy, force=True)
            conn.commit()
            return {"through_seq": target, "analysis_watermark_seq": self._reading_watermark(state),
                    "job": job, "status": "paused" if conn.execute("SELECT paused FROM message_reading_control WHERE service='message_reading'").fetchone()[0]
                    else "nothing_pending" if job is None else "blocked" if job["status"] in ("failed", "cancelled") else "queued"}

    def _recover_failed_reading(self, conn, job, policy, *, authorized_restart=False):
        """One bounded rebuild, same family/allowance, before a new lease can run."""
        if (job["status"] != "failed" or job["error_class"] not in _RECOVERABLE_READING_ERRORS
                or self._reading_config.get("reading_algorithm", "legacy") == "legacy"
                or not policy["record_enabled"] or not policy["analysis_enabled"]):
            return None
        control = conn.execute("SELECT paused FROM message_reading_control WHERE service='message_reading'").fetchone()
        if control[0]:
            return None
        payload = json.loads(job["payload_json"])
        state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (policy["conversation_key"],)).fetchone()
        if not state or not self._job_policy_matches(payload, policy) or payload.get("start_seq") != self._reading_watermark(state) + 1:
            return None
        family = conn.execute("SELECT * FROM message_reading_families WHERE conversation_key=? AND start_seq=?", (policy["conversation_key"], payload["start_seq"])).fetchone()
        now = self._reading_now()
        if (not family or family["end_seq"] != payload.get("end_seq")
                or (not authorized_restart and family["recovery_count"] >= self._reading_config.get("max_recovery_restarts", 1))
                or (job["deadline"] is not None and job["deadline"] <= now)
                or (not authorized_restart and job["attempts"] >= job["max_attempts"])):
            return None
        if conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','running','retry_wait')").fetchone()[0] >= getattr(self.jobs, "max_pending_jobs", 1024):
            return None
        checkpoint = conn.execute("SELECT * FROM message_reading_checkpoints WHERE family_id=?", (family["family_id"],)).fetchone()
        recovery_count = family["recovery_count"] + 1
        conn.execute("INSERT INTO message_reading_checkpoint_archives VALUES(?,?,?,?,?)",
            (family["family_id"], recovery_count, job["error_class"], _encode(dict(checkpoint)) if checkpoint else "null", now))
        # The archived checkpoint and fragment rows remain local. Never reuse an
        # accumulator whose input/contract may have changed; never reset usage.
        conn.execute("DELETE FROM message_reading_checkpoints WHERE family_id=?", (family["family_id"],))
        conn.execute("UPDATE message_reading_families SET recovery_count=?,revision=revision+1 WHERE family_id=?", (recovery_count, family["family_id"]))
        conn.execute("INSERT INTO message_reading_control_events VALUES(?,?,?,?,?)",
            (family["family_id"], family["revision"] + 1, "recover_checkpoint", _encode({"error_class": job["error_class"], "recovery_count": recovery_count, "authorized_restart": authorized_restart}), now))
        if authorized_restart and job["attempts"] >= job["max_attempts"]:
            # An explicit controlled retry grants one lease; paid usage and the
            # original attempt ledger stay intact, just like retry_controlled.
            conn.execute("UPDATE background_jobs SET max_attempts=max_attempts+1 WHERE job_id=?", (job["job_id"],))
        conn.execute("UPDATE background_jobs SET status='queued',available_at=?,updated_at=?,finished_at=NULL,started_at=NULL,error_class=NULL,lease_owner=NULL,lease_expires_at=NULL,lease_epoch=lease_epoch+1 WHERE job_id=?", (now, now, job["job_id"]))
        return self.jobs._public(conn.execute("SELECT * FROM background_jobs WHERE job_id=?", (job["job_id"],)).fetchone(), include_payload=True)

    def set_reading_paused(self, paused: bool, expected_revision: int):
        if type(paused) is not bool or type(expected_revision) is not int:
            raise ValueError("invalid_service_control")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM message_reading_control WHERE service='message_reading'").fetchone()
            if row["revision"] != expected_revision:
                raise ValueError("service_revision_conflict")
            now = self._reading_now()
            changed = bool(row["paused"]) != paused
            conn.execute("UPDATE message_reading_control SET paused=?,service_epoch=service_epoch+?,revision=revision+1,updated_at=? WHERE service='message_reading'",
                         (int(paused), int(changed), now))
            conn.execute("INSERT INTO message_reading_control_events VALUES(?,?,?,?,?)",
                         ("message_reading", row["revision"] + 1, "pause" if paused else "resume",
                          _encode({"service_epoch": row["service_epoch"] + int(changed)}), now))
            if changed:
                # Revoke outstanding owners without invalidating semantic checkpoints.
                conn.execute("UPDATE background_jobs SET status='queued',lease_owner=NULL,lease_expires_at=NULL,lease_epoch=lease_epoch+1,attempts=MAX(0,attempts-1),available_at=?,updated_at=? WHERE kind='message_analysis' AND status='running'", (now, now))
                if not paused:
                    conn.execute("UPDATE background_jobs SET status='queued',available_at=?,updated_at=? "
                                 "WHERE kind='message_analysis' AND status='retry_wait' "
                                 "AND error_class='background_budget_deferred'", (now, now))
            conn.commit()
        return self.reading_status()

    def _refresh_reading_schedule(self, conn, key, clear_active=False):
        state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)).fetchone()
        watermark = self._reading_watermark(state) if state else 0
        first = conn.execute("SELECT ingested_at FROM message_history_messages WHERE conversation_key=? AND seq=?", (key, watermark + 1)).fetchone()
        conn.execute("INSERT OR IGNORE INTO message_reading_schedules(conversation_key) VALUES(?)", (key,))
        schedule = conn.execute("SELECT * FROM message_reading_schedules WHERE conversation_key=?", (key,)).fetchone()
        policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (key,)).fetchone()
        pending = first[0] if first else None
        due = None
        if pending and policy:
            due = datetime.fromisoformat(pending) + timedelta(seconds=policy["max_wait_seconds"])
            if state["next_seq"] - watermark - 1 >= policy["batch_size"]:
                due = datetime.fromisoformat(pending)
            if schedule["last_dispatch_at"]:
                due = max(due, datetime.fromisoformat(schedule["last_dispatch_at"]) + timedelta(seconds=policy["min_interval_seconds"]))
        conn.execute("UPDATE message_reading_schedules SET pending_since=?,next_due_at=?,active_work_id=CASE WHEN ? THEN NULL ELSE active_work_id END,revision=revision+1 WHERE conversation_key=?",
                     (pending, due.isoformat(timespec="microseconds") if due else None, int(clear_active), key))

    def _enqueue_reading(self, conn, policy, *, force, ignore_job_id=None):
        from app.domains.message_history import ANALYSIS_JOB_KIND
        control = conn.execute("SELECT paused FROM message_reading_control WHERE service='message_reading'").fetchone()
        if control[0] or not policy["record_enabled"] or not policy["analysis_enabled"]:
            return None
        key = policy["conversation_key"]
        self._refresh_reading_schedule(conn, key)
        conn.execute("UPDATE message_reading_schedules SET last_scan_at=? WHERE conversation_key=?", (self._reading_now(), key))
        schedule = conn.execute("SELECT * FROM message_reading_schedules WHERE conversation_key=?", (key,)).fetchone()
        state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (key,)).fetchone()
        start = self._reading_watermark(state) + 1
        if start >= state["next_seq"]:
            return None
        for row in conn.execute("SELECT * FROM background_jobs WHERE kind=? AND scope_id=? AND status IN ('queued','running','retry_wait','failed','cancelled') ORDER BY created_at DESC", (ANALYSIS_JOB_KIND, key)):
            payload = json.loads(row["payload_json"])
            if row["job_id"] != ignore_job_id and self._job_policy_matches(payload, policy) and payload.get("start_seq") == start:
                recovered = self._recover_failed_reading(conn, row, policy) if policy["auto_analyze"] or force or schedule["replay_through_seq"] >= start else None
                return recovered or self.jobs._public(row, include_payload=True)
        family = conn.execute("SELECT * FROM message_reading_families WHERE conversation_key=? AND start_seq=?", (key, start)).fetchone()
        replay = schedule["replay_through_seq"] >= start
        force = force or replay
        if not force and not family and (not policy["auto_analyze"] or not schedule["next_due_at"] or schedule["next_due_at"] > self._reading_now()):
            return None
        if conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued','running','retry_wait')").fetchone()[0] >= getattr(self.jobs, "max_pending_jobs", 1024):
            return None
        token_limit = family["max_tokens"] if family else self._reading_config.get("max_job_tokens", 32768)
        call_limit = family["max_calls"] if family else self._reading_config.get("max_job_calls", 4)
        end = min(state["next_seq"] - 1, start + policy["max_batch_messages"] - 1)
        if replay:
            end = min(end, schedule["replay_through_seq"])
        if family:
            # A replacement owns the original whole range. Shrinking that range
            # would let its suffix acquire a fresh family and a free allowance.
            end = family["end_seq"]
        messages = [self._resolved_message(conn, r) for r in conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? AND seq BETWEEN ? AND ? ORDER BY seq", (key, start, end))]
        count = len(messages)
        if self._range_planner:
            facts = [{"fact_id": r["fact_id"], "kind": r["kind"], "text": r["text"],
                      "source_message_ids": json.loads(r["source_message_ids_json"]), "certainty": r["certainty"]}
                     for r in conn.execute("SELECT * FROM message_history_facts WHERE conversation_key=? AND NOT EXISTS (SELECT 1 FROM message_history_fact_supersessions s WHERE s.conversation_key=message_history_facts.conversation_key AND s.prior_fact_id=message_history_facts.fact_id) ORDER BY created_at DESC,fact_id LIMIT 100", (key,))]
            count = self._range_planner({"messages": messages, "conversation_key": key,
                "family_id": family["family_id"] if family else "message_work_" + hashlib.sha256(_encode([key, start]).encode()).hexdigest(),
                "policy": self._policy(policy), "previous_summary": state["rolling_summary"],
                "previous_facts": list(reversed(facts)), "work_token_limit": token_limit, "work_call_limit": call_limit,
                **self._reading_input_context(conn, key),
                "intelligence_snapshot": self.snapshot_context(key, start - 1, _conn=conn)})
            if type(count) is not int or not 0 <= count <= len(messages):
                raise ValueError("invalid_range_plan")
        too_large = count == 0 or (family is not None and count != len(messages))
        end = family["end_seq"] if family else start + max(1, count) - 1
        family_id = family["family_id"] if family else "message_work_" + hashlib.sha256(_encode([key, start]).encode()).hexdigest()
        if not family:
            conn.execute("INSERT INTO message_reading_families(family_id,conversation_key,start_seq,end_seq,max_tokens,max_calls) VALUES(?,?,?,?,?,?)", (family_id, key, start, end, token_limit, call_limit))
        payload = {"conversation_id": key, "start_seq": start, "end_seq": end, "revision": policy["revision"], "work_family_id": family_id,
                   **{name: policy[name] for name in ("capture_epoch", "analysis_epoch", "processing_revision")}}
        job = self.jobs.enqueue(ANALYSIS_JOB_KIND, key, f"{policy['revision']}:{start}:{end}", payload, max_attempts=5, conn=conn)
        now = self._reading_now()
        if too_large:
            conn.execute("UPDATE background_jobs SET status='failed',error_class='input_too_large',finished_at=?,updated_at=? WHERE job_id=?", (now, now, job["job_id"]))
            job.update(status="failed", error_class="input_too_large")
        conn.execute("UPDATE message_reading_schedules SET last_dispatch_at=?,active_work_id=?,revision=revision+1 WHERE conversation_key=?",
                     (schedule["last_dispatch_at"] if family else now, family_id, key))
        return {**job, "payload": payload}

    def _progress_context(self, conn, job, service_epoch=None):
        lease = self._lease_values(job)
        if lease is None:
            return None
        _, _, canonical = lease
        payload = canonical["payload"]
        if not self._valid_analysis_payload(payload):
            return None
        control = conn.execute("SELECT * FROM message_reading_control WHERE service='message_reading'").fetchone()
        now = self._reading_now()
        valid = conn.execute("SELECT payload_json FROM background_jobs WHERE job_id=? AND status='running' AND lease_owner=? AND lease_epoch=? AND lease_expires_at>? AND (deadline IS NULL OR deadline>?)", (job["job_id"], job["lease_owner"], job["lease_epoch"], now, now)).fetchone()
        policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (payload["conversation_id"],)).fetchone()
        state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (payload["conversation_id"],)).fetchone()
        if (not valid or json.loads(valid[0]) != payload or control["paused"] or (service_epoch is not None and control["service_epoch"] != service_epoch)
                or not policy or not policy["record_enabled"] or not policy["analysis_enabled"] or not self._job_policy_matches(payload, policy)
                or not state or self._reading_watermark(state) + 1 != payload["start_seq"]):
            return None
        family = conn.execute("SELECT * FROM message_reading_families WHERE conversation_key=? AND start_seq=?", (payload["conversation_id"], payload["start_seq"])).fetchone()
        if not family:
            family_id = "message_work_" + hashlib.sha256(_encode([payload["conversation_id"], payload["start_seq"]]).encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO message_reading_families(family_id,conversation_key,start_seq,end_seq,max_tokens,max_calls) VALUES(?,?,?,?,?,?)", (family_id, payload["conversation_id"], payload["start_seq"], payload["end_seq"], self._reading_config.get("max_job_tokens", 32768), self._reading_config.get("max_job_calls", 4)))
            family = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family_id,)).fetchone()
            conn.execute("UPDATE message_reading_schedules SET active_work_id=? WHERE conversation_key=?",
                         (family_id, payload["conversation_id"]))
        policy_values = dict(policy)
        semantic = hashlib.sha256(_encode({k: payload.get(k, policy_values.get(k)) for k in ("conversation_id", "start_seq", "end_seq", "capture_epoch", "analysis_epoch", "processing_revision")}).encode()).hexdigest()
        checkpoint = conn.execute("SELECT * FROM message_reading_checkpoints WHERE family_id=?", (family["family_id"],)).fetchone()
        if checkpoint and checkpoint["semantic_digest"] != semantic:
            # The row still owns this family's unique key. Treating it as
            # absent would later INSERT over it and bypass controlled archival.
            from app.core.background_jobs import BackgroundJobFailure
            raise BackgroundJobFailure("checkpoint_input_changed")
        return family, checkpoint, semantic, control["service_epoch"]

    def _reading_watermark(self, state):
        """Selected generations schedule independently of true full-text coverage."""
        keys = state.keys()
        baseline = state["analysis_baseline_floor_seq"] if "analysis_baseline_floor_seq" in keys else None
        reading_covered = state["reading_covered_seq"] if "reading_covered_seq" in keys else None
        if self._reading_config.get("reading_algorithm", "legacy") == "selected":
            return max(state["covered_seq"], state["generation_published_seq"],
                       reading_covered or 0, baseline or 0)
        return max(state["covered_seq"], reading_covered or 0, baseline or 0)

    def load_reading_progress(self, job):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = self._progress_context(conn, job)
            if not context:
                return None
            family, checkpoint, semantic, epoch = context
            legacy_task_ids = [row["job_id"] for row in conn.execute(
                "SELECT job_id,payload_json FROM background_jobs WHERE kind='message_analysis' AND scope_id=?",
                (family["conversation_key"],))
                if (candidate := json.loads(row["payload_json"])).get("start_seq") == family["start_seq"]
                and "work_family_id" not in candidate]
            archived = conn.execute("SELECT checkpoint_json FROM message_reading_checkpoint_archives WHERE family_id=? ORDER BY recovery_count DESC LIMIT 1", (family["family_id"],)).fetchone()
            previous = json.loads(archived[0]) if archived else None
            restart_feedback = json.loads(previous["checkpoint_json"]).get("recovery_errors") if previous else None
            conn.commit()
            return {"family_id": family["family_id"], "work_family_id": family["family_id"], "cursor": json.loads(checkpoint["cursor_json"]) if checkpoint else 0,
                    "checkpoint": json.loads(checkpoint["checkpoint_json"]) if checkpoint else None, "service_epoch": epoch,
                    "work_token_limit": family["max_tokens"], "work_call_limit": family["max_calls"], "revision": family["revision"],
                    "last_input_digest": checkpoint["input_digest"] if checkpoint else None, "checkpoint_revision": checkpoint["revision"] if checkpoint else 0,
                    "semantic_digest": semantic, "legacy_task_ids": legacy_task_ids,
                    "restart_feedback": restart_feedback}

    def mark_reading_recovery(self, job, service_epoch):
        """Spend the one recovery dispatch before HTTP; a retry cannot renew it."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = self._progress_context(conn, job, service_epoch)
            if not context or context[1] is None:
                return False
            family, prior, _, _ = context
            value = json.loads(prior["checkpoint_json"])
            if not value.get("recovery_pending") or value.get("recovery_dispatched"):
                return False
            value["recovery_dispatched"] = True
            conn.execute("UPDATE message_reading_checkpoints SET checkpoint_json=?,revision=revision+1,updated_at=? WHERE family_id=?",
                         (_encode(value), self._reading_now(), family["family_id"]))
            conn.commit()
            return True

    def freeze_reading_snapshot(self, job, input_digest, checkpoint, service_epoch):
        """Freeze first-dispatch semantics without releasing the active lease."""
        encoded = _encode(checkpoint)
        if len(encoded.encode()) > 1048576:
            raise ValueError("checkpoint_too_large")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = self._progress_context(conn, job, service_epoch)
            if not context or context[1] is not None:
                return False
            family, _, semantic, _ = context
            conn.execute("INSERT INTO message_reading_checkpoints VALUES(?,?,?,?,?,1,?)",
                         (family["family_id"], "0", encoded, input_digest, semantic, self._reading_now()))
            conn.commit()
            return True

    def mark_reading_evidence(self, job, service_epoch):
        """Persist the single evidence dispatch before provider I/O."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = self._progress_context(conn, job, service_epoch)
            if not context or context[1] is None:
                return False
            family, prior, _, _ = context
            value = json.loads(prior["checkpoint_json"])
            if not value.get("evidence_pending") or value.get("evidence_dispatched"):
                return False
            value["evidence_dispatched"] = True
            conn.execute("UPDATE message_reading_checkpoints SET checkpoint_json=?,revision=revision+1,updated_at=? WHERE family_id=?",
                         (_encode(value), self._reading_now(), family["family_id"]))
            conn.commit()
            return True

    def save_reading_progress(self, job, expected_cursor, input_digest, checkpoint, next_cursor, service_epoch):
        if (type(expected_cursor) is not int or expected_cursor < 0 or type(next_cursor) is not int
                or next_cursor not in (expected_cursor, expected_cursor + 1)):
            raise ValueError("invalid_checkpoint_cursor")
        if not isinstance(input_digest, str) or len(input_digest) != 64 or not isinstance(checkpoint, dict):
            raise ValueError("invalid_checkpoint")
        encoded = _encode(checkpoint)
        if len(encoded.encode()) > 1048576:
            raise ValueError("checkpoint_too_large")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = self._progress_context(conn, job, service_epoch)
            if not context:
                return False
            family, prior, semantic, _ = context
            cursor = json.loads(prior["cursor_json"]) if prior else 0
            if cursor != expected_cursor:
                return False
            if checkpoint.get("cursor", next_cursor) != next_cursor or checkpoint.get("last_input_digest", input_digest) != input_digest:
                return False
            if next_cursor == expected_cursor and not (checkpoint.get("recovery_pending") or checkpoint.get("evidence_pending")):
                return False
            prior_digest = prior["input_digest"] if prior else None
            if "previous_input_digest" in checkpoint and checkpoint["previous_input_digest"] != prior_digest:
                return False
            now = self._reading_now()
            if next_cursor > expected_cursor and "fragment_output" in checkpoint:
                conn.execute("INSERT OR IGNORE INTO message_reading_fragments VALUES(?,?,?,?,?,?)",
                             (family["family_id"], expected_cursor, input_digest, semantic,
                              _encode(checkpoint["fragment_output"]), now))
            conn.execute("INSERT INTO message_reading_checkpoints VALUES(?,?,?,?,?,1,?) ON CONFLICT(family_id) DO UPDATE SET cursor_json=excluded.cursor_json,checkpoint_json=excluded.checkpoint_json,input_digest=excluded.input_digest,semantic_digest=excluded.semantic_digest,revision=message_reading_checkpoints.revision+1,updated_at=excluded.updated_at",
                         (family["family_id"], _encode(next_cursor), encoded, input_digest, semantic, now))
            available = (datetime.fromisoformat(now) + timedelta(seconds=self._reading_config.get("yield_delay_seconds", 1))).isoformat(timespec="microseconds")
            conn.execute("UPDATE background_jobs SET status='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL,attempts=MAX(0,attempts-1),updated_at=? WHERE job_id=?", (available, now, job["job_id"]))
            conn.commit()
            return True

    def raise_work_limits(self, family_id, expected_revision, max_tokens, max_calls):
        if any(type(v) is not int or v <= 0 for v in (max_tokens, max_calls)):
            raise ValueError("invalid_work_limits")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family_id,)).fetchone()
            if not row or row["revision"] != expected_revision:
                raise ValueError("work_revision_conflict")
            allowed = conn.execute("SELECT record_enabled FROM message_history_policies WHERE conversation_key=?",
                                   (row["conversation_key"],)).fetchone()
            if not allowed or not allowed[0]:
                raise PermissionError("message_history_not_allowed")
            if max_tokens < row["max_tokens"] or max_calls < row["max_calls"]:
                raise ValueError("work_limits_must_not_decrease")
            conn.execute("UPDATE message_reading_families SET max_tokens=?,max_calls=?,revision=revision+1 WHERE family_id=?", (max_tokens, max_calls, family_id))
            conn.execute("INSERT INTO message_reading_control_events VALUES(?,?,?,?,?)",
                         (family_id, row["revision"] + 1, "raise_work_limits",
                          _encode({"max_tokens": max_tokens, "max_calls": max_calls}), self._reading_now()))
            if max_tokens > row["max_tokens"] or max_calls > row["max_calls"]:
                policy = conn.execute("SELECT * FROM message_history_policies WHERE conversation_key=?", (row["conversation_key"],)).fetchone()
                state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (row["conversation_key"],)).fetchone()
                if (policy and policy["record_enabled"] and policy["analysis_enabled"] and state
                        and self._reading_watermark(state) + 1 == row["start_seq"]):
                    for job in conn.execute("SELECT * FROM background_jobs WHERE kind='message_analysis' AND scope_id=? AND status='failed' ORDER BY created_at DESC", (row["conversation_key"],)).fetchall():
                        payload = json.loads(job["payload_json"])
                        if (payload.get("start_seq") == row["start_seq"] and self._job_policy_matches(payload, policy)
                                and payload.get("work_family_id", family_id) == family_id):
                            if job["error_class"] in _RECOVERABLE_READING_ERRORS:
                                # An actual human quota increase grants a further
                                # rebuild, not an unlimited automatic restart.
                                self._recover_failed_reading(conn, job, policy, authorized_restart=True)
                                break
                            if job["error_class"] != "work_budget_exhausted":
                                continue
                            now = self._reading_now()
                            conn.execute("UPDATE background_jobs SET status='queued',available_at=?,updated_at=?,finished_at=NULL,error_class=NULL,lease_owner=NULL,lease_expires_at=NULL,lease_epoch=lease_epoch+1 WHERE job_id=?", (now, now, job["job_id"]))
                            break
            conn.commit()
            return dict(conn.execute("SELECT * FROM message_reading_families WHERE family_id=?", (family_id,)).fetchone())
