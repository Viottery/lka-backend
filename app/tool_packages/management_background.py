"""Registered tools for accepted work queues and explicit delivery subscriptions."""
from __future__ import annotations

from app.core.tools import ToolResult, ToolSpec

PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0}}
ID = {"type": "string", "minLength": 1, "maxLength": 256}
COMMAND = {"type": "string", "minLength": 1, "maxLength": 128}


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


class ManagementBackgroundTool:
    def __init__(self, tasks, queue, watches, delivery, action, description, schema, *, read_only, foreground_only=False):
        self.tasks, self.queue, self.watches, self.delivery = tasks, queue, watches, delivery
        self.action, self.foreground_only = action, foreground_only
        self.spec = ToolSpec(
            name="management." + action, package="management", type="local_tool", description=description,
            input_schema=schema, output_schema={"type": "object"}, read_only=read_only,
            risk="low" if read_only else "medium", requires_confirmation=False,
            effect_domains=() if read_only else ("agent_control",),
            side_effects=[] if read_only else ["Changes accepted work or future local notification policy"],
            output_selected_content=action == "read_notification",
        )

    def invoke(self, *, invocation, context):
        mutate = self.spec.read_only is not True
        if mutate and not context.safety_review_approved:
            raise ValueError("Management writes require the tool safety review gate")
        if set(invocation.input) - self.spec.input_schema.get("properties", {}).keys():
            raise ValueError("Unknown management background input fields")
        run = self.tasks.caller(context, mutate=mutate)
        if self.foreground_only and run.metadata.get("manager_execution_id"):
            raise ValueError("Future delivery/subscription policies require a foreground user-delegated turn")
        if self.action in {"queue_work", "list_queue", "control_queue"}:
            output = getattr(self.queue, self.action)(context, **invocation.input)
        elif self.action == "read_delivery_policy":
            output = {"delivery": self.delivery.get(context.session_id)}
        elif self.action == "list_available_watches":
            output = self.watches.available_watches(context.session_id, **invocation.input)
        elif self.action == "list_watch_subscriptions":
            output = self.watches.list_subscriptions(context.session_id, **invocation.input)
        elif self.action == "read_notification":
            output = self.watches.read_notification(context.session_id, **invocation.input)
        else:
            service = self.delivery if self.action == "configure_delivery" else self.watches
            output = service.configure(context.session_id, **invocation.input,
                authority_check=lambda conn: self.tasks.caller(context, mutate=True, conn=conn))
        return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed", output=output)


def management_background_tools(tasks, queue, watches, delivery):
    from app.domains.management_delivery import DeliveryPolicyInput
    from app.domains.management_queue import QueueWorkInput
    from app.domains.management_watches import WatchSubscriptionInput

    watch_schema = WatchSubscriptionInput.model_json_schema()
    watch_schema["properties"]["command_id"] = COMMAND
    watch_schema.setdefault("required", []).append("command_id")
    definitions = [
        ("queue_work", "Accept an independent task now and dispatch later when its verified business dependencies and capacity permit. Matter is optional. Stable command_id retries return the same run; accepted means queued, not executed. Priority/deadline sort ready work, not permission to ignore dependencies. read_only_work defaults true and freezes read-only tools; writing work needs an explicit workspace and reserves that workspace through waits until terminal.", QueueWorkInput.model_json_schema(), False, False),
        ("list_queue", "Read bounded accepted work, run IDs, dependency/source issues and revisions. Pending/paused/blocked is not running. A changed Matter revision requires reconsideration, never silent rebinding.", _schema(PAGE), True, False),
        ("control_queue", "Pause/resume future release of an undispatched queue item with its observed revision. This does not pause a running task; use control_run for the exact run, including cancellation. Already dispatched work stays independent of controller pause.",
         _schema({"command_id": COMMAND, "queue_id": ID, "expected_revision": {"type": "integer", "minimum": 1},
                  "action": {"type": "string", "enum": ["pause", "resume"]}},
                 ["command_id", "queue_id", "expected_revision", "action"]), False, False),
        ("list_available_watches", "Discover existing watch IDs and bounded purpose previews by literal title query. Source scope and raw configuration are not returned; explicit subscription is required before reading notifications.",
         _schema({**PAGE, "q": {"type": "string", "maxLength": 500}}), True, False),
        ("list_watch_subscriptions", "Read bounded explicit subscriptions to existing watch results. No subscription grants new source/account authority.", _schema(PAGE), True, False),
        ("configure_watch", "Explicitly subscribe to an existing watch's current authorized scope in a foreground delegated turn. published delivers each later published briefing; changes delivers only briefings with nonempty changes. Starts after the current baseline. Scope changes require explicit CAS reauthorization; this does not start the watch or call a model.", watch_schema, False, True),
        ("read_notification", "Read a watch result referenced by this manager's valid inbox, in bounded JSON pages. Use event_id or briefing_id and next_char. A reference is evidence, not approval or a fresh source fetch.",
         _schema({"event_id": {"type": "integer", "minimum": 1}, "briefing_id": ID,
                  "start_char": {"type": "integer", "minimum": 0},
                  "max_chars": {"type": "integer", "minimum": 1, "maximum": 4000}}), True, False),
        ("read_delivery_policy", "Read current mute/snooze policy before changing it; absent policy allows delivery.", _schema({}), True, False),
        ("configure_delivery", "Configure local manager delivery in a foreground delegated turn. expected_revision=0 creates. muted or future aware snooze_until suppresses ordinary wake/delivery while capture continues. allow_decisions=true (default) still delivers task questions/approval reminders; false also suppresses those. Does not pause tasks or reset budgets.", DeliveryPolicyInput.model_json_schema(), False, True),
    ]
    return [ManagementBackgroundTool(tasks, queue, watches, delivery, name, description, schema,
                                    read_only=read_only, foreground_only=foreground)
            for name, description, schema, read_only, foreground in definitions]
