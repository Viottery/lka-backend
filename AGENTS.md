# AGENTS.md

This repository is **Local Knowledge Agent OS**, a local Python backend for a
knowledge-augmented desktop Agent. Keep this file short: it records only
project-specific rules. Use `docs/` for full architecture details.

## Source Of Truth

Read these before meaningful changes:

- `docs/project_overview.md`
- `docs/backend_engineering_guide.md`
- `docs/backend_implementation_plan.md`
- `docs/api_contract.md`
- `docs/mvp_todolist.md`

`docs/mvp_todolist.md` is the current execution queue. `docs/backend_implementation_plan.md`
is the long-term roadmap, not an automatic task list.

## Backend Startup

Use native Python execution; Docker is not maintained for this repository.
Run the backend in a host-accessible environment, such as the user's native
shell, WSL shell, or an approved host-network command session. Do not rely on a
network-isolated sandbox for a user-facing server: uvicorn may start there, but
`127.0.0.1:8765` can be unreachable from the user's browser, frontend, or other
host commands.

Prepare local config when missing:

```bash
cp .env.example .env
cp config/local.example.toml config/local.toml
```

Install / sync dependencies:

```bash
uv sync
```

Start the backend on the default local address:

```bash
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

If the execution environment cannot write to the default `uv` cache under the
home directory, keep the cache inside the repository:

```bash
uv --cache-dir .uv-cache run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

Verify the running backend:

```bash
curl -sS http://127.0.0.1:8765/health
```

Expected health response includes:

```json
{"status":"ok","version":"0.1.0","service":"local-knowledge-agent-os"}
```

## Current MVP Direction

- The MVP priority is the mail-processing assistant.
- Workspace features remain as supporting context providers.
- Do not add mail-specific Agent endpoints such as `/mail/process`.
- Mail work must go through the generic Agent turn and the `mail` tool package.

## Directory Boundaries

- `app/core/`: Agent Harness core, LLM, session/context, run/trace, safety.
- `app/domains/`: deterministic domain services. No LLM calls here.
- `app/tool_packages/`: Agent-visible tool package implementations.
- `app/integrations/`: third-party service integrations.
- `app/platform/`: OS/path/filesystem adaptation.

Do not put domain services, tool package implementations, or third-party
integration logic back into `app/core/`.

## Agent Harness Rules

- LLM reasoning, tool choice, observations, and feedback belong to Agent Loop.
- Tool Package Registry exposes packages first; expand a package before calling tools.
- Each Agent step may either call one tool or produce a final answer.
- Tool execution must go through Tool Executor schema validation.
- Every Agent-visible tool must explicitly declare `read_only`. Treat
  `read_only != true` as requiring the mandatory safety review gate before
  execution. The gate may run in `skip`, `llm`, or `manual` mode, but the review
  record must still exist.
- Command execution belongs in the `bash` tool package. `bash.run` uses a
  conservative read-only whitelist per command; anything outside the whitelist
  is non-read-only and must pass safety review. Its default `cwd` is the current
  session workspace when configured, otherwise the first configured workspace root;
  relative `cwd` values resolve under that root, and
  commands receive `workspace_root`, `WORKSPACE_ROOT`, `LKA_WORKSPACE_ROOT`, and
  `LKA_WORKSPACE_ROOTS` environment variables.
- Agent core must remain package/domain agnostic. Do not hard-code concrete
  package names, tool names, domain workflows, routing keywords, or tool-call
  examples in `app/core/` prompts or fallback logic.
- Package/domain behavior belongs in `app/tool_packages/` metadata, tool
  descriptions, schemas, hints, and domain services. Agent core may consume
  registry metadata, but it must not encode package-specific policy itself.
- Package-specific constraints, such as read/write boundaries or cross-package
  workflows, must be represented by tool package metadata and enforced through
  registered tools, not by special cases in Agent core.
- `selected_package` is a deprecated compatibility/run-log field equivalent to
  `initial_package`. Do not pass it into decision, decision repair, tool result
  check, or answer prompts. Use `expanded_packages`, `used_packages`, and
  `active_package` for cross-package run state.
- Long tool results may be compacted before entering later LLM prompt
  observations. Full tool results must remain in `tool_events` and local run
  logs.
- `filesystem.read_file` and `filesystem.edit_file` resolve relative paths from
  the current session workspace when configured, otherwise the first configured
  workspace root, and support `workspace_root`,
  `WORKSPACE_ROOT`, and `LKA_WORKSPACE_ROOT` variable expansion in path fields.

## LLM And Run Requirements

- Models and clients must not be hard-coded in business logic.
- LLM selection priority is request override, then session preference, then config default.
- LLM calls must be async-compatible and must not block the FastAPI event loop.
- Stream and non-stream replies should share the same LLM service core.
- Provider errors must be classified and recorded in run log / audit, not collapsed into generic failures.

## Local Data And Safety

- The backend targets native Windows/Linux Python. Docker is not maintained.
- SQLite is the local persistence baseline.
- Default bind address is `127.0.0.1:8765`, but it must remain configurable.
- `config/local.toml` and `data/agent_logs/` must stay out of git.
- Run logs may contain prompts, LLM output, and mail bodies; keep them local.
- Do not store full prompts in session metadata or normal API responses.
- High-risk or write-capable tools must support confirmation.
- Agent must not get raw database write access; writes go through registered domain tools.
