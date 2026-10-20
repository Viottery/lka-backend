"""Session-local continuation for derived context and its original messages."""

from contextlib import closing

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.context_sources import source_passages
from app.core.tools import ToolPackageSpec, ToolResult, ToolSpec

CONTEXT_PACKAGE = ToolPackageSpec(
    name="context", description="Revisit session conversation state and original source messages omitted from the prompt.",
    decision_hints=[
        "context.read pages derived records of this session, not durable memory or verified source documents.",
        "Use context.source with a returned source_fragments passage_id (or message seq for legacy records) to inspect original wording; assistant reports are not verified tool evidence.",
        "Follow next_offset for omitted material. A cached context version is not a new source fetch or permission.",
        "Internal tags identify lifecycle for inspection only; current user corrections override older derived state.",
    ],
)


class ContextSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: int | None = Field(default=None, ge=1)
    passage_id: str | None = Field(default=None, pattern=r"^p_[0-9]+_[0-9]+_[0-9]+_[0-9a-f]{16}$", max_length=150)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)

    @model_validator(mode="after")
    def selector(self):
        if (self.seq is None) == (self.passage_id is None):
            raise ValueError("Choose exactly one of passage_id or seq")
        return self


class ReadContextRecordsTool:
    spec = ToolSpec(
        name="context.read", package="context", type="local_tool", read_only=True,
        description="Read bounded complete context records from the current session; optionally search literal text. Returns version, source message seqs, and pagination. Derived state is not source verification.",
        scope_filtering_required=True, output_selected_content=True,
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "maxLength": 200},
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "revision": {"type": "integer", "minimum": 1},
        }},
    )

    def __init__(self, store):
        self.store = store

    def invoke(self, *, invocation, context):
        if context.tool_view is not None:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Child runs cannot expand unrestricted parent history.")
        try:
            snapshot = self.store.snapshot(context.session_id, include_history=False)
            if invocation.input.get("revision", snapshot["revision"]) != snapshot["revision"]:
                raise ValueError("Context version changed; restart paging")
            query = invocation.input.get("query", "").casefold()
            records = [r for r in snapshot["records"] if r["validity"] in {"current", "disputed"}
                       and query in (r["content"] + " " + r["topic"] + " " + r["subject"]).casefold()]
            records.sort(key=lambda r: (-r["priority"], -r["last_source_seq"], r["record_id"]))
            offset, limit = invocation.input.get("offset", 0), invocation.input.get("limit", 3)
            page = records[offset:offset+limit]
            # Source quotes are separately read with context.source, not duplicated into every page.
            view = [{**{k: v for k, v in r.items() if k != "sources"},
                     "source_messages": sorted({s["seq"] for s in r["sources"]}),
                     "source_fragments": [{k: s[k] for k in ("passage_id", "seq", "start", "end", "role", "created_at") if k in s}
                                          for s in r["sources"]]} for r in page]
            output = {"revision": snapshot["revision"], "covered_seq": snapshot["covered_seq"],
                      "records": view, "total": len(records), "offset": offset,
                      "next_offset": offset+len(page) if offset+len(page) < len(records) else None,
                      "source_reader": "context.source", "verification": "derived_state_only"}
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed", output=output)
        except ValueError as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="rejected", error=str(exc))


class ReadContextSourceTool:
    spec = ToolSpec(
        name="context.source", package="context", type="local_tool", read_only=True,
        description="Read an exact source passage by passage_id, or a full original message by seq, in the current session. Character offset is relative to the selected passage/message. No tool re-execution or source-file access; agent claims remain unverified reports.",
        scope_filtering_required=True, output_selected_content=True,
        input_schema={"type": "object", "additionalProperties": False,
                      "oneOf": [{"required": ["seq"]}, {"required": ["passage_id"]}], "properties": {
            "seq": {"type": "integer", "minimum": 1},
            "passage_id": {"type": "string", "pattern": "^p_[0-9]+_[0-9]+_[0-9]+_[0-9a-f]{16}$", "maxLength": 150},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000},
        }},
    )

    def __init__(self, store):
        self.store = store

    def invoke(self, *, invocation, context):
        if context.tool_view is not None:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name,
                              status="rejected", error="Child runs cannot expand unrestricted parent history.")
        try:
            if ("seq" in invocation.input) == ("passage_id" in invocation.input):
                raise ValueError("Choose exactly one of passage_id or seq")
            params = ContextSourceInput.model_validate(invocation.input)
            passage_id = params.passage_id
            if passage_id is not None:
                seq = int(passage_id.split("_")[1])
            else:
                seq = params.seq
            with closing(self.store.connect()) as conn, conn:
                conn.execute("BEGIN")
                self.store.scope(conn, context.session_id)
                row = conn.execute("SELECT * FROM agent_session_context_messages WHERE session_id=? AND seq=?",
                                   (context.session_id, seq)).fetchone()
            if row is None:
                raise ValueError("Message unavailable in this session")
            start, stop = 0, len(row["content"])
            if passage_id is not None:
                passage = next((p for p in source_passages(context.session_id, dict(row)) if p["passage_id"] == passage_id), None)
                if passage is None:
                    raise ValueError("Source passage unavailable or changed in this session")
                start, stop = passage["start"], passage["end"]
            offset = params.offset
            end = min(stop, start+offset+params.max_chars)
            if offset > stop-start:
                raise ValueError("Offset exceeds source length")
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="completed", output={
                "seq": row["seq"], "role": row["role"], "created_at": row["created_at"],
                "content": row["content"][start+offset:end], "offset": offset, "total_chars": stop-start,
                "next_offset": end-start if end < stop else None,
                **({"passage_id": passage_id, "source_start": start, "source_end": stop} if passage_id is not None else {}),
                "upstream_verification": "conversation_only",
            })
        except ValueError as exc:
            return ToolResult(invocation_id=invocation.invocation_id, tool_name=self.spec.name, status="rejected", error=str(exc))
