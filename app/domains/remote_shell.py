"""Operator trust admission for delegated native host-user execution.

This grant acknowledges arbitrary host filesystem/network access. It is never
derived from task prose, a tool allowlist alone, or a workspace cwd.
"""
from collections.abc import Collection

from app.core.context_driver import ToolView


def approve_host_execution_authority(
    *,
    shell_trust: str,
    allowed_tools: Collection[str],
    source_view: ToolView | None,
    effect_constraints: object = (),
    mounted_workspace: bool = False,
) -> bool:
    """Fail closed whenever native processes could bypass inherited limits.

    The node must still freeze the resulting grant, force manual reviews and
    revalidate live configuration and execution leases at each tool invocation.
    The returned grant does not broaden registered data/source tools.
    """
    if (shell_trust != "host_user" or "bash.run" not in allowed_tools
            or effect_constraints or mounted_workspace):
        return False
    if source_view is None:
        return True
    return bool(
        source_view.full_workspace_authority
        and source_view.full_data_authority
        and not source_view.allowed_paths
        and not source_view.allowed_source_ids
        and not source_view.allowed_account_ids
        and not source_view.read_only_tools
        and source_view.max_tool_calls is None
        and source_view.side_effect_level.value == "external"
        and source_view.allows_tool(tool_name="bash.run", package="bash", read_only=False)
    )
