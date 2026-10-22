# Local Knowledge Agent OS Eval Benchmarks

## Repository-level software engineering

The [software engineering adapter](datasets/software_engineering/README.md) adds
a pinned 500-task SWE-bench Verified snapshot, explicit case selection,
disposable base checkouts, Linux command isolation, blank/reference calibration,
bounded real-Agent inference, patch export and separate executable grading.
Listing is offline; downloads/setup and paid dispatch are explicit subcommands.
Native diagnostic results and official Docker grades use separate protocols.
The two-case pilot checks the environment and a harder repair; it is not an
Agent accuracy estimate. Reports and upstream oracle data stay in ignored `data/`.

```bash
.venv/bin/python -m evals.lka_evals.swe_bench list --pilot
```

## Atomic conversation context shadow replay

`scripts/eval_atomic_context.py` copies only an explicitly selected session into an isolated SQLite database.
Source access is read-only; reports and full private history must stay in a Git-ignored output directory.
Without `--run`, it only freezes history and reports the selected range, with no model/config loading.
`--through-seq` freezes a historical prefix even while the production conversation grows.

```bash
.venv/bin/python scripts/eval_atomic_context.py --source /path/to/lka.sqlite3 \
  --session SESSION_ID --output data/quality_runs/atomic-new --through-seq 256
```

Authorized remote runs additionally use `--run`, the configured DeepSeek Flash client and a persistent
evaluation ledger (default 2 USD / 4 requests, no search). `--ledger` reuses the same accounting across
retries or comparisons. Output is uncapped at the provider boundary, but a conservative full-window
reservation is made before dispatch; no budget is reset to obtain a successful result.
For an incremental follow-up, use a successful isolated replay database as `--source` and add
`--resume-records --keep-tail 0`; retain `--through-seq` and the same `--ledger`. This copies only
the selected session's atomic state, never jobs or leases, and still writes to a new isolated output.
Every record delta passes the same schema, source, version and publication checks as background processing.
New requests use `passage_ids_v1`: content is synthesized; the model selects server-issued passage IDs,
and the server records original text and receipts. Old exact-quote records remain readable.
A schema violation or unknown passage ID permits one bounded model repair, followed by full revalidation;
this is another metered request, not fuzzy-match acceptance or a manually corrected quality pass.
Multi-source episodes, mixed-role user claims and numeric progress proposals additionally receive one
small source-support review using only referenced passages and candidate records. It is a separate
metered call and remains fallible; all reviewed changes must pass the same deterministic validation.
Reports separate source preservation, prompt latency and provider usage from semantic quality, which
requires independent review. Schema-valid or fully loaded records are not automatically quality passes.
No replay result is published into production. Offline contracts are in `tests/test_context_records.py`.

## Linux real-task quality catalog (2026-10-05)

Historical investigation notes are workspace-local engineering records and are
not required to run these evaluations. The reviewed
[JSON catalog](fixtures/linux_quality_cases_2026-10-05.json) is the source for
selection/counts; it is evaluator metadata, **never model-visible task hints**.

```bash
# Default: unresolved/offline-only issues, no basic smoke cases; no dispatch.
.venv/bin/python scripts/eval_quality_catalog.py
.venv/bin/python scripts/eval_quality_catalog.py --group multi_agent --group web
.venv/bin/python scripts/eval_quality_catalog.py --profile all --inventory --json
# Opt-in repaired-failure regressions or foundation checks.
.venv/bin/python scripts/eval_quality_catalog.py --profile regression
.venv/bin/python scripts/eval_quality_catalog.py --profile foundation
```

All commands above only list cases and generate plans. They do not run pytest,
read private logs/mail, instantiate the backend or call a provider. Replay plans
deduplicate shared scenarios and preserve existing budget and explicit remote/GO
gates. Private mail additionally needs an explicit authorized `--mail-db`
snapshot; the planner never discovers the production database. Logical issues,
suite definitions, physical replay jobs and public queries are separate counts,
not one accuracy denominator. `fixed_offline` is not a model-quality pass.

`historical_holdout` selects previously used transfer cases, **not unseen cases**.
Use new undisclosed fixtures/seeds for future generalization checks. Foundation
checks may be skipped during focused quality iterations, but run relevant
foundation plus repaired regressions when their tools/contracts change and
before release. Do not disable safety/authorization boundary regressions merely
because a task is simple.

The existing suite runner also supports exact include/exclude IDs and any-of
tags on tagged suites, preserving the original suite order:

```bash
.venv/bin/python -m evals.lka_evals.runner evals/suites/mail_qa.yaml \
  --case ntuso_requirements --list --json
.venv/bin/python -m evals.lka_evals.runner evals/suites/mail_qa.yaml \
  --exclude-case ntuso_requirements --list --json
```

`--list` never constructs a subject or judge, even with real/judge flags.
Unknown IDs/tags, duplicates and empty selections fail before execution.
Executed reports persist the selected/total counts and skipped IDs; skipped
tests are never counted as passes. Scripted metric success still does not
certify semantic task completion. Latency-only failures are separately listed
but remain failed, without relaxing thresholds. Existing default execution
behavior remains.

One unresolved historical failure is versioned as an **opt-in** reproducer:

```bash
.venv/bin/pytest -q evals/reproductions/skip_degrade_delivery.py
```

At this catalog revision it intentionally yields 3 failures / 7 passes: accepted
degradation loses the note and a child's historical partial summary. It is not
collected by default (`skip_degrade_delivery.py` is not a `test_*.py` file), and
is not hidden using xfail or relaxed assertions. A focus/regression plan may
include it; use the case status to interpret failure rather than calling the
whole quality inventory green. No production fix is claimed.

## Message reading shadow comparison

`scripts/compare_message_reading.py` compares the current selector, experimental
multi-lane selector, and message input encodings on identical frozen windows.
It defaults to offline measurement, opens source SQLite read-only, and requires
an explicitly supplied ignored output directory. Identity/text bindings remain
inline with `compact_records`; dictionary encoding `codec_v2` remains experimental.

```bash
.venv/bin/python scripts/compare_message_reading.py --source /path/to/messages.sqlite3 \
  --output-dir docs/_engineering/reports/reading-comparison-new
```

Real calls require authorization for the source and configured provider, `--run`,
and an explicit shared `--ledger` path. Retries reuse that ledger; defaults cap
the whole experiment at 12 requests and 180,000 reserved input/output tokens.
`--resume-from` verifies saved requests and reuses previous dispatches, including
failures. `--arms` and `--window` support bounded additional probes. Returned
schema failures remain failures; permission, transport and budget failures stop
dispatch. Reports separate token counts, candidate retention, source attribution
and provisional reference coverage. Agent-reviewed references are not human gold,
and a structurally valid response is not semantic success. No result is published
to production and no report automatically increases a gray rollout percentage.

## Synthetic Vision API Probe

`scripts/probe_vision_api.py` defaults to listing its limits without dispatch.
With explicit authorization, it sends at most three generated images through the
configured model: ordinary completion, scanned-PDF rendering plus image tool,
and streaming. Each request has a 512 output-token limit and a 45-second transport
timeout; model use is recorded in an isolated ledger. It enables only the selected
model's image capability in memory and never changes private configuration.

```bash
.venv/bin/python scripts/probe_vision_api.py
# Paid calls; use a new, ignored output directory and explicitly authorize first.
.venv/bin/python scripts/probe_vision_api.py --run \
  --output docs/_engineering/reports/vision-probe-new
```

Checks compare random image-only codes, shape counts, table values and sums.
An accepted image request alone is not a semantic pass; incomplete/limited replies
fail. The probe stops after the first failed case. Three simple synthetic passes
establish endpoint compatibility only, not complex-document OCR quality or a
general accuracy rate. Reports, images and usage stay local.

## Offline Memory Release Regression Foundation

Run the deterministic, local memory provenance/scope/retraction/injection
checks without an LLM, network, or live user data:

```bash
uv run python scripts/eval_memory_release.py
uv run pytest tests/test_eval_memory_release.py
```

The anonymous JSONL fixture executes against a temporary SQLite database and
the real `MemoryService`/`MemoryContextProvider`. Reports show total and
per-dimension sample counts, pass rates, provenance attribution, cross-project
scope isolation, post-retraction injection, and external-injection promotion.
The five-case fixture is only a regression foundation; its report explicitly
marks production gate eligibility false. It does not evaluate model answer
quality, baselines without memory/with summaries, task completion, latency,
backlog, token/cost, worker crash recovery, Safety Gate bypass, or outbound
privacy policy. Those dimensions require fixed larger labeled sets and runtime
evidence before a production release decision.

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

## Offline Public RAG Retrieval Benchmark

`evals/lka_evals/public_retrieval.py` runs directly against isolated local
`KnowledgeService` instances. It never calls an LLM or downloads a model. The
checked-in HotpotQA, 2WikiMultiHopQA, MuSiQue, and MultiHop-RAG JSONL files are
imported into temporary databases. Reports include dataset hashes, Recall@k,
MRR@k, nDCG@k, supporting-document/all-hop recall, forbidden-leak count,
fallback rate, failure rate, and p50/p95 query latency. Each dataset/mode result
also includes query slices by dataset label, supporting-document source type,
and a deterministic coarse question-language label (`en`, `zh`, `mixed`,
`other`, or `unknown`), including slice-level leak, fallback, failure, and
latency summaries. Corpus import/index-build times and query times are reported
separately. Embedding indexing and reranker availability probing warm
local inference before timed queries; these are warm-query results, not cold
startup latency. Model identifiers, dimensions, cache path/existence, Python,
platform, and warm-cache notes are recorded in report configuration.
The runner mirrors the default `knowledge.search` candidate policy: at most
two chunks per document, and the configured default reranker batch size, text
limits, and 30-candidate cap. Its metrics are document-level retrieval metrics,
not answer correctness.

`public_rewrite_probe.py` is a separate, question-only diagnostic for multi-hop
retrieval. It deterministically splits a question into up to eight clause queries,
uses bounded parallel retrieval and one global rerank, and reports document-level
metrics plus the number of queries actually rewritten. It never reads gold answers
or supporting-document labels to form queries. It is **not** an Agent/LLM rewrite
evaluation and does not measure final answer accuracy. For example:

Curated multi-hop failure IDs, retrieval-stage observations, and annotation
questions are recorded in `evals/fixtures/knowledge/multihop_rewrite_failcases.json`
with optional investigation notes kept in the local engineering archive. The annotation questions need
manual review and are not automatic ground-truth assertions.

```bash
uv --cache-dir .uv-cache run python -m evals.lka_evals.public_rewrite_probe \
  --dataset evals/datasets/public_multihop/multihop_rag_600.jsonl \
  --sample-limit 100 --seed 20260929 --top-k 10 \
  --output /tmp/lka-multihop-rewrite-probe.json
```

The comparison report checks each available mode against keyword on the same
dataset, showing Recall@k/all-hop deltas and p95 latency ratio. Its illustrative
defaults allow at most a 0.05 recall-point drop and 2x keyword p95; set project
thresholds explicitly when using it as a gate. Unavailable modes are never
compared as fallback scores. These comparisons do not establish a universal
model ranking or justify a production default without full-data and language
slices.

```bash
uv --cache-dir .uv-cache run python -m evals.lka_evals.public_retrieval \
  --mode keyword --top-k 10 --sample-limit 200 \
  --output /tmp/lka-public-rag-baseline.json
```

To measure semantic, hybrid, and hybrid+rerank, first explicitly cache compatible
models outside this runner. Then pass `--embedding-model`, its actual
`--embedding-dimensions`, `--rerank-model`, and `--model-cache-dir`; add
`--mode semantic --mode hybrid --mode hybrid_rerank`. Without a cached model,
the affected rows are `unavailable` with null quality metrics, not misleading
keyword fallback scores. Production retrieval now attempts local reranking by
default, with `local_files_only = true`; an unavailable model leaves retrieval
ordering intact and reports a warning. Benchmark latency before deployment.

To compare a local reranker without an embedding model, include
`--mode keyword_rerank --rerank-model BAAI/bge-reranker-base`. This retrieves
the keyword candidate pool and reranks it locally; it does not initialize the
semantic index. The default model cache is `data/runtime/models` (override with
`--model-cache-dir`). If the model cannot be loaded locally, the mode is
reported unavailable rather than scored as keyword-only.

For Agent-answer cases, `expect.knowledge_grounding` accepts
`{"evidence_sufficient": true|false, "permitted_source_refs": [...]}`. It checks
`[source-ref]` citations against successful, privacy-approved knowledge tool
results and a deterministic abstention marker when evidence is insufficient.
Optional explicit `claims: [{"text": "...", "supported": false}]` fixture
annotations flag literal unsupported claim text in the answer. This is an
oracle-driven regression check, not automatic claim discovery. Citation syntax
and reference membership are structural checks and do not mean that cited
evidence semantically supports the answer.

## Multi-Agent Evaluation Foundations

For a small real-LLM selection probe, run the synthetic read-only cases in
`scripts/probe_multi_agent_selection.py`. It enables multi-Agent planning only
inside a temporary LangGraph runtime, disables mail sync and external experts,
and leaves non-read-only tool calls at the manual safety gate. The configured
LLM provider is used, so these commands incur provider cost:

```bash
uv run python scripts/probe_multi_agent_selection.py --case forced_compare --output /tmp/forced-compare.json
uv run python scripts/probe_multi_agent_selection.py --case optional_release_audit --planning both --output /tmp/release-audit-pair.json
uv run python scripts/probe_multi_agent_selection.py --list-cases
```

The probe requires explicit `--case` selections; listing never calls a model.
The synthetic catalog covers explicit two-/three-way forks, optional comparison
and release review, one-file lookup, a three-child fan-in DAG, strict scope
language, conflicting sources, an explicit no-fork multi-file task, untrusted
instructions embedded in a file, missing-evidence abstention, four-child
capacity/batching, and a missing-child-input replan probe. The last two are
diagnostic cases, not claimed runtime successes. For a bounded
cross-section, run only a few different dimensions per session:

```bash
uv run python scripts/probe_multi_agent_selection.py \
  --case forced_dag --case conflicting_sources \
  --case negative_prompt_injection --case missing_evidence \
  --output /tmp/multi-agent-cross-section.json
```

`forced_scope_stress` installs a trusted, case-local read-only filesystem fork
policy. Natural-language wording alone is not treated as a permission grant or
revocation; other cases retain the normal general-Agent capability scope.

`--planning both` runs the *same prompt* once with fork planning enabled and
once disabled. Compare child completion, answer evidence, wall time, LLM calls,
and token counts; answer-term checks are only a coverage smoke signal, not a
semantic correctness judge. The examples are small and stochastic, so a
serial answer to a split-friendly case is not automatically a missed fork.
For fork-format diagnosis, the report also records the first fork schema
outcome, field-level validation errors, retry feedback, and whether the
configured client used function calling. It omits raw model output and prompts.
The report also records Plan dependencies, per-child lifecycle/tool audit and
effective scope, plus machine-readable `checks`. Those checks verify structure
and exact answer tokens; they do not prove semantic correctness, honest source
attribution, or absence of all prompt-injection effects. Inspect the full
synthetic answer and child trace for failed or ambiguous cases. A missing
child audit is reported as unknown, never as a read-only pass.
If a run reaches manual confirmation or a user-question interrupt, the probe
records its partial parent/child state and exits nonzero; it never approves the
interrupted operation. Executed read-only tools and effective permission scope
are reported separately because a harmless observed action does not prove a
least-privilege grant.
The child-error/replan case records failed tool outcomes separately from Child
Run status and checks for a durable PlanPatch event. A child can currently
answer about a failed read and finish its Agent run; that is not equivalent to
independent proof that its assigned output contract was satisfied.

`evals/lka_evals/multi_agent_metrics.py` computes selected lifecycle, DAG,
approval, and latency metrics from caller-supplied durable run/event JSON. It
does not execute tasks or read live user data; missing evidence is reported as
unavailable rather than counted as success. `evals/lka_evals/badcases.py` handles
explicitly selected, de-identified badcase manifests and regression selection.
These are data/metric foundations, not yet an isolated multi-Agent suite runner
or a live shadow-evaluation service. Do not import raw production prompts,
mail content, credentials, or writable workspace fixtures into badcases.

To create an offline JSON report, provide explicit exported input files and an
explicit output path:

```bash
uv run python -m evals.lka_evals.multi_agent_report \
  --runs /path/to/run-records.json \
  --events /path/to/run-events.json \
  --output /path/to/multi-agent-report.json
```

Each input is a JSON array of durable records/events, or JSON `null` when that
data source is unavailable. The command never queries a live backend or scans
log directories. Reports use schema version 1 and the contract in
`evals/schemas/multi_agent_trace_report.schema.json`. Trace terminal completeness,
dependency correctness, and terminal consistency are structural hard gates: unavailable evidence makes the
report fail (`passed: false`, exit status 1), as does a failed gate.
Passing these gates does not by itself establish task-answer quality or scope safety.

### Offline Planner Decision Slice

`evals/suites/planner_quality.yaml` is a versioned, deterministic oracle slice
for child failure/timeout, blocked dependencies, missing or conflicting
evidence, invalid DAG feedback, and required-step coverage. Supply structured
candidate outputs explicitly; the evaluator calls existing plan/fork/patch
schema and policy validation using only the case's trusted fixture ceilings.
It reports structure/policy validity separately from oracle decision match.
Scripted fixture scores describe only those supplied fixtures and must not be
reported as real LLM Planner capability. This evaluator never invokes a model
or discovers backend logs. The missing-evidence case uses an explicit external
verifier with `require_evidence: true` and `replan_required: true`; the current
default `VerificationPolicy.require_evidence` is false, so that case does not
claim the runtime raises this requirement automatically.

```bash
uv run python -m evals.lka_evals.planner_quality \
  evals/suites/planner_quality.yaml \
  --candidates /path/to/planner-candidates.json \
  --output /tmp/planner-quality-report.json
```

Run the bundled scripted baseline as an evaluator smoke example:

```bash
uv run python -m evals.lka_evals.planner_quality \
  evals/suites/planner_quality.yaml \
  --candidates evals/fixtures/planner_quality_scripted_candidates.json \
  --output /tmp/planner-quality-scripted-report.json
```

This checks the evaluator plumbing and oracle fixture only; its scores are not
model results.

Candidate files use `schema_version: 1`, `candidate_kind`, optional `provider`,
`model`, and `configuration_id`, plus an `outputs` array. Each output has a
`case_id` and `action`; fork and patch actions include an `operation` object.
`candidate_kind: "real_llm_export"` and its provider/model fields are caller
claims. The report records them as unverified; the evaluator does not establish
provenance.

## Suite Format

Suite files are JSON-compatible YAML in this first version. This avoids adding a
YAML parser dependency while leaving the `.yaml` extension in place.

Each case supports:

- `case_id`
- `operation`: defaults to `agent_turn`; direct runtime operations include
  `health`, `capabilities`, `workspace_index`, and `tool_sequence`
- `workflow`: currently `mail_qa` or `mail_to_matter`
- `difficulty`: optional label such as `easy`, `medium`, or `hard`
- `setup.mail_fixtures`: virtual fixture datasets from `evals/fixtures/mail`
- `setup.file_workspace_fixture`: isolated file workspace from `evals/fixtures/files`
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
- `tool_output_excludes_all`
- `tool_statuses_match`
- `filesystem_snapshot_contains_all`
- `filesystem_snapshot_excludes_all`
- `safety_review_count`
- `safety_review_tools`
- `safety_review_statuses`
- `safety_review_modes`
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
- copying `setup.file_workspace_fixture` into a temporary workspace and setting
  `LKA_WORKSPACE_ROOTS` before runtime creation;
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
- `workspace_context`: direct workspace indexing against a local fixture project.
- `capabilities`: direct health and capability catalog contracts.
- `failure_recovery`: deterministic failure-budget contracts.
- `stream_contract`: SSE contract for `--subject stream` against a disposable backend.
- `file_ops/filesystem_tools`: easy/medium/hard filesystem read/edit contracts.
- `file_ops/bash_tools`: easy/medium/hard bash read-only, write-gating, and
  background-session contracts.
- `file_ops/safety_review_tools`: scripted Agent-turn coverage for non-read-only
  filesystem and bash calls that must record skip-mode safety reviews.
- `file_ops/real_llm_file_bash_tasks`: real LLM Agent-turn file/bash tasks
  without `tool_plan` or `scripted_answer`; run with `--llm-mode real` and a
  real `--local-config`.

## Persistent management delegate evaluations

The offline `reasoning_controls` selection covers per-request four-level reasoning,
provider mappings and failures, streaming/non-streaming trace, frozen runs and
management inheritance. It uses synthetic requests and mocked transport; passing
does not measure real-model task quality or latency. Run it with
`--case reasoning_controls --run` using the management driver below.

`evals.lka_evals.management_cases` is a dedicated driver for persistent manager
sessions and event-driven follow-up turns. It does not use the generic one-turn
runner to approximate this lifecycle. The public synthetic catalog is
`fixtures/management_delegates.json`; no private report or runtime database is
needed. Listing and default dry runs do not load private configuration, create a
runtime, or call providers:

```bash
.venv/bin/python -m evals.lka_evals.management_cases --list --json
.venv/bin/python -m evals.lka_evals.management_cases --case date_handoff
.venv/bin/python -m evals.lka_evals.management_cases --run \
  --case ownership --case restart_dedup --case lease --case manual_high_risk \
  --output /tmp/management-contracts-new
```

Offline selections invoke bounded pytest node IDs against actual ownership,
lease, restart/deduplication and registered-tool manual approval contracts. These
passes do not establish real-model semantic quality.

The additional offline selections `history_scope` and `simple_matter_capture`
cover supervisor-only history access, source pagination, compact single-Matter
creation/editing, safety reviews, revisions and atomic autonomous publication.
Their scripted harness checks cover both legacy and checkpointed turns; they
do not claim measured real-provider latency improvements.

Model scenarios require an explicit selection, both dispatch gates, a fresh
output directory and an explicitly authorized budget, for example:

```bash
.venv/bin/python -m evals.lka_evals.management_cases --run --llm-mode real \
  --case date_handoff --output /tmp/management-date-new \
  --ledger /path/to/shared-evaluation-budget.sqlite3 \
  --call-limit 24 --usd-limit 0.30 --local-config config/local.toml
```

The driver reads the configured default model only after those gates. Current
ledger pricing supports only configured `deepseek-flash`; an unpriced model is
refused. All registered clients, including review calls, are metered at the async
provider boundary. Search is disabled. Reservations and failures remain charged;
reuse `--ledger` across retries, never clear it to obtain a successful result.
Stored management ledger ceilings cannot be raised. Every run uses fresh data and
synthetic workspace files, disables unrelated background features and never
starts/restarts a service or connects to the daily database.

`parallel_lines` requires two delegates and four independent execution sessions,
then checks source content, ownership, completed-run readbacks, report references,
parent report reads, dates, compatibility, capacity and arithmetic. `date_handoff`
generates a new source identifier at execution and requires `needs_decision` for
the date mismatch. `approval_conflict` tests a missing current approval against an
obsolete approval; its earlier budget-limited attempt remains **unvalidated**, not
a pass. Missing trace evidence, partial runs, unknown results and budget failures
never count as passes. Structured gates still require independent semantic review;
inspect local report and trace artifacts before a production release decision.

### Assistant orchestration regression selection

The management catalog also includes offline `assistant_orchestration`,
`tool_selection_recovery`, `execution_workspace`, and `result_evidence` groups.
They cover role policy before routing without ordinary-session inheritance,
legacy/resumable and native/text rejection recovery, read-only parameter grants,
frontend-configured task directories with no backend fallback, and owned-task
evidence paging, nullable default task workspaces without parameter-repair turns,
and bounded Matter write/mail coverage receipts alongside the answer, with
historical scope, source/permission revalidation, and continuation for incomplete
evidence. Select with `python -m evals.lka_evals.management_cases --case <group>`;
the default remains a plan, never a provider call. Scripted contracts do not
certify real-model planning quality.

The offline selections `session_progress_cache`, `management_notifications`, and
`management_result_recovery` cover cross-turn continuation without replaying
successful operations, revocable historical result pages, explicit background
notification decisions and atomic silent publication, and failed-run evidence
indexes with owned-task result recovery. They use synthetic temporary data and
no provider calls. Historical pages remain historical after a complete read;
successful operation counts do not certify business completion.
The cache selection also checks exact registered source-text read requests,
two- and three-level derived-artifact revocation, historical-origin propagation,
cycle/depth rejection, and completed-reader recovery after source revocation.
It also covers tight provider-input budgets: recent historical evidence retains
its negation and partial-result qualifications ahead of old context, omitted
views retain their continuation routes, and fork compaction preserves the
verification verdict and its constraints without increasing the input budget.
Progress continuation also covers JSON/native progress-note envelopes, incremental
notes and resolved-gap clearing, advisory review without field-only model retries,
and mandatory write approval despite progress review. Exact duplicate execution
and source/evidence validation still apply. These are offline protocol checks,
not measured real-model latency or semantic-quality improvements.
Source-controlled provenance claims never establish an artifact lineage.

```bash
# Selection only; no runtime, private configuration, or provider dispatch.
.venv/bin/python -m evals.lka_evals.management_cases \
  --case session_progress_cache --case management_notifications \
  --case management_result_recovery
# Execute only these offline regressions and preserve their report.
.venv/bin/python -m evals.lka_evals.management_cases --run \
  --case session_progress_cache --case management_notifications \
  --case management_result_recovery --output /tmp/management-recovery-contracts-new
```
