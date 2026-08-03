# Local Knowledge Agent OS Backend

This repository contains the cross-platform Backend Core for Local Knowledge
Agent OS. It focuses on a small, working HTTP service with SQLite persistence,
native Windows/Linux execution support, and a clear path for future expansion.

## Current Scope

- FastAPI application with the implemented HTTP routes
- SQLite-backed storage for workspaces
- Local-only default binding for development
- Native Windows/Linux backend execution
- Docker and Docker Compose support as an optional runtime path
- Platform support for workspace path resolution and read-only filesystem scanning
- Documentation for the current architecture and API contract

## Repository Responsibilities

- `app/`: backend application code, including API routes, runtime orchestration, schemas, platform helpers, and storage helpers.
- `docs/`: canonical project documentation, execution rules, API contract, and implementation tracking.
- `Dockerfile`: builds the backend container image and starts the API server inside the container.
- `docker-compose.yml`: optional local container stack with Qdrant and local port binding.
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
    runtime.py       # Main runtime orchestration
  platform/
    detect.py        # Runtime platform detection
    paths.py         # Workspace path resolution
    filesystem.py    # Cross-platform workspace metadata scanning
  storage/
    db.py            # SQLite bootstrap and connection helpers
docs/
  README.md
  project_overview.md
  api_contract.md
  backend_engineering_guide.md
  backend_implementation_plan.md
  platform_support.md
  ai_coding_standard.md
  mvp_todolist.md
```

## Quick Start: Linux

```bash
cp .env.example .env
uv sync
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

## Quick Start: Windows PowerShell

```powershell
Copy-Item .env.example .env
uv sync
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

The service listens on `http://127.0.0.1:8765` by default.

## Local Provider Config

Provider, mail, and embedding settings live in a local TOML file:

```bash
cp config/local.example.toml config/local.toml
```

`config/local.toml` is ignored by git. Use it for local provider choices and
secret environment variable names:

- `llm`: third-party LLM API provider, model, base URL, and API key env var.
- `mail.outlook`: Microsoft Graph Device Code Flow settings. Outlook does not
  require storing an email password for this path.
- `mail.imap`: optional IMAP settings for providers that require an app password.
- `embedding`: local embedding provider config. The default is `BAAI/bge-m3`
  for multilingual retrieval, with model files cached under `./data/models`.

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

## Docker

```bash
docker compose up --build
```

The default compose file starts the API and a local Qdrant container.

## Implemented APIs

- `GET /health`
- `POST /workspaces/index`
- `GET /capabilities`
- `POST /runtime/debug`
- `POST /mail/import`
- `GET /mail/search`
- `POST /mail/process`
- `GET /mail/matters`
- `POST /mail/outlook/auth/start`
- `POST /mail/outlook/auth/complete`
- `POST /mail/outlook/sync`

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
