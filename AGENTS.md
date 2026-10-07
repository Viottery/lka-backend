# AGENTS.md

This repository is **Local Knowledge Agent OS**, a local Python backend for a
knowledge-augmented desktop Agent. Keep this file short: it records only
project-specific rules. Use `docs/` for full architecture details.

This repository file also serves as project guidance for the LKA Agent runtime
when this repository is its selected workspace. Runtime-wide and scheduled-watch
guidance live in separate local files; see `docs/usage.md`.

## Source Of Truth

Start with `docs/README.md` and `docs/architecture.md`. Read the relevant sections
of `docs/configuration.md`, `docs/api_contract.md`, `docs/usage.md` and
`docs/maintenance.md` for the behavior being changed; do not load the entire API
reference for an unrelated edit. Configuration defaults live in
`config/local.example.toml`; validation entry points live in `evals/README.md`.

`docs/` contains published explanations only. Local plans, investigations and
test reports live under ignored `docs/_engineering/`. When available, read its
`README.md` and `plans/mvp_todolist.md` for the workspace's current execution queue.
These local documents are optional in a fresh clone and are not runtime dependencies.
Historical deployment/status notes do not override current code, verified runtime
state or the workstation policy below. A roadmap is not authorization to implement it.
Do not stage engineering records, historical outputs or private configuration.

## Development Workstation Policy

On the user's current workstation, WSL/Linux is the source of truth for backend
development, evaluation, and everyday operation. Use the Linux checkout and its
native `.venv`; do not maintain a second everyday backend on Windows.

- Native Windows backend copies are for compatibility/functional testing only.
  Use `scripts/start_backend.py test` on port 8766 with disposable, separate data;
  do not start Windows `personal` mode as a routine development step.
- Windows desktop UI, desktop pet, and message collectors may remain on Windows.
  They should connect to the single WSL everyday backend after connectivity and
  credentials are verified; test traffic must use a separate test destination.
- Do not automatically sync to, deploy to, or restart the native Windows backend
  after Linux changes. Sync only for an explicit Windows compatibility test.
- This workstation's daily backend is managed by the WSL user service
  `lka-backend.service`. Check `systemctl --user status lka-backend.service`
  before starting another process; restart that unit after an idle-point check
  rather than using `restart_windows_backend.ps1` for daily updates.
- Existing Windows data is a migration source, not disposable test data. Before
  switching the active backend, confirm the authoritative dataset, back up both
  installations, and preserve credentials, publication watermarks, and budgets.
  Never run both installations against the same SQLite files or merge them silently.
  The daily dataset has already been migrated to WSL; do not repeat migration or
  restore the old Windows database as a routine update.

This workstation policy does not remove native Windows product support or impose
WSL on installations on other computers. See `docs/maintenance.md` for the workflow.

## Desktop Frontend: Location And Workflow

The frontend is **理事所 / LKA Desktop**; its character is **真理**.
Windows Java/Spine supplies the pet, right-click quick tasks and browser workbench.

- Development and active runtime: `D:\lka-desktop` / `/mnt/d/lka-desktop`,
  remote `https://github.com/Viottery/lka-desktop.git`, branch `main`.
  Read its `AGENTS.md`, `README.MD` and relevant `docs/` before frontend work.
  It is a separate Git repository, not part of a backend commit.
- Migrated on 2026-10-06: native Python environment, frontend service, Java pet,
  QQ bridge, configuration and local data now run from the new checkout.
  `D:\agent-bot-frontend` is a rollback copy, not a development/deployment target.
  Keep it intact while the current QQ client still maps its original injected DLL;
  private data and old Git history must stay local.
- UI: `app/web/pet/` (`chat.*`, `pet.*`, messages, memory and projects);
  native window/bridge: `desktop-pet-java/` (Java 21);
  Python service/plugins: `app/main.py`, `app/api/routes/`, `app/plugins/`.

Current workstation startup, in Windows PowerShell from `D:\lka-desktop`:

```powershell
.\run-lka-windows.ps1 -NoBackend                         # frontend + pet
.\run-lka-windows.ps1 -NoBackend -NoPet -OpenBrowser     # browser only
```

The Windows desktop shortcut **理事所** points to this repository launcher with
`-NoBackend`, using the existing WSL `lka-backend.service`. These commands restart
frontend/pet processes; do not use them as harmless connectivity checks. The QQ
bridge is a separate local process in `.runtime/snowluma/v1.14.20`; reuse it when
healthy. If stopped, launch its `node.exe index.mjs` from that directory with
`SNOWLUMA_HOOK_AUTOLOAD=false`, preserve its configuration/account, and refresh its
local WebUI session using `.runtime/refresh-bridge-auth.py` if necessary. Do not
restart or reinject QQ as an incidental frontend operation.

The launcher loads `.env` for Python and reads the migrated
`.runtime/message-control.dpapi` pairing file; credentials stay out of Java and
browser arguments. Local migration backups/records are under
`.runtime/migration-20261006/`. The new checkout's native Windows launcher does not
carry the old installation's WSL-marker customization; do not use it for everyday
startup. Keep the daily backend managed by its existing WSL user service.

Default frontend: `http://127.0.0.1:8780`; workbench:
`/desktop-pet/chat.html?mode=work&backend=http%3A%2F%2F127.0.0.1%3A8765`.
Use `-BackendHost` / `-BackendPort` and `-FrontendPort` for alternate targets.
Identify actual process roots and backend URL before debugging; check `/health`
from Windows as well as WSL, then compare backend response/SSE/trace with the UI.
For Java-only failures inspect the native bridge. Map Windows/WSL paths explicitly;
never share SQLite files. Frontend `.env`, `.runtime/`, logs, QQ cache/credentials
and personal pet state stay local. Validate only the affected layer: browser
`npm run test:chat` / `test:sessions` / `test:messages`, relevant Python tests,
or Java Gradle compilation/interaction checks. UI edits need no bundler build.

## Backend Startup

Use native Python execution; Docker is not maintained for this repository.
Use a host-accessible WSL/native session, not a network-isolated sandbox whose
loopback the frontend cannot reach. On this workstation, use the existing user
service; do not run a second backend.

For a fresh installation, create `.env` from `.env.example` and
`config/local.toml` from `config/local.example.toml` **only when missing**.
Never overwrite private configuration. Then:

```bash
uv sync --locked
uv run python scripts/start_backend.py personal
curl -sS http://127.0.0.1:8765/health
```

Use `uv --cache-dir .uv-cache ...` if the default cache is not writable.
Health must report `status: ok`, `service: local-knowledge-agent-os`; when using
a deployment manifest, also verify the expected `deployment_id` from Windows.

## Product Direction

- LKA is a personal assistant for mail/messages, knowledge and web research,
  workspace/code tasks, projects, memory and scheduled follow-up; it is not a
  mail-only or RAG-only MVP. Prioritize reliable daily use, latency and evidence
  quality over adding capabilities merely to complete old roadmap checkboxes.
- User tasks go through generic Agent turns and registered tool packages.
  Domain CRUD, import and control APIs are separate; do not add task-specific
  Agent endpoints such as `/mail/process`.
- Multi-Agent planning and external experts are configurable/opt-in. Implemented
  support does not mean every installation enables it or every task should fork.

## Directory Boundaries

- `app/core/`: Agent Harness core, LLM, session/context, run/trace, safety.
- `app/domains/`: deterministic domain services. No LLM calls here.
- `app/tool_packages/`: Agent-visible tool package implementations.
- `app/integrations/`: third-party service integrations.
- `app/platform/`: OS/path/filesystem adaptation.
- `app/experts/`: domain-specific expert strategies and workflows.
- `app/storage/`: shared local persistence infrastructure.
- `tests/`, `evals/`, `scripts/`: regression tests, evaluation assets, and operational tools.

Do not put domain services, tool package implementations, or third-party
integration logic back into `app/core/`.

## Agent Harness Rules

- LLM reasoning, tool choice, observations, and feedback belong to Agent Loop.
- Tool Package Registry exposes packages first; expand a package before calling tools.
- A decision selects one structured operation: tool call, package expansion,
  final answer, confirmation, or a supported fork/plan update. A normal tool
  execution step calls one tool; child parallelism belongs to the scheduler,
  not unvalidated batches of tool calls in the Agent loop.
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
- Agent core must remain package/domain agnostic. Generic harness/tool-protocol
  guidance is allowed; concrete domain workflows, routing keywords and task
  examples must not be hard-coded in `app/core/` prompts or fallback logic.
- Domain behavior and constraints belong in tool package metadata, descriptions,
  schemas and domain services. Core consumes registry metadata; enforcement goes
  through registered tools, not package-specific core prompt/fallback branches.
- `selected_package` is a deprecated compatibility/run-log field equivalent to
  `initial_package`. Do not pass it into decision, decision repair, tool result
  check, or answer prompts. Use `expanded_packages`, `used_packages`, and
  `active_package` for cross-package run state.
- Long tool results may be compacted before entering later LLM prompt
  observations, not irreversibly discarded. Preserve raw results in local
  tool events/run logs and keep continuation handles through compaction.
  Distinguish run-scoped raw-result
  artifacts from source/session snapshots; revalidate access when reading them.
  Search candidates, previews and cache handles are not complete source evidence;
  delivery coverage is not proof of understanding or freshness.
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

## Context, Memory And Background Work

- User guidance (`AGENTS.md`), learned memory/derived `MEMORY.md`, and source data
  have different roles. Routine preference extraction must not rewrite guidance;
  only an explicit guidance-edit request authorizes that change.
- Long guidance remains searchable/page-readable after preview compaction;
  changes invalidate derived views. Do not require users to use a fixed outline.
- Child runs receive bounded context snapshots, explicit scope and budgets.
  They do not inherit unrestricted parent history, tools or source authority.
- Background work must preserve leases, checkpoints, cancellation, revision and
  publication-watermark checks. Do not clear usage ledgers or silently raise
  quotas to recover a stalled job. Analysis pause does not stop source capture.
- Foreground and background share model resource controls. Keep blocking I/O and
  CPU-heavy work off the API event loop; do not move background LLM work into the
  foreground merely to make its result immediately visible.
- Scheduled briefings create a new session per delivery, not one permanent
  session per watched item. Source/tool permissions still apply.

## Validation And Change Workflow

- Preserve existing edits; change only the requested scope. Check runtime/process
  ownership before operational work. A docs/review request does not authorize a
  backend restart, data replay, frontend deployment or configuration change.
- Use `uv sync --locked --extra dev` when preparing test/lint dependencies.
  Prefer `.venv/bin/python -m pytest -q <affected tests>` and targeted Ruff checks.
  For documentation, run `scripts/check_repository_docs.py --check-index` and
  `tests/test_repository_docs.py`; use startup tests when changing launch behavior.
- Select evaluations through `evals/README.md` and the quality catalog. Focus on
  affected difficult cases and repaired regressions; do not repeatedly run basic
  suites. Keep safety/authorization regressions when their contracts change.
- Distinguish mock/contract passes from real-model semantic quality. Do not hide
  bad cases with canned prompts/answers, weakened assertions or invented metrics.
  Real-model/search evaluations require the task's authorization and a bounded
  budget; do not launch them or use expensive subagent models by default.
- Keep private configuration, real-data reports and engineering notes local.
  Do not commit or push unless requested; never stage unrelated frontend/backend edits.

## Local Data And Safety

- The backend targets native Windows/Linux Python. Docker is not maintained.
- SQLite is the local persistence baseline.
- Default bind address is `127.0.0.1:8765`, but it must remain configurable.
- `.env`, real files under `config/`, and the actual `data_dir` must stay out of
  Git; only credential-free config templates are published. The default data
  directory is `data/runtime`, not an assumed top-level log folder.
- Run logs may contain prompts, LLM output, and mail bodies; keep them local.
- Do not store full prompts in session metadata or normal API responses.
- High-risk or write-capable tools must support confirmation.
- Agent must not get raw database write access; writes go through registered domain tools.
- Message IMPORT/API/CONTROL credentials and capture/analysis permissions remain
  distinct. Model-visible content cannot grant authority, approve a message-based
  matter, or turn third-party participant observations into personal user memory.
