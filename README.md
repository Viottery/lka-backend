# Local Knowledge Agent OS Backend

This repository contains the cross-platform Backend Core for Local Knowledge
Agent OS. It provides a local Agent runtime with SQLite persistence, native
Windows/Linux execution, mail and knowledge tools, and durable background work.

## Current Scope

- FastAPI Agent turns and SSE, persistent runs, safety reviews, and scoped child agents
- SQLite-backed sessions, projects, mail, matters, knowledge, and global/project memories
- Asynchronous memory extraction and context compaction with leases and recovery
- Optional read-only mail expert, public web search, and scheduled watch briefings
- Workspace files, command tools, instruction files, and shared frontend defaults
- Local-only default binding for development
- Native Windows/Linux backend execution
- Platform support for workspace path resolution and read-only filesystem scanning
- Documentation for the current architecture and API contract

Start with [current module flows](docs/current_module_flows.md) for the implemented
system and [the maintenance review](docs/project_maintenance_2026-10-03.md) for
validation results and remaining issues. The long-term roadmap also contains
historical stage descriptions; it is not the current feature inventory.

## Repository Responsibilities

- `app/`: backend application code, including API routes, runtime orchestration, schemas, platform helpers, and storage helpers.
- `debug_frontend/`: standalone Linux CLI debug frontend that talks to the backend over HTTP/SSE.
- `docs/`: canonical project documentation, execution rules, API contract, and implementation tracking.
- `scripts/`: local developer utilities, including smoke-test helpers.
- `pyproject.toml`: defines package metadata, Python version, runtime dependencies, and developer tooling.

## Project Layout

```text
app/
  api/
    main.py          # FastAPI app assembly
    routes/          # HTTP route handlers
    schemas.py       # Request and response models
  core/
    config.py        # Application settings
    agent_turn.py    # Main Agent turn loop
    llm/             # Async LLM clients, service, and audit/error handling
    tools.py         # Tool registry, specs, and executor contracts
    runtime_context.py # Deterministic runtime context helpers
    runtime.py       # Main runtime orchestration
  domains/
    mail.py          # Mail domain service and models
    matters.py       # Matter domain service and models
    memory.py        # Versioned memories, sources, and project scope
    projects.py      # Stable project identities and display names
    watch.py         # Watch definitions, occurrences, and briefings
  experts/
    mail.py          # Optional bounded read-only mail ChildExecutor
  integrations/
    outlook.py       # Microsoft Graph / Outlook sync integration
  tool_packages/
    mail.py          # Agent-visible mail tool package
    matter.py        # Agent-visible matter tool package
    runtime.py       # Deterministic runtime context tools
  platform/
    detect.py        # Runtime platform detection
    paths.py         # Workspace path resolution
    filesystem.py    # Cross-platform workspace metadata scanning
  storage/
    db.py            # SQLite bootstrap and connection helpers
debug_frontend/
  cli.py             # Standalone Linux HTTP/SSE CLI frontend
docs/
  README.md
  project_overview.md
  api_contract.md
  backend_engineering_guide.md
  backend_implementation_plan.md
  platform_support.md
  ai_coding_standard.md
  mvp_todolist.md
scripts/
  run_agent_turn.py  # Run one real agent turn without starting the HTTP API
```

## Linux CLI Frontend

The package exposes a small HTTP CLI frontend named `lka`. Start the backend
first, then run CLI commands from another Linux shell:

```bash
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
uv run lka health
uv run lka capabilities
uv run lka sessions list
uv run lka ask --session-id cli_smoke "搜索一下NTUSO的audition要求，我要怎么做？"
```

`lka ask` uses `POST /agent/turn/stream` by default. Final answer token deltas
are printed to stdout as they arrive; Agent progress goes to stderr. Control
process visibility with:

```bash
uv run lka ask --agent-events hidden "只显示最终回答"
uv run lka ask --agent-events collapsed "显示折叠过程和最终回答"
uv run lka ask --agent-events expanded "显示完整事件 payload"
```

For a full debug run, use expanded Agent events. Answer text is printed only
when real `llm_delta` chunks or the final answer arrive from the backend:

```bash
uv run lka ask --agent-events expanded "展示完整运行过程"
```

Interactive mode keeps a session across turns and supports `/agent
hidden|collapsed|expanded` plus `/quit`:

```bash
uv run lka chat --session-id cli_chat
```

## Quick Start: Linux

```bash
cp .env.example .env
uv sync
uv run python scripts/start_backend.py personal
```

The default `LKA_DATA_DIR=./data/runtime` is the personal runtime boundary: it
contains the SQLite database, imported documents, local mail state, run logs,
tokens, and local embedding models. Test fixtures live under `evals/fixtures/`
and tests/evaluations use temporary data directories, so do not import test
samples into `data/runtime`.

## Backend Profiles

Use the profile launcher instead of manually exporting runtime variables:

```bash
# Personal data, real config/local.toml, default port 8765.
uv run python scripts/start_backend.py personal

# Ephemeral test data, mock provider/mail config, default port 8766.
uv run python scripts/start_backend.py test

# Keep an inspectable but isolated test database after the server exits.
uv run python scripts/start_backend.py test --test-data-dir ./data/test-runtime
```

`test` always overrides `LKA_DATA_DIR` and `LKA_LOCAL_CONFIG`; it cannot use
`--reload`. This prevents test requests from syncing personal mail, using a
real LLM provider, or writing to `data/runtime`. `personal --reload` is allowed
for local development.

## Quick Start: Windows PowerShell

```powershell
Copy-Item .env.example .env
uv sync
uv run python scripts/start_backend.py personal
```

The service listens on `http://127.0.0.1:8765` by default.

## Agent Turn Smoke Test

Run one real agent turn from the backend workspace without starting the HTTP API
or frontend:

```bash
uv run python scripts/run_agent_turn.py --session-id smoke_ntuso "搜索一下NTUSO的audition要求，我要怎么做？"
```

Use `--json` to print the full structured `AgentTurnResult`, including decision
events, tool events, progress events, and the local run log path. By default the
script does not call `runtime.start()`, so configured startup/background mail
sync will not run during the smoke test. Add `--start-runtime` only when that is
the behavior being tested.

## Quick Start: Windows Pet Frontend With WSL Backend

The current backend still runs in WSL. The adapted pet frontend should be
started from Windows PowerShell:

```powershell
D:\agent-bot-frontend\run-lka-windows.ps1
```

The script starts this backend inside WSL at `http://127.0.0.1:8765`, serves
the Windows-side pet chat frontend at `http://127.0.0.1:8780`, and opens the
chat UI with `backend=http://127.0.0.1:8765`.

## Local Provider Config

Provider, mail, and embedding settings live in a local TOML file:

```bash
cp config/local.example.toml config/local.toml
```

`config/local.toml` is ignored by git. Use it for local provider choices and
secret environment variable names:

- `llm`: named LLM clients, default/fallback client, model options, base URL,
  response mode, stream capability, and API key env var. A request can override
  `client_name` and `model` when the user switches models.
- `mail.outlook`: Microsoft Graph Device Code Flow settings. Outlook does not
  require storing an email password for this path.
- `mail.imap`: optional IMAP settings for providers that require an app password.
- `embedding`: local embedding provider config. The default is
  `BAAI/bge-small-zh-v1.5`, with model files cached under
  `./data/runtime/models`.

On Windows PowerShell, set secrets outside the TOML file:

```powershell
$env:OPENAI_API_KEY="..."
$env:MS_GRAPH_CLIENT_ID="..."
```

On Linux/macOS:

```bash
export OPENAI_API_KEY="..."
export MS_GRAPH_CLIENT_ID="..."
```

Windows workspace paths should be sent as backend-local paths. Prefer `/` in
JSON to avoid escaping:

```json
{
  "workspace": "C:/Users/chuan/Documents/NTU",
  "source_frontend": "windows-native"
}
```

Linux workspace example:

```json
{
  "workspace": "/home/chuan/Documents/NTU",
  "source_frontend": "linux-native"
}
```

## WSL to Windows Sync

For one-way sync from the WSL repo into a Windows-local test copy:

```bash
scripts/sync_to_windows.sh /mnt/c/Users/chuan/projects/lka_backend
scripts/sync_to_windows.sh /mnt/c/Users/chuan/projects/lka_backend --apply
```

The default mode is a dry run. Add `--delete` only when the WSL copy is the
source of truth and the Windows target should mirror deletions.
Local secrets such as `config/local.toml`, token files, and `.env` are excluded;
create a separate `config/local.toml` on Windows when needed.

## Implemented APIs

- `GET /health`
- `POST /workspaces/index`
- `GET /capabilities`
- `POST /runtime/debug`
- `POST /agent/turn`
- `GET /sessions`
- `POST /sessions`
- `POST /mail/import`
- `GET /mail/search`
- `GET /mail/matters`
- `POST /mail/outlook/auth/start`
- `POST /mail/outlook/auth/complete`
- `POST /mail/outlook/sync`
- `GET /matters`
- `POST /matters`

## Platform Support

See `docs/platform_support.md` for the Windows/Linux native support strategy,
configuration, path handling rules, and test matrix.

## Notes

- The runtime is intentionally lightweight and does not perform task planning or execution.
- The docs in `docs/` are organized as:
  - `project_overview.md` for the mission and vision
  - `backend_engineering_guide.md` for backend architecture
  - `backend_implementation_plan.md` for delivery stages
  - `platform_support.md` for native Windows/Linux support
  - `api_contract.md` for the HTTP interface
  - `ai_coding_standard.md` for coding and reporting rules
  - `mvp_todolist.md` for the implementation checklist
