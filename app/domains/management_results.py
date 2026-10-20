"""Read persisted task evidence through management ownership, never source paths."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from app.domains.managed_tasks import TERMINAL
from app.domains.management import ManagementConflictError

# Deliberately omit prompts, input, metadata, errors and arbitrary command stdout.
_OUTPUT_FIELDS = frozenset({
    "path", "paths", "content", "text", "status", "success", "created", "modified",
    "bytes_written", "bytes_read", "sha256", "old_sha256", "new_sha256", "size_bytes",
    "exit_code", "returncode", "line_count", "start_line", "end_line", "truncated",
    "next_line", "next_offset", "next_char", "total_chars", "total_lines", "entries",
    "name", "type", "is_file", "is_dir", "exists", "url", "title", "results", "items",
})


def _project(value):
    if isinstance(value, dict):
        return {key: _project(item) for key, item in value.items() if key in _OUTPUT_FIELDS}
    if isinstance(value, list):
        return [_project(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return None


class ManagementResultService:
    def __init__(self, *, tasks):
        self.tasks = tasks

    def _owned_session(self, conn, context, session_id):
        row = conn.execute("SELECT kind FROM agent_sessions WHERE session_id=? AND status!='deleted'",
                           (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        delegates = getattr(self.tasks, "delegates", None)
        own_delegate = delegates.get_for_session(conn, context.session_id) if delegates else None
        linked = conn.execute("SELECT 1 FROM managed_session_links WHERE manager_session_id=? AND task_session_id=?",
                              (context.session_id, session_id)).fetchone()
        if linked and row['kind'] == 'ordinary':
            return
        if not own_delegate and delegates:
            target = delegates.get_for_session(conn, session_id)
            if target and target['parent_session_id'] == context.session_id:
                return
            linked_delegate = conn.execute(
                "SELECT 1 FROM managed_session_links l JOIN manager_delegates d ON d.session_id=l.manager_session_id "
                "WHERE d.parent_session_id=? AND l.task_session_id=?",
                (context.session_id, session_id),
            ).fetchone()
            if linked_delegate and row['kind'] == 'ordinary':
                return
        raise ManagementConflictError("Task result requires an owned management task or delegated root")

    @staticmethod
    def _evidence(conn, record):
        # Match provenance explicitly: other turns' messages must never substitute.
        rows = conn.execute(
            "SELECT message_id,payload FROM agent_session_messages WHERE session_id=? AND role='agent' "
            "AND json_extract(payload,'$.run_id')=? ORDER BY rowid DESC",
            (record['session_id'], record['run_id']),
        ).fetchall()
        events, source = None, None
        for row in rows:
            payload = json.loads(row['payload'])
            if isinstance(payload.get('tool_events'), list):
                events, source = payload['tool_events'], 'final_agent_message'
                break
        snapshot = record.get('result_snapshot') or {}
        if events is None and isinstance(snapshot.get('tool_events'), list):
            events, source = snapshot['tool_events'], 'result_snapshot'
        artifacts = conn.execute(
            "SELECT artifact_id,payload FROM agent_run_artifacts WHERE run_id=? AND kind='tool_result' "
            "ORDER BY created_at,artifact_id", (record['run_id'],),
        ).fetchall()
        by_id = {row['artifact_id']: json.loads(row['payload']) for row in artifacts}
        if events is None:
            events = [{'result': result} for result in by_id.values()]
            source = 'tool_result_artifacts'
        evidence, seen = [], set()
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get('result'), dict):
                continue
            result = event['result']
            invocation = result.get('invocation_id')
            # Persisted raw result replaces an observation preview when available.
            raw = by_id.get('tool_result_' + invocation) if isinstance(invocation, str) else None
            if raw is not None:
                result = raw
                seen.add('tool_result_' + invocation)
            evidence.append(ManagementResultService._event(result, event.get('tool_name'), source))
        # A terminal failure may have persisted a tool result before its final message.
        for artifact_id, result in by_id.items():
            if artifact_id not in seen and not any(
                item.get('invocation_id') == result.get('invocation_id') for item in evidence
            ):
                evidence.append(ManagementResultService._event(result, None, 'tool_result_artifacts'))
        return evidence

    @staticmethod
    def _event(result, tool_name, source):
        return {
            'invocation_id': result.get('invocation_id'),
            'tool_name': result.get('tool_name') or tool_name,
            'status': result.get('status'),
            'execution_started': result.get('execution_started') if type(result.get('execution_started')) is bool else None,
            'source': source,
            'output': _project(result.get('output', {})),
        }

    def read_task_result(self, context, *, run_id, section='answer', start_char=0, max_chars=4000):
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
            raise ValueError('run_id must contain 1..256 characters')
        if section not in {'answer', 'evidence'}:
            raise ValueError('section must be answer or evidence')
        if type(start_char) is not int or start_char < 0 or type(max_chars) is not int or not 1 <= max_chars <= 8000:
            raise ValueError('start_char must be non-negative and max_chars must be 1..8000')
        with closing(self.tasks._conn()) as conn:
            conn.execute('BEGIN')
            self.tasks.caller(context, conn=conn)
            row = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            record = json.loads(row[0])
            if record.get('parent_run_id') is not None:
                raise ManagementConflictError('Task result requires an independent root')
            self._owned_session(conn, context, record['session_id'])
            output = {
                'run': self.tasks._run_view(record), 'observed_at': datetime.now(UTC).isoformat(),
                'terminal': record['status'] in TERMINAL, 'business_completion': 'not_determined',
                'evidence_scope': 'Persisted execution evidence, not current source/file state or approval for another operation.',
            }
            if not output['terminal']:
                return output
            if section == 'evidence':
                value = json.dumps(self._evidence(conn, record), ensure_ascii=False, separators=(',', ':'))
            else:
                # The final answer is source content, not a permission or verification claim.
                snapshot = record.get('result_snapshot') or {}
                value = snapshot.get('answer')
                if not isinstance(value, str):
                    message = conn.execute(
                        "SELECT content FROM agent_session_messages WHERE session_id=? AND role='agent' "
                        "AND json_extract(payload,'$.run_id')=? ORDER BY rowid DESC LIMIT 1",
                        (record['session_id'], run_id),
                    ).fetchone()
                    value = message[0] if message else ''
        end = min(start_char + max_chars, len(value))
        return {**output, 'section': section, 'encoding': 'json' if section == 'evidence' else 'text',
                'content': value[start_char:end], 'start_char': start_char, 'total_chars': len(value),
                'next_char': end if end < len(value) else None}
