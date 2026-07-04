# Local Knowledge Agent OS Backend

This repository contains the Linux backend scaffold for Local Knowledge Agent OS.
It focuses on a small, working HTTP service with SQLite persistence and a clear
path for future expansion.

## Current Scope

- FastAPI application with the implemented HTTP routes
- SQLite-backed storage for workspaces, tasks, traces, and confirmations
- Local-only default binding for development
- Docker and Docker Compose support
- Documentation for the current architecture and API contract

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
  storage/
    db.py            # SQLite bootstrap and connection helpers
docs/
  README.md
  project_overview.md
  api_contract.md
  backend_engineering_guide.md
  backend_implementation_plan.md
```

## Quick Start

```bash
cp .env.example .env
uv sync
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

The service listens on `http://127.0.0.1:8765` by default.

## Docker

```bash
docker compose up --build
```

The default compose file starts the API and a local Qdrant container.

## Implemented APIs

- `GET /health`
- `POST /workspaces/index`
- `POST /tasks/plan`
- `POST /tasks/run`
- `GET /tasks/{task_id}`
- `GET /capabilities`
- `GET /traces`
- `GET /traces/{trace_id}`
- `POST /confirmations/{confirmation_id}`

## Notes

- The runtime is intentionally lightweight and rule-based.
- High-risk actions are surfaced as confirmation requests.
- The docs in `docs/` are organized as:
  - `project_overview.md` for the mission and vision
  - `backend_engineering_guide.md` for backend architecture
  - `backend_implementation_plan.md` for delivery stages
  - `api_contract.md` for the HTTP interface
