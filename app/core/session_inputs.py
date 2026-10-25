"""Persisted FIFO user turns, independently admitted through the normal root slot."""
from __future__ import annotations

import hashlib
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
                 authorize, policy, validate_attachments, replace=None, batch_attachments_fit=None,
                 validate_answer_selection=None,
                 capacity=100, poll_seconds=0.25):
        self._conn = conn_factory
        self.sessions, self.run_store, self.dispatcher = sessions, run_store, dispatcher
        self.authorize, self.policy = authorize, policy
        self.validate_attachments = validate_attachments
        self.replace = replace
        self.batch_attachments_fit = batch_attachments_fit
        self.validate_answer_selection = validate_answer_selection
        self.capacity, self.poll_seconds = capacity, poll_seconds
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""CREATE TABLE IF NOT EXISTS session_input_queues (
                    session_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                    watched_run_id TEXT, next_sequence INTEGER NOT NULL DEFAULT 1
                )""")
            def create_inputs(table):
                conn.execute(f"""CREATE TABLE {table} (
                    input_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    sequence INTEGER NOT NULL, request_json TEXT NOT NULL,
                    run_id TEXT, created_at TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_id,client_message_id), UNIQUE(session_id,sequence)
                )""")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_inputs'").fetchone() is None:
                create_inputs('session_inputs')
            else:
                unique_run_indexes = set()
                for index in conn.execute("PRAGMA index_list('session_inputs')").fetchall():
                    columns = [row['name'] for row in conn.execute('SELECT name FROM pragma_index_info(?)', (index['name'],))]
                    if index['unique'] and columns == ['run_id']:
                        unique_run_indexes.add(index['name'])
                if unique_run_indexes:
                    # Inline UNIQUE cannot be dropped. Keep all receipts and every
                    # other index/constraint while replacing only that cardinality.
                    indexes = [row['sql'] for row in conn.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='session_inputs' AND sql IS NOT NULL"
                    ) if row['name'] not in unique_run_indexes]
                    create_inputs('session_inputs_batch_migration')
                    conn.execute('INSERT INTO session_inputs_batch_migration SELECT * FROM session_inputs')
                    conn.execute('DROP TABLE session_inputs')
                    conn.execute('ALTER TABLE session_inputs_batch_migration RENAME TO session_inputs')
                    for statement in indexes:
                        conn.execute(statement)
            conn.execute('CREATE INDEX IF NOT EXISTS session_inputs_pending ON session_inputs(session_id,run_id)')

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
        row = conn.execute("SELECT status,record_payload FROM agent_runs WHERE run_id=?", (state['watched_run_id'],)).fetchone()
        return row is None or (SessionInputQueue._is_user_run(row) and row['status'] in {'failed', 'cancelled', 'timed_out'})

    @staticmethod
    def _is_user_run(row):
        metadata = json.loads(row['record_payload']).get('metadata', {})
        return bool(metadata.get('session_input_id')) or (metadata.get('input_envelope') or {}).get('origin', 'user') == 'user'

    @classmethod
    def _watched_user_run(cls, conn, active):
        if active is None:
            return None
        row = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?', (active[0],)).fetchone()
        return active[0] if row is not None and cls._is_user_run(row) else None

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
               safety_review_mode=None, llm=None, replace_run_id=None):
        if not 1 <= len(client_message_id) <= 128 or not client_message_id.strip():
            raise ValueError('client_message_id must contain 1 to 128 characters')
        if not user_input.strip() and not attachment_ids:
            raise ValueError('Input requires text or attachments')
        if len(user_input) > 100000:
            raise ValueError('Input text exceeds 100000 characters')
        if replace_run_id is not None and (not 1 <= len(replace_run_id) <= 128 or not replace_run_id.strip()):
            raise ValueError('replace_run_id must contain 1 to 128 characters')
        llm_options = dict(llm or {'response_mode': 'text'})
        for name in ('reasoning_level', 'answer_client_name', 'answer_model'):
            if llm_options.get(name) is None:
                llm_options.pop(name, None)
        request = {'user_input': user_input, 'attachment_ids': list(attachment_ids),
                   'safety_review_mode': safety_review_mode, 'llm': llm_options}
        # Keep ordinary request fingerprints compatible with already accepted inputs.
        if replace_run_id is not None:
            request['replace_run_id'] = replace_run_id
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        # Exact retries bypass mutable attachment/model validation.
        with closing(self._conn()) as conn:
            self.authorize(conn, session_id)
            previous = conn.execute('SELECT request_json FROM session_inputs WHERE session_id=? AND client_message_id=?',
                                    (session_id, client_message_id)).fetchone()
        if previous is None:
            self.validate_attachments(session_id=session_id, attachment_ids=list(attachment_ids))
            if replace_run_id is not None:
                return self._submit_replacement(session_id, client_message_id, request, encoded)
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.authorize(conn, session_id)
            previous = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND client_message_id=?',
                                    (session_id, client_message_id)).fetchone()
            if previous is None:
                self.authorize(conn, session_id, accepting=True)
                # The write reservation also serializes this read-only policy
                # snapshot with controller preference updates.
                accepted_policy = self.policy(session_id)
                answer_selection = {
                    'llm_answer_client_name': accepted_policy.llm_answer_client_name,
                    'llm_answer_model': accepted_policy.llm_answer_model,
                }
                if llm_options.get('answer_client_name') is not None or llm_options.get('answer_model') is not None:
                    answer_selection = {'llm_answer_client_name': llm_options.get('answer_client_name'),
                                        'llm_answer_model': llm_options.get('answer_model')}
            if previous is not None:
                if previous['request_json'] != encoded:
                    raise SessionInputConflict('client_message_id_conflict')
                return self._receipt(conn, session_id, previous)
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
                             (receipt['paused'], self._watched_user_run(conn, (receipt['active_run_id'],))
                              if receipt['active_run_id'] else None, session_id))
                self.sessions.append_message_in_transaction(
                    conn, session_id=session_id, role='user', content=user_input, message_id=item['message_id'],
                    payload={'entrypoint': 'agent.turn', 'input_id': input_id, 'client_message_id': client_message_id,
                             'attachment_ids': list(attachment_ids), 'answer_model_selection': answer_selection},
                )
                receipt = self._snapshot(conn, session_id)
            return {'input': item, 'queue': {key: receipt[key] for key in ('paused', 'pending_count', 'active_run_id')}}

    def _receipt(self, conn, session_id, row):
        receipt = self._snapshot(conn, session_id)
        # Old exact retries remain available even outside the recent tail.
        status = conn.execute('SELECT status FROM agent_runs WHERE run_id=?', (row['run_id'],)).fetchone()
        item = {key: row[key] for key in ('input_id', 'client_message_id', 'message_id', 'sequence', 'run_id')}
        item['status'] = ('cancelled' if row['cancelled'] else 'queued' if row['run_id'] is None else
                          'failed' if status is None or status[0] == 'timed_out' else
                          status[0] if status[0] in TERMINAL_RUN_STATUSES or status[0] == 'queued' else 'running')
        return {'input': item, 'queue': {key: receipt[key] for key in ('paused', 'pending_count', 'active_run_id')}}

    def _submit_replacement(self, session_id, client_message_id, request, encoded):
        """Use the existing replacement gate and its transaction, never a second root admission."""
        run_id = request['replace_run_id']
        with closing(self._conn()) as conn:
            self.authorize(conn, session_id, accepting=True)
            source = conn.execute('SELECT session_id,record_payload FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
            if (source is None or source['session_id'] != session_id
                    or json.loads(source['record_payload']).get('parent_run_id') is not None):
                raise SessionInputConflict('replacement_requires_same_session_root')
        if self.replace is None:
            raise SessionInputConflict('session_input_replacement_unavailable')
        input_id, message_id = 'input_' + uuid4().hex, 'input_message_' + uuid4().hex

        def created(conn, replacement):
            self.authorize(conn, session_id, accepting=True)
            if conn.execute('SELECT 1 FROM session_inputs WHERE session_id=? AND client_message_id=?',
                            (session_id, client_message_id)).fetchone():
                raise SessionInputConflict('client_message_id_conflict')
            # The superseded root is already cancelled in this transaction.
            if self._snapshot(conn, session_id)['pending_count'] >= self.capacity:
                raise SessionInputConflict('session_input_queue_full')
            conn.execute('INSERT OR IGNORE INTO session_input_queues(session_id) VALUES(?)', (session_id,))
            sequence = conn.execute('SELECT next_sequence FROM session_input_queues WHERE session_id=?',
                                    (session_id,)).fetchone()[0]
            conn.execute('INSERT INTO session_inputs(input_id,session_id,client_message_id,message_id,sequence,request_json,run_id,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (input_id, session_id, client_message_id, message_id, sequence, encoded,
                          replacement.run_id, datetime.now(UTC).isoformat()))
            self.sessions.append_message_in_transaction(
                conn, session_id=session_id, role='user', content=request['user_input'], message_id=message_id,
                payload={'entrypoint': 'agent.turn', 'input_id': input_id, 'client_message_id': client_message_id,
                         'attachment_ids': request['attachment_ids'], 'trace_id': replacement.trace_id,
                         'answer_model_selection': {name: replacement.metadata.get('session_policy', {}).get(name)
                                                    for name in ('llm_answer_client_name', 'llm_answer_model')}},
            )
            # Explicit replacement clears a failure pause and watches the new run;
            # later FIFO inputs retain their identities and place after this turn.
            conn.execute('UPDATE session_input_queues SET next_sequence=next_sequence+1,paused=0,watched_run_id=? '
                         'WHERE session_id=?', (replacement.run_id, session_id))
            self.dispatcher.enqueue(replacement.run_id, conn=conn)

        command_id = 'session_input_' + hashlib.sha256(
            json.dumps([session_id, client_message_id]).encode()).hexdigest()
        try:
            self.replace(run_id, user_input=request['user_input'], command_id=command_id,
                         request_fingerprint=hashlib.sha256(encoded.encode()).hexdigest(),
                         metadata_updates={'session_input_id': input_id, 'session_input_message_id': message_id,
                                           'session_input_options': request}, on_created=created)
        except (ValueError, KeyError) as exc:
            raise SessionInputConflict(str(exc)) from exc
        with closing(self._conn()) as conn:
            conn.execute('BEGIN')
            self.authorize(conn, session_id)
            row = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND client_message_id=?',
                               (session_id, client_message_id)).fetchone()
            if row is None or row['request_json'] != encoded:
                raise SessionInputConflict('replacement_receipt_unavailable')
            return self._receipt(conn, session_id, row)

    def resume(self, session_id, *, input_id=None, answer_client_name=None, answer_model=None):
        if input_id is None and (answer_client_name is not None or answer_model is not None):
            raise ValueError('Answer model recovery requires input_id')
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.authorize(conn, session_id)
            if input_id is not None:
                item = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND input_id=? '
                                    'AND run_id IS NULL AND cancelled=0', (session_id, input_id)).fetchone()
                if item is None:
                    raise SessionInputConflict('session_input_not_pending')
                request = json.loads(item['request_json'])
                if self.validate_answer_selection is not None:
                    self.validate_answer_selection(
                        session_id=session_id, attachment_ids=request['attachment_ids'],
                        llm_answer_client_name=answer_client_name, llm_answer_model=answer_model,
                    )
                message = conn.execute('SELECT payload FROM agent_session_messages WHERE session_id=? AND message_id=?',
                                       (session_id, item['message_id'])).fetchone()
                if message is None:
                    raise SessionInputConflict('session_input_history_unavailable')
                payload = json.loads(message[0])
                selection = {'llm_answer_client_name': answer_client_name, 'llm_answer_model': answer_model}
                if payload.get('answer_model_selection') != selection:
                    payload.setdefault('original_answer_model_selection', payload.get('answer_model_selection'))
                    payload['answer_model_selection'] = selection
                    payload['answer_model_recovery'] = {'source': 'explicit_resume',
                                                        'updated_at': datetime.now(UTC).isoformat()}
                    conn.execute('UPDATE agent_session_messages SET payload=? WHERE session_id=? AND message_id=?',
                                 (json.dumps(payload, ensure_ascii=False), session_id, item['message_id']))
            active = self._active(conn, session_id)
            conn.execute('UPDATE session_input_queues SET paused=0,watched_run_id=? WHERE session_id=?',
                         (self._watched_user_run(conn, active), session_id))
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
            # Keep the polling read-only and avoid opening a write transaction for
            # idle queues. Include queues needing failed-watch reconciliation,
            # even when they have no pending input.
            session_ids = [row[0] for row in conn.execute("""
                SELECT q.session_id
                FROM session_input_queues q
                WHERE (q.paused=0 AND q.watched_run_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM agent_runs w WHERE w.run_id=q.watched_run_id
                )) OR (q.paused=0 AND q.watched_run_id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM agent_runs w WHERE w.run_id=q.watched_run_id
                      AND (coalesce(json_extract(w.record_payload,'$.metadata.session_input_id'),'')!=''
                           OR coalesce(json_extract(w.record_payload,'$.metadata.input_envelope.origin'),'user')='user')
                      AND w.status IN ('failed','cancelled','timed_out')
                )) OR (q.paused=0 AND EXISTS (
                    SELECT 1 FROM agent_runs a WHERE a.session_id=q.session_id
                      AND a.status NOT IN ('completed','failed','cancelled','timed_out')
                      AND json_extract(a.record_payload,'$.parent_run_id') IS NULL
                      AND a.run_id IS NOT q.watched_run_id
                      AND (coalesce(json_extract(a.record_payload,'$.metadata.session_input_id'),'')!=''
                           OR coalesce(json_extract(a.record_payload,'$.metadata.input_envelope.origin'),'user')='user')
                )) OR (q.paused=0 AND NOT EXISTS (
                    SELECT 1 FROM agent_runs a WHERE a.session_id=q.session_id
                      AND a.status NOT IN ('completed','failed','cancelled','timed_out')
                      AND json_extract(a.record_payload,'$.parent_run_id') IS NULL
                ) AND EXISTS (
                    SELECT 1 FROM session_inputs i WHERE i.session_id=q.session_id
                      AND i.run_id IS NULL AND i.cancelled=0
                ))
                ORDER BY q.session_id
            """)]
        for sid in session_ids:
            try:
                self._bind(sid)
            except (KeyError, SessionInputConflict):
                # Disabled, deleted, or no longer eligible sessions stay inert.
                continue

    @staticmethod
    def _accepted_answer_selection(conn, item):
        row = conn.execute('SELECT payload FROM agent_session_messages WHERE session_id=? AND message_id=?',
                           (item['session_id'], item['message_id'])).fetchone()
        selection = json.loads(row[0]).get('answer_model_selection') if row else None
        if not isinstance(selection, dict):
            return None  # Inputs accepted before dual-model support keep their legacy policy behavior.
        return {name: selection.get(name) for name in ('llm_answer_client_name', 'llm_answer_model')}

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
                watched = self._watched_user_run(conn, active)
                if watched:
                    conn.execute('UPDATE session_input_queues SET watched_run_id=? WHERE session_id=?', (watched, sid))
                return
            row = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND run_id IS NULL AND cancelled=0 ORDER BY sequence LIMIT 1',
                               (sid,)).fetchone()
            if row is None:
                return
            request = json.loads(row['request_json'])
            accepted_answer = self._accepted_answer_selection(conn, row)
            answer_options = request.get('llm', {})
            if accepted_answer is not None:
                policy.update(accepted_answer)
            elif answer_options.get('answer_client_name') is not None or answer_options.get('answer_model') is not None:
                policy['llm_answer_client_name'] = answer_options.get('answer_client_name')
                policy['llm_answer_model'] = answer_options.get('answer_model')
            requested_reasoning = request.get('llm', {}).get('reasoning_level')
            policy_reasoning = policy.get('llm_reasoning_level')
            policy_reasoning_source = policy.get('llm_reasoning_source')
            effective_reasoning = requested_reasoning or policy_reasoning or 'low'
            effective_reasoning_source = ('request' if requested_reasoning else
                                          policy_reasoning_source or
                                          ('session' if policy_reasoning else 'default'))
            policy['llm_reasoning_level'] = effective_reasoning
            policy['llm_reasoning_source'] = effective_reasoning_source
            now = datetime.now(UTC).isoformat()
            run = AgentRunRecord(run_id='agent_run_' + uuid4().hex, trace_id='agent_turn_' + uuid4().hex,
                                 session_id=sid, user_input=request['user_input'], created_at=now, status=AgentRunStatus.QUEUED,
                                 metadata={'entrypoint': 'agent.turn', 'orchestrator': 'langgraph',
                                           'llm_reasoning_level': effective_reasoning,
                                           'llm_reasoning_source': effective_reasoning_source,
                                           'session_policy': policy,
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

    @staticmethod
    def render_batch(inputs):
        """Render real, ordered user messages for a single model turn."""
        if len(inputs) == 1:
            return inputs[0]['user_input']
        parts = []
        for item in inputs:
            text = f"[User message {item['sequence']}]\n{item['user_input']}"
            if item['attachment_ids']:
                text += '\n[Attachments: ' + ', '.join(item['attachment_ids']) + ']'
            parts.append(text)
        return '\n\n'.join(parts)

    def freeze_for_dispatch(self, run, guard):
        """Freeze once, at first execution, including inputs accepted after bind.

        Keep the manager-to-SQLite lock order used by run controls, so a pause or
        cancellation cannot race a cache refresh and overwrite the frozen input.
        Checkpoint recovery never calls this path or expands an existing batch.
        """
        manager = self.dispatcher.run_manager
        with manager._lock:
            with closing(self._conn()) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                guard.check(conn=conn)
                self.authorize(conn, run.session_id)
                row = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?', (run.run_id,)).fetchone()
                current = AgentRunRecord.model_validate_json(row[0]) if row else None
                if current is None or current.status != AgentRunStatus.QUEUED:
                    raise ValueError('Session input is no longer queued')
                if current.metadata.get('session_input_batch_frozen'):
                    return current
                first = conn.execute('SELECT * FROM session_inputs WHERE input_id=? AND run_id=? AND cancelled=0',
                                     (current.metadata['session_input_id'], current.run_id)).fetchone()
                if first is None:
                    raise ValueError('Accepted session input binding is unavailable')
                first_request = json.loads(first['request_json'])
                options = (first_request.get('safety_review_mode'), first_request.get('llm'),
                           self._accepted_answer_selection(conn, first))
                selected, rendered, attachment_ids = [], [], []
                rows = conn.execute('SELECT * FROM session_inputs WHERE session_id=? AND sequence>=? ORDER BY sequence',
                                    (current.session_id, first['sequence'])).fetchall()
                for item in rows:
                    if item['cancelled']:
                        continue
                    # A replacement jumps ahead of older queued messages, but
                    # neither side of that explicit boundary consumes the other.
                    if item['input_id'] != first['input_id'] and item['run_id'] is not None:
                        break
                    request = json.loads(item['request_json'])
                    if (request.get('safety_review_mode'), request.get('llm'),
                        self._accepted_answer_selection(conn, item)) != options:
                        break
                    if selected and request.get('replace_run_id'):
                        break
                    combined = list(dict.fromkeys(attachment_ids + request['attachment_ids']))
                    candidate = {'sequence': item['sequence'], 'user_input': request['user_input'],
                                 'attachment_ids': request['attachment_ids']}
                    if selected:
                        if len(combined) > 4 or len(self.render_batch(rendered + [candidate])) > 100000:
                            break
                        # Recheck the prefix even when later text adds no files.
                        if self.batch_attachments_fit is not None and combined:
                            try:
                                if not self.batch_attachments_fit(current.session_id, combined):
                                    break
                            except (KeyError, OSError, ValueError):
                                # A revoked attachment must not bind additional
                                # messages; its own turn will fail preflight.
                                break
                    selected.append(item)
                    rendered.append(candidate)
                    attachment_ids = combined
                batch = []
                for item, source in zip(selected, rendered, strict=True):
                    message = conn.execute('SELECT content,payload FROM agent_session_messages WHERE message_id=? AND session_id=? AND role=\'user\'',
                                           (item['message_id'], current.session_id)).fetchone()
                    if message is None or message['content'] != source['user_input']:
                        raise ValueError('Accepted session input history is unavailable or changed')
                    payload = json.loads(message['payload'])
                    if payload.get('attachment_ids') != source['attachment_ids'] or payload.get('input_id') != item['input_id']:
                        raise ValueError('Accepted session input history is unavailable or changed')
                    payload['trace_id'] = current.trace_id
                    conn.execute('UPDATE agent_session_messages SET payload=? WHERE message_id=?',
                                 (json.dumps(payload, ensure_ascii=False), item['message_id']))
                    conn.execute('UPDATE session_inputs SET run_id=? WHERE input_id=?', (current.run_id, item['input_id']))
                    batch.append({'input_id': item['input_id'], 'message_id': item['message_id'],
                                  'sequence': item['sequence'], 'attachment_ids': source['attachment_ids'],
                                  'content_sha256': hashlib.sha256(source['user_input'].encode()).hexdigest()})
                frozen = current.model_copy(update={
                    'user_input': self.render_batch(rendered),
                    'metadata': {**current.metadata, 'session_input_batch_frozen': True, 'session_input_batch': batch,
                                 'session_input_options': {key: value for key, value in first_request.items()
                                                           if key not in {'user_input', 'attachment_ids'}} | {'attachment_ids': attachment_ids}},
                })
                self.run_store.save_run_in_transaction(conn, frozen.model_dump(mode='json'), updated_at=datetime.now(UTC).isoformat())
            return manager.refresh_after_transaction(frozen)

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
