"""Management tools reuse the generic executor and its mandatory review gate."""
from __future__ import annotations

from contextlib import closing

from app.core.tools import ToolPackageSpec, ToolResult, ToolSpec

MANAGEMENT_PACKAGE = ToolPackageSpec(
    name="management", description="Manage durable goals, discover ordinary sessions, start independent tasks and safely intervene.",
    risk="medium", requires_expansion=True,
    decision_hints=[
        "User-facing updates should focus on outcomes, blockers, decisions and evidence. Include internal session/run IDs only when needed for user control or requested debugging.",
        "Observe the target run before control; latest_run from a session read is sufficient when available. Use stable command_id values for retries.",
        "Session discovery and message reads already include latest_run snapshots. Reuse them; list_runs is for history and inspect_run for a separately requested run.",
        "Command status=accepted is a receipt, not a run state. Describe its run_snapshot, not the state observed before the command; replacement queues a new run and retires the old one.",
        "delegate_work status=accepted and state=active only confirm the durable line is enabled; its first run may not exist yet. Use read_delegate.latest_run to describe queued/running/completed execution accurately.",
        "Command snapshots are frozen at snapshot_at and remain identical on retries. Use a new read for current status. pause_requested is not a paused acknowledgement.",
        "Pause a running task, wait until paused, then send_instruction with expected_run_id.",
        "Tasks remain independent after this manager turn ends. Explicit work-line delegation continues and returns without the ordinary autonomy switch; other terminal task events and goal timers require autonomous management.",
        "set_goal persists foreground changes immediately. In an autonomous execution it stages a proposal: status=proposed is not yet applied; publication applies it atomically with the answer. Use one proposal per goal per execution. A failed prior proposal requires reading the current goal and a new command_id.",
        "A goal timer is one-shot. Set an explicit timezone-aware next_wake_at for a future check, or wait_run_id for a task terminal event. Inspect long objectives with goal_id and start_char.",
        "Treat Matter as optional business memory, not a requirement for a simple query. Use overview/search to reuse existing work; internal abstract responsibilities may have no deadline. Promote the same Matter ID for a concrete user commitment.",
        "Before delegation, carry all relevant user acceptance criteria and shared constraints into each line's objective and execution instructions. On return verify each criterion from actual values/evidence; source IDs and successful runs alone are insufficient. Missing or conflicting facts require targeted follow-up or a decision, never an assumed pass.",
        "For multiple independent responsibilities, use delegate_work for each durable work line. Its subagent can manage several own execution sessions and report_delegate to wake a fresh parent turn after this turn ends. Read reports with read_delegate; use instruct_delegate for follow-up on an idle line and control_delegate to stop a line. Use direct start_task only for a bounded individual execution.",
        "Within a delegated line, start bounded independent tasks with explicit deliverables, scope and evidence. Use start_work to atomically gate business dependencies and bind the new run; run completed alone is not verified business completion. Long waits belong in independent tasks, so this manager turn can return.",
        "Read assigned task results before review. Accepted review returns a completion_update, not an already completed Matter; autonomous review must first publish before a later organize_work completion update.",
        "Use queue_work for dependent work or mutually exclusive write resources; acceptance is durable even while waiting. Inspect list_queue issues before retrying; never silently rebind a changed Matter or reset budgets. Queued instruction changes require cancellation and a new command; resume keeps the original policy.",
        "For existing watches, use list_available_watches then configure_watch in a foreground delegated turn. Only subscribed published results may be read_notification evidence. configure_delivery mutes/snoozes automatic delivery while capture continues, with an explicit decision-reminder exception.",
        "Use read_policies before explicit foreground configure_attention/configure_schedule. Repeated observation without changed facts should not produce new work; requested periodic summaries remain scheduled deliveries.",
        "Controller must be active for writes. Waiting confirmations and user questions require the original user control path.",
    ],
)

TEXT = {"type": "string", "minLength": 1, "maxLength": 256}
COMMAND = {"type": "string", "minLength": 1, "maxLength": 128}
PURPOSE = {"type": "string", "minLength": 1, "maxLength": 16000}
PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0}}

DEFINITIONS = [
    ("list_goals", "Read this manager's durable goals, revisions, timers and task waiting conditions.", True,
     {**PAGE}, []),
    ("read_goal", "Read the full goal objective in pages after a truncated list preview.", True,
     {"goal_id": TEXT, "start_char": {"type": "integer", "minimum": 0}}, ["goal_id"]),
    ("read_events", "Read this manager's ordered durable timer/task event inbox; consumed_execution_id identifies published evidence.", True,
     {"after": {"type": "integer", "minimum": 0}, "limit": PAGE["limit"]}, []),
    ("set_goal", "Create or revise a durable goal. Omit goal_id to create; updates require expected_revision. Omitted fields keep current values; null clears a timer or task wait. Autonomous changes are proposals applied only with successful final publication.", False,
     {"command_id": COMMAND, "goal_id": TEXT, "expected_revision": {"type": "integer", "minimum": 1},
      "objective": PURPOSE, "status": {"type": "string", "enum": ["active", "waiting", "paused", "completed", "cancelled"]},
      "next_wake_at": {"anyOf": [{"type": "string", "maxLength": 100}, {"type": "null"}]},
      "wait_run_id": {"anyOf": [TEXT, {"type": "null"}]}}, ["command_id"]),
    ("list_sessions", "Find ordinary sessions by literal title substring; returns each latest_run status snapshot and next_offset. No extra run query is needed when that snapshot answers the question.", True,
     {**PAGE, "q": {"type": "string", "maxLength": 500}}, []),
    ("read_session", "Read paged messages and latest_run state in one snapshot; use both for task results/status without a separate inspect/list_runs. Continue truncated content with message_id and start_char=next_char.", True,
     {**PAGE, "session_id": TEXT, "message_id": TEXT,
      "start_char": {"type": "integer", "minimum": 0}}, ["session_id"]),
    ("list_runs", "Discover a session's root runs newest first, including runs started from another device.", True,
     {**PAGE, "session_id": TEXT}, ["session_id"]),
    ("inspect_run", "Inspect task status and pause acknowledgement; read_session provides full result text.", True,
     {"run_id": TEXT}, ["run_id"]),
    ("start_task", "Create an independent ordinary session and durable resumable task. Original user quotes are attached by the server.", False,
     {"command_id": COMMAND, "purpose": PURPOSE, "title": {"type": "string", "minLength": 1, "maxLength": 40},
      "workspace_path": {"type": "string", "minLength": 1, "maxLength": 4096}}, ["command_id", "purpose"]),
    ("send_instruction", "Start the next turn, or replace a checkpoint-paused run. Receipt run_snapshot describes the NEW queued run; replaced_run_snapshot is the retired old run, not current paused state. Running tasks must acknowledge pause first. expected_run_id binds the latest observed root; null requires an empty run history.", False,
     {"command_id": COMMAND, "purpose": PURPOSE, "session_id": TEXT,
      "expected_run_id": {"anyOf": [TEXT, {"type": "null"}]}},
     ["command_id", "purpose", "session_id", "expected_run_id"]),
    ("control_run", "Request pause, enqueue resume of a paused task, or cancel an explicit task root. Does not approve confirmations or answer user questions.", False,
     {"command_id": COMMAND, "run_id": TEXT, "action": {"type": "string", "enum": ["pause", "resume", "cancel"]}},
     ["command_id", "run_id", "action"]),
]


class ManagementTool:
    def __init__(self, service, definition):
        name, description, read_only, properties, required = definition
        self.service, self.action = service, name
        self.spec = ToolSpec(
            name="management." + name, package="management", type="local_tool", description=description,
            risk="low" if read_only else "medium", read_only=read_only, requires_confirmation=False,
            side_effects=[] if read_only else ["Creates or controls independent task runs"],
            effect_domains=() if read_only else ("agent_control",),
            input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            output_selected_content=name == "read_session",
            output_schema={"type": "object"},
        )

    def invoke(self, *, invocation, context):
        if not self.spec.read_only and not context.safety_review_approved:
            raise ValueError("Management writes require the tool safety review gate")
        if self.action == "list_runs":
            self.service.caller(context)
            with closing(self.service._conn()) as conn:
                self.service._target(conn, invocation.input["session_id"], manager_session_id=context.session_id)
            output = self.service.list_runs(context=context, **invocation.input)
        else:
            output = getattr(self.service, self.action)(context, **invocation.input)
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                          status="completed", output=output)


def management_tools(service):
    return [ManagementTool(service, definition) for definition in DEFINITIONS]


def management_work_tools(service):
    """Matter-aware tools retain the same executor/review path as task control."""
    from app.domains.management_work import OrganizeWorkInput

    definitions = [
        ("overview", "Read bounded work summaries, user/internal visibility, management state and latest task evidence. Ordinary questions need no new work item.", True,
         {**PAGE, "visibility": {"type": "string", "enum": ["user", "internal", "all"]}}, []),
        ("inspect_work", "Read a Matter, its management sidecar and supports/depends_on links. Business title, status and deadline come from Matter.", True,
         {"matter_id": TEXT}, ["matter_id"]),
    ]
    tools = [ManagementTool(service, item) for item in definitions]
    tool = ManagementTool(service, (
        "organize_work", "Atomically organize up to 20 work operations: create, CAS update, supports/depends_on link or bind_goal. Internal work may have no deadline. Use user visibility for concrete user-facing commitments. Autonomous receipts are proposed until final publication, not applied. Bound goal business changes must go through this tool, not set_goal.",
        False, {}, [],
    ))
    tool.spec.input_schema = OrganizeWorkInput.model_json_schema()
    tool.spec.effect_domains = ("matters",)
    tool.spec.side_effects = ["Creates or revises Matters and their management relationships"]
    tools.append(tool)
    return tools


def management_assignment_tools(service):
    from app.domains.management_assignments import AssignmentInput, ReviewInput, StartWorkInput

    definitions = [
        ("readiness", "Read depends_on readiness before starting dependent work. Dependencies require Matter done plus an accepted review of that revision; supports links do not block. This does not stop an already-started unbound run.", True, {"matter_id": TEXT}, ["matter_id"]),
        ("result_view", "Read bounded execution bindings and latest verification. Run completed does not imply business complete. Follow read_result to read the actual messages.", True, {"matter_id": TEXT}, ["matter_id"]),
        ("assign_existing", "Bind an existing root owned by this manager to a Matter revision, rejecting unready dependencies. This does not start an execution; inspect readiness before start_task. Autonomous writes are proposed until publication.", False, {}, []),
        ("review", "Record evidence-based verification of an assigned completed run. Evidence must reference a real agent message with the exact run provenance. accepted returns completion_update; apply that separately with organize_work. No automatic Matter completion. Autonomous review is proposed until publication.", False, {}, []),
    ]
    tools = [ManagementTool(service, item) for item in definitions]
    tools[2].spec.input_schema = AssignmentInput.model_json_schema()
    tools[3].spec.input_schema = ReviewInput.model_json_schema()
    start = ManagementTool(service, (
        "start_work", "Atomically check a Matter revision and verified dependencies, create an independent task, and bind it. Prefer this for managed work; simple tasks need not create Matters. Stable command_id retries return the original accepted task. Task acceptance is immediate and survives this manager turn; it is not business completion.",
        False, {}, [],
    ))
    start.spec.input_schema = StartWorkInput.model_json_schema()
    tools.append(start)
    return tools
