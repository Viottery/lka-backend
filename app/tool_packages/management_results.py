"""Bounded task result reads in the existing management namespace."""
from app.tool_packages.management import TEXT, ManagementTool


def management_result_tools(service):
    tool = ManagementTool(service, (
        'read_task_result',
        ('Read an owned independent task/delegated root final answer and actual persisted tool evidence. Includes current run status: read results directly without a preliminary inspect_run call. '
        'Every read includes a bounded evidence_index and successful_operation_count, even for empty or failed answers. Answer reads also include bounded execution_receipts with actual Matter write record IDs/revisions/values and mail_coverage_receipts with permission-validated scan bounds, counts and body-delivery/truncation metadata. Coverage is not understanding or freshness; unknown optional source length does not require another read. Inspect missing/truncated required fields. If these cover the requested acceptance facts, no separate evidence call is needed.  Inspect error_type; a failed run can retain successful operations. Load prior evidence before repeating work. Page the index with evidence_offset. An index read_result selects an event JSON fragment: stop after evidence_chars; next_char refers to the full evidence array. Use receipt read_evidence or section=evidence for a specific missing or conflicting fact; follow next_char for the requested pages (JSON fragments must be '
        'joined before parsing). Projected evidence omits message bodies; do not page that projection repeatedly to find absent source text. For needed read-only source output use an evidence_index read_observation request (section=observation) with invocation_id, then expand observation and read the returned current-run artifact. This does not refresh sources or authorize writes; bash and write outputs remain projected evidence only. Run completion is not business completion. Evidence describes past '
        'execution, not current files, source freshness, or approval for another action. No arbitrary path reads.'),
        True,
        {'run_id': TEXT, 'section': {'type': 'string', 'enum': ['answer', 'evidence', 'observation']},
         'start_char': {'type': 'integer', 'minimum': 0},
         'invocation_id': TEXT,
         'evidence_offset': {'type': 'integer', 'minimum': 0},
         'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 8000}},
        ['run_id'],
    ))
    tool.spec.output_selected_content = True
    return [tool]
