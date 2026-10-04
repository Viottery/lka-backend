# Memory and background subsystem: implementation notes

This document describes the current local backend implementation. The broader release
plan and remaining acceptance gates are in [the TODO](memory_background_todolist.md).

## Data flow

1. A completed Agent answer is persisted with its session message. An outbox callback
   enqueues `memory_extract` in the same SQLite transaction, carrying only the answer
   message ID. Startup recovery scans committed answers after the feature's first-enable
   watermark and repairs a missing job. A deleted session is not extracted.
2. A bounded pool (two workers by default) resolves corresponding user messages by trace ID.
   Local default extraction accepts an explicit `记住：...` statement or a
   low-impact direct enduring preference such as `我希望以后...`. Optional
   configured-LLM extraction is disabled by default to avoid an additional model
   call for every less-explicit message. Enabling the memory/background feature
   authorizes background processing of persisted conversation content; the
   optional model path does not require separate content approval. Obvious
   secrets/PII are still rejected as *published memories*, not as model input.
   These deterministic direct statements never call the optional model.
   An explicit `这个项目`/`本项目` claim is classified as project-scoped and
   requires a project identity; it is not silently promoted to a global rule.
   Tool, mail, web, RAG and Agent answer text are not extraction sources.
   Model claims/evidence now share an extractive contract and one production/evaluation
   validator. Evidence is matched against the original user message; cropped negation,
   correction, conditional or transient qualifiers are rejected even when the candidate
   is a literal substring. Full source-faithful negative/conditional statements remain
   valid. The bounded English/Chinese linguistic guard is conservative, not a semantic
   entailment model; complex wording can be rejected rather than silently generalized.
   Malformed candidates are rejected individually, and model `explicit` is never
   publication authority. Optional raw evaluation diagnostics stay local.
3. `MemoryService` stores provenance, candidate/active status, scope, versions, expiry,
   correction and retraction. Model-inferred items remain candidates; direct
   `记住：...` requests, low-impact explicit enduring preferences, or user API writes
   activate memory. Project identity is a stable
   internal ID bound to a resolved workspace path; paths are not merged by name.
4. Root Agent turns receive a bounded, active-only `recalled_memories` view. It contains
   IDs, provenance, timestamps and confidence; personal-sensitive entries and other
   projects are excluded. Child Agents receive no automatic memory view. The read-only
   `memory` tool package supports scoped search and exact read for the root Agent; it
   allows child reads only for explicitly granted, frozen snapshot references;
   revocation/source expiry is checked again when tools read them.
5. The conversation context window keeps raw sequenced messages. At 70% of its
   configured budget it enqueues a `context_compact` job; publishing uses the captured
   independent summary revision and message watermark, so new turns remain raw tail
   without starving publication during continuous conversation. At the hard window
   threshold, a configured durable callback captures the raw range for background
   compression and provides a bounded emergency prompt view without claiming semantic
   coverage. Its enqueue uses a savepoint so committed conversation data survives an
   enqueue failure; recovery repairs the missing job. Without a background callback,
   the legacy synchronous fallback runs outside the SQLite write transaction and uses
   the same CAS. Original session messages are retained.
   When the default model has a valid local tokenizer, this session-window
   estimate uses that tokenizer; otherwise it retains the legacy character-based
   estimate. Session thresholds are advisory, not a full request-capacity check.
6. A generated `MEMORY.md` view is stored under the configured data directory for
   global memory or a stable project ID. It lists active memory IDs, versions,
   sources and update times. Generation detects an edited file hash and refuses to
   overwrite it. Users may preview then import one edited content block at a time;
   missing/unknown IDs and changed metadata are rejected. This never touches
   user-owned `AGENTS.md` files.

Memory never grants tool permissions, changes `AGENTS.md`, or authorizes external
actions. Current-turn user instructions and user-owned guidance outrank derived memory.
Session deletion revokes its user-message sources before tombstoning and retracts
memories with no other valid source; restoring the session does not silently
republish them. Raw audit data remains local.
Deletion and provenance revocation share one SQLite transaction. A claim with another
still-valid source remains active; a source already revoked is not reactivated by a
late worker. The answer records the project identity at turn time so queued extraction
does not follow a later workspace switch.

## Configuration and controls

Frontend management endpoints are documented in the
[API contract](api_contract.md#前端配置控制与观察). `/background/config` supports
versioned partial updates and reset; overrides are stored in SQLite rather than
rewriting TOML. It returns active and desired configurations separately and applies
all configuration changes on the next backend startup. Invalid saved overrides
fall back to the base config and surface `config_load_error`, allowing local reset.
The JSON Schema endpoint exposes editable field constraints without provider
credentials. The existing global/project learning policy remains immediately
effective and independent of these restart-only settings.

Job retry/cancel endpoints use `expected_updated_at` CAS. Manual retry is limited
to memory extraction and compaction, preserves job identity and completed-input
checkpoints, and does not reset the token ledger. Cancel fences late publishers,
but cannot retract a completed publication or guarantee aborting an in-flight HTTP
call. The authenticated SSE endpoint is an aggregate sampling stream with an
initial snapshot and keepalives, not a durable event replay protocol. Session
context-status queries select only metadata and counts, never raw history bodies.

### Incomplete generation and reasoning recovery

Background memory extraction and context compaction separate the generation
allowance from the final summary's context-loading allowance. `[memory]`
`generation_output_tokens` defaults to 4096 and `recovery_output_tokens` to 8192;
both include provider reasoning tokens. The existing `max_job_tokens` allowance,
workload admission and request timeout still apply. These limits are not a promise
that a provider reserves any portion of a single call for its final answer.

Normal calls leave the provider's reasoning mode unchanged. An empty final body,
partial response or length-exhausted generation is not a usable result, even if
its body happens to parse as JSON. At most one immediate recovery call is made.
For explicitly configured compatible endpoints, recovery can disable reasoning;
otherwise it uses a larger output allowance without sending vendor-specific fields.
Set `thinking_control = "deepseek"` in `[llm]` for the legacy single-client
configuration or the relevant `[[llm.clients]]` entry only after verifying that
the endpoint accepts `thinking.type`. Named clients do not inherit this capability
from the legacy configuration. The local PackyAPI endpoint has been opted in.

Incomplete extraction never publishes new model-derived memories. Exhausted or
invalid compaction output uses the deterministic local summarizer, retaining
critical user wording and raw history rather than publishing partial model output.
Network/provider failures remain classified failures of the existing durable job
mechanism; reasoning recovery does not blindly retry transport failures. This
recovery is scoped to the memory subsystem, not all foreground Agent calls or
scheduled-watch reasoning. Long completed summaries still use the existing
context-loading budget; raw session messages remain the authoritative record.

`[memory]` in `config/local.toml` controls `enabled`, `background_enabled`,
`allow_remote_extraction`, `max_recalled_items`, and `max_recalled_chars`.
Disabling memory leaves existing records and session history readable but stops prompt
injection and new extraction. The worker is independent of interactive Agent turns.
`allow_remote_extraction` is a cost/quality switch, not a second privacy-consent
switch. Background extraction reads the persisted user turn associated with a
completed Agent answer; background compaction reads that session's persisted
messages. These *generic memory jobs* have no independent knowledge-base,
workspace or mail read capabilities. Separately authorized watch/background
tasks may actively read their granted sources through scoped tools; this does
not broaden generic memory jobs. Source/trust checks still prevent quoted
external instructions or model output from becoming user-owned long-term rules.

Whole-prompt budgeting is configured separately under `[llm]` (or each named
`[[llm.clients]]`): set `context_window_tokens` to a capacity verified for that
exact provider route, `output_reserve_tokens` (default 8192), and optionally
`tokenizer_json_path` to a local Hugging Face `tokenizers` JSON. The current
machine uses a git-ignored copy of the official
[DeepSeek V4.1 Flash tokenizer](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/resolve/main/tokenizer.json)
at `data/tokenizers/deepseek-v4.1-flash/tokenizer.json`. PackyAPI accepted a
synthetic 148,738-input-token call, so local config uses a 148,000-token verified
floor rather than assuming the upstream 1M window. A separate 4,096-token
safety margin reduces the maximum assembled input to 131,072. Do not copy this
capacity to other model routes without testing them; an unconfigured remote
model fails before dispatch. The configured output reserve also becomes the
request's `max_tokens` unless a call explicitly specifies another cap.
Run events expose the full-prompt `input_token_estimate`, provider-reported
`input_token_actual` when available, and their signed delta. The session-window
estimate excludes system instructions, tool definitions, and other dynamic
context. A request/model override uses that model's tokenizer both for the
foreground session threshold and final Agent-turn preflight without mutating
other turns' counting; the background compaction worker uses the default model.
The local tokenizer asset used for this calibration has SHA-256
`c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`;
it is not committed, so another machine must install and verify it separately.

Reproduce the no-private-data calibration with
`uv --cache-dir .uv-cache run python scripts/eval_prompt_budget.py` (offline)
or append `--remote` to make a few synthetic provider calls. On 2026-10-02 the
configured route returned local/provider input pairs of 7040/7062 (Chinese),
2862/2884 (JSON), and 3511/3533 (Unicode); the maximum provider/local ratio
was 1.0077. The tool-schema case is explicitly skipped remotely because this
client is not configured to send native function definitions. These samples
calibrate the current route only; they do not establish a universal tokenizer
guarantee or a large-scale latency/quality release gate.

Local API:

- `GET /memories?scope=global|project&workspace_path=...&include_candidates=true`
- `POST /memories` for a user-confirmed memory; `GET /memories/{id}` for detail
- `GET /memories/export?limit=500&offset=0` for paginated scoped JSON export;
  lists also return `next_offset`. `GET /memories/{id}/sources` returns provenance
  metadata, not original private message bodies.
- `PATCH /memories/{id}` with `expected_version`; `DELETE /memories/{id}?expected_version=...`
- `GET/PUT /memories/learning` for global or project learning pause/resume
- `GET /memories/file`, `POST /memories/file/generate`,
  `GET /memories/file/preview`, `POST /memories/file/import` for controlled
  `MEMORY.md` maintenance
- `GET /background/jobs` for payload-free queue status and failures
- `GET /background/health` for payload-free aggregate queue health

These private controls accept unauthenticated requests only from a loopback client.
If the backend must be reached from another host, set `LKA_MEMORY_API_TOKEN` and send
`Authorization: Bearer <token>`; once set, the token is required even on loopback.
This protects only the new memory/job endpoints, not the legacy API as a whole.

Project-scoped API calls require a workspace path within configured roots. In a normal
turn, `忘记 <memory_id>` also retracts an exact, in-scope active memory before recall;
ambiguous phrases do not guess which memory to delete.

## Verification and current limits

Focused tests cover queue leasing/fencing, compaction CAS and tail preservation,
scope isolation, correction/retraction, session deletion, answer-to-memory-to-recall
integration, outbox recovery, and read-tool scope checks. Run:

```bash
uv --cache-dir .uv-cache run pytest -q tests/test_background_jobs.py tests/test_async_context_compaction.py tests/test_memory_service.py tests/test_memory_extraction.py tests/test_memory_context.py tests/test_memory_files.py tests/test_memory_tools.py tests/test_memory_integration.py tests/test_eval_memory_extraction.py tests/test_sessions.py
uv --cache-dir .uv-cache run pytest -q tests/test_eval_memory_release.py tests/test_prompt_budget_integration.py
uv --cache-dir .uv-cache run python scripts/eval_memory_release.py
```

This is not yet a full release of every TODO item. In particular, provider-specific
true-token accounting has a configured local-tokenizer path and a calibrated
4,096-token safety reserve for the current DeepSeek Flash route; AgentTurn now
fits a separate 131,072-token whole-input target before provider calls. Without
a configured tokenizer it uses a conservative UTF-8 byte bound, and unconfigured
compatible models fail before dispatch. Over-budget prompts first omit
older observations, lower-ranked memories, older context messages (never the
last two), then instruction previews with their read handles intact. Mandatory
system/task/schema content fails clearly instead of being silently truncated.
`llm_started` events record local estimate, limit and counting method; provider
usage remains available on completion for calibration. This is not yet a full
provider-by-provider tokenizer or output-capacity guarantee. Large-scale independent
retrieval/evaluation and measured production p95/cost/quality release gates remain
unverified. Generic extraction intentionally does not independently read domain
sources. The opt-in model path awaits the configured async provider from the worker.
Two bounded real-provider compaction calls in this run timed out with no response;
model quality and billed usage therefore cannot be inferred. Watch,
mail sync and Graph-run scheduling still use their established, separate workers.
The 11-case offline extraction fixture currently reports 100% precision/recall and
zero false promotions, but it is deliberately small and curated; those numbers do
not establish production quality or satisfy the TODO's release thresholds.
The separate five-case release smoke fixture is likewise synthetic and explicitly
reports `production_gate_eligible: false`. A production gate still needs an
independent annotated corpus, live-model comparisons, latency and cost baselines,
and user-reviewed deployment evidence; no additional per-message export consent
is required for this personal application.

## Background resource and lifecycle controls

`[background]` configures total concurrency (4), reserved interactive slots (2),
memory/I/O concurrency (1 each), hourly/daily token limits, optional configured
cost rates, request network timeout (30s), and maximum materialized pending jobs
(1024). The shared LLM service records provider usage when available and charges
a conservative estimate on missing usage/errors. Token reservations survive
restart; monetary figures are estimates using the configured common rates, not
provider invoices or a per-model price table. Admission is process-local for
concurrency; the SQLite budget ledger is shared across processes.

Overflow remains durable, identifier-only pending input records, drained eight
at a time as queue capacity returns. Historical terminal payloads are compacted
after 30 days while retaining idempotency identities and original audit sources.
Usage diagnostics retain 90 days. This bounds runnable jobs and raw payloads,
not the number of raw conversations/provenance IDs, which intentionally persist.
Timed-out worker shutdown retains live thread references and cannot silently
spawn a second pool over an uninterruptible handler.

Extraction debounces bursts for five seconds, groups at most eight committed
answers, and optionally batches three user messages per model request. Shared
ambiguous evidence substrings are rejected rather than counted as independent
support. Only completed runs, active sessions and allowed learning scopes enter
the batch; oversized extraction inputs are bounded in SQL before materializing.
Direct deterministic confirmations activate; model `explicit` flags do not.
Independent valid evidence can promote ordinary candidates. Clear in-scope
forget/correction requests retract before recall, without waiting for a worker.
The foreground gate receives the exact persisted user message ID, not a lookup
for the latest matching text. It validates the ID, role and original content and
persists a global/current-project correction watermark before recall. Publication
checks matching correction fences inside its existing SQLite transaction, together
with source validity and the worker lease. A queued or in-flight earlier claim
cannot be resurrected after withdrawal; a later genuine user source remains eligible.
The correction's own source is also fenced so a forget request repeating the old
claim does not reinforce it. Suppressed candidates are terminal skips, not retries.
Legacy two-argument callbacks remain compatible but cannot create unidentified
source fences. Lexical targeting requires substantial overlap: generic preference
verbs must not erase unrelated preferences. This is a conservative linguistic rule,
not universal semantic matching; source messages and audit history remain local.
Successfully handled input IDs are checkpointed under the live lease. A later
batch timeout retries only unfinished inputs, not already published sources.
Publication and checkpoint are separate transactions: a crash between them can
repeat inference, but source-based publication remains idempotent. This is not
exactly-once inference or provider billing.
Compaction requests coalesce into a durable latest watermark per kind/scope.
A running target is immutable; its successor waits separately. A persistent
ordered head prevents late old-prefix follow-up from replacing a newer request,
including after restart/materialization. Equal-watermark continuation remains
allowed for bounded incremental compaction. Health exposes pending watermark
count separately from pending extraction inputs.

Preference conflicts use deterministic, evidence-grounded slot/polarity/condition
hints. Conflicting confirmed preferences remain reviewable but are withheld from
prompt injection. Inferred candidates cannot suppress an existing confirmed
preference. This covers selected bilingual preference axes, not universal semantic
contradiction detection. Expiry comes only from explicit USER wording; a date-only
deadline means the end of that UTC date. Only a new confirmed source can extend
an existing expired deadline; same-source retry cannot revive withdrawn claims.
Correction removes obsolete content-specific conflict hints; revocation and
current-peer expiry checks prevent a resolved conflict permanently withholding
the surviving preference. Resolving a conflict does not itself promote a candidate.

Compaction preserves a fixed complete-message prefix, structured goals/decisions/
constraints/corrections/open questions and quoted critical user anchors. Long
history is published in bounded increments with follow-up jobs. A huge message
uses deterministic fallback rather than exhausting the entire task allowance;
original text remains in local persistence. Local fallback is lossy and is not
equivalent to a semantically complete summary. Large changed instruction files
load a latest preview immediately with `index_status=pending`; index rebuild runs
in a bounded background thread, never showing an old summary as current.
Summary sidecar metadata records model selection, job ID, covered watermark,
bounded input trace IDs, method and possible lossy fallback. Summary revision CAS
is independent of raw-message revision: new messages do not invalidate a valid
old-prefix summary, while a newer published summary does.

Generated MEMORY.md sidecars intentionally accept content-only edits to their
documented blocks, not arbitrary Markdown as machine authority. Unknown content
is rejected without overwriting it. Views above 1000 records or 8 MiB fail with
an explicit paginated API alternative; this limit does not prevent exact memory
reads or long user-owned AGENTS.md reads.

Historical observations record age, TTL, source version when provided, permission
and workspace versions. Permission narrowing/unknown scope/workspace changes
exclude incompatible cache. Even within TTL, a historical result is never current
evidence and never satisfies this turn's completed-tool deduplication. Both legacy
and Graph regression scenarios import new mail between identical queries and
actually search/load again. Arbitrary external source versions are not automatically
revalidated; the conservative historical marker is deliberate.
Root recall includes at most eight representative source IDs per item, alongside
source_count/sources_omitted, content_truncated and an exact memory read reference.
Frozen child references similarly disclose total source count and truncation.
ContextDriver budgets the entire serialized reference using a conservative byte
bound, not only claim text; the final assembled LLM request still uses the model's
configured tokenizer/preflight. Full provenance remains in the local store/API.

Project relocation uses an explicit local-control API and retains stable identity;
reusing the old path creates a distinct project. No automatic directory-name
matching or disk movement is performed. Watch/Graph/mail retain their established
queue contracts; they share LLM resource admission where applicable, rather than
being migrated wholesale into a new scheduler.
Watch briefs enforce server-owned structured output contracts and cited contiguous
source excerpts, retain unconfirmed/failed-source information, and distinguish
new from unchanged events. An excerpt match establishes provenance, not truth or
semantic entailment. Detailed validation and residual gates are in the
[development report](memory_background_development_report.md).
