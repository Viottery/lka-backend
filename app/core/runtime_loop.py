"""Fixed-stage runtime loop for debug infrastructure validation."""

from __future__ import annotations

from hashlib import sha1
from typing import Any

from pydantic import BaseModel

from app.core.context import ContextAssembler, SessionContext, TaskContext
from app.core.events import EventRecord
from app.core.llm import LLMClient, LLMResponse
from app.core.retrieval import RetrievalProvider, RetrievalResult
from app.core.tools import MockToolExecutor, ToolInvocation, ToolResult
from app.core.tracing import TraceRecord, TraceRecorder


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class RuntimeDebugRun(BaseModel):
    trace_id: str
    session_context: SessionContext
    task_context: TaskContext
    events: list[EventRecord]
    retrieval_result: RetrievalResult
    tool_invocation: ToolInvocation
    tool_result: ToolResult
    llm_response: LLMResponse
    trace: TraceRecord


class RuntimeLoop:
    """Run a deterministic debug chain across context, retrieval, tool, LLM, and trace."""

    def __init__(
        self,
        *,
        context_assembler: ContextAssembler,
        retrieval_provider: RetrievalProvider,
        llm_client: LLMClient,
        tool_executor: MockToolExecutor,
        trace_recorder: TraceRecorder,
    ) -> None:
        self.context_assembler = context_assembler
        self.retrieval_provider = retrieval_provider
        self.llm_client = llm_client
        self.tool_executor = tool_executor
        self.trace_recorder = trace_recorder

    def _event(
        self,
        *,
        trace_id: str,
        sequence: int,
        event_type: str,
        session_id: str,
        context_id: str | None,
        payload: dict[str, Any],
    ) -> EventRecord:
        return EventRecord(
            event_id=_stable_id("evt", trace_id, str(sequence), event_type),
            event_type=event_type,
            session_id=session_id,
            context_id=context_id,
            payload=payload,
            status="completed",
        )

    def run_debug(
        self,
        *,
        session_id: str,
        workspace: str | None,
        user_input: str,
    ) -> RuntimeDebugRun:
        trace_id = _stable_id("trace", session_id, workspace, user_input)
        session_context_id = _stable_id("ctx_session", session_id)
        task_context_id = _stable_id("ctx_task", session_id, workspace, user_input)
        workspace_id = _stable_id("ws", workspace) if workspace is not None else None
        events: list[EventRecord] = []

        session_context = self.context_assembler.build_session_context(
            context_id=session_context_id,
            session_id=session_id,
            workspace=workspace,
            workspace_id=workspace_id,
            user_input=user_input,
        )
        events.append(
            self._event(
                trace_id=trace_id,
                sequence=1,
                event_type="session_context.updated",
                session_id=session_id,
                context_id=session_context.context_id,
                payload={"workspace": workspace, "goal_summary": session_context.goal_summary},
            )
        )

        task_context = self.context_assembler.derive_task_context(
            context_id=task_context_id,
            session=session_context,
        )
        events.append(
            self._event(
                trace_id=trace_id,
                sequence=2,
                event_type="task_context.derived",
                session_id=session_id,
                context_id=task_context.context_id,
                payload={"lifecycle_status": task_context.lifecycle_status},
            )
        )

        retrieval_result = self.retrieval_provider.retrieve(
            workspace=workspace,
            query=user_input,
            task_context=task_context,
        )
        task_context = self.context_assembler.enrich_task_context(
            task_context=task_context,
            related_files=retrieval_result.related_files,
            related_snippets=retrieval_result.related_snippets,
            source_ref=retrieval_result.provider,
        )
        events.append(
            self._event(
                trace_id=trace_id,
                sequence=3,
                event_type="retrieval.completed",
                session_id=session_id,
                context_id=task_context.context_id,
                payload={
                    "provider": retrieval_result.provider,
                    "related_file_count": len(retrieval_result.related_files),
                    "related_snippet_count": len(retrieval_result.related_snippets),
                },
            )
        )

        invocation_id = _stable_id("tool_invocation", trace_id, self.tool_executor.spec.name)
        tool_invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool=self.tool_executor.spec,
            session_id=session_id,
            context_id=task_context.context_id,
            input={
                "goal_summary": task_context.goal_summary,
                "related_file_count": len(task_context.related_files),
            },
        )
        tool_result = self.tool_executor.invoke(
            invocation_id=tool_invocation.invocation_id,
            task_context=task_context,
        )
        events.append(
            self._event(
                trace_id=trace_id,
                sequence=4,
                event_type="tool.completed",
                session_id=session_id,
                context_id=task_context.context_id,
                payload={
                    "tool_name": tool_result.tool_name,
                    "invocation_id": tool_result.invocation_id,
                    "status": tool_result.status,
                },
            )
        )

        llm_response = self.llm_client.complete(user_input=user_input, task_context=task_context)
        events.append(
            self._event(
                trace_id=trace_id,
                sequence=5,
                event_type="llm.completed",
                session_id=session_id,
                context_id=task_context.context_id,
                payload={
                    "provider": llm_response.provider,
                    "status": llm_response.status,
                    "prompt_summary": llm_response.prompt_summary,
                },
            )
        )

        trace = TraceRecord(
            trace_id=trace_id,
            session_id=session_id,
            context_id=task_context.context_id,
            status="completed",
            events=events,
            verification_clues=[
                "SessionContext was created or updated.",
                "TaskContext was derived and enriched with retrieval metadata.",
                "Mock tool and mock LLM completed without external side effects.",
            ],
        )
        trace_event = self._event(
            trace_id=trace_id,
            sequence=6,
            event_type="trace.recorded",
            session_id=session_id,
            context_id=task_context.context_id,
            payload={"trace_id": trace.trace_id, "event_count": len(events) + 1},
        )
        trace.events.append(trace_event)
        self.trace_recorder.persist_debug_run(trace)

        return RuntimeDebugRun(
            trace_id=trace_id,
            session_context=session_context,
            task_context=task_context,
            events=trace.events,
            retrieval_result=retrieval_result,
            tool_invocation=tool_invocation,
            tool_result=tool_result,
            llm_response=llm_response,
            trace=trace,
        )
