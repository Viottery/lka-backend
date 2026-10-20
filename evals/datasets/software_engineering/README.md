# Repository-level software engineering evaluation

This adapter evaluates LKA's generic Agent loop on real issue-to-patch tasks.
It does not replace the Agent with SWE-agent, Codex, a patch-generating prompt,
or an LLM judge. Models receive the issue and a disposable base checkout.
Gold patches, hints, expected test IDs and heldout tests stay evaluator-only.

## Benchmark choice

The initial dataset is [SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified),
500 human-screened Python repository issues. Its upstream issue, base commit,
reference patch, test patch and FAIL_TO_PASS / PASS_TO_PASS labels support
reproducible task identity and executable checks. This is a useful first fit for
LKA's Python/Linux filesystem and Bash tools. Public historical tasks may have
appeared in model training; these are not an unseen holdout.

Other candidates reviewed against their primary documentation:

| Benchmark | Useful coverage | Initial deployment decision |
| --- | --- | --- |
| [SWE-bench Lite](https://www.swebench.com/SWE-bench/guides/evaluation/) | Smaller issue-repair subset and official grading ecosystem | Prefer Verified's human-screened tasks; sample explicitly for pilots |
| [SWE-bench Pro V2](https://github.com/scaleapi/SWE-bench_Pro-os/tree/main/v2) | Longer tasks, multiple repositories/languages, Harbor verifiers and reference solutions | Strong subsequent stress benchmark; different container/task protocol needs its own adapter |
| [SWE-rebench V2](https://huggingface.co/datasets/nebius/SWE-rebench-V2) | Broader language/repository coverage, task metadata and execution images | Useful subsequent transfer/freshness study; a dataset row alone does not establish a clean unseen test split |
| [SWE-smith](https://github.com/SWE-bench/SWE-smith) | Large generated repair corpus and environment construction | Useful training/diagnostic expansion; not the initial independent acceptance benchmark |
| [Aider polyglot](https://github.com/Aider-AI/polyglot-benchmark) | Executable multilingual coding exercises | Complementary language/editing checks; less direct coverage of real repository issue investigation |

LKA's existing mail/RAG suites, scripted tool contracts and small file-edit cases
remain useful regressions. They do not establish repository repair quality.
The Linux quality catalog counts issues and replay jobs, not this benchmark's
task denominator. Keep these results separate.

## Reproducible deployment

The checked-in [catalog](../../fixtures/software_engineering.json) pins dataset
revision `78f471bf655a3137b2e8a75af1501690ec009ec3`, a two-case diagnostic
selection and native environment dependencies. The pilot contains an environment
control (`pallets__flask-5014`) and a harder task (`sympy__sympy-17630`). It is not
a representative accuracy sample or a cross-model ranking.

Run commands from the repository root. Fetching requires a Parquet reader only
for conversion; it does not change the backend dependency lock or `.venv`:

```bash
uv --cache-dir .uv-cache run --no-project --with pyarrow==19.0.1 \
  python -m evals.lka_evals.swe_bench fetch
.venv/bin/python -m evals.lka_evals.swe_bench list --pilot
.venv/bin/python -m evals.lka_evals.swe_bench list --sample 10 --seed 20261020
```

`fetch` writes a fresh ignored `data/benchmarks/swe-verified/<revision>/` directory
with the original Parquet, upstream README/license attribution, normalized JSONL,
source URLs and SHA256 manifest. Existing outputs are never overwritten.
`list` needs only that local snapshot; it imports no runtime or private model
configuration and makes no network/model calls. Exact `--case` selections reject
unknown or duplicate IDs. Sampling is deterministic by sorted ID and seed.
`run` and `grade` also accept repeated exact `--case` selections from an existing
prepared bundle; reports retain unselected prepared IDs. This permits calibrated
cases to run while other cases remain explicit environment failures in the study.

Prepare each native profile separately; the full 500-row dataset is deployed,
but only these two environment profiles have a bundled native setup:

```bash
.venv/bin/python -m evals.lka_evals.swe_bench env --profile flask \
  --output data/benchmarks/swe-envs/flask
.venv/bin/python -m evals.lka_evals.swe_bench prepare --case pallets__flask-5014 \
  --output data/benchmarks/swe-prepared/flask
.venv/bin/python -m evals.lka_evals.swe_bench calibrate \
  --prepared data/benchmarks/swe-prepared/flask \
  --python data/benchmarks/swe-envs/flask/bin/python \
  --output data/benchmarks/swe-calibration/flask
```

For the complex pilot, use `--profile sympy`, `--case sympy__sympy-17630`
and distinct paths. `prepare` downloads only the frozen base archive and creates
a local snapshot Git repository, without later commits, remotes or PR history.
Dataset, task, archive and pristine-tree hashes are checked before use. `env`
installs pinned packages into a dedicated uv-managed Python environment and
records the installed dependency freeze. No backend service is started/restarted.

Native execution requires Linux, Bubblewrap and working unprivileged namespaces.
The command sandbox exposes only the disposable checkout, system programs and the
read-only benchmark interpreter/environment, with an empty credential environment,
private process/filesystem namespaces and no network. Failed namespace setup stops
execution; there is no fallback to host shell execution. Filesystem tool paths are
confined; all tool invocations are serialized with shell commands to prevent
concurrent symlink changes across host filesystem checks. Thus multi-agent latency
here includes serialized tool execution and is not production concurrency latency.
Commands support synchronous execution only. This is a Linux evaluation profile,
not a change to LKA's native Windows backend support.

## Calibration, inference and grading

Calibration uses fresh copies for two controls: the empty patch must fail every
FAIL_TO_PASS test while preserving PASS_TO_PASS, and the reference patch must
pass all required tests. Missing tests, skips, collection errors or timeouts never
pass. Native SymPy bare labels are mapped uniquely to test definitions using AST
inspection of the heldout files; unresolved or ambiguous mappings fail. SymPy
uses its original `bin/test -C --verbose` runner, matching upstream's test command;
Flask uses exact pytest nodes. Their status parsers remain separate.
Control runs do not call a model and are not Agent capability scores.

Paid runs need explicit authorization and `--allow-paid`; a successful calibration
must match the exact prepared manifest and environment. For example:

```bash
.venv/bin/python -m evals.lka_evals.swe_bench run --allow-paid \
  --prepared data/benchmarks/swe-prepared/flask \
  --calibration data/benchmarks/swe-calibration/flask/report.json \
  --python data/benchmarks/swe-envs/flask/bin/python \
  --output data/quality_runs/swe-flask-attempt-1 \
  --ledger data/quality_runs/swe-shared-budget.sqlite3 \
  --usd-limit 2 --call-limit 120 --max-steps 40 --timeout 900
```

The worker reads only the selected provider/model and Agent settings from local
configuration; personal domains, memory, background work and external experts
are disabled. Multi-agent planning is opt-in with `--multi-agent`. Safety mode
defaults to the configured value; `--safety-review` is an explicit experiment
override. Mandatory review records and Tool Executor schema validation remain.
The generic issue prompt gives no file-localization or solution hints.

All provider clients share a persistent reservation ledger, including reviews
and child calls. Failed calls and unknown usage retain their reservation. Retries
must reuse the same `--ledger`; stored ceilings cannot be raised. The existing
ledger tariff supports `deepseek-flash` only and refuses unpriced models.
The parent enforces a per-case process deadline; killing an incomplete worker
preserves logs, ledger charges and its partial patch. Never count an interrupted
run as completed. Changing the configured model requires extending pricing and
validating the provider adapter explicitly.

Each attempt saves the actual working-tree patch independently of the final
answer, `predictions.jsonl`, raw local runtime records, tool events, safety reviews,
parent/child trace, model usage, elapsed time and execution status. Patch capture
uses a fresh index, ignoring the candidate's Git configuration. Answer claims are
not the correctness oracle. Outputs must stay under ignored `data/`.

Grade separately in fresh copies, after the Agent process has stopped:

```bash
.venv/bin/python -m evals.lka_evals.swe_bench grade \
  --prepared data/benchmarks/swe-prepared/flask \
  --calibration data/benchmarks/swe-calibration/flask/report.json \
  --python data/benchmarks/swe-envs/flask/bin/python \
  --predictions data/quality_runs/swe-flask-attempt-1/predictions.jsonl \
  --output data/quality_runs/swe-flask-grade-1
```

The grader restores required test files, applies the official heldout test patch,
and runs the selected tests (SymPy uses the original file scope). Changes to test
runners/configuration are
rejected. New candidate tests are not scored. It reports resolved/unresolved,
patch errors, environment errors, timeouts and missing predictions separately;
the denominator remains all selected tasks. This is not an adversarially
tamper-proof grader: trusted tests execute modified repository code.

Every native result is labeled `native_diagnostic_not_official`. Dependency and
execution differences mean it must not be presented as an official SWE-bench
leaderboard result. The existing official container protocol is retained:

```bash
.venv/bin/python -m evals.lka_evals.swe_bench official-plan \
  --prepared data/benchmarks/swe-prepared/flask \
  --predictions data/quality_runs/swe-flask-attempt-1/predictions.jsonl
```

This prints an official harness command pinned to `swebench==5.0.2` with a fresh
run ID and one worker; it does not install Docker or run containers.
Run it on a separately prepared Docker host, following the
[official evaluation guide](https://www.swebench.com/SWE-bench/guides/evaluation/).
An unavailable official environment remains ungraded. Never replace missing
official grades with native diagnostics or reuse a cached run ID for changed patches.

## Reading failures

Use trace evidence to distinguish investigation/localization errors, an incorrect
or incomplete patch, missing regression validation, tool/protocol recovery,
context loss, safety/user interruption, provider failures and budget exhaustion.
The runner records evidence; it does not infer these semantic causes with a
keyword-based automatic judge. Review the failing tests, diff and tool sequence.
For larger studies, freeze a stratified selection across repositories/difficulty,
hold budgets/settings constant, and report per-repository counts, uncertainty and
all missing/failed attempts. Do not generalize from the two-case pilot.

Offline adapter contracts:

```bash
.venv/bin/python -m pytest -q tests/test_swe_dataset.py tests/test_swe_sandbox.py \
  tests/test_swe_native.py tests/test_swe_bench.py
```
