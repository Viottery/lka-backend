from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.api.main import create_app  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one Local Knowledge Agent turn without starting the HTTP API.",
    )
    parser.add_argument(
        "user_input",
        nargs="+",
        help="User query to submit to the agent turn loop.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Existing or new session id. Omit to let the session service create one.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full AgentTurnResult as JSON.",
    )
    parser.add_argument(
        "--start-runtime",
        action="store_true",
        help=(
            "Run runtime.start()/stop() around the turn. This may trigger configured "
            "startup/background mail sync."
        ),
    )
    return parser


def _summarize_result(result) -> str:
    lines: list[str] = [
        "Agent turn completed.",
        f"session_id: {result.session_id}",
        f"trace_id: {result.trace_id}",
        f"selected_package: {result.selected_package}",
        f"log_path: {result.log_path}",
        "",
        "Answer:",
        result.answer,
        "",
        "Decision events:",
    ]
    for event in result.decision_events:
        details = [f"step={event.step_index}", f"action={event.action}"]
        if event.tool_name:
            details.append(f"tool={event.tool_name}")
        if event.selected_package:
            details.append(f"package={event.selected_package}")
        if event.reason:
            details.append(f"reason={event.reason}")
        lines.append("- " + " | ".join(details))

    lines.append("")
    lines.append("Tool events:")
    if not result.tool_events:
        lines.append("- none")
    for event in result.tool_events:
        status = event.result.get("status")
        feedback_status = event.feedback.get("status")
        lines.append(
            f"- {event.tool_name} | status={status} | feedback={feedback_status}"
        )

    lines.append("")
    lines.append("Progress events:")
    for event in result.progress_events:
        tool_suffix = f" | tool={event.tool_name}" if event.tool_name else ""
        lines.append(f"- [{event.type}] {event.message}{tool_suffix}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    user_input = " ".join(args.user_input).strip()
    if not user_input:
        raise SystemExit("user_input must not be empty")

    app = create_app()
    runtime = app.state.runtime
    started = False
    try:
        if args.start_runtime:
            runtime.start()
            started = True
        result = runtime.run_agent_turn(
            session_id=args.session_id,
            user_input=user_input,
        )
    finally:
        if started:
            runtime.stop()

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(_summarize_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
