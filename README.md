# Local Knowledge Agent OS Backend

This repository contains the Linux backend scaffold for Local Knowledge Agent OS.
It focuses on a small, working HTTP service with SQLite persistence and a clear
path for future expansion.

## Current Scope

- FastAPI application with the implemented HTTP routes
- SQLite-backed storage for workspaces
- Local-only default binding for development
- Docker and Docker Compose support
- Documentation for the current architecture and API contract

## Repository Responsibilities

- `app/`: backend application code, including API routes, runtime orchestration, schemas, and storage helpers.
- `docs/`: canonical project documentation, execution rules, API contract, and implementation tracking.
- `Dockerfile`: builds the backend container image and starts the API server inside the container.
- `docker-compose.yml`: runs the local API stack and the Qdrant service with local port binding.
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
  storage/
    db.py            # SQLite bootstrap and connection helpers
docs/
  README.md
  project_overview.md
  api_contract.md
  backend_engineering_guide.md
  backend_implementation_plan.md
  ai_coding_standard.md
  mvp_todolist.md
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
- `GET /capabilities`

## Notes

- The runtime is intentionally lightweight and does not perform task planning or execution.
- The docs in `docs/` are organized as:
  - `project_overview.md` for the mission and vision
  - `backend_engineering_guide.md` for backend architecture
  - `backend_implementation_plan.md` for delivery stages
  - `api_contract.md` for the HTTP interface
  - `ai_coding_standard.md` for coding and reporting rules
  - `mvp_todolist.md` for the implementation checklist
