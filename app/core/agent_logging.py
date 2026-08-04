"""Deterministic, human-readable agent run logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRunLogger:
    """Write local Markdown logs for agent runs without using an LLM."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def write_mail_process_log(
        self,
        *,
        run_id: str,
        session_id: str,
        user_input: str,
        package_catalog: list[dict[str, Any]],
        expanded_package: str,
        tool_events: list[dict[str, Any]],
        llm_event: dict[str, Any] | None,
        final_result: dict[str, Any],
    ) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{run_id}.md"
        sections = [
            "# Agent Run Log",
            "",
            f"- generated_at: `{_utc_iso()}`",
            f"- run_id: `{run_id}`",
            f"- session_id: `{session_id}`",
            f"- user_input: {user_input}",
            f"- expanded_package: `{expanded_package}`",
            "",
            "## Package Catalog",
            "",
            self._json_block(package_catalog),
            "",
            "## Tool Calls",
            "",
        ]
        if not tool_events:
            sections.extend(["No tool calls were recorded.", ""])
        for index, event in enumerate(tool_events, start=1):
            sections.extend(
                [
                    f"### Tool Call {index}: `{event.get('tool_name')}`",
                    "",
                    f"- selected_at: `{event.get('selected_at')}`",
                    f"- completed_at: `{event.get('completed_at')}`",
                    "",
                    "Input:",
                    "",
                    self._json_block(event.get("input", {})),
                    "",
                    "Result:",
                    "",
                    self._json_block(event.get("result", {})),
                    "",
                ]
            )

        sections.extend(["## LLM Interaction", ""])
        if llm_event is None:
            sections.extend(["No LLM call was attempted for this run.", ""])
        else:
            sections.extend(
                [
                    f"- provider: `{llm_event.get('provider')}`",
                    f"- status: `{llm_event.get('status')}`",
                    f"- started_at: `{llm_event.get('started_at')}`",
                    f"- completed_at: `{llm_event.get('completed_at')}`",
                    "",
                    "System Prompt:",
                    "",
                    self._text_block(str(llm_event.get("system_prompt", ""))),
                    "",
                    "User Prompt:",
                    "",
                    self._text_block(str(llm_event.get("user_prompt", ""))),
                    "",
                    "Output:",
                    "",
                    self._text_block(str(llm_event.get("output", ""))),
                    "",
                ]
            )

        sections.extend(
            [
                "## Final Result",
                "",
                self._json_block(final_result),
                "",
            ]
        )
        path.write_text("\n".join(sections), encoding="utf-8")
        return path

    def _json_block(self, value: Any) -> str:
        return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"

    def _text_block(self, value: str) -> str:
        return "```text\n" + value + "\n```"
