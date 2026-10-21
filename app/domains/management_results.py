"""Read persisted task evidence through management ownership, never source paths."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256

from app.core.answer_evidence import cached_result_read_routes
from app.core.context_driver import ToolView
from app.core.tools import ToolContext, tool_scope_discovery_denial
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

_MATTER_WRITE_TOOLS = frozenset({
    "matter.create", "matter.create_many", "matter.update", "matter.link_source",
})
_MATTER_RECEIPT_FIELDS = (
    "matter_id", "revision", "title", "status", "due_at",
    "scheduled_on", "scheduled_start", "scheduled_end",
)
_MATTER_RECEIPT_FIELD_LIMITS = {
    "matter_id": 160, "title": 240, "status": 40, "due_at": 64,
    "scheduled_on": 32, "scheduled_start": 64, "scheduled_end": 64,
}
_MATTER_RECEIPT_MAX_RECORDS = 20
_ANSWER_RECEIPT_MAX_CHARS = 12_000
_ANSWER_RECEIPT_MAX_ITEMS = 20
_ANSWER_RECEIPT_MAX_RECORDS_PER_ITEM = 4
_ANSWER_MATTER_RECEIPT_MAX_CHARS = 8_500
_ANSWER_MAIL_RECEIPT_MAX_CHARS = 3_000
_MAIL_READ_TOOLS = frozenset({'mail.list', 'mail.load_messages'})


def _project(value):
    if isinstance(value, dict):
        return {key: _project(item) for key, item in value.items() if key in _OUTPUT_FIELDS}
    if isinstance(value, list):
        return [_project(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return None


class ManagementResultService:
    def __init__(self, *, tasks, registry=None):
        self.tasks = tasks
        self.registry = registry

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

    def _evidence(self, conn, record):
        # Match provenance explicitly: other turns' messages must never substitute.
        rows = conn.execute(
            "SELECT message_id,payload,created_at FROM agent_session_messages WHERE session_id=? AND role='agent' "
            "AND json_extract(payload,'$.run_id')=? ORDER BY rowid DESC",
            (record['session_id'], record['run_id']),
        ).fetchall()
        events, source, saved_at = None, None, None
        for row in rows:
            payload = json.loads(row['payload'])
            if isinstance(payload.get('tool_events'), list):
                events, source, saved_at = payload['tool_events'], 'final_agent_message', row['created_at']
                break
        snapshot = record.get('result_snapshot') or {}
        if events is None and isinstance(snapshot.get('tool_events'), list):
            events, source, saved_at = snapshot['tool_events'], 'result_snapshot', record.get('completed_at')
        artifacts = conn.execute(
            "SELECT artifact_id,payload,content_hash,created_at FROM agent_run_artifacts WHERE run_id=? AND kind='tool_result' "
            "AND substr(artifact_id,1,12)='tool_result_' ORDER BY created_at,artifact_id", (record['run_id'],),
        ).fetchall()
        by_id, invalid_artifacts = {}, {}
        artifact_times = {row['artifact_id']: row['created_at'] for row in artifacts}
        for row in artifacts:
            artifact_id, payload_text = row['artifact_id'], row['payload']
            key_invocation = artifact_id.removeprefix('tool_result_')
            try:
                raw = json.loads(payload_text)
            except (TypeError, ValueError):
                raw = None
            hash_matches = (isinstance(payload_text, str)
                            and sha256(payload_text.encode()).hexdigest() == row['content_hash'])
            identity_matches = (bool(key_invocation) and isinstance(raw, dict)
                                and raw.get('invocation_id') == key_invocation
                                and isinstance(raw.get('tool_name'), str) and bool(raw['tool_name']))
            if not hash_matches:
                invalid_artifacts[artifact_id] = 'artifact_hash_mismatch'
            elif not identity_matches:
                invalid_artifacts[artifact_id] = 'artifact_identity_mismatch'
            else:
                by_id[artifact_id] = raw
        if events is None:
            events = []
            source = 'tool_result_artifacts'
        evidence, handled_artifacts = [], set()
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get('result'), dict):
                continue
            result = event['result']
            invocation = result.get('invocation_id')
            event_tool = event.get('tool_name')
            artifact_id = 'tool_result_' + invocation if isinstance(invocation, str) else None
            artifact_present = artifact_id in by_id or artifact_id in invalid_artifacts
            event_identity_matches = (
                isinstance(invocation, str) and bool(invocation)
                and isinstance(event_tool, str) and bool(event_tool)
                and result.get('tool_name') == event_tool
                and (event.get('invocation_id') is None or event.get('invocation_id') == invocation)
            )
            if not event_identity_matches:
                if artifact_present:
                    handled_artifacts.add(artifact_id)
                evidence.append(self._withheld_event(
                    invocation, event_tool, 'event_identity_mismatch', saved_at))
                continue
            if artifact_id in invalid_artifacts:
                handled_artifacts.add(artifact_id)
                evidence.append(self._withheld_event(
                    invocation, event_tool, invalid_artifacts[artifact_id], artifact_times.get(artifact_id)))
                continue
            # Persisted raw result replaces an observation preview only when its
            # invocation and registered event name agree with the artifact key.
            raw = by_id.get(artifact_id) if artifact_id else None
            if raw is not None:
                if raw.get('invocation_id') != invocation or raw.get('tool_name') != event_tool:
                    handled_artifacts.add(artifact_id)
                    evidence.append(self._withheld_event(
                        invocation, event_tool, 'artifact_identity_mismatch', artifact_times.get(artifact_id)))
                    continue
                result = raw
                handled_artifacts.add(artifact_id)
            evidence.append(self._event(
                result, event_tool, 'tool_result_artifacts' if raw is not None else source,
                artifact_times.get(artifact_id) if raw is not None else saved_at))
        # A terminal failure may have persisted a tool result before its final message.
        for artifact_id, result in by_id.items():
            if artifact_id not in handled_artifacts:
                evidence.append(self._event(result, None, 'tool_result_artifacts', artifact_times[artifact_id]))
        for artifact_id, reason in invalid_artifacts.items():
            if artifact_id not in handled_artifacts:
                evidence.append(self._withheld_event(
                    artifact_id.removeprefix('tool_result_'), None, reason, artifact_times[artifact_id]))
        return evidence

    @staticmethod
    def _withheld_event(invocation_id, tool_name, reason, saved_at):
        return {
            'invocation_id': invocation_id if isinstance(invocation_id, str) else None,
            'tool_name': tool_name if isinstance(tool_name, str) else None,
            'status': 'unavailable', 'execution_started': None,
            'source': 'artifact_integrity_mismatch', 'saved_at': saved_at,
            'output': {'result_withheld': True, 'reason': reason},
        }

    def _event(self, result, tool_name, source, saved_at):
        name = result.get('tool_name') or tool_name
        registered = self.registry.get_tool_or_none(name) if self.registry and isinstance(name, str) else None
        reader = (self.tasks.run_store.tool_result_requires_lineage(name)
                  or callable(getattr(registered, 'result_artifact_source', None)))
        event = {
            'invocation_id': result.get('invocation_id'),
            'tool_name': name,
            'status': result.get('status'),
            'execution_started': result.get('execution_started') if type(result.get('execution_started')) is bool else None,
            'source': source, 'saved_at': saved_at,
            # A cached page is a proxy for its actual producer. Projecting its
            # text here would bypass the ancestor's revocation and permissions.
            'output': ({'body_withheld': True, 'reason': 'cached_reader_receipt',
                        'read_original_producer': True} if reader else _project(result.get('output', {}))),
        }
        result_output = result.get('output')
        result_error = result.get('error')
        result_has_error = result_error not in (None, '')
        explicit_failure = isinstance(result_output, dict) and result_output.get('success') is False
        if (name in _MATTER_WRITE_TOOLS and registered is not None
                and registered.spec.name == name and registered.spec.package == 'matter'
                and registered.spec.read_only is False and not reader
                and not result_has_error and not explicit_failure
                and isinstance(result.get('invocation_id'), str) and bool(result['invocation_id'])):
            event['execution_receipt'] = self._matter_write_receipt(
                name, result.get('invocation_id'), result.get('status'),
                result.get('execution_started'), result.get('output'), max_records=None)
        return event

    @staticmethod
    def _matter_write_receipt(tool_name, invocation_id, status, execution_started, output,
                              *, max_records=_MATTER_RECEIPT_MAX_RECORDS):
        """Project only stable identifiers and scheduling fields from Matter writes."""
        matter_values = []
        reported_count = None
        write_succeeded = status == 'completed' and execution_started is True
        output_recognized = False
        if write_succeeded and isinstance(output, dict):
            if isinstance(output.get('matter'), dict):
                matter_values = [output['matter']]
                reported_count = 1
                output_recognized = True
            elif isinstance(output.get('matters'), list):
                matter_values = output['matters']
                count = output.get('matters_created')
                reported_count = count if type(count) is int and count >= 0 else len(matter_values)
                output_recognized = True

        records = []
        records_limit = max_records
        for matter in matter_values if records_limit is None else matter_values[:records_limit]:
            if not isinstance(matter, dict):
                records.append({'available': False, 'missing_fields': list(_MATTER_RECEIPT_FIELDS)})
                continue
            record = {}
            missing, truncated = [], []
            for field in _MATTER_RECEIPT_FIELDS:
                if field not in matter:
                    missing.append(field)
                    continue
                value = matter[field]
                if field == 'revision':
                    if type(value) is int and value > 0:
                        record[field] = value
                    else:
                        missing.append(field)
                    continue
                if value is None:
                    if field in {'matter_id', 'title', 'status'}:
                        missing.append(field)
                    else:
                        record[field] = None
                elif isinstance(value, str):
                    if field in {'matter_id', 'title', 'status'} and not value.strip():
                        missing.append(field)
                        continue
                    limit = _MATTER_RECEIPT_FIELD_LIMITS[field]
                    record[field] = value[:limit]
                    if len(value) > limit:
                        truncated.append(field)
                else:
                    missing.append(field)
            records.append({**record, 'missing_fields': missing, 'truncated_fields': truncated})

        truncated_records = records_limit is not None and len(matter_values) > records_limit
        available = output_recognized
        return {
            'kind': 'matter_write',
            'invocation_id': invocation_id if isinstance(invocation_id, str) else None,
            'tool_name': tool_name,
            'execution_status': status if isinstance(status, str) else 'unknown',
            'execution_started': execution_started if type(execution_started) is bool else None,
            'records': records,
            'records_available': available,
            'records_complete': (available and not truncated_records
                                 and reported_count == len(matter_values)
                                 and all(not row.get('missing_fields') and not row.get('truncated_fields')
                                         for row in records)),
            'records_withheld_reason': (
                None if available else
                'write_not_confirmed_completed' if not write_succeeded else
                'matter_output_missing_or_unrecognized'
            ),
            'records_truncated': truncated_records,
            'tool_reported_count': reported_count,
            'receipt_scope': 'historical_tool_output_not_current_state_verification',
        }

    @staticmethod
    def _answer_receipt_preview(receipts, *, max_chars=_ANSWER_MATTER_RECEIPT_MAX_CHARS):
        items, truncated = [], len(receipts) > _ANSWER_RECEIPT_MAX_ITEMS
        for receipt in receipts[:_ANSWER_RECEIPT_MAX_ITEMS]:
            item = dict(receipt)
            full_records = item.get('records') if isinstance(item.get('records'), list) else []
            item['records'] = full_records[:_ANSWER_RECEIPT_MAX_RECORDS_PER_ITEM]
            if len(item['records']) < len(full_records):
                item['records_truncated'] = True
                item['records_complete'] = False
                truncated = True
            items.append(item)
            candidate = {
                'items': items, 'total': len(receipts), 'truncated': truncated,
                'scope': 'Historical execution receipts only; not current Matter state or semantic verification.',
                'max_chars': max_chars,
            }
            while len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) > max_chars:
                last = items[-1]
                rows = last.get('records')
                if isinstance(rows, list) and rows:
                    rows.pop()
                    last['records_truncated'] = True
                    last['records_complete'] = False
                    truncated = True
                    candidate['truncated'] = True
                elif len(items) > 1:
                    items.pop()
                    truncated = True
                    candidate['truncated'] = True
                else:
                    items.pop()
                    truncated = True
                    candidate['truncated'] = True
                    break
            if not items or items[-1] is not item:
                break
        result = {
            'items': items, 'total': len(receipts), 'truncated': truncated,
            'scope': 'Historical execution receipts only; not current Matter state or semantic verification.',
            'max_chars': max_chars,
        }
        return result

    @staticmethod
    def _mail_coverage_preview(receipts):
        items, truncated = [], len(receipts) > _ANSWER_RECEIPT_MAX_ITEMS
        for receipt in receipts[:_ANSWER_RECEIPT_MAX_ITEMS]:
            items.append(dict(receipt))
            candidate = {
                'items': items, 'total': len(receipts), 'truncated': truncated,
                'scope': 'Validated tool delivery only; not mailbox freshness or content understanding.',
                'max_chars': _ANSWER_MAIL_RECEIPT_MAX_CHARS,
            }
            while len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) > _ANSWER_MAIL_RECEIPT_MAX_CHARS:
                items.pop()
                truncated = True
                candidate['truncated'] = True
                break
        return {
            'items': items, 'total': len(receipts), 'truncated': truncated,
            'scope': 'Validated tool delivery only; not mailbox freshness or content understanding.',
            'max_chars': _ANSWER_MAIL_RECEIPT_MAX_CHARS,
        }

    @staticmethod
    def _progress_result_digest(value):
        return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def _validated_mail_operation_input(self, conn, record, invocation_id, tool_name, raw_result):
        row = conn.execute(
            "SELECT artifact_id,payload,content_hash FROM agent_run_artifacts "
            "WHERE run_id=? AND kind='session_progress' ORDER BY updated_at DESC LIMIT 1",
            (record['run_id'],),
        ).fetchone()
        if row is None or row['artifact_id'] != 'session_progress_' + record['run_id']:
            return None
        payload_text = row['payload']
        if (not isinstance(payload_text, str)
                or sha256(payload_text.encode()).hexdigest() != row['content_hash']):
            return None
        try:
            progress = json.loads(payload_text)
        except (TypeError, ValueError):
            return None
        if not isinstance(progress, dict) or progress.get('run_id') != record['run_id']:
            return None
        raw_operations = progress.get('operations')
        if not isinstance(raw_operations, list):
            return None
        operations = [item for item in raw_operations
                      if isinstance(item, dict) and item.get('evidence_id') == invocation_id]
        if len(operations) != 1:
            return None
        operation = operations[0]
        if (operation.get('tool_name') != tool_name
                or operation.get('artifact_id') != 'tool_result_' + invocation_id
                or operation.get('status') != raw_result.get('status')
                or operation.get('content_hash') != self._progress_result_digest(raw_result)
                or not isinstance(operation.get('input'), dict)):
            return None
        return operation['input']

    @staticmethod
    def _bounded_rank_range(value, *, allow_zero=False):
        if not isinstance(value, dict):
            return None
        start, end = value.get('start_rank'), value.get('end_rank')
        minimum = 0 if allow_zero else 1
        if (type(start) is not int or type(end) is not int
                or start < minimum or end < minimum or (allow_zero and (start == 0) != (end == 0))
                or (not allow_zero and end < start)):
            return None
        return {'start_rank': start, 'end_rank': end}

    @staticmethod
    def _mail_timestamp(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None

    def _mail_list_coverage(self, raw_result, tool_input):
        output = raw_result.get('output')
        if not isinstance(output, dict):
            return None
        start = tool_input.get('start_rank', 1)
        end = tool_input.get('end_rank')
        if type(start) is not int or start < 1 or (end is not None and (type(end) is not int or end < start or end - start >= 20)):
            return None
        received_from = self._mail_timestamp(tool_input.get('received_from'))
        received_before = self._mail_timestamp(tool_input.get('received_before'))
        folder = tool_input.get('folder')
        if received_from is None or received_before is None or (folder is not None and (not isinstance(folder, str) or len(folder) > 80)):
            return None

        missing = []
        total_matches = output.get('total_matches')
        returned_count = output.get('returned_count')
        has_more = output.get('has_more')
        if type(total_matches) is not int or total_matches < 0:
            total_matches = None
            missing.append('total_matches')
        if type(returned_count) is not int or returned_count < 0:
            returned_count = None
            missing.append('returned_count')
        if type(has_more) is not bool:
            has_more = None
            missing.append('has_more')
        requested_range = self._bounded_rank_range(output.get('requested_range'))
        coverage = self._bounded_rank_range(output.get('coverage'), allow_zero=True)
        next_range = None
        raw_next = output.get('next_range')
        if raw_next is not None:
            next_range = self._bounded_rank_range(raw_next)
            if next_range is None:
                missing.append('next_range')
        elif 'next_range' not in output:
            missing.append('next_range')
        if requested_range is None:
            missing.append('requested_range')
        if coverage is None:
            missing.append('coverage')
        messages = output.get('messages')
        if not isinstance(messages, list):
            card_count = None
            missing.append('messages')
        else:
            card_count = len(messages)
        count_consistent = (returned_count is not None and card_count is not None
                            and returned_count == card_count)
        effective_end = end if end is not None else start + 19
        expected_coverage = (None if returned_count is None else
                             {'start_rank': start, 'end_rank': start + returned_count - 1}
                             if returned_count else {'start_rank': 0, 'end_rank': 0})
        has_more_consistent = (type(has_more) is bool and total_matches is not None
                               and returned_count is not None
                               and has_more == (start + returned_count <= total_matches))
        next_range_consistent = (
            has_more is False and next_range is None
            or has_more is True and total_matches is not None and returned_count is not None
            and next_range == {'start_rank': start + returned_count,
                               'end_rank': min(start + returned_count + 19, total_matches)}
        )
        range_consistent = (
            requested_range == {'start_rank': start, 'end_rank': effective_end}
            and coverage == expected_coverage and has_more_consistent and next_range_consistent
            and count_consistent
        )
        return {
            'kind': 'mail_list_coverage', 'available': True,
            'received_from': received_from, 'received_before': received_before,
            'folder': folder,
            'total_matches': total_matches, 'returned_count': returned_count,
            'delivered_card_count': card_count, 'has_more': has_more,
            'requested_range': requested_range, 'coverage': coverage, 'next_range': next_range,
            'count_consistent': count_consistent, 'range_consistent': range_consistent,
            'window_enumeration_complete': bool(requested_range and requested_range['start_rank'] == 1
                                                and has_more is False and range_consistent),
            'missing_fields': missing,
            'receipt_scope': 'validated_tool_delivery_only_not_mailbox_freshness_or_content_understanding',
        }

    def _mail_load_coverage(self, raw_result, tool_input):
        requested = tool_input.get('message_ids')
        if (not isinstance(requested, list) or not 1 <= len(requested) <= 3
                or any(not isinstance(item, str) or not item or len(item) > 256 for item in requested)
                or len(set(requested)) != len(requested)):
            return None
        output = raw_result.get('output')
        if not isinstance(output, dict) or not isinstance(output.get('messages'), list):
            return None
        requested_set = set(requested)
        seen, delivered = set(), []
        unexpected = 0
        for message in output['messages']:
            if not isinstance(message, dict):
                unexpected += 1
                continue
            message_id = message.get('message_id')
            if not isinstance(message_id, str) or message_id not in requested_set:
                unexpected += 1
                continue
            if message_id in seen:
                unexpected += 1
                continue
            seen.add(message_id)
            body = message.get('body_text')
            truncation = message.get('body_truncated')
            missing = []
            if not isinstance(body, str):
                body_chars = None
                missing.append('body_text')
            else:
                body_chars = len(body)
            if type(truncation) is not bool:
                truncation = None
                missing.append('body_truncated')
            source_chars = message.get('source_body_chars')
            if (type(source_chars) is not int or source_chars < 0
                    or body_chars is not None and source_chars < body_chars):
                source_chars = None
            body_complete = isinstance(body, str) and truncation is False
            delivered.append({
                'message_id': message_id, 'body_truncated': truncation,
                'delivered_body_chars': body_chars,
                'source_body_chars': source_chars, 'source_body_chars_known': source_chars is not None,
                'body_complete': body_complete,
                'missing_fields': missing,
            })
        missing_ids = [message_id for message_id in requested if message_id not in seen]
        return {
            'kind': 'mail_load_coverage', 'available': True,
            'requested_count': len(requested), 'delivered_count': len(delivered),
            'delivered_messages': delivered, 'missing_message_ids': missing_ids,
            'omitted_count': len(missing_ids), 'unexpected_output_count': unexpected,
            'delivery_complete': not missing_ids and unexpected == 0,
            'all_delivered_bodies_complete': all(message['body_complete'] for message in delivered)
            and bool(delivered),
            'receipt_scope': 'validated_tool_delivery_only_not_mailbox_freshness_or_content_understanding',
        }

    def _mail_coverage_receipt(self, conn, context, record, evidence_item):
        tool_name = evidence_item.get('tool_name')
        registered = self.registry.get_tool_or_none(tool_name) if self.registry and isinstance(tool_name, str) else None
        if (tool_name not in _MAIL_READ_TOOLS or registered is None
                or registered.spec.name != tool_name or registered.spec.package != 'mail'
                or registered.spec.read_only is not True):
            return None
        base = {
            'tool_name': tool_name, 'invocation_id': evidence_item.get('invocation_id'),
            'execution_status': evidence_item.get('status'),
            'execution_started': evidence_item.get('execution_started'),
            'available': False, 'unavailable_reason': 'source_or_operation_input_unavailable',
        }
        invocation_id = evidence_item.get('invocation_id')
        if isinstance(invocation_id, str) and invocation_id:
            base['read_observation'] = {'run_id': record['run_id'], 'section': 'observation',
                                       'invocation_id': invocation_id}
        if (evidence_item.get('source') == 'artifact_integrity_mismatch'
                or not isinstance(invocation_id, str) or evidence_item.get('status') != 'completed'
                or evidence_item.get('execution_started') is not True):
            base['unavailable_reason'] = 'source_result_not_verified'
            return base
        try:
            raw, _, _ = self._observation_source(conn, context, record, invocation_id)
        except (KeyError, ValueError, TypeError, ManagementConflictError):
            return base
        if (raw.get('tool_name') != tool_name or raw.get('invocation_id') != invocation_id
                or raw.get('status') != 'completed' or raw.get('execution_started') is not True
                or raw.get('error') not in (None, '')
                or isinstance(raw.get('output'), dict) and raw['output'].get('success') is False):
            base['unavailable_reason'] = 'source_result_identity_or_status_mismatch'
            return base
        tool_input = self._validated_mail_operation_input(conn, record, invocation_id, tool_name, raw)
        if tool_input is None:
            return base
        coverage = (self._mail_list_coverage(raw, tool_input) if tool_name == 'mail.list'
                    else self._mail_load_coverage(raw, tool_input))
        if coverage is None:
            base['unavailable_reason'] = 'validated_operation_bounds_unavailable'
            return base
        return {**base, **coverage, 'available': True, 'unavailable_reason': None}

    @staticmethod
    def _digest(value):
        return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _observation_source(self, conn, context, record, invocation_id):
        self._owned_session(conn, context, record['session_id'])
        if record.get('parent_run_id') is not None:
            raise ManagementConflictError('Task result requires an independent root')
        session = conn.execute("SELECT status,created_at,metadata FROM agent_sessions WHERE session_id=?",
                               (record['session_id'],)).fetchone()
        if session is None or session['status'] != 'active':
            raise ManagementConflictError('Historical evidence requires an active source session')
        row = conn.execute("SELECT payload,content_hash,created_at FROM agent_run_artifacts "
                           "WHERE artifact_id=? AND run_id=? AND kind='tool_result'",
                           ('tool_result_' + invocation_id, record['run_id'])).fetchone()
        if row is None:
            raise KeyError(invocation_id)
        raw = json.loads(row['payload'])
        if sha256(row['payload'].encode()).hexdigest() != row['content_hash']:
            raise ManagementConflictError('Historical result identity or hash changed')
        if self.tasks.run_store.load_tool_result_artifact('tool_result_' + invocation_id, record['run_id']) != raw:
            raise ManagementConflictError('Historical result source is no longer readable; use section=evidence for its receipt')
        name = raw.get('tool_name')
        tool = self.registry.get_tool_or_none(name) if self.registry and isinstance(name, str) else None
        if (raw.get('invocation_id') != invocation_id or tool is None or tool.spec.read_only is not True
                or tool.spec.package == 'bash' or name.startswith('bash.')):
            raise ManagementConflictError('Raw historical output is unavailable for this tool; use section=evidence for its receipt')
        if callable(getattr(tool, 'result_artifact_source', None)):
            raise ManagementConflictError('Select the original producer evidence entry; cached-reader output cannot substitute original source permissions')
        # The ownership relation permits this bounded historical read. It does
        # not widen either source or reader tool permissions.
        frozen = record.get('metadata', {}).get('session_policy', {})
        if not frozen or name not in (frozen.get('allowed_tools') or []):
            raise ManagementConflictError('Historical tool was outside the source run permission snapshot')
        policies = [self.tasks.policy(record['session_id']), self.tasks.policy(context.session_id)]
        if any(policy.allowed_tools is not None and name not in policy.allowed_tools for policy in policies):
            raise ManagementConflictError('Historical tool permission has been revoked')
        view = context.tool_view
        if (view is not None and not view.allows_tool(tool_name=name, package=tool.spec.package, read_only=True)
                or tool_scope_discovery_denial(tool.spec, view)):
            raise ManagementConflictError('Historical tool is outside the current reader scope')
        # Cache-aware source tools may impose stronger account/source checks.
        validate = getattr(tool, 'allow_cached_observation', None)
        if callable(validate):
            operation = conn.execute("SELECT payload FROM agent_run_artifacts WHERE run_id=? AND kind='session_progress' "
                                     "ORDER BY updated_at DESC LIMIT 1", (record['run_id'],)).fetchone()
            progress = json.loads(operation['payload']) if operation else {}
            item = next((item for item in progress.get('operations', [])
                         if item.get('evidence_id') == invocation_id), None)
            source_views = record.get('metadata', {}).get('context_views', {})
            source_tool_view = source_views.get('tool') if isinstance(source_views, dict) else None
            source_view = ToolView.model_validate(source_tool_view) if source_tool_view else None
            reader_view = context.tool_view
            scope_views = [view for view in (source_view, reader_view) if view is not None]
            combined_view = None
            if scope_views:
                def scoped_ids(field):
                    values = []
                    for view in scope_views:
                        explicit = set(getattr(view, field))
                        if explicit:
                            values.append(explicit)
                        elif view.child_run_id is not None and not view.full_data_authority:
                            values.append(set())
                    if not values:
                        return ()
                    result = values[0]
                    for value in values[1:]:
                        result = result.intersection(value)
                    if not result:
                        raise ManagementConflictError('Historical reader and source scopes do not overlap')
                    return tuple(sorted(result))

                base_view = reader_view or source_view
                combined_view = base_view.model_copy(update={
                    'child_run_id': (reader_view.child_run_id if reader_view and reader_view.child_run_id
                                     else source_view.child_run_id if source_view else None),
                    'allowed_source_ids': scoped_ids('allowed_source_ids'),
                    'allowed_account_ids': scoped_ids('allowed_account_ids'),
                    'full_data_authority': all(view.full_data_authority for view in scope_views),
                })
            source_context = context.model_copy(update={
                'session_id': record['session_id'], 'tool_view': combined_view,
            })
            if (item is None or not isinstance(item.get('input'), dict)
                    or validate(tool_input=item['input'], result=raw, context=source_context) is not True):
                raise ManagementConflictError('Historical source access cannot be revalidated')
            if validate(tool_input=item['input'], result=raw, context=context) is not True:
                raise ManagementConflictError('Historical reader source access cannot be revalidated')
        reader = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?', (context.run_id,)).fetchone()
        reader = json.loads(reader[0]) if reader else {}
        boundary = {'source_session_id': record['session_id'], 'source_run_id': record['run_id'],
                    'source_session_created_at': session['created_at'],
                    'source_context_views': record.get('metadata', {}).get('context_views'),
                    'reader_context_views': reader.get('metadata', {}).get('context_views'),
                    'source_metadata_hash': self._digest(json.loads(session['metadata'])),
                    'source_policy': policies[0].model_dump(mode='json'),
                    'reader_policy': policies[1].model_dump(mode='json'),
                    'source_run_policy': frozen,
                    'source_content_hash': row['content_hash']}
        return raw, boundary, row['created_at']

    @staticmethod
    def _observation_output(value):
        # Keep domain fields (including mail messages/body_text), but never
        # expose command streams or prompt/internal-input envelopes.
        blocked = {'stdout', 'stderr', 'raw_prompt', 'prompt', 'prompts', 'system_prompt',
                   'developer_prompt', 'input', 'tool_input', '_authorization_source_tokens'}
        if isinstance(value, dict):
            return {key: ManagementResultService._observation_output(item)
                    for key, item in value.items() if key not in blocked}
        if isinstance(value, list):
            return [ManagementResultService._observation_output(item) for item in value]
        return value

    def _import_observation(self, conn, context, record, invocation_id):
        raw, boundary, saved_at = self._observation_source(conn, context, record, invocation_id)
        artifact_id = 'progress_import_management_' + self._digest(
            [context.run_id, record['run_id'], invocation_id, boundary])
        now = datetime.now(UTC).isoformat()
        store = self.tasks.run_store
        store.put_artifact(artifact_id='origin_' + artifact_id, run_id=context.run_id,
            kind='management_import_origin', payload={
                'source_run_id': record['run_id'], 'invocation_id': invocation_id,
                'target_run_id': context.run_id, 'boundary': boundary,
                'reader_tool_view': context.tool_view.model_dump(mode='json') if context.tool_view is not None else None},
            summary='Revocable owned-task historical evidence origin.', created_at=now)
        store.put_artifact(artifact_id=artifact_id, run_id=context.run_id, kind='tool_result',
            payload={key: raw[key] for key in ('invocation_id', 'tool_name', 'status', 'execution_started') if key in raw}
                    | {'output': self._observation_output(raw.get('output', {}))},
            summary='Historical owned-task result; no source refresh or authorization grant.', created_at=now)
        tool = self.registry.get_tool_or_none(raw['tool_name'])
        routes = cached_result_read_routes(artifact_id=artifact_id,
            output=self._observation_output(raw.get('output', {})),
            roles=tool.spec.output_evidence_roles if tool is not None else [])
        return {'artifact_id': artifact_id, 'read_tool': 'observation.read', 'read_package': 'observation',
                **routes,
                'path': routes['read_request']['path'], 'source_run_id': record['run_id'], 'invocation_id': invocation_id,
                'saved_at': saved_at, 'historical_only': True, 'current_evidence': False,
                'note': 'Execute read_request as provided; container_read_request reads the full result object. max_chars is optional for strings only. Loading does not refresh sources, repeat operations, or approve new actions.'}

    def can_read_import(self, artifact_id, run_id):
        try:
            if not artifact_id.startswith('progress_import_management_'):
                return False
            origin = self.tasks.run_store.load_artifact('origin_' + artifact_id)
            if not origin or origin.get('target_run_id') != run_id:
                return False
            with closing(self.tasks._conn()) as conn:
                conn.execute('BEGIN')
                reader = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
                source = conn.execute('SELECT record_payload FROM agent_runs WHERE run_id=?',
                                      (origin['source_run_id'],)).fetchone()
                if reader is None or source is None:
                    return False
                reader, source = json.loads(reader[0]), json.loads(source[0])
                context = ToolContext(session_id=reader['session_id'], run_id=run_id,
                    tool_view=ToolView.model_validate(origin['reader_tool_view']) if origin.get('reader_tool_view') else None)
                self.tasks.caller(context, conn=conn)
                raw, boundary, _ = self._observation_source(conn, context, source, origin['invocation_id'])
                imported = conn.execute("SELECT payload,content_hash FROM agent_run_artifacts "
                                        "WHERE artifact_id=? AND run_id=? AND kind='tool_result'",
                                        (artifact_id, run_id)).fetchone()
                expected = {key: raw[key] for key in ('invocation_id', 'tool_name', 'status', 'execution_started') if key in raw}
                expected['output'] = self._observation_output(raw.get('output', {}))
                return bool(boundary == origin.get('boundary') and imported
                            and sha256(imported['payload'].encode()).hexdigest() == imported['content_hash']
                            and json.loads(imported['payload']) == expected)
        except (KeyError, ValueError, TypeError, ManagementConflictError):
            return False

    def read_task_result(self, context, *, run_id, section='answer', start_char=0, max_chars=4000, evidence_offset=0, invocation_id=None):
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
            raise ValueError('run_id must contain 1..256 characters')
        if section not in {'answer', 'evidence', 'observation'}:
            raise ValueError('section must be answer, evidence or observation')
        if section == 'observation' and (not isinstance(invocation_id, str) or not invocation_id.strip() or len(invocation_id) > 256):
            raise ValueError('section=observation requires invocation_id with 1..256 characters')
        if section != 'observation' and invocation_id is not None:
            raise ValueError('invocation_id is only supported for section=observation')
        if type(start_char) is not int or start_char < 0 or type(max_chars) is not int or not 1 <= max_chars <= 8000:
            raise ValueError('start_char must be non-negative and max_chars must be 1..8000')
        if type(evidence_offset) is not int or evidence_offset < 0:
            raise ValueError('evidence_offset must be non-negative')
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
            evidence = self._evidence(conn, record)
            if section != 'observation':
                mail_receipts = []
                for item in evidence:
                    mail_receipt = self._mail_coverage_receipt(conn, context, record, item)
                    if mail_receipt is not None:
                        item['mail_coverage_receipt'] = mail_receipt
                        mail_receipts.append(mail_receipt)
            else:
                mail_receipts = []
            serialized = [json.dumps(item, ensure_ascii=False, separators=(',', ':')) for item in evidence]
            index, char_offset = [], 1
            evidence_routes = {}
            for position, (item, text) in enumerate(zip(evidence, serialized)):
                read_route = {'run_id': run_id, 'section': 'evidence',
                              'start_char': char_offset, 'max_chars': min(8000, len(text))}
                evidence_routes[item.get('invocation_id')] = {
                    'request': read_route, 'evidence_chars': len(text),
                }
                if evidence_offset <= position < evidence_offset + 20:
                    index.append({**{key: item.get(key)[:256] if isinstance(item.get(key), str) else item.get(key) for key in (
                        'invocation_id', 'tool_name', 'status', 'source', 'saved_at')},
                        'read_result': {'run_id': run_id, 'section': 'evidence',
                                        'start_char': char_offset, 'max_chars': min(8000, len(text))},
                        'evidence_chars': len(text)})
                    name, invocation = item.get('tool_name'), item.get('invocation_id')
                    registered = self.registry.get_tool_or_none(name) if self.registry and isinstance(name, str) else None
                    if (registered is not None and registered.spec.read_only is True
                            and registered.spec.package != 'bash' and not name.startswith('bash.')
                            and not callable(getattr(registered, 'result_artifact_source', None))
                            and isinstance(invocation, str) and 1 <= len(invocation) <= 256):
                        index[-1]['read_observation'] = {'run_id': run_id, 'section': 'observation',
                                                         'invocation_id': invocation}
                char_offset += len(text) + 1
            count = sum(item.get('status') == 'completed' and item.get('source') == 'tool_result_artifacts' for item in evidence)
            output.update(evidence_index={'items': index, 'total': len(evidence),
                'offset': evidence_offset,
                'next_offset': evidence_offset + 20 if evidence_offset + 20 < len(evidence) else None},
                successful_operation_count=count, successful_operation_count_source='tool_result_artifacts',
                error_type=record.get('error_type'),
                recovery_hint=('Load prior evidence before repeating successful operations. A failed run can retain successful tool results.'
                               if evidence else 'No persisted tool evidence was found for this run.'))
            receipts = []
            for item in evidence:
                receipt = item.get('execution_receipt')
                if not isinstance(receipt, dict):
                    continue
                receipt = dict(receipt)
                route = evidence_routes.get(item.get('invocation_id'))
                if route is not None:
                    receipt['read_evidence'] = route['request']
                    receipt['evidence_chars'] = route['evidence_chars']
                receipts.append(receipt)
            output['execution_receipts'] = self._answer_receipt_preview(receipts)
            output['mail_coverage_receipts'] = self._mail_coverage_preview(mail_receipts)
            snapshot = record.get('result_snapshot') or {}
            output.update(task_outcome=snapshot.get('task_outcome') or 'unknown',
                          stop_reason=snapshot.get('stop_reason'),
                          failure_category=('temporary_resource_deferral' if 'budget_deferred' in {record.get('error_type'), snapshot.get('stop_reason')}
                                            else record.get('error_type')))
            progress = conn.execute("SELECT artifact_id,created_at FROM agent_run_artifacts WHERE run_id=? "
                                    "AND kind='session_progress' ORDER BY updated_at DESC LIMIT 1", (run_id,)).fetchone()
            if progress:
                output['cached_progress'] = {'available': True, 'saved_at': progress['created_at'],
                    'source_run_id': run_id, 'historical_only': True,
                    'read_result': {'run_id': run_id, 'section': 'evidence'}}
            if section == 'observation':
                output['observation'] = self._import_observation(conn, context, record, invocation_id)
                return output
            if not output['terminal']:
                return output
            if section == 'evidence':
                value = '[' + ','.join(serialized) + ']'
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
