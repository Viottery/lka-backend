"""Persisted FIFO user turns, independently admitted through the normal root slot."""
from __future__ import annotations

import json
import threading
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from app.core.agent_runs import AgentRunRecord, AgentRunStatus
from app.core.session_admission import TERMINAL_RUN_STATUSES


class SessionInputConflict(ValueError):
    """The queue or an idempotency key cannot accept this request."""


class SessionInputQueue:
    """Policy and attachment validation are supplied by the application boundary.

    SQLite serializes acceptance and binding. A bound input always retains its
    original run; recovery belongs exclusively to the checkpoint-safe dispatcher.
    """

    def __init__(self, conn_factory, *, sessions, run_store, dispatcher,
                 authorize, policy, validate_attachments, capacity=100, poll_seconds=0.25):
        self._conn = conn_factory
        self.sessions, self.run_store, self.dispatcher = sessions, run_store, dispatcher
        self.authorize, self.policy = authorize, policy
        self.validate_attachments = validate_attachments
        self.capacity, self.poll_seconds = capacity, poll_seconds
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_input_queues (
                    session_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                    watched_run_id TEXT, next_sequence INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS session_inputs (
                    input_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    sequence INTEGER NOT NULL, request_json TEXT NOT NULL,
                    run_id TEXT UNIQUE, created_at TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_id,client_message_id), UNIQUE(session_id,sequence)
                );
                CREATE INDEX IF NOT EXISTS session_inputs_pending ON session_inputs(session_id,run_id);
            """)

    @staticmethod
    def _active(conn, sid):
        return conn.execute("SELECT run_id FROM agent_runs WHERE session_id=? AND status NOT IN "
                            "('completed','failed','cancelled','timed_out') "
                            "AND json_extract(record_payload,'$.parent_run_id') IS NULL "
                            "ORDER BY created_at,run_id LIMIT 1", (sid,)).fetchone()

    @staticmethod
    def _failed_watch(conn, state):
        if state is None or not state['watched_run_id']:
            return False
        row = conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (state['watched_run_id'],)).fetchone()
        return row is None or row[0] in {'failed', 'cancelled', 'timed_out'}

    def _snapshot(self, conn, sid):
        state = conn.execute('SELECT * FROM session_input_queues WHERE session_id=?', (sid,)).fetchone()
        # Retain every pending receipt and a bounded tail of completed receipts.
        rows = conn.execute("SELECT i.*,r.status AS run_status FROM session_inputs i "
                            "LEFT JOIN agent_runs r ON r.run_id=i.run_id WHERE i.session_id=? "
                            "AND ((i.cancelled=0 AND (i.run_id IS NULL OR r.status NOT IN ('completed','failed','cancelled','timed_out'))) "
                            "OR i.sequence IN (SELECT sequence FROM session_inputs WHERE session_id=? "
                            "ORDER BY sequence DESC LIMIT 100)) ORDER BY i.sequence", (sid, sid)).fetchall()
        inputs = []
        for row in rows:
            status = row['run_status']
            status = ('cancelled' if row['cancelled'] else 'queued' if row['run_id'] is None else
                      'failed' if status is None or status == 'timed_out' else
                      status if status in TERMINAL_RUN_STATUSES or status == 'queued' else 'running')
            inputs.append({key: row[key] for key in (
                'input_id', 'client_message_id', 'message_id', 'sequence', 'run_id') } | {'status': status})
        active = self._active(conn, sid)
        pending = conn.execute("SELECT count(*) FROM session_inputs i LEFT JOIN agent_runs r ON r.run_id=i.run_id "
                               "WHERE i.session_id=? AND i.cancelled=0 AND (i.run_id IS NULL OR r.status NOT IN "
                               "('completed','failed','cancelled','timed_out'))", (sid,)).fetchone()[0]
        return {'inputs': inputs, 'paused': bool(state and (state['paused'] or self._failed_watch(conn, state))),
                'pending_count': pending, 'active_run_id': active[0] if active else None}

    def snapshot(self, session_id):
        with closing(self._conn()) as conn:
            conn.execute('BEGIN')
            self.authorize(conn, session_id)
            return self._snapshot(conn, session_id)

    def submit(self, session_id, *, client_message_id, user_input, attachment_ids,
               safety_review_mode=None, llm=None):
        if not 1 <= len(client_message_id) <= 128 or not client_message_id.strip():
            raise ValueError('client_message_id must contain 1 to 128 characters')
        if not user_input.strip() and not attachment_ids:
            raise ValueError('Input requires text or attachments')
        request = {'user_input': user_input, 'attachment_ids': list(attachment_ids),
                   'safety_review_mode': safety_review_mode, 'llm': llm or {'response_mode': 'text'}}
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        # Exact retries bypass mutable attachment/model validation.
        with closing(self._conn()) as conn:
            self.authorize(conn, session_id)
            previous = conn.execute('SELECT request_json FROM session_inputs WHERE session_id=? AND client_message_id=?',
                                    (session_id, client_message_id)).fetchone()
        if previous is None:
            self.validate_attachments(session_id=session_id, attachment_ids=list(attachment_ids))
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.authorize(conn, session_id)
            previous = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND client_message_id=?',
                                    (session_id, client_message_id)).fetchone()
            if previous is None:
                self.authorize(conn, session_id, accepting=True)
            if previous is not None:
                if previous['request_json'] != encoded:
                    raise SessionInputConflict('client_message_id_conflict')
                receipt = self._snapshot(conn, session_id)
                # Old exact retries remain available even outside the recent tail.
                status = conn.execute('SELECT status FROM agent_runs WHERE run_id=?', (previous['run_id'],)).fetchone()
                item = {key: previous[key] for key in ('input_id', 'client_message_id', 'message_id', 'sequence', 'run_id')}
                item['status'] = ('cancelled' if previous['cancelled'] else 'queued' if previous['run_id'] is None else 'failed' if status is None or status[0] == 'timed_out'
                                  else status[0] if status[0] in TERMINAL_RUN_STATUSES or status[0] == 'queued' else 'running')
            else:
                receipt = self._snapshot(conn, session_id)
                if receipt['pending_count'] >= self.capacity:
                    raise SessionInputConflict('session_input_queue_full')
                conn.execute('INSERT OR IGNORE INTO session_input_queues(session_id) VALUES(?)', (session_id,))
                state = conn.execute('SELECT * FROM session_input_queues WHERE session_id=?', (session_id,)).fetchone()
                input_id = 'input_' + uuid4().hex
                item = {'input_id': input_id, 'client_message_id': client_message_id,
                        'message_id': 'input_message_' + uuid4().hex, 'sequence': state['next_sequence'],
                        'status': 'queued', 'run_id': None}
                conn.execute('INSERT INTO session_inputs(input_id,session_id,client_message_id,message_id,sequence,request_json,run_id,created_at) VALUES(?,?,?,?,?,?,NULL,?)',
                             (input_id, session_id, client_message_id, item['message_id'], item['sequence'], encoded,
                              datetime.now(UTC).isoformat()))
                conn.execute('UPDATE session_input_queues SET next_sequence=next_sequence+1, '
                             'paused=CASE WHEN ? THEN 1 ELSE paused END, '
                             'watched_run_id=COALESCE(?,watched_run_id) WHERE session_id=?',
                             (receipt['paused'], receipt['active_run_id'], session_id))
                self.sessions.append_message_in_transaction(
                    conn, session_id=session_id, role='user', content=user_input, message_id=item['message_id'],
                    payload={'entrypoint': 'agent.turn', 'input_id': input_id, 'client_message_id': client_message_id,
                             'attachment_ids': list(attachment_ids)},
                )
                receipt = self._snapshot(conn, session_id)
            return {'input': item, 'queue': {key: receipt[key] for key in ('paused', 'pending_count', 'active_run_id')}}

    def resume(self, session_id):
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.authorize(conn, session_id)
            active = self._active(conn, session_id)
            conn.execute('UPDATE session_input_queues SET paused=0,watched_run_id=? WHERE session_id=?',
                         (active[0] if active else None, session_id))
            return self._snapshot(conn, session_id)

    @staticmethod
    def pending_in_transaction(conn, session_id):
        return conn.execute("SELECT 1 FROM session_inputs i LEFT JOIN agent_runs r ON r.run_id=i.run_id "
                            "WHERE i.session_id=? AND i.cancelled=0 AND (i.run_id IS NULL OR r.status NOT IN "
                            "('completed','failed','cancelled','timed_out')) LIMIT 1", (session_id,)).fetchone() is not None

    def cancel_pending_in_transaction(self, conn, session_id):
        """Deletion cancels unstarted work permanently, including after restoration."""
        conn.execute('UPDATE session_inputs SET cancelled=1 WHERE session_id=? AND run_id IS NULL', (session_id,))
        conn.execute('UPDATE session_input_queues SET paused=1 WHERE session_id=?', (session_id,))

    def tick(self):
        with closing(self._conn()) as conn:
            session_ids = [row[0] for row in conn.execute('SELECT session_id FROM session_input_queues')]
        for sid in session_ids:
            try:
                self._bind(sid)
            except (KeyError, SessionInputConflict):
                # Disabled, deleted, or no longer eligible sessions stay inert.
                continue

    def _bind(self, sid):
        policy = self.policy(sid).model_dump(mode='json')
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.authorize(conn, sid)
            state = conn.execute('SELECT * FROM session_input_queues WHERE session_id=?', (sid,)).fetchone()
            if self._failed_watch(conn, state):
                conn.execute('UPDATE session_input_queues SET paused=1,watched_run_id=NULL WHERE session_id=?', (sid,))
                return
            if state['paused']:
                return
            active = self._active(conn, sid)
            if active:
                conn.execute('UPDATE session_input_queues SET watched_run_id=? WHERE session_id=?', (active[0], sid))
                return
            row = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND run_id IS NULL AND cancelled=0 ORDER BY sequence LIMIT 1',
                               (sid,)).fetchone()
            if row is None:
                return
            request = json.loads(row['request_json'])
            now = datetime.now(UTC).isoformat()
            run = AgentRunRecord(run_id='agent_run_' + uuid4().hex, trace_id='agent_turn_' + uuid4().hex,
                                 session_id=sid, user_input=request['user_input'], created_at=now, status=AgentRunStatus.QUEUED,
                                 metadata={'entrypoint': 'agent.turn', 'orchestrator': 'langgraph', 'session_policy': policy,
                                           'session_input_id': row['input_id'], 'session_input_message_id': row['message_id'],
                                           'session_input_options': request})
            self.run_store.save_run_in_transaction(conn, run.model_dump(mode='json'), updated_at=now)
            payload = json.loads(conn.execute('SELECT payload FROM agent_session_messages WHERE message_id=?',
                                              (row['message_id'],)).fetchone()[0])
            payload['trace_id'] = run.trace_id
            conn.execute('UPDATE agent_session_messages SET payload=? WHERE message_id=?',
                         (json.dumps(payload, ensure_ascii=False), row['message_id']))
            conn.execute('UPDATE session_inputs SET run_id=? WHERE input_id=?', (run.run_id, row['input_id']))
            conn.execute('UPDATE session_input_queues SET watched_run_id=? WHERE session_id=?', (run.run_id, sid))
            self.dispatcher.enqueue(run.run_id, conn=conn)
        # The manager lazily loads durable runs. Do not refresh a stale record:
        # the dispatcher may already have claimed it immediately after commit.

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='lka-session-inputs', daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - retain inputs after transient storage errors.
                self.last_error = type(exc).__name__
            self._stop.wait(self.poll_seconds)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
