"""Tool package and invocation contracts for agent execution."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.core.context import TaskContext


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
    side_effects: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    invocation_id: str
    tool: ToolSpec
    session_id: str
    context_id: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolContext(BaseModel):
    session_id: str
    trace_id: str | None = None
    context_id: str | None = None
    workspace_root: str | None = None
    safety_review_approved: bool = False
    safety_review_id: str | None = None


class ToolResult(BaseModel):
    invocation_id: str
    tool_name: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


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
    checker = getattr(tool, "is_read_only_invocation", None)
    if callable(checker):
        return bool(checker(tool_input))
    return tool.spec.read_only


class ToolExecutor:
    """Execute registered tools and normalize failures into ToolResult."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        tool = self.registry.get_tool_or_none(tool_name)
        if tool is None:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="rejected",
                error="Tool is not registered.",
            )
        read_only = effective_tool_read_only(tool, tool_input)
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
            )
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool=tool.spec,
            session_id=context.session_id,
            context_id=context.context_id or "",
            input=tool_input,
        )
        try:
            return tool.invoke(invocation=invocation, context=context)
        except Exception as exc:
            return ToolResult(
                invocation_id=invocation_id,
                tool_name=tool_name,
                status="failed",
                error=str(exc),
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
        except Exception as exc:
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
