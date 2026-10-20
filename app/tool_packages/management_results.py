"""Bounded task result reads in the existing management namespace."""
from app.tool_packages.management import TEXT, ManagementTool


def management_result_tools(service):
    tool = ManagementTool(service, (
        'read_task_result',
        ('Read an owned independent task/delegated root final answer and actual persisted tool evidence. Includes current run status: read results directly without a preliminary inspect_run call. '
        'Use section=evidence to verify operations; follow next_char for all pages (JSON fragments must be '
        'joined before parsing). Run completion is not business completion. Evidence describes past '
        'execution, not current files, source freshness, or approval for another action. No arbitrary path reads.'),
        True,
        {'run_id': TEXT, 'section': {'type': 'string', 'enum': ['answer', 'evidence']},
         'start_char': {'type': 'integer', 'minimum': 0},
         'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 8000}},
        ['run_id'],
    ))
    tool.spec.output_selected_content = True
    return [tool]
