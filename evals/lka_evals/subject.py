"""Benchmark subject adapters."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.api.main import create_app
from app.core.config import get_settings
from app.core.llm import LLMResponseMode
from app.core.tools import ToolContext
from app.domains.matters import MatterCreateInput, MatterSourceLinkInput, MatterUpdateInput

from evals.lka_evals.fixtures import (
    apply_setup,
    prepare_filesystem_fixture,
    snapshot_filesystem_fixture,
)
from evals.lka_evals.log_parser import parse_agent_log
from evals.lka_evals.scripted_llm import EvalScriptedLLM


@dataclass
class EvalRunArtifact:
    suite_id: str
    case_id: str
    subject: str
    request: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    fixture_index: dict[str, Any] = field(default_factory=dict)
    log: dict[str, Any] = field(default_factory=dict)
    sse_frames: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None


@dataclass(frozen=True)
class RuntimeSubjectOptions:
    llm_mode: str = "scripted"
    local_config: Path | None = None


class RuntimeSubject:
    """Run a case by directly calling the in-process runtime."""

    name = "runtime"

    def __init__(self, options: RuntimeSubjectOptions | None = None) -> None:
        self.options = options or RuntimeSubjectOptions()

    def run_case(self, *, suite_id: str, case: dict[str, Any]) -> EvalRunArtifact:
        case_id = str(case.get("case_id") or "case")
        request_payload = _request_payload(suite_id=suite_id, case=case)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"lka_eval_{suite_id}_{case_id}_") as temp_dir:
            temp_path = Path(temp_dir)
            old_env = {
                "LKA_DATA_DIR": os.environ.get("LKA_DATA_DIR"),
                "LKA_LOCAL_CONFIG": os.environ.get("LKA_LOCAL_CONFIG"),
                "LKA_WORKSPACE_ROOTS": os.environ.get("LKA_WORKSPACE_ROOTS"),
            }
            os.environ["LKA_DATA_DIR"] = str(temp_path / "data")
            local_config = self._local_config_for(case)
            os.environ["LKA_LOCAL_CONFIG"] = (
                str(local_config) if local_config is not None else str(temp_path / "missing-local.toml")
            )
            filesystem_index = prepare_filesystem_fixture(
                temp_path=temp_path,
                setup=case.get("setup"),
            )
            if filesystem_index.get("workspace_root"):
                os.environ["LKA_WORKSPACE_ROOTS"] = str(filesystem_index["workspace_root"])
            get_settings.cache_clear()
            fixture_index: dict[str, Any] = {}
            try:
                app = create_app()
                runtime = app.state.runtime
                runtime.agent_turn_loop.default_rate_limit_wait_seconds = 0.0
                fixture_index = apply_setup(runtime, case.get("setup"))
                if filesystem_index:
                    fixture_index["filesystem"] = filesystem_index
                request_payload = _resolve_eval_placeholders(
                    request_payload,
                    fixture_index=fixture_index,
                    previous_results=[],
                )
                effective_llm_mode = self._llm_mode_for(case)
                if effective_llm_mode == "scripted":
                    runtime.agent_turn_loop.llm_client = EvalScriptedLLM(
                        {**case, "_eval_fixture_index": fixture_index}
                    )
                elif runtime.agent_turn_loop.llm_client is None:
                    raise RuntimeError(
                        "Real LLM mode requested, but no LLM client was configured. "
                        "Pass --local-config with a real provider config and ensure API key env vars are set."
                    )
                operation = str(case.get("operation") or "agent_turn")
                if operation == "agent_turn":
                    response = runtime.run_agent_turn(
                        session_id=request_payload.get("session_id"),
                        user_input=str(request_payload.get("user_input") or ""),
                        llm_client_name=_llm_option(request_payload, "client_name"),
                        llm_model=_llm_option(request_payload, "model"),
                        llm_response_mode=_response_mode(request_payload),
                    )
                    result = response.model_dump(mode="json")
                    result["safety_reviews"] = [
                        review.model_dump(mode="json")
                        for review in runtime.agent_run_manager.list_safety_reviews(response.run_id)
                    ]
                    filesystem_snapshot = snapshot_filesystem_fixture(fixture_index)
                    if filesystem_snapshot:
                        result["filesystem_snapshot"] = filesystem_snapshot
                    log = parse_agent_log(result.get("log_path"))
                else:
                    result = _run_direct_operation(
                        runtime=runtime,
                        case=case,
                        temp_path=temp_path,
                        fixture_index=fixture_index,
                    )
                    filesystem_snapshot = snapshot_filesystem_fixture(fixture_index)
                    if filesystem_snapshot:
                        result["filesystem_snapshot"] = filesystem_snapshot
                    log = {}
                error = None
            except Exception as exc:
                result = {}
                log = {}
                error = {"type": type(exc).__name__, "message": str(exc)}
            finally:
                _restore_env(old_env)
                get_settings.cache_clear()
        completed = time.perf_counter()
        return EvalRunArtifact(
            suite_id=suite_id,
            case_id=case_id,
            subject=self.name,
            request=request_payload,
            result=result,
            fixture_index=fixture_index,
            log=log,
            timings={"wall_time_ms": round((completed - started) * 1000, 3)},
            error=error,
        )

    def _llm_mode_for(self, case: dict[str, Any]) -> str:
        value = case.get("llm_mode")
        if isinstance(value, str):
            return value
        return self.options.llm_mode

    def _local_config_for(self, case: dict[str, Any]) -> Path | None:
        value = case.get("local_config")
        if isinstance(value, str) and value:
            return Path(value)
        return self.options.local_config


class HttpSubject:
    """Run a case against an already running backend."""

    name = "http"

    def __init__(self, *, base_url: str, allow_setup: bool = False, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.allow_setup = allow_setup
        self.timeout = timeout

    def run_case(self, *, suite_id: str, case: dict[str, Any]) -> EvalRunArtifact:
        case_id = str(case.get("case_id") or "case")
        request_payload = _request_payload(suite_id=suite_id, case=case)
        started = time.perf_counter()
        fixture_index: dict[str, Any] = {}
        try:
            if self.allow_setup:
                fixture_index = self._apply_http_setup(case.get("setup"))
            result = self._request_json("POST", "/agent/turn", request_payload)
            log = parse_agent_log(result.get("log_path"))
            error = None
        except Exception as exc:
            result = {}
            log = {}
            error = {"type": type(exc).__name__, "message": str(exc)}
        completed = time.perf_counter()
        return EvalRunArtifact(
            suite_id=suite_id,
            case_id=case_id,
            subject=self.name,
            request=request_payload,
            result=result,
            fixture_index=fixture_index,
            log=log,
            timings={"wall_time_ms": round((completed - started) * 1000, 3)},
            error=error,
        )

    def _apply_http_setup(self, setup: dict[str, Any] | None) -> dict[str, Any]:
        setup = setup or {}
        fixture_index: dict[str, Any] = {"http_setup": True}
        mail_fixtures = setup.get("mail_fixtures")
        if isinstance(mail_fixtures, list):
            from evals.lka_evals.fixtures import load_fixture

            for fixture_name in mail_fixtures:
                if not isinstance(fixture_name, str):
                    continue
                relative_path = fixture_name if fixture_name.endswith(".json") else f"mail/{fixture_name}.json"
                payload = load_fixture(relative_path)
                self._request_json("POST", "/mail/import", payload)
        return fixture_index

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        payload_obj = json.loads(body)
        if not isinstance(payload_obj, dict):
            raise RuntimeError(f"HTTP response was not an object: {path}")
        return payload_obj


class StreamSubject(HttpSubject):
    """Run a streaming case against an already running backend."""

    name = "stream"

    def run_case(self, *, suite_id: str, case: dict[str, Any]) -> EvalRunArtifact:
        case_id = str(case.get("case_id") or "case")
        request_payload = _request_payload(suite_id=suite_id, case=case)
        request_payload.setdefault("llm", {})
        request_payload["llm"].setdefault("response_mode", "stream")
        started = time.perf_counter()
        fixture_index: dict[str, Any] = {}
        try:
            if self.allow_setup:
                fixture_index = self._apply_http_setup(case.get("setup"))
            frames = self._request_sse("/agent/turn/stream", request_payload)
            result = _result_from_sse_frames(frames)
            log = parse_agent_log(result.get("log_path"))
            error = None
        except Exception as exc:
            frames = []
            result = {}
            log = {}
            error = {"type": type(exc).__name__, "message": str(exc)}
        completed = time.perf_counter()
        return EvalRunArtifact(
            suite_id=suite_id,
            case_id=case_id,
            subject=self.name,
            request=request_payload,
            result=result,
            fixture_index=fixture_index,
            log=log,
            sse_frames=frames,
            timings={"wall_time_ms": round((completed - started) * 1000, 3)},
            error=error,
        )

    def _request_sse(self, path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            text = response.read().decode("utf-8")
        return _parse_sse(text)


def build_subject(
    *,
    name: str,
    base_url: str,
    allow_http_setup: bool,
    timeout: float,
    llm_mode: str = "scripted",
    local_config: Path | None = None,
) -> RuntimeSubject | HttpSubject | StreamSubject:
    if name == "runtime":
        return RuntimeSubject(
            RuntimeSubjectOptions(
                llm_mode=llm_mode,
                local_config=local_config,
            )
        )
    if name == "http":
        return HttpSubject(base_url=base_url, allow_setup=allow_http_setup, timeout=timeout)
    if name == "stream":
        return StreamSubject(base_url=base_url, allow_setup=allow_http_setup, timeout=timeout)
    raise ValueError(f"Unknown subject: {name}")


def _request_payload(*, suite_id: str, case: dict[str, Any]) -> dict[str, Any]:
    request_payload = case.get("request")
    if not isinstance(request_payload, dict):
        request_payload = {}
    case_id = str(case.get("case_id") or "case")
    payload = {
        "session_id": request_payload.get("session_id")
        or f"eval_{suite_id}_{case_id}",
        "user_input": request_payload.get("user_input") or case.get("user_input") or "",
    }
    llm_options = request_payload.get("llm")
    if isinstance(llm_options, dict):
        payload["llm"] = dict(llm_options)
    return payload


def _run_direct_operation(
    *,
    runtime: Any,
    case: dict[str, Any],
    temp_path: Path,
    fixture_index: dict[str, Any],
) -> dict[str, Any]:
    operation = str(case.get("operation") or "")
    params = case.get("params") if isinstance(case.get("params"), dict) else {}
    if operation == "health":
        return {"operation": operation, "output": runtime.health()}
    if operation == "capabilities":
        return {
            "operation": operation,
            "output": {
                "capabilities": [
                    capability.model_dump(mode="json")
                    for capability in runtime.list_capabilities()
                ]
            },
        }
    if operation == "workspace_index":
        workspace = params.get("workspace")
        if not isinstance(workspace, str):
            fixture = params.get("workspace_fixture")
            if not isinstance(fixture, str):
                raise ValueError("workspace_index requires workspace or workspace_fixture.")
            workspace = str(_prepare_workspace_fixture(temp_path=temp_path, fixture=fixture))
        response = runtime.index_workspace(
            workspace=workspace,
            source_frontend=params.get("source_frontend")
            if isinstance(params.get("source_frontend"), str)
            else "linux-native",
            options=params.get("options") if isinstance(params.get("options"), dict) else None,
        )
        payload = response.model_dump(mode="json")
        fixture_index.setdefault("workspaces", []).append(payload)
        return {"operation": operation, "output": payload}
    if operation == "matter_create":
        matter = runtime.create_matter(
            payload=MatterCreateInput.model_validate(params.get("matter") or {})
        )
        return {"operation": operation, "output": {"matter": matter.model_dump(mode="json")}}
    if operation == "matter_update":
        matter_id = str(params.get("matter_id") or "")
        matter = runtime.update_matter(
            matter_id=matter_id,
            payload=MatterUpdateInput.model_validate(params.get("update") or {}),
        )
        return {"operation": operation, "output": {"matter": matter.model_dump(mode="json")}}
    if operation == "matter_link_source":
        matter_id = str(params.get("matter_id") or "")
        matter = runtime.link_matter_source(
            matter_id=matter_id,
            source_link=MatterSourceLinkInput.model_validate(params.get("source_link") or {}),
        )
        return {"operation": operation, "output": {"matter": matter.model_dump(mode="json")}}
    if operation == "tool_sequence":
        return _run_tool_sequence(runtime=runtime, case=case, fixture_index=fixture_index)
    raise ValueError(f"Unsupported direct operation: {operation}")


def _run_tool_sequence(
    *,
    runtime: Any,
    case: dict[str, Any],
    fixture_index: dict[str, Any],
) -> dict[str, Any]:
    params = case.get("params") if isinstance(case.get("params"), dict) else {}
    steps = params.get("steps")
    steps = steps if isinstance(steps, list) else []
    session_id = str(params.get("session_id") or f"eval_tool_sequence_{case.get('case_id') or 'case'}")
    trace_id = str(params.get("trace_id") or f"eval_trace_{case.get('case_id') or 'case'}")
    tool_events: list[dict[str, Any]] = []
    previous_results: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        wait_seconds = step.get("wait_seconds")
        if isinstance(wait_seconds, int | float) and wait_seconds > 0:
            time.sleep(float(wait_seconds))
        tool_name = str(step.get("tool_name") or "")
        tool_input = _resolve_eval_placeholders(
            step.get("tool_input") if isinstance(step.get("tool_input"), dict) else {},
            fixture_index=fixture_index,
            previous_results=previous_results,
        )
        approved = bool(step.get("safety_review_approved"))
        result = runtime.tool_executor.execute(
            invocation_id=str(step.get("invocation_id") or f"eval_tool_{index:03d}"),
            tool_name=tool_name,
            tool_input=tool_input,
            context=ToolContext(
                session_id=session_id,
                trace_id=trace_id,
                safety_review_approved=approved,
                safety_review_id=str(step.get("safety_review_id") or f"eval_review_{index:03d}")
                if approved
                else None,
            ),
        )
        payload = result.model_dump(mode="json")
        tool_events.append({"tool_name": tool_name, "input": tool_input, "result": payload})
        previous_results.append(payload)
        if result.status != "completed" and not bool(step.get("continue_on_failure", True)):
            break
    return {"operation": "tool_sequence", "tool_events": tool_events}


def _resolve_eval_placeholders(
    value: Any,
    *,
    fixture_index: dict[str, Any],
    previous_results: list[dict[str, Any]],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_eval_placeholders(
                item,
                fixture_index=fixture_index,
                previous_results=previous_results,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_eval_placeholders(
                item,
                fixture_index=fixture_index,
                previous_results=previous_results,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if value == "$workspace_root":
        filesystem = fixture_index.get("filesystem")
        return str(filesystem.get("workspace_root") or "") if isinstance(filesystem, dict) else ""
    if value.startswith("$file:"):
        relative_path = value.split(":", 1)[1]
        filesystem = fixture_index.get("filesystem")
        files = filesystem.get("files") if isinstance(filesystem, dict) else {}
        item = files.get(relative_path) if isinstance(files, dict) else None
        return str(item.get("path") or "") if isinstance(item, dict) else ""
    if value == "$sha256_from_read_file":
        for result in reversed(previous_results):
            output = result.get("output") if isinstance(result, dict) else {}
            if isinstance(output, dict) and isinstance(output.get("sha256"), str):
                return output["sha256"]
        return ""
    if value == "$bash_session_id_from_last_run":
        for result in reversed(previous_results):
            output = result.get("output") if isinstance(result, dict) else {}
            if isinstance(output, dict) and isinstance(output.get("session_id"), str):
                return output["session_id"]
        return ""
    if value == "$bash_next_offset_from_last_read":
        for result in reversed(previous_results):
            output = result.get("output") if isinstance(result, dict) else {}
            if isinstance(output, dict) and isinstance(output.get("next_offset"), int):
                return output["next_offset"]
        return 0
    return value


def _prepare_workspace_fixture(*, temp_path: Path, fixture: str) -> Path:
    source = Path("evals/fixtures/workspaces") / fixture
    if not source.exists() or not source.is_dir():
        raise ValueError(f"Workspace fixture not found: {source}")
    target = temp_path / "workspaces" / fixture
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def _response_mode(request_payload: dict[str, Any]) -> LLMResponseMode:
    value = _llm_option(request_payload, "response_mode")
    if value == "stream":
        return LLMResponseMode.STREAM
    if value == "json":
        return LLMResponseMode.JSON
    return LLMResponseMode.TEXT


def _llm_option(request_payload: dict[str, Any], key: str) -> str | None:
    llm_options = request_payload.get("llm")
    if not isinstance(llm_options, dict):
        return None
    value = llm_options.get(key)
    return str(value) if isinstance(value, str) and value else None


def _restore_env(old_env: dict[str, str | None]) -> None:
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _parse_sse(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for raw_frame in text.strip().split("\n\n"):
        if not raw_frame.strip():
            continue
        fields: dict[str, str] = {}
        for line in raw_frame.splitlines():
            if ": " not in line:
                continue
            name, value = line.split(": ", 1)
            fields[name] = value
        data = json.loads(fields.get("data", "{}"))
        frames.append(
            {
                "id": fields.get("id"),
                "event": fields.get("event"),
                "data": data,
            }
        )
    return frames


def _result_from_sse_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    final_answer = ""
    run_completed: dict[str, Any] = {}
    for frame in frames:
        if frame.get("event") == "final_answer":
            final_answer = str(frame.get("data", {}).get("message") or "")
        if frame.get("event") == "run_completed":
            run_completed = frame.get("data", {})
    payload = run_completed.get("payload") if isinstance(run_completed, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    return {
        "run_id": run_completed.get("run_id"),
        "answer": final_answer,
        "selected_package": payload.get("selected_package"),
        "log_path": payload.get("log_path"),
    }


def run_async(value: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(value)
