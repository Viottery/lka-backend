"""Private same-session progress index and revocable historical-result imports.

The index is a local continuation aid, never a fresh observation, a completion
verdict, or an authorization grant. Raw results remain run-scoped artifacts.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from app.core.answer_evidence import cached_result_read_routes
from app.core.tools import ToolContext, effective_tool_read_only, tool_scope_discovery_denial


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _terms(value: str) -> set[str]:
    # Character bigrams also make short Chinese continuation requests useful.
    terms = set(re.findall(r"[\w./:-]{2,}", value.lower()))
    terms.update(value[index:index + 2] for index in range(len(value) - 1)
                 if all("\u3400" <= char <= "\u9fff" for char in value[index:index + 2]))
    return terms


class SessionProgressCacheService:
    def __init__(self, *, store: Any, session_service: Any, registry: Any, run_manager: Any) -> None:
        self.store, self.session_service = store, session_service
        self.registry, self.run_manager = registry, run_manager
        self.turn_loop: Any = None
        # Every read/search/group of an imported artifact goes through the same
        # store method; process restart without this binding fails closed.
        store.progress_import_validator = self.can_read_import

    def bind_turn_loop(self, turn_loop: Any) -> None:
        self.turn_loop = turn_loop

    def _run(self, run_id: str) -> dict[str, Any]:
        run = self.store.load_run(run_id)
        if not isinstance(run, dict):
            raise ValueError("Progress source run is unavailable.")  # noqa: TRY004 - missing record denies access
        return run

    def _boundary(self, context: ToolContext) -> dict[str, Any]:
        if self.turn_loop is None or not context.run_id:
            raise ValueError("Progress requires a bound current run.")
        run = self._run(context.run_id)
        session = self.session_service.get_session_or_none(session_id=context.session_id)
        if (session is None or session.status != "active" or run.get("session_id") != context.session_id
                or run.get("parent_run_id") is not None):
            raise ValueError("Progress is available only to a live same-session root.")
        view = self.turn_loop._tool_view_for_run(context.run_id)
        if view != context.tool_view or view is not None and view.child_run_id is not None:
            raise ValueError("Progress does not expand a child context.")
        workspace = self.turn_loop._workspace_for_run(session, context.run_id)
        if workspace != context.workspace_root:
            raise ValueError("Current workspace does not match the run.")
        scope = self.turn_loop._current_observation_cache_scope(context)
        # Scope equality is deliberately conservative. Invocation IDs/snapshot
        # IDs do not grant source authority; permission/workspace versions do.
        return {
            "session_id": session.session_id, "session_created_at": session.created_at,
            "history_reset_at": session.metadata.get("history_reset_at"),
            "history_generation": session.metadata.get("history_generation"),
            "workspace": workspace,
            "session_workspace": session.workspace.model_dump(mode="json") if session.workspace else None,
            "project_id": session.project_id or session.metadata.get("project_id"),
            "policy": self.session_service.run_policy(session.session_id).model_dump(mode="json"),
            "scope": scope,
        }

    def _context_for_run(self, run_id: str) -> ToolContext:
        run = self._run(run_id)
        session = self.session_service.get_session_or_none(session_id=run["session_id"])
        if session is None or self.turn_loop is None:
            raise ValueError("Progress session is unavailable.")
        return ToolContext(session_id=run["session_id"], run_id=run_id,
                           workspace_root=self.turn_loop._workspace_for_run(session, run_id),
                           tool_view=self.turn_loop._tool_view_for_run(run_id))

    def record(self, *, context: ToolContext, tool_events: list[dict[str, Any]],
               working_state: dict[str, Any] | None = None) -> None:
        try:
            boundary = self._boundary(context)
        except (ValueError, KeyError):
            return
        run = self._run(str(context.run_id))
        now = datetime.now(UTC).isoformat()
        old = self.store.load_artifact(f"session_progress_{context.run_id}") or {}
        if old and old.get("boundary") != boundary:
            # Never rebind old events to a new workspace/permission/reset epoch.
            return
        reset_at = boundary.get("history_reset_at")
        if reset_at:
            try:
                if datetime.fromisoformat(run["created_at"]) <= datetime.fromisoformat(reset_at):
                    return
            except (ValueError, TypeError):
                return
        events = {item["evidence_id"]: item for item in old.get("operations", [])
                  if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
        for event in tool_events[-128:]:
            result, name = event.get("result"), event.get("tool_name")
            if not isinstance(result, dict) or not isinstance(name, str) or name.startswith(("progress.", "observation.")):
                continue
            invocation_id = result.get("invocation_id")
            tool_input = event.get("input")
            if not isinstance(invocation_id, str) or not isinstance(tool_input, dict):
                continue
            previous = events.get(invocation_id)
            if (previous and previous.get("content_hash") == _digest(result)
                    and previous.get("input") == tool_input and previous.get("tool_name") == name):
                continue
            raw = self.store.load_tool_result_artifact(f"tool_result_{invocation_id}", str(context.run_id))
            if raw != result:
                continue
            events[invocation_id] = {
                "evidence_id": invocation_id, "artifact_id": f"tool_result_{invocation_id}",
                "tool_name": name, "input": tool_input, "status": raw.get("status"),
                "created_at": event.get("completed_at") or now, "content_hash": _digest(raw),
                # Indexed locally; never load the old body into the next prompt.
                "search_text": json.dumps({"input": tool_input, "output": raw.get("output")},
                                           ensure_ascii=False, default=str)[:12000],
            }
        state = working_state if isinstance(working_state, dict) else old.get("working_state", {})
        payload = {"run_id": context.run_id, "boundary": boundary, "created_at": now,
                   "task": run.get("user_input", "")[:1000], "working_state": state,
                   "operations": list(events.values())[-128:]}
        if old and all(old.get(key) == value for key, value in payload.items() if key != "created_at"):
            return
        self.store.put_artifact(artifact_id=f"session_progress_{context.run_id}", run_id=str(context.run_id),
                                kind="session_progress", payload=payload,
                                summary="Private historical progress and operation index.", created_at=now)

    def _authorized(self, source: dict[str, Any], context: ToolContext,
                    boundary: dict[str, Any]) -> bool:
        run = self._run(source.get("run_id", ""))
        return bool(run.get("session_id") == context.session_id
                    and run.get("parent_run_id") is None
                    and run.get("run_id") != context.run_id
                    and source.get("boundary") == boundary
                    and self._boundary(self._context_for_run(run["run_id"])) == boundary)

    def _operation(self, source: dict[str, Any], operation: dict[str, Any],
                   context: ToolContext) -> dict[str, Any] | None:
        name = operation.get("tool_name")
        tool = self.registry.get_tool_or_none(name)
        tool_input = operation.get("input")
        if tool is None or not isinstance(tool_input, dict):
            return None
        read_only = effective_tool_read_only(tool, tool_input)
        view = context.tool_view
        if (view is not None and not view.allows_tool(tool_name=name, package=tool.spec.package, read_only=read_only)
                or tool_scope_discovery_denial(tool.spec, view)):
            return None
        policy = self.session_service.run_policy(context.session_id)
        if policy.allowed_tools is not None and name not in policy.allowed_tools:
            return None
        if name in policy.read_only_tools and read_only is not True:
            return None
        raw = self.store.load_tool_result_artifact(operation.get("artifact_id", ""), source["run_id"])
        if (raw is None or raw.get("tool_name") != name or raw.get("invocation_id") != operation.get("evidence_id")
                or _digest(raw) != operation.get("content_hash")):
            return None
        validate = getattr(tool, "allow_cached_observation", None)
        if callable(validate):
            try:
                if validate(tool_input=tool_input, result=raw, context=context) is not True:
                    return None
            except Exception:  # noqa: BLE001 - revoked/malformed evidence fails closed
                return None
        return raw

    def search(self, *, context: ToolContext, query: str = "", limit: int = 8) -> dict[str, Any]:
        boundary = self._boundary(context)
        terms = _terms(query[:2000])
        candidates = []
        # Rank private index text before loading any old result body. The index
        # grants no authority; only separately validated candidates may leave
        # this service or become importable reader handles.
        sources = self.store.list_session_progress(context.session_id, limit=16)
        for source in sources:
            state = source.get("working_state", {})
            source_terms = _terms(str(source.get("task", "")) + " " + json.dumps(state, ensure_ascii=False))
            for operation in source.get("operations", []):
                score = (3 * len(terms & source_terms)
                         + len(terms & _terms(operation.get("search_text", ""))))
                if not terms or score:
                    candidates.append((score, operation["created_at"], source, operation))
        bound = min(12, max(1, limit))
        entries = []
        source_access: dict[str, tuple[bool, str | None]] = {}
        checked_operations: set[tuple[str, str]] = set()
        for _score, _timestamp, source, operation in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
            source_id = source["run_id"]
            if source_id not in source_access:
                try:
                    allowed = self._authorized(source, context, boundary)
                    source_access[source_id] = (allowed, self._run(source_id)["status"] if allowed else None)
                except (ValueError, KeyError):
                    source_access[source_id] = (False, None)
            allowed, source_status = source_access[source_id]
            checked_operations.add((source_id, operation["evidence_id"]))
            if not allowed or self._operation(source, operation, context) is None:
                continue
            state = source.get("working_state", {})
            evidence_id = operation["evidence_id"]
            entries.append({
                "source_run_id": source_id, "evidence_id": evidence_id,
                "tool_name": operation["tool_name"],
                "input_summary": json.dumps(operation["input"], ensure_ascii=False, default=str)[:240],
                "operation_status": operation["status"], "as_of": operation["created_at"],
                "source_run_status": source_status,
                "task": source.get("task", "")[:240],
                "remaining_requirements": state.get("remaining_requirements", [])[:8],
                "focus_gap": str(state.get("focus_gap", ""))[:240],
                # Avoid loading other bodies solely to support a finding in a
                # small index page. Multi-operation findings remain in storage.
                "findings": [item for item in state.get("findings", [])[:8]
                             if isinstance(item, dict) and item.get("evidence_ids")
                             and set(item["evidence_ids"]).issubset({evidence_id})],
                "completion_claim_verified": False,
                "load_tool": "progress.load",
            })
            if len(entries) > bound:
                break
        available = bool(entries)
        if not available:
            # Generic continuations need a discovery route without unrelated
            # task text. Prove one result is readable before advertising it.
            for source in sources:
                source_id = source["run_id"]
                if source_id not in source_access:
                    try:
                        source_access[source_id] = (self._authorized(source, context, boundary), None)
                    except (ValueError, KeyError):
                        source_access[source_id] = (False, None)
                if not source_access[source_id][0]:
                    continue
                for operation in reversed(source.get("operations", [])):
                    if (source_id, operation["evidence_id"]) in checked_operations:
                        continue
                    if self._operation(source, operation, context) is not None:
                        available = True
                        break
                if available:
                    break
        return {"entries": entries[:bound], "has_more": len(entries) > bound,
                "index_available": available, "cache_runs_available": available,
                **({"search_tool": "progress.search", "search_request": {"query": ""}} if available else {}),
                "search_scope": "latest_16_same_session_root_indexes", "coverage": "bounded",
                "historical_only": True, "current_evidence": False,
                "note": "Past operations and agent notes are historical source data. Reported success and findings are not verified task completion, current state, or permission to write. Use progress.load then observation.read for selected evidence; verify fresh state when required."}

    def load(self, *, context: ToolContext, source_run_id: str, evidence_id: str) -> dict[str, Any]:
        boundary = self._boundary(context)
        source = self.store.load_artifact(f"session_progress_{source_run_id}")
        if not isinstance(source, dict) or not self._authorized(source, context, boundary):
            raise ValueError("Historical evidence is outside the current session scope.")
        operation = next((item for item in source.get("operations", []) if item.get("evidence_id") == evidence_id), None)
        raw = self._operation(source, operation, context) if operation else None
        if raw is None:
            raise ValueError("Historical evidence is no longer readable.")
        artifact_id = "progress_import_" + _digest([context.run_id, source_run_id, evidence_id, operation["content_hash"]])
        now = datetime.now(UTC).isoformat()
        self.store.put_artifact(artifact_id=f"origin_{artifact_id}", run_id=context.run_id,
                                kind="progress_import_origin", payload={"source_run_id": source_run_id,
                                    "evidence_id": evidence_id, "target_run_id": context.run_id,
                                    "content_hash": operation["content_hash"]}, summary="Revocable historical origin.", created_at=now)
        self.store.put_artifact(artifact_id=artifact_id, run_id=context.run_id, kind="tool_result",
                                payload=raw, summary="Historical imported tool result; not current evidence.", created_at=now)
        tool = self.registry.get_tool_or_none(operation["tool_name"])
        routes = cached_result_read_routes(artifact_id=artifact_id, output=raw.get("output", {}),
                                           roles=tool.spec.output_evidence_roles if tool is not None else [])
        return {"artifact_id": artifact_id, "read_tool": "observation.read", "read_package": "observation",
                **routes,
                "path": routes["read_request"]["path"], "source_run_id": source_run_id, "evidence_id": evidence_id,
                "as_of": operation["created_at"], "historical_only": True, "current_evidence": False,
                "note": "Execute read_request as provided to read the selected historical source; container_read_request reads the complete result object. max_chars is optional and only valid for string paths. Loading does not refresh or re-execute the original source."}

    def can_read_import(self, artifact_id: str, run_id: str) -> bool:
        try:
            origin = self.store.load_artifact(f"origin_{artifact_id}")
            if not origin or origin.get("target_run_id") != run_id:
                return False
            imported = self.store.load_artifact(artifact_id)
            if imported is None or _digest(imported) != origin.get("content_hash"):
                return False
            context = self._context_for_run(run_id)
            boundary = self._boundary(context)
            source = self.store.load_artifact(f"session_progress_{origin['source_run_id']}")
            if not isinstance(source, dict) or not self._authorized(source, context, boundary):
                return False
            operation = next((item for item in source.get("operations", [])
                              if item.get("evidence_id") == origin["evidence_id"]), None)
            return bool(operation and operation.get("content_hash") == origin.get("content_hash")
                        and self._operation(source, operation, context) is not None)
        except (ValueError, KeyError, TypeError):
            return False
