# Local Knowledge Agent OS Eval Benchmarks

This directory contains reusable, deterministic benchmark tooling for the
current Agent backend. The first version focuses on directly computable metrics:
no LLM judge is required.

## Goals

- Evaluate current Agent Harness modules with isolated local data.
- Preserve a stable contract for future benchmark suites and reports.
- Support deterministic in-process runs and optional real backend HTTP/SSE runs.
- Make run logs, tool events, LLM audit, latency, token usage, and side effects measurable.

## Subjects

- `runtime`: default. Creates an isolated in-process FastAPI runtime per case,
  sets a temporary `LKA_DATA_DIR`, injects fixtures, and uses `EvalScriptedLLM`
  unless `--llm-mode real` or `case.llm_mode = "real"` is used.
- `http`: calls an already running backend via `POST /agent/turn`.
- `stream`: calls an already running backend via `POST /agent/turn/stream` and
  validates SSE contract metrics.

HTTP setup is disabled by default because it writes fixture data into the target
backend. Use `--allow-http-setup` only against a disposable backend.

## Run

```bash
uv --cache-dir .uv-cache run python -m evals.lka_evals.runner evals/suites/smoke.yaml --subject runtime
uv --cache-dir .uv-cache run python -m evals.lka_evals.runner evals/suites/mail_qa.yaml --subject runtime
```

Run isolated benchmark with the real configured LLM provider:

```bash
uv --cache-dir .uv-cache run python -m evals.lka_evals.runner \
  evals/suites/real_llm_hard_mail_tasks.yaml \
  --subject runtime \
  --llm-mode real \
  --local-config config/local.toml \
  --timeout 180
```

`--llm-mode real` still isolates local data by using a temporary `LKA_DATA_DIR`.
It does not replace the Agent LLM; route, decision, tool-result check, and
answer generation are performed by the real configured provider. Ensure the
provider API key environment variables referenced by `--local-config` are set.

Against a real local backend:

```bash
uv --cache-dir .uv-cache run python -m evals.lka_evals.runner evals/suites/mail_qa.yaml \
  --subject http \
  --base-url http://127.0.0.1:8765 \
  --allow-http-setup
```

Reports are written to `evals/reports/` as JSON and Markdown.

## Suite Format

Suite files are JSON-compatible YAML in this first version. This avoids adding a
YAML parser dependency while leaving the `.yaml` extension in place.

Each case supports:

- `case_id`
- `operation`: defaults to `agent_turn`; direct runtime operations include
  `health`, `capabilities`, and `workspace_index`
- `workflow`: currently `mail_qa` or `mail_to_matter`
- `setup.mail_fixtures`: virtual fixture datasets from `evals/fixtures/mail`
- `request.session_id`
- `request.user_input`
- `search_query`: query used by the scripted runtime LLM
- `scripted_answer`: deterministic answer for scripted runtime subject
- `matter`: payload used by `mail_to_matter`
- `tool_plan`: deterministic Agent-visible tool sequence for package tests
- `expect`: deterministic assertions and budgets

## Current Output Contract

The primary artifact is the current `AgentTurnResult`:

- `run_id`
- `session_id`
- `trace_id`
- `answer`
- `selected_package`
- `package_catalog`
- `session_context_window`
- `expanded_tools`
- `decision_events`
- `tool_events`
- `progress_events`
- `verification_warnings`
- `llm_events`
- `log_path`

Run log evaluation is based on the current markdown sections:

- `## User Input`
- `## Package Catalog`
- `## Session Context Window`
- `## Expanded Tools`
- `## Decision Events`
- `## Tool Events`
- `## Progress Events`
- `## Verification Warnings`
- `## LLM Events`
- `## Answer`

JSON sections must contain valid fenced JSON blocks. Future work can add a JSON
sidecar artifact, but the first version intentionally evaluates the current log.

## Implemented Metrics

- `no_runtime_error`
- `selected_package_match`
- `tool_sequence_exact_match`
- `tool_sequence_f1`
- `required_tool_called`
- `forbidden_tool_not_called`
- `answer_contains_all`
- `answer_contains_any`
- `answer_excludes_all`
- `output_contains_all`
- `output_equals`
- `capabilities_include`
- `workspace_min_files`
- `workspace_min_chunks`
- `tool_output_contains_all`
- `evidence_recall_at_k`
- `loaded_required_messages`
- `mail_search_recall_at_k`
- `mail_search_precision_at_k`
- `mail_search_mrr`
- `mail_search_forbidden_at_k`
- `matter_write_precision`
- `matter_source_link_recall`
- `tool_success_rate`
- `schema_rejection_count`
- `llm_call_count`
- `reported_token_total`
- `wall_time_ms`
- `run_log_completeness_rate`
- `sse_sequence_valid`

Mail search metrics are computed from `mail.search` tool outputs. They use
`expect.mail_search_relevant_external_ids` and
`expect.mail_search_forbidden_external_ids` when present, otherwise they reuse
`expect.evidence_external_ids` and `expect.forbidden_evidence_external_ids`.
`expect.mail_search_k` defaults to `10`. Recall and forbidden retrieval are
gating metrics; precision and MRR are quantitative by default and can be made
gating with `mail_search_min_precision_at_k` or `mail_search_min_mrr`.

## Data Isolation

`runtime` subject isolates each case by:

- creating a temporary data directory;
- setting `LKA_DATA_DIR` to that directory;
- setting `LKA_LOCAL_CONFIG` to a missing temp file so no real provider config is loaded;
- clearing `get_settings()` cache before and after each case;
- importing only virtual fixtures specified by the case.

No benchmark case should depend on or mutate the user's real mailbox.

## Current Suites

- `smoke`: eval plumbing and basic mail QA path.
- `mail_qa`: expanded virtual mailbox QA with distractors, cancellations,
  reschedules, deadlines, bilingual messages, timezone notes, and attachments.
- `hard_mail_tasks`: difficult but feasible mail/cross-package tasks with dense
  distractors, conflicting update chains, multi-evidence aggregation, and
  source-linked matter creation.
- `real_llm_hard_mail_tasks`: same spirit as hard mail tasks, but intentionally
  contains no scripted search query or scripted answer. Use it with
  `--llm-mode real` to evaluate the full real LLM Agent loop.
- `mail_to_matter`: mail evidence to independent matter creation and source link.
- `matter_tools`: Agent-visible matter create/search/update/link tool sequence.
- `runtime_tools`: Agent-visible `runtime.now`.
- `workspace_context`: direct workspace indexing against a local fixture project.
- `capabilities`: direct health and capability catalog contracts.
- `failure_recovery`: deterministic failure-budget contracts.
- `stream_contract`: SSE contract for `--subject stream` against a disposable backend.
