"""Tool package and invocation contracts for agent execution."""

from __future__ import annotations

import threading
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, Field

from app.core.context import TaskContext
from app.core.context_driver import ToolView


def _scope_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if isinstance(item, (str, int)) and str(item))
    return ()


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except ValueError:
        return False
    return True


class ToolPackageSpec(BaseModel):
    name: str
    description: str
    risk: str = "low"
    requires_expansion: bool = True
    tool_names: list[str] = Field(default_factory=list)
    routing_hints: list[str] = Field(default_factory=list)
    decision_hints: list[str] = Field(default_factory=list)
    observation_cache: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    name: str
    type: str
    description: str
    package: str | None = None
    risk: str = "low"
    requires_confirmation: bool = False
    read_only: bool | None = None
    # Discovery only: some inputs may be read-only even when the tool is not.
    # Execution must still classify and authorize the actual invocation input.
    supports_read_only_invocations: bool = False
    side_effects: list[str] = Field(default_factory=list)
    resource_lock_group: str | None = None
    resource_lock_fields: tuple[str, ...] = ()
    scope_path_fields: tuple[str, ...] = ()
    scope_source_fields: tuple[str, ...] = ()
    scope_account_fields: tuple[str, ...] = ()
    scope_filtering_required: bool = False
    scope_uses_sources: bool = False
    scope_uses_accounts: bool = False
    scope_uses_workspace: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # Registered producer metadata, never a value read from ToolResult.output.
    # Preserve bounded evidence leaves without changing the overall gate cap.
    output_preview_max_string_chars: int = Field(default=700, strict=True, ge=700, le=1200)


class ToolInvocation(BaseModel):
    invocation_id: str
    tool: ToolSpec
    session_id: str
    context_id: str
    input: dict[str, Any] = Field(default_factory=dict)


def tool_scope_discovery_denial(spec: ToolSpec, tool_view: ToolView | None) -> str | None:
    """Share input-independent scope denials between discovery and execution.

    Visibility is not authorization: argument selection, paths, review and run
    lifetime still pass through ToolExecutor for every invocation.
    """
    if tool_view is None or tool_view.child_run_id is None:
        return None
    source_ids = tool_view.allowed_source_ids
    account_ids = tool_view.allowed_account_ids
    uses_sources = spec.scope_uses_sources or bool(spec.scope_source_fields)
    uses_accounts = spec.scope_uses_accounts or bool(spec.scope_account_fields)
    uses_workspace = spec.scope_uses_workspace or bool(spec.scope_path_fields)
    if uses_sources and not source_ids and not tool_view.full_data_authority:
        return "Child run has no authorized source scope for this tool."
    if uses_accounts and not account_ids and not tool_view.full_data_authority:
        return "Child run has no authorized account scope for this tool."
    if uses_sources and source_ids and not spec.scope_source_fields and not spec.scope_filtering_required:
        return "Tool does not enforce this child run's source scope."
    if uses_accounts and account_ids and not spec.scope_account_fields and not spec.scope_filtering_required:
        return "Tool does not enforce this child run's account scope."
    if uses_workspace and not tool_view.allowed_paths and not tool_view.full_workspace_authority:
        return "Child run has no authorized workspace scope for this tool."
    if uses_workspace and not spec.scope_path_fields and not spec.scope_filtering_required and not tool_view.full_workspace_authority:
        return "Tool does not enforce this child run's workspace scope."
    return None


class ToolContext(BaseModel):
    session_id: str
    trace_id: str | None = None
    context_id: str | None = None
    workspace_root: str | None = None
    safety_review_approved: bool = False
    safety_review_id: str | None = None
    tool_view: ToolView | None = None
    run_id: str | None = None


class ToolResult(BaseModel):
    invocation_id: str
    tool_name: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Server-owned invocation boundary; absent legacy evidence stays unknown.
    execution_started: bool | None = Field(default=None, strict=True)


class Tool(Protocol):
    spec: ToolSpec

    def invoke(self, *, invocation: ToolInvocation, context: ToolContext) -> ToolResult:
        ...


class ToolRegistry:
    """Static package-aware registry for MVP tools."""

    def __init__(self) -> None:
        self._packages: dict[str, ToolPackageSpec] = {}
        self._tools: dict[str, Tool] = {}

    def register_package(self, package: ToolPackageSpec) -> None:
        self._packages[package.name] = package

    def register_tool(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool
        if tool.spec.package and tool.spec.package in self._packages:
            package = self._packages[tool.spec.package]
            if tool.spec.name not in package.tool_names:
                package.tool_names.append(tool.spec.name)

    def list_packages(self) -> list[ToolPackageSpec]:
        return list(self._packages.values())

    def list_tools(self, *, package: str | None = None) -> list[ToolSpec]:
        tools = [tool.spec for tool in self._tools.values()]
        if package is None:
            return tools
        return [tool for tool in tools if tool.package == package]

    def get_tool(self, name: str) -> Tool:
        return self._tools[name]

    def get_tool_or_none(self, name: str) -> Tool | None:
        return self._tools.get(name)


def effective_tool_read_only(tool: Tool, tool_input: dict[str, Any]) -> bool | None:
    try:
        checker = getattr(tool, "is_read_only_invocation", None)
        if callable(checker):
            result = checker(tool_input)
            return result if type(result) is bool else None
    except Exception:  # noqa: BLE001 - unknown classification cannot grant read-only authority.
        return None
    return tool.spec.read_only


class ToolExecutor:
    """Execute registered tools and normalize failures into ToolResult."""

    _admission_guard: ClassVar[threading.Lock] = threading.Lock()
    _admission: ClassVar[dict[tuple[str, str], threading.BoundedSemaphore]] = {}
    _resource_guard: ClassVar[threading.Lock] = threading.Lock()
    _resource_locks: ClassVar[dict[tuple[str, str], threading.Lock]] = {}
    _admission_limits: ClassVar[dict[str, int]] = {
        "global": 16,
        "session": 4,
        "provider": 4,
        "package": 4,
        "workspace": 2,
    }

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.run_manager: Any | None = None

    def execute(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_view: ToolView | None = None,
    ) -> ToolResult:
        if tool_view is None:
            tool_view = context.tool_view
        tool = self.registry.get_tool_or_none(tool_name)
        if tool is None:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                error="Tool is not registered.",
                execution_started=False,
            )
        read_only = effective_tool_read_only(tool, tool_input)
        denial_reason = (
            tool_view.denial_reason(
                tool_name=tool_name, package=tool.spec.package, read_only=read_only,
            )
            if tool_view is not None else None
        )
        if denial_reason is not None:
            messages = {
                "context_expired": "The immutable ContextSnapshot ToolView has expired.",
                "tool_not_granted": "Tool is not granted by the immutable ContextSnapshot ToolView.",
                "package_not_granted": "Tool package is not granted by the immutable ContextSnapshot ToolView.",
                "invocation_not_read_only": (
                    "This invocation was not proven read-only within the immutable ContextSnapshot ToolView. "
                    "This does not forbid all read-only invocations of the tool; subsequent invocations "
                    "still pass every original authorization and safety check."
                ),
            }
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                output={"authorization_denial": {
                    "code": denial_reason,
                    "current_read_only": read_only,
                    "tool_granted": denial_reason == "invocation_not_read_only",
                    "allowed_side_effect_level": tool_view.side_effect_level.value,
                }},
                error=messages[denial_reason],
                execution_started=False,
            )
        scope_error = self._check_scope(
            spec=tool.spec,
            tool_input=tool_input,
            context=context,
            tool_view=tool_view,
        )
        if scope_error is not None:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                error=scope_error,
                execution_started=False,
            )
        if read_only is not True and not context.safety_review_approved:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                output={
                    "safety_review_required": True,
                    "read_only": read_only,
                    "risk": tool.spec.risk,
                    "side_effects": tool.spec.side_effects,
                },
                error="Non-read-only tools require an approved safety review.",
                execution_started=False,
            )
        guard_failure = self._run_guard(context=context, tool_view=tool_view)
        if guard_failure is not None:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                error=guard_failure,
                execution_started=False,
            )
        validation_errors = self._validate_input(
            schema=tool.spec.input_schema,
            value=tool_input,
            path="tool_input",
        )
        if validation_errors:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                output={"validation_errors": validation_errors},
                error="Tool input failed schema validation.",
                execution_started=False,
            )
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool=tool.spec,
            session_id=context.session_id,
            context_id=context.context_id or "",
            input=tool_input,
        )
        execution_started = False
        try:
            with self._admit(
                tool=tool,
                context=context,
                tool_input=tool_input,
                read_only=read_only,
                tool_view=tool_view,
            ):
                failure = self._run_guard(context=context, tool_view=tool_view)
                if failure is not None:
                    return ToolResult(
                        invocation_id=invocation_id,
                        tool_name=tool_name,
                        status="rejected",
                        error=failure,
                        execution_started=False,
                    )
                execution_started = True
                result = tool.invoke(invocation=invocation, context=context)
                return result.model_copy(update={"execution_started": True})
        except Exception as exc:  # noqa: BLE001 - normalize registered tool failures.
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="failed",
                error=str(exc),
                execution_started=execution_started,
            )

    def _run_guard(self, *, context: ToolContext, tool_view: ToolView | None) -> str | None:
        run_id = (tool_view.child_run_id if tool_view else None) or context.run_id
        manager = self.run_manager
        if manager is None or run_id is None:
            return None
        run = manager.get_run(run_id)
        if run is None:
            return "Agent run is unavailable; tool invocation was stopped."
        if run.status.value in {"cancelled", "timed_out", "failed", "completed"} or manager.is_cancel_requested(run_id):
            return "Agent run is cancelled or terminal; tool invocation was stopped."
        if tool_view and tool_view.expires_at and tool_view.expires_at <= datetime.now(UTC):
            if run.parent_run_id is not None:
                manager.timeout_child_run(run_id, error="Child Agent exceeded its wall-time budget.")
            return "Child Agent context expired; tool invocation was stopped."
        if tool_view and tool_view.max_tool_calls is not None:
            count = sum(event.type == "tool_started" for event in manager.list_events(run_id))
            if count > tool_view.max_tool_calls:
                return "Child Agent exceeded its tool-call budget."
        return None

    def _check_scope(
        self,
        *,
        spec: ToolSpec,
        tool_input: dict[str, Any],
        context: ToolContext,
        tool_view: ToolView | None,
    ) -> str | None:
        """Apply tool-declared argument scope and fail closed on unfiltered data tools."""
        if tool_view is None or tool_view.child_run_id is None:
            return None
        if denial := tool_scope_discovery_denial(spec, tool_view):
            return denial
        source_ids = tuple(getattr(tool_view, "allowed_source_ids", ()))
        account_ids = tuple(getattr(tool_view, "allowed_account_ids", ()))
        paths = tuple(getattr(tool_view, "allowed_paths", ()))
        for field in spec.scope_source_fields:
            requested = _scope_values(tool_input.get(field))
            if source_ids and not requested and not spec.scope_filtering_required:
                return f"Tool argument {field!r} must explicitly select sources in the child scope."
            if requested and not set(requested).issubset(source_ids):
                return f"Tool argument {field!r} requests a source outside the child scope."
        for field in spec.scope_account_fields:
            requested = _scope_values(tool_input.get(field))
            if account_ids and not requested and not spec.scope_filtering_required:
                return f"Tool argument {field!r} must explicitly select accounts in the child scope."
            if requested and not set(requested).issubset(account_ids):
                return f"Tool argument {field!r} requests an account outside the child scope."
        for field in spec.scope_path_fields:
            for raw_path in _scope_values(tool_input.get(field)):
                path_value = raw_path
                if context.workspace_root:
                    path_value = (
                        path_value.replace("$workspace_root", context.workspace_root)
                        .replace("${workspace_root}", context.workspace_root)
                        .replace("$WORKSPACE_ROOT", context.workspace_root)
                        .replace("${WORKSPACE_ROOT}", context.workspace_root)
                        .replace("$LKA_WORKSPACE_ROOT", context.workspace_root)
                        .replace("${LKA_WORKSPACE_ROOT}", context.workspace_root)
                    )
                candidate = Path(path_value).expanduser()
                if not candidate.is_absolute() and context.workspace_root:
                    candidate = Path(context.workspace_root) / candidate
                candidate = candidate.resolve(strict=False)
                if not paths or not any(_is_within(candidate, Path(root)) for root in paths):
                    return f"Tool argument {field!r} resolves outside the child workspace scope."
        return None

    @classmethod
    def _semaphore(cls, kind: str, key: str) -> threading.BoundedSemaphore:
        identity = (kind, key)
        with cls._admission_guard:
            semaphore = cls._admission.get(identity)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(cls._admission_limits[kind])
                cls._admission[identity] = semaphore
            return semaphore

    @classmethod
    def _resource_lock(cls, group: str, key: str) -> threading.Lock:
        identity = (group, key)
        with cls._resource_guard:
            lock = cls._resource_locks.get(identity)
            if lock is None:
                lock = threading.Lock()
                cls._resource_locks[identity] = lock
            return lock

    def _admit(
        self,
        *,
        tool: Tool,
        context: ToolContext,
        tool_input: dict[str, Any],
        read_only: bool | None,
        tool_view: ToolView | None,
    ):
        stack = ExitStack()
        keys = [
            ("global", "all"),
            ("session", context.session_id),
            ("provider", tool.spec.type),
            ("package", tool.spec.package or "unpackaged"),
        ]
        if context.workspace_root:
            keys.append(("workspace", context.workspace_root))
        semaphores = [self._semaphore(kind, key) for kind, key in sorted(keys)]
        acquired: list[threading.BoundedSemaphore] = []
        locks: list[threading.Lock] = []
        try:
            for semaphore in semaphores:
                while not semaphore.acquire(timeout=0.1):
                    failure = self._run_guard(context=context, tool_view=tool_view)
                    if failure:
                        raise RuntimeError(failure)
                acquired.append(semaphore)
            if read_only is not True:
                group = tool.spec.resource_lock_group or "global-write"
                values = tuple(str(tool_input.get(field, "")) for field in tool.spec.resource_lock_fields)
                if group == "workspace":
                    values = (context.workspace_root or "unscoped", *values)
                resource_key = "|".join(values) if values else "all"
                lock = self._resource_lock(group, resource_key)
                wait_started_at: float | None = None
                first_attempt_at = monotonic()
                run_id = (tool_view.child_run_id if tool_view else None) or context.run_id
                while not lock.acquire(timeout=0.1):
                    if wait_started_at is None:
                        wait_started_at = first_attempt_at
                        self._record_resource_lock_event(
                            run_id=run_id,
                            event_type="resource_lock_wait_started",
                            tool_name=tool.spec.name,
                            group=group,
                        )
                    failure = self._run_guard(context=context, tool_view=tool_view)
                    if failure:
                        self._record_resource_lock_event(
                            run_id=run_id,
                            event_type="resource_lock_wait_cancelled",
                            tool_name=tool.spec.name,
                            group=group,
                            elapsed_ms=max(0, round((monotonic() - wait_started_at) * 1000)),
                        )
                        raise RuntimeError(failure)
                locks.append(lock)
                if wait_started_at is not None:
                    self._record_resource_lock_event(
                        run_id=run_id,
                        event_type="resource_lock_wait_completed",
                        tool_name=tool.spec.name,
                        group=group,
                        elapsed_ms=max(0, round((monotonic() - wait_started_at) * 1000)),
                    )
            stack.callback(lambda: [lock.release() for lock in reversed(locks)])
            stack.callback(lambda: [semaphore.release() for semaphore in reversed(acquired)])
            return stack
        except Exception:
            stack.close()
            for lock in reversed(locks):
                lock.release()
            for semaphore in reversed(acquired):
                semaphore.release()
            raise

    def _record_resource_lock_event(
        self,
        *,
        run_id: str | None,
        event_type: str,
        tool_name: str,
        group: str,
        elapsed_ms: int | None = None,
    ) -> None:
        manager = self.run_manager
        if manager is None or run_id is None:
            return
        payload: dict[str, Any] = {"tool_name": tool_name, "resource_group": group}
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        run = manager.get_run(run_id)
        messages = {
            "resource_lock_wait_started": "Waiting for a shared tool resource lock.",
            "resource_lock_wait_completed": "Shared tool resource lock acquired.",
            "resource_lock_wait_cancelled": "Resource lock wait stopped by a run guard.",
        }
        manager.append_event(
            run_id,
            event_type,
            messages.get(event_type, "Resource lock wait state changed."),
            stage="tool",
            payload=payload,
            parent_run_id=run.parent_run_id if run else None,
            child_run_id=run_id if run and run.parent_run_id else None,
        )

    def validate_output(
        self,
        *,
        tool_name: str,
        result: ToolResult,
    ) -> list[str]:
        errors: list[str] = []
        if not result.invocation_id:
            errors.append("ToolResult.invocation_id is required.")
        if result.tool_name != tool_name:
            errors.append(
                f"ToolResult.tool_name must be {tool_name!r}, got {result.tool_name!r}."
            )
        if not result.status:
            errors.append("ToolResult.status is required.")
        try:
            result.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - serialization can raise provider/model errors.
            errors.append(f"ToolResult must be JSON serializable: {exc}")

        tool = self.registry.get_tool_or_none(tool_name)
        if tool is None:
            errors.append("Tool is not registered.")
            return errors

        if result.status != "completed":
            if not result.error:
                errors.append("Non-completed ToolResult must include error.")
            return errors

        if tool.spec.output_schema:
            errors.extend(
                self._validate_output(
                    schema=tool.spec.output_schema,
                    value=result.output,
                    path="output",
                )
            )
        elif not isinstance(result.output, dict):
            errors.append("ToolResult.output must be a JSON object.")
        return errors

    def _validate_output(
        self,
        *,
        schema: dict[str, Any],
        value: Any,
        path: str,
    ) -> list[str]:
        normalized_schema = self._normalize_output_schema(schema)
        return self._validate_value(
            schema=normalized_schema,
            value=value,
            path=path,
        )

    def _normalize_output_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        if schema.get("type") == "object":
            return schema
        return {
            "type": "object",
            "required": [key for key in schema if isinstance(key, str)],
            "properties": {
                key: self._normalize_type_schema(spec)
                for key, spec in schema.items()
            },
        }

    def _validate_input(
        self,
        *,
        schema: dict[str, Any],
        value: Any,
        path: str,
    ) -> list[str]:
        normalized_schema = self._normalize_schema(schema)
        return self._validate_value(
            schema=normalized_schema,
            value=value,
            path=path,
        )

    def _normalize_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        if schema.get("type") == "object":
            return schema
        return {
            "type": "object",
            "required": [],
            "properties": {
                key: self._normalize_type_schema(spec)
                for key, spec in schema.items()
            },
        }

    def _normalize_type_schema(self, spec: Any) -> dict[str, Any]:
        if isinstance(spec, dict):
            return spec
        if isinstance(spec, str):
            return {"type": spec}
        if isinstance(spec, list):
            return {"type": spec}
        return {}

    def _validate_value(
        self,
        *,
        schema: dict[str, Any],
        value: Any,
        path: str,
    ) -> list[str]:
        expected_type = schema.get("type")
        errors: list[str] = []
        if expected_type is not None and not self._matches_type(value, expected_type):
            errors.append(
                f"{path} must be {self._format_expected_type(expected_type)}, "
                f"got {type(value).__name__}."
            )
            return errors

        allowed_values = schema.get("allowed_values")
        if (
            isinstance(allowed_values, list)
            and value is not None
            and value not in allowed_values
        ):
            errors.append(
                f"{path} must be one of {allowed_values}, got {value!r}."
            )

        minimum = schema.get("minimum")
        if (
            isinstance(minimum, int | float)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and value < minimum
        ):
            errors.append(f"{path} must be >= {minimum}, got {value!r}.")

        if self._type_allows(expected_type, "object") and isinstance(value, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                required = schema.get("required")
                if isinstance(required, list):
                    for key in required:
                        if isinstance(key, str) and key not in value:
                            errors.append(f"{path}.{key} is required.")
                for key, item in value.items():
                    property_schema = properties.get(key)
                    if isinstance(property_schema, dict):
                        errors.extend(
                            self._validate_value(
                                schema=property_schema,
                                value=item,
                                path=f"{path}.{key}",
                            )
                        )

        if self._type_allows(expected_type, "array") and isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    errors.extend(
                        self._validate_value(
                            schema=item_schema,
                            value=item,
                            path=f"{path}[{index}]",
                        )
                    )

        return errors

    def _matches_type(self, value: Any, expected_type: Any) -> bool:
        if expected_type == "null":
            return value is None
        if isinstance(expected_type, list):
            return any(self._matches_type(value, item) for item in expected_type)
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return (isinstance(value, int | float) and not isinstance(value, bool))
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        return True

    def _type_allows(self, expected_type: Any, candidate: str) -> bool:
        if expected_type is None:
            return True
        if isinstance(expected_type, list):
            return candidate in expected_type
        return expected_type == candidate

    def _format_expected_type(self, expected_type: Any) -> str:
        if isinstance(expected_type, list):
            return " or ".join(str(item) for item in expected_type)
        return str(expected_type)


class MockToolExecutor:
    """A read-only tool executor used only by the debug runtime path."""

    spec = ToolSpec(
        name="runtime_debug_echo",
        type="local_tool",
        description="Echo structured task context metadata for runtime debugging.",
        risk="low",
        requires_confirmation=False,
        read_only=True,
        input_schema={"goal_summary": "string", "related_file_count": "integer"},
        output_schema={"message": "string", "context_id": "string"},
    )

    def invoke(self, *, invocation_id: str, task_context: TaskContext) -> ToolResult:
        return ToolResult(
            invocation_id=invocation_id,
            tool_name=self.spec.name,
            status="completed",
            output={
                "message": "Mock tool completed without modifying files.",
                "context_id": task_context.context_id,
                "related_file_count": len(task_context.related_files),
            },
        )
