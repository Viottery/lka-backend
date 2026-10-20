"""Delegate tools use the existing mandatory management safety gate."""
from app.tool_packages.management import COMMAND, PAGE, PURPOSE, TEXT, ManagementTool


def management_delegate_tools(service):
    definitions = [
        ('delegate_work', ('Create a durable child manager for a bounded objective. The child cannot delegate again. '
         'Default execution tools are a frozen read-only non-management whitelist; explicit names freeze another whitelist. '
         'workspace_path selects the default directory, not an access boundary; tasks may select their own directory. '
         'Outside-workspace access requires per-call review and high-risk operations require user approval.'), False,
         {'command_id': COMMAND, 'objective': PURPOSE, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 40},
          'workspace_path': {'type': 'string', 'minLength': 1, 'maxLength': 4096,
                             'description': 'Existing default working directory; omit to inherit the frontend default independent workspace.'},
          'allowed_tools': {'type': 'array', 'maxItems': 256, 'items': TEXT}}, ['command_id', 'objective']),
        ('instruct_delegate', ('Send a bounded follow-up to an owned delegate after its current manager root is terminal. '
         'Requires the observed delegate revision. Original approval/question control stays with the user.'), False,
         {'command_id': COMMAND, 'delegate_id': TEXT, 'expected_revision': {'type': 'integer', 'minimum': 1},
          'instruction': PURPOSE}, ['command_id', 'delegate_id', 'expected_revision', 'instruction']),
        ('control_delegate', ('Pause or cancel an owned delegate and cancel its unfinished manager/task roots. '
         'Pausing fences further steps; it does not promise a resumable checkpoint. After roots terminate, '
         'use instruct_delegate to reactivate a paused line with a new bounded instruction.'), False,
         {'command_id': COMMAND, 'delegate_id': TEXT, 'expected_revision': {'type': 'integer', 'minimum': 1},
          'action': {'type': 'string', 'enum': ['pause', 'cancel']}},
         ['command_id', 'delegate_id', 'expected_revision', 'action']),
        ('list_delegates', 'List owned delegate summaries or this child manager\'s own delegation.', True, PAGE, []),
        ('read_delegate', 'Read a bounded page of objective and published reports, including evidence references.', True,
         {'delegate_id': TEXT, 'start_char': {'type': 'integer', 'minimum': 0}}, ['delegate_id']),
        ('report_delegate', ('Propose a report to the parent. Only successful autonomous publication delivers it. '
         'Completion requires all owned ordinary tasks to be terminal. Evidence is owned task references, never source content.'), False,
         {'command_id': COMMAND, 'kind': {'type': 'string', 'enum': ['progress', 'completed', 'needs_decision', 'blocked']},
          'summary': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
          'evidence': {'type': 'array', 'maxItems': 20, 'items': {'type': 'object', 'minProperties': 1,
                       'properties': {'session_id': TEXT, 'run_id': TEXT, 'message_id': TEXT}, 'additionalProperties': False}}},
         ['command_id', 'kind', 'summary']),
    ]
    return [ManagementTool(service, definition) for definition in definitions]
