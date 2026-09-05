from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8765"


class AgentEventMode(str, Enum):
    HIDDEN = "hidden"
    COLLAPSED = "collapsed"
    EXPANDED = "expanded"


@dataclass(frozen=True)
class CliConfig:
    base_url: str
    timeout: float


@dataclass
class StreamDisplayState:
    printed_delta: bool = False
    printed_final: bool = False
    printed_answer: str = ""
    last_collapsed_message: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = CliConfig(
        base_url=args.base_url.rstrip("/"),
        timeout=args.timeout,
    )
    try:
        return args.handler(config, args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lka",
        description="Linux command-line frontend for Local Knowledge Agent OS.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LKA_BASE_URL", DEFAULT_BASE_URL),
        help=f"Backend base URL. Defaults to {DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Check backend health.")
    health.set_defaults(handler=_cmd_health)

    capabilities = subparsers.add_parser("capabilities", help="List capability catalog.")
    capabilities.add_argument("--json", action="store_true", help="Print raw JSON.")
    capabilities.set_defaults(handler=_cmd_capabilities)

    index = subparsers.add_parser("index", help="Index a backend-local workspace path.")
    index.add_argument("workspace")
    index.add_argument("--source-frontend", default="linux-native")
    index.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    index.add_argument("--skip-hidden", action=argparse.BooleanOptionalAction, default=True)
    index.add_argument("--allow-symlinks", action=argparse.BooleanOptionalAction, default=False)
    index.add_argument("--max-files", type=int, default=50000)
    index.add_argument("--json", action="store_true", help="Print raw JSON.")
    index.set_defaults(handler=_cmd_index)

    ask = subparsers.add_parser("ask", help="Run one Agent turn.")
    ask.add_argument("user_input", nargs="+")
    ask.add_argument("--session-id")
    ask.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True)
    ask.add_argument(
        "--agent-events",
        choices=[mode.value for mode in AgentEventMode],
        default=AgentEventMode.COLLAPSED.value,
        help="Show Agent progress as hidden, collapsed, or expanded.",
    )
    ask.add_argument("--llm-client")
    ask.add_argument("--llm-model")
    ask.add_argument("--json", action="store_true", help="Print raw JSON for non-stream mode.")
    ask.set_defaults(handler=_cmd_ask)

    chat = subparsers.add_parser("chat", help="Start an interactive streaming chat.")
    chat.add_argument("--session-id")
    chat.add_argument(
        "--agent-events",
        choices=[mode.value for mode in AgentEventMode],
        default=AgentEventMode.COLLAPSED.value,
    )
    chat.add_argument("--llm-client")
    chat.add_argument("--llm-model")
    chat.set_defaults(handler=_cmd_chat)

    sessions = subparsers.add_parser("sessions", help="Manage Agent sessions.")
    session_subparsers = sessions.add_subparsers(dest="session_command", required=True)
    sessions_list = session_subparsers.add_parser("list", help="List sessions.")
    sessions_list.add_argument("--limit", type=int, default=50)
    sessions_list.add_argument("--json", action="store_true")
    sessions_list.set_defaults(handler=_cmd_sessions_list)
    sessions_create = session_subparsers.add_parser("create", help="Create a session.")
    sessions_create.add_argument("--title")
    sessions_create.add_argument("--initial-message")
    sessions_create.add_argument("--json", action="store_true")
    sessions_create.set_defaults(handler=_cmd_sessions_create)
    sessions_show = session_subparsers.add_parser("show", help="Show one session.")
    sessions_show.add_argument("session_id")
    sessions_show.add_argument("--json", action="store_true")
    sessions_show.set_defaults(handler=_cmd_sessions_show)
    sessions_workspace = session_subparsers.add_parser(
        "workspace", help="Set the backend-local working directory for one session."
    )
    sessions_workspace.add_argument("session_id")
    sessions_workspace.add_argument("path")
    sessions_workspace.add_argument(
        "--platform", choices=["linux", "windows", "macos"], default="linux"
    )
    sessions_workspace.add_argument("--json", action="store_true")
    sessions_workspace.set_defaults(handler=_cmd_sessions_workspace)

    mail = subparsers.add_parser("mail", help="Mail frontend commands.")
    mail_subparsers = mail.add_subparsers(dest="mail_command", required=True)
    mail_search = mail_subparsers.add_parser("search", help="Search local mail.")
    mail_search.add_argument("query")
    mail_search.add_argument("--limit", type=int, default=10)
    mail_search.add_argument("--json", action="store_true")
    mail_search.set_defaults(handler=_cmd_mail_search)

    matters = subparsers.add_parser("matters", help="Matter frontend commands.")
    matter_subparsers = matters.add_subparsers(dest="matter_command", required=True)
    matters_list = matter_subparsers.add_parser("list", help="List matters.")
    matters_list.add_argument("--limit", type=int, default=50)
    matters_list.add_argument("--status")
    matters_list.add_argument("--json", action="store_true")
    matters_list.set_defaults(handler=_cmd_matters_list)

    return parser


def _cmd_health(config: CliConfig, _args: argparse.Namespace) -> int:
    data = _request_json(config, "GET", "/health")
    print(
        f"{data.get('status', 'unknown')} "
        f"{data.get('service', 'unknown')} "
        f"{data.get('version', '')}".strip()
    )
    return 0


def _cmd_capabilities(config: CliConfig, args: argparse.Namespace) -> int:
    data = _request_json(config, "GET", "/capabilities")
    if args.json:
        _print_json(data)
        return 0
    rows = [
        [
            item.get("name", ""),
            item.get("type", ""),
            item.get("risk", ""),
            "yes" if item.get("requires_confirmation") else "no",
        ]
        for item in data.get("capabilities", [])
    ]
    _print_table(["name", "type", "risk", "confirm"], rows)
    return 0


def _cmd_index(config: CliConfig, args: argparse.Namespace) -> int:
    payload = {
        "workspace": args.workspace,
        "source_frontend": args.source_frontend,
        "options": {
            "recursive": args.recursive,
            "skip_hidden": args.skip_hidden,
            "allow_symlinks": args.allow_symlinks,
            "max_files": args.max_files,
        },
    }
    data = _request_json(config, "POST", "/workspaces/index", payload=payload)
    if args.json:
        _print_json(data)
        return 0
    print(
        "workspace_id={workspace_id} status={status} files={indexed_files} "
        "chunks={indexed_chunks}".format(**data)
    )
    return 0


def _cmd_ask(config: CliConfig, args: argparse.Namespace) -> int:
    user_input = " ".join(args.user_input).strip()
    payload = _agent_turn_payload(args, user_input)
    if args.stream:
        _stream_agent_turn(
            config,
            payload,
            mode=AgentEventMode(args.agent_events),
            out=sys.stdout,
            err=sys.stderr,
        )
        return 0

    data = _request_json(config, "POST", "/agent/turn", payload=payload)
    if args.json:
        _print_json(data)
        return 0
    print(data.get("answer", ""))
    _print_non_stream_agent_events(data, mode=AgentEventMode(args.agent_events))
    return 0


def _cmd_chat(config: CliConfig, args: argparse.Namespace) -> int:
    mode = AgentEventMode(args.agent_events)
    session_id = args.session_id
    print("Local Knowledge Agent CLI. Commands: /quit, /agent hidden|collapsed|expanded.")
    while True:
        try:
            user_input = input("lka> ").strip()
        except EOFError:
            print()
            return 0
        if not user_input:
            continue
        if user_input in {"/quit", "/exit"}:
            return 0
        if user_input.startswith("/agent "):
            mode = _parse_agent_mode(user_input)
            print(f"agent events: {mode.value}")
            continue
        payload = _agent_turn_payload(args, user_input, session_id=session_id)
        result = _stream_agent_turn(
            config,
            payload,
            mode=mode,
            out=sys.stdout,
            err=sys.stderr,
        )
        session_id = result.get("session_id") or session_id


def _cmd_sessions_list(config: CliConfig, args: argparse.Namespace) -> int:
    data = _request_json(config, "GET", "/sessions", query={"limit": args.limit})
    if args.json:
        _print_json(data)
        return 0
    rows = [
        [
            item.get("session_id", ""),
            item.get("title") or "",
            item.get("status", ""),
            item.get("updated_at", ""),
        ]
        for item in data.get("sessions", [])
    ]
    _print_table(["session_id", "title", "status", "updated_at"], rows)
    return 0


def _cmd_sessions_create(config: CliConfig, args: argparse.Namespace) -> int:
    payload = {
        "title": args.title,
        "initial_message": args.initial_message,
        "metadata": {"frontend": "linux-cli"},
    }
    data = _request_json(config, "POST", "/sessions", payload=payload)
    if args.json:
        _print_json(data)
        return 0
    session = data.get("session", {})
    print(session.get("session_id", ""))
    return 0


def _cmd_sessions_show(config: CliConfig, args: argparse.Namespace) -> int:
    data = _request_json(config, "GET", f"/sessions/{args.session_id}")
    if args.json:
        _print_json(data)
        return 0
    session = data.get("session", {})
    print(f"session_id: {session.get('session_id', args.session_id)}")
    print(f"title: {session.get('title') or ''}")
    print(f"status: {session.get('status') or ''}")
    workspace = session.get("workspace")
    if isinstance(workspace, dict):
        print(f"workspace: {workspace.get('path')} ({workspace.get('platform')})")
    print("messages:")
    for message in data.get("messages", []):
        print(f"- {message.get('role')}: {message.get('content')}")
    return 0


def _cmd_sessions_workspace(config: CliConfig, args: argparse.Namespace) -> int:
    data = _request_json(
        config,
        "PUT",
        f"/sessions/{args.session_id}/workspace",
        payload={"path": args.path, "platform": args.platform},
    )
    if args.json:
        _print_json(data)
        return 0
    workspace = data.get("workspace", {})
    print(f"workspace: {workspace.get('path', '')} ({workspace.get('platform', '')})")
    return 0


def _cmd_mail_search(config: CliConfig, args: argparse.Namespace) -> int:
    data = _request_json(
        config,
        "GET",
        "/mail/search",
        query={"q": args.query, "limit": args.limit},
    )
    if args.json:
        _print_json(data)
        return 0
    rows = [
        [
            item.get("message_id", ""),
            item.get("received_at", ""),
            item.get("sender", ""),
            item.get("subject", ""),
        ]
        for item in data.get("messages", [])
    ]
    _print_table(["message_id", "received_at", "sender", "subject"], rows)
    return 0


def _cmd_matters_list(config: CliConfig, args: argparse.Namespace) -> int:
    query: dict[str, Any] = {"limit": args.limit}
    if args.status:
        query["status"] = args.status
    data = _request_json(config, "GET", "/matters", query=query)
    if args.json:
        _print_json(data)
        return 0
    rows = [
        [
            item.get("matter_id", ""),
            item.get("status", ""),
            item.get("priority", ""),
            item.get("title", ""),
        ]
        for item in data.get("matters", [])
    ]
    _print_table(["matter_id", "status", "priority", "title"], rows)
    return 0


def _agent_turn_payload(
    args: argparse.Namespace,
    user_input: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    llm = {}
    client_name = getattr(args, "llm_client", None)
    model = getattr(args, "llm_model", None)
    if client_name:
        llm["client_name"] = client_name
    if model:
        llm["model"] = model
    if getattr(args, "stream", True):
        llm["response_mode"] = "stream"
    payload: dict[str, Any] = {
        "session_id": session_id or getattr(args, "session_id", None),
        "user_input": user_input,
    }
    if llm:
        payload["llm"] = llm
    return payload


def _stream_agent_turn(
    config: CliConfig,
    payload: dict[str, Any],
    *,
    mode: AgentEventMode,
    out,
    err,
) -> dict[str, Any]:
    request = _build_request(config, "POST", "/agent/turn/stream", payload=payload)
    request.add_header("Accept", "text/event-stream")
    state = StreamDisplayState()
    result: dict[str, Any] = {}
    try:
        with urlopen(request, timeout=config.timeout) as response:
            for event in _iter_sse(response):
                data = event.get("data", {})
                if isinstance(data, dict):
                    result.update(_stream_result_metadata(data))
                _render_stream_event(
                    event,
                    mode=mode,
                    state=state,
                    out=out,
                    err=err,
                )
    except HTTPError as exc:
        raise CliError(_http_error_message(exc)) from exc
    except URLError as exc:
        raise CliError(f"failed to connect to backend: {exc.reason}") from exc

    if state.printed_delta or state.printed_final:
        print(file=out)
    return result


def _iter_sse(response) -> Iterator[dict[str, Any]]:
    event_type = "message"
    event_id = ""
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                yield _build_sse_event(event_id, event_type, data_lines)
            event_type = "message"
            event_id = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event_type = value
        elif name == "id":
            event_id = value
        elif name == "data":
            data_lines.append(value)

    if data_lines:
        yield _build_sse_event(event_id, event_type, data_lines)


def _build_sse_event(event_id: str, event_type: str, data_lines: list[str]) -> dict[str, Any]:
    raw_data = "\n".join(data_lines)
    try:
        data: Any = json.loads(raw_data)
    except json.JSONDecodeError:
        data = raw_data
    return {"id": event_id, "event": event_type, "data": data}


def _render_stream_event(
    event: dict[str, Any],
    *,
    mode: AgentEventMode,
    state: StreamDisplayState,
    out,
    err,
) -> None:
    data = event.get("data")
    if not isinstance(data, dict):
        return
    event_type = event.get("event")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}

    if event_type == "llm_delta":
        if not _is_answer_delta(payload):
            _render_agent_process_delta(data, mode=mode, state=state, err=err)
            return
        delta = payload.get("delta")
        if delta:
            print(delta, end="", flush=True, file=out)
            state.printed_delta = True
            state.printed_answer += str(delta)
        return

    if event_type == "final_answer":
        final_answer = str(data.get("message", ""))
        if state.printed_delta:
            suffix = _missing_answer_suffix(state.printed_answer, final_answer)
            if suffix:
                print(suffix, end="", flush=True, file=out)
                state.printed_answer += suffix
        else:
            print(final_answer, end="", flush=True, file=out)
            state.printed_answer = final_answer
            state.printed_final = True
        return

    if mode is AgentEventMode.HIDDEN:
        return
    if mode is AgentEventMode.COLLAPSED:
        message = _collapsed_agent_message(data)
        if message and message != state.last_collapsed_message:
            print(f"[agent] {message}", flush=True, file=err)
            state.last_collapsed_message = message
        return

    _print_expanded_agent_event(data, file=err)


def _stream_result_metadata(data: dict[str, Any]) -> dict[str, Any]:
    result = {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if isinstance(payload.get("session_id"), str):
        result["session_id"] = payload["session_id"]
    if isinstance(payload.get("trace_id"), str):
        result["trace_id"] = payload["trace_id"]
    if isinstance(data.get("run_id"), str):
        result["run_id"] = data["run_id"]
    return result


def _is_answer_delta(payload: dict[str, Any]) -> bool:
    return payload.get("display_target") == "assistant_answer"


def _render_agent_process_delta(
    data: dict[str, Any],
    *,
    mode: AgentEventMode,
    state: StreamDisplayState,
    err,
) -> None:
    if mode is AgentEventMode.HIDDEN:
        return
    if mode is AgentEventMode.EXPANDED:
        _print_expanded_agent_event(data, file=err)
        return
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    content_role = payload.get("content_role") or data.get("stage") or "llm"
    message = f"streaming {content_role}"
    if message != state.last_collapsed_message:
        print(f"[agent] {message}", flush=True, file=err)
        state.last_collapsed_message = message


def _missing_answer_suffix(printed_answer: str, final_answer: str) -> str:
    if not final_answer or printed_answer == final_answer:
        return ""
    if final_answer.startswith(printed_answer):
        return final_answer[len(printed_answer) :]
    return ""


def _collapsed_agent_message(data: dict[str, Any]) -> str:
    stream_part = data.get("stream_part")
    event_type = data.get("type")
    message = str(data.get("message") or "").strip()
    if stream_part in {"lifecycle", "llm_audit"} or event_type == "heartbeat":
        return ""
    if event_type in {"package_selected", "no_package"}:
        return message
    if event_type in {"assistant_message", "tool_feedback", "verification_warning"}:
        return message
    if event_type in {"run_failed", "run_cancelled"}:
        return message
    if event_type in {"tool_started", "tool_completed", "tool_failed"}:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        tool_name = payload.get("tool_name") or payload.get("name") or ""
        return f"{event_type}: {tool_name}".strip()
    return ""


def _print_expanded_agent_event(data: dict[str, Any], *, file) -> None:
    event_type = data.get("type", "event")
    sequence = data.get("sequence", "?")
    stage = data.get("stage") or "-"
    message = data.get("message") or ""
    print(f"[agent:{sequence}] {event_type} stage={stage} {message}", file=file)
    payload = data.get("payload")
    if payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=file)


def _print_non_stream_agent_events(data: dict[str, Any], *, mode: AgentEventMode) -> None:
    if mode is AgentEventMode.HIDDEN:
        return
    events = data.get("progress_events", [])
    if mode is AgentEventMode.COLLAPSED:
        for event in events:
            message = event.get("message")
            if message:
                print(f"[agent] {message}", file=sys.stderr)
        return
    print(json.dumps(events, ensure_ascii=False, indent=2), file=sys.stderr)


def _parse_agent_mode(command: str) -> AgentEventMode:
    _, _, value = command.partition(" ")
    try:
        return AgentEventMode(value.strip())
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in AgentEventMode)
        raise CliError(f"unknown agent event mode; expected one of: {allowed}") from exc


def _request_json(
    config: CliConfig,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = _build_request(config, method, path, payload=payload, query=query)
    try:
        with urlopen(request, timeout=config.timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise CliError(_http_error_message(exc)) from exc
    except URLError as exc:
        raise CliError(f"failed to connect to backend: {exc.reason}") from exc
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CliError(f"backend returned non-JSON response: {body[:200]}") from exc


def _build_request(
    config: CliConfig,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Request:
    url = f"{config.base_url}{path}"
    if query:
        clean_query = {key: value for key, value in query.items() if value is not None}
        url = f"{url}?{urlencode(clean_query)}"
    body = None
    headers = {"User-Agent": "lka-cli/0.1.0"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return Request(url, data=body, headers=headers, method=method)


def _http_error_message(exc: HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        detail = body.strip()
    else:
        detail = data.get("detail") or data
    return f"backend returned HTTP {exc.code}: {detail}"


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    text_rows = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in text_rows))
        if text_rows
        else len(header)
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in text_rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


class CliError(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
