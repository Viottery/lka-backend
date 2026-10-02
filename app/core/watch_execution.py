"""Safe, bounded Child Agent adapter for one daily watch occurrence.

This module prepares an isolated Child Run with a server-owned immutable
ToolView. It deliberately does not schedule occurrences or persist briefings.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core.context_driver import (
    ContextDerivationStatus,
    ContextDriver,
    ContextRequest,
    ContextViewMode,
)
from app.core.llm_workloads import workload_scope
from app.core.multi_agent import (
    Plan,
    PlanStatus,
    PlanStep,
    RuntimeBudget,
    ScopeGrant,
    SideEffectLevel,
    TaskResultStatus,
)

WATCH_READ_ONLY_TOOL_ALLOWLIST = frozenset(
    {
        "mail.search",
        "mail.load_messages",
        "knowledge.search",
        "knowledge.load_chunks",
        "knowledge.load_document",
        "web.search",
        "web.open",
        "matter.search",
        "matter.list",
        "filesystem.read_file",
    }
)
# The child budget accounts for prompt input as well as generated tokens. The
# regular harness includes its package/tool contract in each call, so 10k was
# insufficient even for one authorized mailbox search/load/finalize cycle.
WATCH_MAX_TOKENS = 30_000
WATCH_MAX_LLM_CALLS = 6
WATCH_MAX_TOOL_CALLS = 8
WATCH_MAX_WALL_TIME_SECONDS = 120


class WatchExecutionResult(BaseModel):
    """Typed outcome and evidence references for a single watch follow-up."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskResultStatus
    summary: str = Field(min_length=1)
    run_id: str
    child_run_id: str
    snapshot_id: str
    evidence_refs: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    evidence_metadata: tuple[dict[str, Any], ...] = ()
    retrieval_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    failure_category: str | None = None


class WatchExecutionAdapter:
    """Run one explicitly scoped watch task through the regular Child Agent."""

    def __init__(
        self,
        *,
        runtime: Any,
        context_driver: ContextDriver | None = None,
        policy_version: str = "watch_read_only_v1",
        permission_version: str = "watch_explicit_grants_v1",
    ) -> None:
        self.runtime = runtime
        self.context_driver = context_driver or ContextDriver()
        self.policy_version = policy_version
        self.permission_version = permission_version

    async def run_async(
        self,
        *,
        parent_run_id: str,
        watch_id: str,
        goal: str,
        source_ids: tuple[str, ...] = (),
        account_ids: tuple[str, ...] = (),
        web_enabled: bool = False,
        matter_enabled: bool = False,
        workspace_paths: tuple[str, ...] = (),
        knowledge_enabled: bool = True,
        instruction_tools_enabled: bool = True,
        llm_client_name: str | None = None,
        llm_model: str | None = None,
    ) -> WatchExecutionResult:
        """Execute one bounded read-only Child Agent run for a watch."""

        if not goal.strip():
            raise ValueError("Watch goal must not be empty.")
        if not watch_id.strip():
            raise ValueError("watch_id must not be empty.")

        run_manager = self.runtime.agent_run_manager
        registry = self.runtime.tool_registry
        parent = run_manager.get_run(parent_run_id)
        if parent is None:
            raise KeyError(f"Parent Agent run not found: {parent_run_id}")

        requested_tools = set(WATCH_READ_ONLY_TOOL_ALLOWLIST)
        if not source_ids:
            requested_tools.difference_update(
                {
                    "knowledge.search",
                    "knowledge.load_chunks",
                    "knowledge.load_document",
                }
            )
        if not knowledge_enabled:
            requested_tools.difference_update(
                {
                    "knowledge.search",
                    "knowledge.load_chunks",
                    "knowledge.load_document",
                }
            )
        if not source_ids or not account_ids:
            requested_tools.difference_update(
                {
                    "mail.search",
                    "mail.load_messages",
                    "matter.search",
                    "matter.list",
                }
            )
        if not web_enabled:
            requested_tools.difference_update({"web.search", "web.open"})
        if not matter_enabled:
            requested_tools.difference_update({"matter.search", "matter.list"})
        if not workspace_paths:
            requested_tools.discard("filesystem.read_file")
        elif "filesystem.read_file" in requested_tools:
            settings = getattr(self.runtime, "settings", None)
            configured_roots = (
                settings.parsed_workspace_roots()
                if settings is not None
                and callable(getattr(settings, "parsed_workspace_roots", None))
                else ()
            )
            roots = tuple(Path(root).resolve(strict=False) for root in configured_roots)
            if not roots:
                raise ValueError(
                    "Workspace watch scope is unavailable without configured workspace roots."
                )
            resolved_paths = tuple(Path(path).resolve(strict=False) for path in workspace_paths)
            if any(
                not any(path == root or root in path.parents for root in roots)
                for path in resolved_paths
            ):
                raise ValueError(
                    "Workspace watch paths must stay within configured workspace roots."
                )
            workspace_paths = tuple(path.as_posix() for path in resolved_paths)
        if not requested_tools:
            raise ValueError("Watch has no authorized read-only information source.")
        if instruction_tools_enabled:
            requested_tools.update({"instructions.read", "instructions.search"})
        registered_read_tools = {
            spec.name
            for spec in registry.list_tools()
            if spec.name in requested_tools and spec.read_only is True and spec.package
        }
        packages = tuple(
            sorted(
                {
                    spec.package
                    for spec in registry.list_tools()
                    if spec.name in registered_read_tools and spec.package
                }
            )
        )
        allowed_tools = tuple(sorted(registered_read_tools))
        if not allowed_tools:
            raise ValueError("Watch has no registered read-only information source.")

        plan_id = f"watch_plan_{uuid4().hex}"
        step_id = "daily_follow_up"
        parent_scope = ScopeGrant(
            workspace_paths=tuple(sorted(set(workspace_paths))),
            source_ids=tuple(sorted(set(source_ids))),
            account_ids=tuple(sorted(set(account_ids))),
            allowed_packages=packages,
            allowed_tools=allowed_tools,
            side_effect_level=SideEffectLevel.READ,
        )
        plan_step = PlanStep(
            correlation_id=parent.trace_id,
            step_id=step_id,
            objective=goal.strip(),
            output_contract=(
                "A concise watch update summary with evidence references, distinguishing "
                "new changes, unchanged information, unconfirmed information, and any "
                "decision needed. Return one JSON object with keys summary, changes, unchanged, "
                "unconfirmed, decisions. Each array item must include a concise claim and "
                "evidence_refs containing exact collected evidence IDs or source URLs. "
                "Do not claim completeness from bounded search results; zero search results "
                "mean unconfirmed, never unchanged. Include event_id for stable external IDs "
                "and importance when a numeric score is justified.\n\n"
                "JSON Schema:\n```json-schema\n"
                + json.dumps(
                    {
                        "type": "object",
                        "required": ["summary", "changes", "unchanged", "unconfirmed", "decisions"],
                        "additionalProperties": False,
                        "properties": {
                            "summary": {"type": "string", "minLength": 1},
                            **{
                                section: {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["claim", "evidence_refs"],
                                        "properties": {
                                            "claim": {"type": "string", "minLength": 1},
                                            "evidence_refs": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {"type": "string", "minLength": 1},
                                            },
                                        },
                                    },
                                }
                                for section in ("changes", "unchanged", "unconfirmed", "decisions")
                            },
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n```"
            ),
            allowed_packages=packages,
            allowed_tools=allowed_tools,
            side_effect_level=SideEffectLevel.READ,
            budget=RuntimeBudget(
                max_tokens=WATCH_MAX_TOKENS,
                max_llm_calls=WATCH_MAX_LLM_CALLS,
                max_tool_calls=WATCH_MAX_TOOL_CALLS,
                max_wall_time_seconds=WATCH_MAX_WALL_TIME_SECONDS,
            ),
        )
        plan = Plan(
            correlation_id=parent.trace_id,
            plan_id=plan_id,
            parent_run_id=parent_run_id,
            session_id=parent.session_id,
            objective=f"Daily watch follow-up {watch_id}",
            steps=(plan_step,),
            status=PlanStatus.VALIDATED,
        )
        run_manager.record_multi_agent_plan(
            parent_run_id,
            event_type="watch_child_plan_created",
            payload={"operation_id": plan_id, "watch_id": watch_id},
            plan=plan.model_dump(mode="json"),
        )
        child = run_manager.create_child_run(
            parent_run_id=parent_run_id,
            plan_id=plan_id,
            step_id=step_id,
            attempt=1,
            user_input=goal.strip(),
        )
        bounded_budget = RuntimeBudget(
            max_tokens=WATCH_MAX_TOKENS,
            max_llm_calls=WATCH_MAX_LLM_CALLS,
            max_tool_calls=WATCH_MAX_TOOL_CALLS,
            max_wall_time_seconds=WATCH_MAX_WALL_TIME_SECONDS,
        )
        derived = await self.context_driver.derive(
            ContextRequest(
                snapshot_id=f"watch_snapshot_{uuid4().hex}",
                child_run_id=child.run_id,
                parent_run_id=parent_run_id,
                session_id=child.session_id,
                plan_id=plan_id,
                plan_step=plan_step,
                parent_effective_scope=parent_scope,
                session_scope=parent_scope,
                workspace_scope=parent_scope,
                policy_scope=parent_scope,
                budget=bounded_budget,
                policy_version=self.policy_version,
                workspace_version="watch_no_workspace_v1",
                permission_version=self.permission_version,
                expires_in_seconds=WATCH_MAX_WALL_TIME_SECONDS,
                view_mode=ContextViewMode.WORKING,
                evidence_budget_tokens=0,
            )
        )
        if (
            derived.status != ContextDerivationStatus.READY
            or derived.snapshot is None
            or derived.views is None
        ):
            run_manager.fail_child_run(
                child.run_id,
                error_type="watch_context",
                error=derived.reason or derived.status.value,
            )
            raise RuntimeError(
                f"Could not derive watch execution context: {derived.reason or derived.status.value}"
            )

        with workload_scope(
            "background_io",
            task_id=f"watch:{watch_id}:{child.run_id}",
            max_tokens=WATCH_MAX_TOKENS,
        ):
            task_result = await self.runtime.run_child_agent_async(
                child_run_id=child.run_id,
                snapshot=derived.snapshot,
                views=derived.views,
                llm_client_name=llm_client_name,
                llm_model=llm_model,
            )
        evidence_pairs = [(ref.evidence_id, ref.source_ref) for ref in task_result.evidence_refs]
        retrieval_failures: list[str] = []
        metadata_by_ref: dict[str, dict[str, Any]] = {}
        session_service = getattr(self.runtime, "session_service", None)
        if session_service is not None:
            try:
                messages = session_service.get_session(session_id=child.session_id).messages
            except KeyError:
                messages = ()
            for message in messages:
                if message.role != "agent" or message.payload.get("run_id") != child.run_id:
                    continue
                for event in message.payload.get("tool_events", []):
                    if not isinstance(event, dict):
                        continue
                    result = event.get("result", {})
                    if not isinstance(result, dict):
                        continue
                    if result.get("status") in {"failed", "rejected"}:
                        retrieval_failures.append(
                            f"{event.get('tool_name', 'unknown_tool')}:{result['status']}"
                        )
                        continue
                    if (
                        event.get("tool_name") == "web.search"
                        and result.get("status") == "completed"
                    ):
                        output = result.get("output", {})
                        rows = output.get("results", []) if isinstance(output, dict) else []
                        for row in rows if isinstance(rows, list) else []:
                            if isinstance(row, dict) and isinstance(row.get("url"), str):
                                url = row["url"]
                                evidence_id = f"web_{sha256(url.encode()).hexdigest()[:16]}"
                                evidence_pairs.append((evidence_id, url))
                                metadata_by_ref[row["url"]] = {
                                    "source_time": row.get("published_at"),
                                    "fetched_at": row.get("provider_fetched_at"),
                                    "excerpt": str(row.get("snippet") or "")[:1200],
                                }
                    if (
                        event.get("tool_name") in {"mail.search", "mail.load_messages"}
                        and result.get("status") == "completed"
                    ):
                        output = result.get("output", {})
                        rows = output.get("messages", []) if isinstance(output, dict) else []
                        for row in rows if isinstance(rows, list) else []:
                            if not isinstance(row, dict):
                                continue
                            message_id = row.get("message_id")
                            if not isinstance(message_id, str) or not message_id:
                                continue
                            source_ref = row.get("source_ref") or f"mail_message:{message_id}"
                            evidence_pairs.append((message_id, str(source_ref)))
                            metadata = {
                                "source_time": row.get("received_at"),
                                "excerpt": str(row.get("body_text") or row.get("snippet") or "")[
                                    :1200
                                ],
                            }
                            metadata_by_ref[message_id] = metadata
                            metadata_by_ref[str(source_ref)] = metadata
                    if event.get("tool_name") != "web.open" or result.get("status") != "completed":
                        continue
                    output = result.get("output", {})
                    url = output.get("url") if isinstance(output, dict) else None
                    if isinstance(url, str) and url.startswith("https://"):
                        evidence_pairs.append((f"web_{sha256(url.encode()).hexdigest()[:16]}", url))
                        metadata_by_ref[url] = {
                            "fetched_at": output.get("fetched_at"),
                            "excerpt": str(output.get("text") or "")[:1200],
                        }
                    if len(evidence_pairs) >= 20:
                        break
                break
        evidence_pairs = list(dict.fromkeys(evidence_pairs))[:20]
        return WatchExecutionResult(
            status=task_result.status,
            summary=task_result.summary,
            run_id=parent_run_id,
            child_run_id=task_result.child_run_id,
            snapshot_id=task_result.snapshot_id,
            evidence_refs=tuple(ref for ref, _ in evidence_pairs),
            evidence_sources=tuple(source for _, source in evidence_pairs),
            evidence_metadata=tuple(
                {key: value for key, value in metadata_by_ref.get(source, {}).items() if value}
                for _, source in evidence_pairs
            ),
            retrieval_failures=tuple(dict.fromkeys(retrieval_failures)),
            warnings=task_result.warnings,
            failure_category=(task_result.failure.category if task_result.failure else None),
        )
