# LangGraph Core Migration Plan

## 1. Objective

Move Agent execution orchestration from the current imperative `AgentTurnLoop` to
LangGraph while preserving the project's own Agent semantics, Tool Registry, Tool
Executor, safety policy, LLM provider abstraction, RAG services, HTTP contract, and
frontend event contract.

LangGraph is the execution state machine and durable-control substrate. It is not a
replacement for domain services, tool definitions, provider adapters, or policy.

## Current Implementation Status

Status as of 2026-09-07:

- [x] Created a pre-migration Git/worktree snapshot outside the repository.
- [x] Added a runtime-facing `AgentTurnRunner` protocol.
- [x] Added LangGraph as a locked local dependency and implemented an
  `AgentGraphRunner` with a configurable in-memory or SQLite checkpointer.
- [x] Added `agent.orchestrator = "legacy" | "langgraph"`; legacy remains the default.
- [x] Split the initial scaffold into explicit `initialize_run`, `prepare_context`,
  `route_package`, `expand_package`, `decide_next_operation`,
  `validate_operation`, `safety_gate`, `execute_tool`, `build_observation`,
  `answer`, `verify_answer` and `finalize_run` nodes.
- [x] Verified basic synchronous and SSE tool-call runs through graph mode while
  preserving the existing result and event contracts.
- [x] Kept full tool results, LLM events and prompt observations out of graph
  checkpoints; graph state keeps project-owned immutable artifact references.
- [x] Added local SQLite graph persistence in LangGraph mode and an explicit runtime
  recovery path for an incomplete run after a backend restart.
- [x] Added a nested project-owned LangGraph tool lifecycle
  (`safety_gate` -> `execute_tool` -> `build_observation`) and durable tool claims.
- [x] Added project-owned durable SQLite persistence for runs, run events, safety
  reviews, large result artifacts and tool invocation claims.
- [x] Added deterministic tool invocation IDs and durable claims: a completed tool
  result is reused, while an invocation left executing after a crash is treated as
  uncertain and is never replayed automatically.
- [x] Replaced graph-mode blocking manual review with LangGraph `interrupt`/resume;
  the decision API resumes only on the first pending-to-terminal transition.
- [x] Restored completed results from the durable result artifact after a runtime
  restart, not only incomplete checkpointed runs.
- [x] Persisted the final result artifact before publishing `completed`, with an
  artifact-reference fallback for the final-checkpoint crash window.
- [x] Split manual-review state persistence from the interrupt node so the initial
  review progress is part of the resumable checkpoint.
- [x] Added sequence-based SSE reconnect, event/status queries and explicit
  cancellation; transport disconnects no longer cancel an active run.
- [x] Kept live token snapshots for SSE compatibility while persisting only deltas
  and rebuilding snapshots during durable replay.

The migration scope is complete: LangGraph owns the outer ReAct control flow,
checkpointing, interrupts and recovery. Project-owned `AgentTurnLoop` methods still
implement routing, decisions, prompts, observations and answers, while the existing
`ToolRegistry`, `ToolExecutor`, safety policy and domain packages remain authoritative.
The legacy loop is retained only as an explicit compatibility orchestrator, not as a
bridge node called by graph mode.

### Current Status And Checkpoint Semantics

The current graph stores two related but separate values:

- `status` is the public run lifecycle and uses the existing values `queued`,
  `running`, `waiting_confirmation`, `completed`, `failed` and `cancelled`.
- `phase` is the internal graph position. A successful tool turn progresses through
  context preparation, package routing/expansion, decision/validation, safety,
  execution, observation, answer, verification and finalization nodes.

The current checkpoint design is intentionally bounded:

- one `graph_thread_id` per `run_id`; conversation `session_id` is not reused as the
  graph thread;
- `checkpoint_schema_version = 1` is stored in graph state;
- `checkpoint_backend = "sqlite"` is the graph-mode default and stores snapshots in
  `data/runtime/agent_checkpoints.sqlite3`; `memory` remains available for disposable
  tests;
- SQLite persistence uses a short-lived synchronous `SqliteSaver` inside a dedicated
  worker thread. This keeps project synchronous services and nested provider streams
  off the FastAPI event loop while avoiding cross-loop SQLite connections;
- the checkpointer writes after the initial input and each graph node;
- checkpoint state contains request/run identity, status, phase, a compact working
  set and immutable references to project-owned runtime/result artifacts;
- complete prompts, raw mail bodies and full tool events are excluded;
- the full `AgentTurnResult` is persisted as a project-owned artifact and can be
  reconstructed after the process-local result cache is lost.

Recovery is explicit through `Runtime.resume_agent_run(run_id)` (or its async
equivalent). It restores the durable run record and resumes the latest interrupted
node; a completed run instead returns the persisted result artifact. Startup does not
automatically resume every incomplete run. Tool invocations use durable claims, and
session/finalization effects use stable IDs, so graph replay neither repeats completed
tool side effects nor duplicates user/agent session messages or lifecycle events.

## 2. Scope And Non-goals

In scope:

- Explicit graph nodes, conditional transitions, checkpoints, interrupts and resume.
- Durable run state for manual safety review, cancellation, failed-run diagnosis and
  recovery after a backend restart.
- Preservation of the existing `/agent/turn`, `/agent/turn/stream`, and safety-review
  API shapes while changing their internal implementation.
- A compatibility migration with a temporary legacy/langgraph runtime switch.

Out of scope:

- Replacing `ToolRegistry`, `ToolExecutor`, domain services, RAG, session context, or
  `LLMService` with LangChain abstractions.
- Using a prebuilt LangChain/LangGraph agent that owns prompts, tool selection or
  safety policy.
- Changing package-specific rules in `app/core/`; package behavior remains registry
  metadata and tool-schema driven.
- Treating this migration as a fix for retrieval quality, model quality, or provider
  latency.

## 3. Baseline To Preserve

The current `AgentTurnLoop` owns a step-limited route -> decision -> tool ->
observation -> decision -> answer loop. `ToolExecutor` is the only Agent-visible
tool execution path. Non-read-only tools require a recorded safety review. API SSE
already exposes typed progress and token-delta events.

The following contracts must stay externally stable during migration:

- `AgentTurnRequest` and `AgentTurnResponse`.
- Existing run IDs, trace IDs, progress-event types and `llm_delta` display targets.
- The package-first registry, explicit expansion, and one-tool-per-decision rule.
- Native Function Calling preference with existing operation-first JSON fallback.
- `selected_package` remains compatibility/log-only; graph decisions use initial,
  active, expanded and used package state.

## 4. Target Ownership Model

```text
FastAPI route / SSE adapter
  -> AgentGraphRunner
       -> LangGraph StateGraph + checkpointer
            -> existing Agent decision services
            -> existing SafetyPolicy and ToolExecutor
            -> existing LLMService and provider adapters
            -> existing SessionService / context services
            -> existing RunEventPublisher / local run log writer
  -> domain services and integrations
```

### 4.1 LangGraph owns

- node scheduling, loops and conditional edges;
- graph state transitions and checkpoint persistence;
- manual-review interrupt / resume lifecycle;
- graph-level cancellation and terminal state transitions;
- internal graph event stream.

### 4.2 Project code continues to own

- prompt construction, model invocation, Function Calling and JSON fallback;
- operation parsing, schema validation, repair and local feedback;
- Tool Package metadata, package expansion and tool availability;
- tool execution, `read_only` policy, safety-review decision policy and audit fields;
- observation compaction, sensitive-data filtering, RAG retrieval and answer content;
- the public SSE protocol and local Markdown run log format.

## 5. State, Identity And Persistence

### 5.1 Identifiers

- `session_id`: conversation and context-window identity. It is not a graph thread.
- `run_id`: one user-triggered execution and public API identifier.
- `graph_thread_id`: durable LangGraph checkpoint identity. Generate one per `run_id`,
  such as `agent_run_<id>`, and store it with the run.
- `trace_id`: cross-layer observability identifier.
- `invocation_id`: deterministic tool-attempt identity, derived from run ID, decision
  step and operation position. It is the idempotency key for side-effecting tools.

Do not use `session_id` as `graph_thread_id`: concurrent turns in one conversation
would otherwise share mutable graph state and recovery history.

### 5.2 AgentGraphState

State must be Pydantic/JSON serializable and bounded. A proposed shape is:

```text
run: run_id, graph_thread_id, session_id, trace_id, status, timing, cancellation
request: user_input, LLM selection, response mode, workspace context reference
planning: route, initial_package, active_package, expanded_packages, used_packages
tools: expanded_tool_descriptors, pending_operation, invocation counter
evidence: compact observations, tool-event references, verification warnings
safety: pending_review ID, approved/rejected decision, resume payload
answer: final answer, terminal reason, error classification
```

Full prompts, unbounded mail bodies and raw tool outputs must not become normal graph
state. Keep full results in existing tool events and local run logs; state receives a
budgeted observation plus stable event/source references. Checkpoint payloads must be
subject to the same local privacy and retention policy as run logs.

### 5.3 Durable Stores

Introduce project-owned repository interfaces rather than allowing graph code to
write directly to arbitrary tables:

- `AgentRunRepository`: runs, public status, events, result snapshot and graph thread
  mapping. It replaces the in-memory-only run manager.
- `SafetyReviewRepository`: review records and idempotent decisions.
- LangGraph SQLite checkpointer: graph snapshots keyed by `graph_thread_id`.

The initial production backend may use SQLite because it matches the project storage
baseline. Schema changes, retention duration, cleanup and local file permissions must
be separately reviewed before enabling durable production checkpoints.

## 6. Graph Nodes And Transitions

```text
initialize_run
  -> prepare_context
  -> route_package
  -> expand_initial_package
  -> decide_next_operation
  -> validate_operation
  -> safety_gate --------- interrupt(manual review) -> resume_safety_gate
  -> execute_tool
  -> build_observation
  -> decide_next_operation
  -> answer
  -> finalize_run
```

### 6.1 Node contract

| Node | Responsibility | Side effects |
| --- | --- | --- |
| `initialize_run` | Resolve session, identities, runtime options, initial event. | Durable run record. |
| `prepare_context` | Read SessionContext and bounded cached observations. | None. |
| `route_package` | Call existing route decision logic. | LLM audit/event only. |
| `expand_initial_package` | Read registry metadata and expose initial tool schemas. | None. |
| `decide_next_operation` | Reuse existing operation-first decision / native FC logic. | LLM audit/event only. |
| `validate_operation` | Validate action, package expansion and tool input locally. | Event only. |
| `safety_gate` | Evaluate `read_only`; record review; skip/LLM/manual policy. | Durable review record. |
| `execute_tool` | Invoke existing ToolExecutor exactly once for an invocation ID. | Tool side effect. |
| `build_observation` | Validate result, compact observation, update package state. | Tool/run event audit. |
| `answer` | Reuse existing answer stage and final verification. | LLM audit/event only. |
| `finalize_run` | Persist terminal result, context exchange and run log. | Durable final record/log. |

Conditional edges are data-driven only: `final_answer`, `expand_package`, `call_tool`,
rejected operation, malformed operation, cancellation, and terminal failure. No node
may branch on a concrete package or tool name.

### 6.2 Safety Interrupt Protocol

For `read_only != true`:

1. `safety_gate` creates an idempotent review record using `run_id + invocation_id`.
2. `skip` and `llm` modes record an immediate decision and return a normal graph
   transition.
3. `manual` mode emits the existing `safety_review_required` public event and calls
   LangGraph `interrupt` with only review-safe payload.
4. The existing decision endpoint persists approve/reject, then invokes
   `AgentGraphRunner.resume(run_id, decision)`.
5. The resumed graph re-enters `safety_gate`, reads the persisted decision and either
   enters `execute_tool` or records a rejected ToolResult observation.

`execute_tool` must never be in the same resumable node as review creation. Its
idempotency key must be checked by the execution/audit layer so retry, replay or
process recovery cannot duplicate a write.

### 6.3 Cancellation Protocol

- The SSE disconnect path records a durable cancellation request, not merely an
  in-memory flag.
- Every node boundary checks it before entering an LLM or tool operation.
- A running provider call cannot necessarily be force-stopped; its late result is
  discarded unless the run remains active.
- Cancellation is a terminal graph state and emits the existing cancellation event.

## 7. Events And HTTP Compatibility

Create a project-owned `RunEventPublisher` interface. Graph nodes publish canonical
events through it; the SSE adapter reads persisted events by sequence and emits the
same event names and payloads now consumed by the frontend.

Required mapping:

| Current public event family | Graph origin |
| --- | --- |
| `run_started`, package events, tool events, feedback | node lifecycle / publisher |
| `llm_delta` | existing LLM stream callback inside route, decision or answer node |
| safety-review events | `safety_gate` and resume path |
| `final_answer`, `run_completed`, `run_failed` | `answer` / `finalize_run` |

The frontend must not receive raw LangGraph events. This keeps the API stable and
allows later graph implementation changes without frontend coupling.

## 8. Migration Phases

### Phase 0: Characterize Current Semantics

1. Freeze representative tests for route, package expansion, valid and malformed
   tool calls, native Function Calling fallback, tool-output feedback, all three
   safety modes, cancellation, SSE token ordering and final-answer visibility.
2. Capture compatibility fixtures from deterministic/mock clients only; do not use
   personal mail or secrets in fixtures.
3. Define the canonical event sequence contract and allowable terminal statuses.

Acceptance: legacy tests establish a baseline that a graph runner must pass unchanged.

### Phase 1: Extract Stable Boundaries

1. Extract current loop subroutines into dependency-injected services for routing,
   decision, operation validation, observation building, answering and finalization.
2. Introduce `RunEventPublisher`, `AgentRunRepository` and `SafetyReviewRepository`
   interfaces with adapters over the existing in-memory implementation.
3. Keep `AgentTurnLoop` as the active implementation and verify no user-visible
   behavior changes.

Acceptance: existing test suite passes; no LangGraph dependency is yet required for
production execution.

### Phase 2: Graph Parity Runner

1. Add and lock a supported LangGraph version after a compatibility spike.
2. Implement `AgentGraphRunner` with an in-memory checkpointer and the target graph.
3. Add a configuration switch, `agent.orchestrator = legacy | langgraph`, defaulting
   to `legacy`.
4. Run the same deterministic contract tests against both runners and compare public
   result, tool sequence, review decision and SSE event order.

Acceptance: graph mode reaches semantic parity for read-only turns, package expansion,
tool rejection, malformed model output and normal final answers.

### Phase 3: Durable Run And Manual Review

1. [x] Add reviewed SQLite tables/repositories for runs, events, reviews, artifacts
   and tool invocation claims.
2. [x] Implement the `interrupt`/resume protocol without blocking a worker thread.
3. [x] Change safety-review API handlers to resume the graph by `run_id` only for the
   first pending-to-terminal review transition.
4. [x] Add recovery tests for interrupted, approved manual-review and completed runs.

Acceptance: manual review survives SSE disconnect and backend restart; no
side-effecting tool runs twice. Completed by the targeted LangGraph recovery and
safety-review tests.

### Phase 4: Streaming, Cancellation And Observability

1. Wire existing LLM token callbacks into graph-node event publication.
2. Preserve the public `display_target` distinction between agent-process deltas and
   assistant-answer deltas.
3. Persist event sequences and implement reconnect-from-sequence behavior.
4. Make cancellation durable and test disconnected clients, cancellation during LLM
   generation, and cancellation before tool execution.

Acceptance: frontend receives equivalent token/event behavior and can reconnect to a
running or paused run without losing state.

### Phase 5: Cutover And Removal

1. Make LangGraph the default runner after parity, manual-review and restart tests
   pass.
2. Keep `legacy` only for one release window and telemetry comparison.
3. Remove `AgentTurnLoop` orchestration, thread/Condition waiting paths and
in-memory-only run state once no caller relies on them.
4. Update architecture, API and operational documentation.

Acceptance: one graph-based control plane remains; no duplicate business logic or
parallel long-term orchestration implementations remain.

## 9. Test Matrix

- Unit: each node's input/output state, edge selection and state-budget invariants.
- Unit: invocation ID, review ID and idempotency behavior.
- Integration: legacy-vs-graph result/event parity using MockLLM.
- Integration: Function Calling and JSON fallback run through the same graph edges.
- Integration: manual approve/reject, process restart, reconnect and cancellation.
- Integration: non-read-only filesystem and bash calls execute once only.
- API: existing synchronous turn, SSE turn and safety-review endpoints retain schemas.
- Privacy: checkpoint excludes full raw prompt/tool body by default and respects
  existing redaction/deny behavior.
- Regression: existing agent, safety, LLM audit, sessions, file, bash and knowledge
  tests.

## 10. Decisions Required Before Phase 3

1. Persistent checkpoint retention, cleanup interval and disk quota.
2. Whether local checkpoint data needs encryption beyond host filesystem protections.
3. Exact SQLite schema and migration versioning strategy.
4. Cancellation semantics for a client-disconnected SSE request: cancel immediately,
   or allow an explicitly backgrounded run to continue.
5. Supported LangGraph version and upgrade policy after the Phase 2 compatibility
   spike.

## 11. Completion Criteria

The migration is complete only when LangGraph is the sole orchestration runtime and:

- all Agent-visible tools still execute only through ToolExecutor;
- all non-read-only operations still create a review record before execution;
- manual approval survives backend restart and cannot duplicate side effects;
- public HTTP/SSE event contracts remain compatible;
- token deltas continue to stream with correct display targets;
- run logs, audit events and failure classifications remain queryable;
- no package/domain-specific policy has been introduced into the core graph.
