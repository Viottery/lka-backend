# Real LLM Baseline 20260822T062233Z

- Git HEAD: `d16f8c6bc0d926b3e9a80b143c34eb1b99207edd`
- Working tree dirty: `True`
- Config: `config/local.toml` contents were not copied.
- Source report dir: `/tmp/lka_eval_real_reports_net`

## Overall

- Cases: `4/31` passed
- Pass rate: `12.90%`
- Weighted score: `0.8504`

## Subjects

| subject | pass | pass_rate | weighted_score | wall_ms_sum | llm_events | provider_failures | tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| runtime | 4/30 | 13.33% | 0.8484 | 1208434 | 187 | 1 | 469374 |
| stream | 0/1 | 0.00% | 0.9091 | 37733 | 0 | 0 | 0 |

## Suites

| suite | subject | pass | score | wall_avg_ms | llm_events | provider_failures | tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| capabilities | runtime | 2/2 | 1.0000 | 417 | 0 | 0 | 0 |
| failure_recovery | runtime | 0/1 | 0.9412 | 46998 | 7 | 0 | 15794 |
| hard_mail_tasks | runtime | 0/6 | 0.8646 | 41390 | 48 | 0 | 131575 |
| mail_qa | runtime | 0/11 | 0.8824 | 40600 | 83 | 0 | 203216 |
| mail_to_matter | runtime | 0/1 | 0.8788 | 58247 | 10 | 0 | 33260 |
| matter_tools | runtime | 0/1 | 0.4118 | 102773 | 2 | 1 | 858 |
| real_llm_hard_mail_tasks | runtime | 1/4 | 0.7894 | 53997 | 21 | 0 | 48914 |
| runtime_tools | runtime | 0/1 | 0.4118 | 5532 | 2 | 0 | 1701 |
| session_followup | runtime | 0/1 | 0.8824 | 34124 | 7 | 0 | 16133 |
| smoke | runtime | 0/1 | 0.8750 | 48559 | 7 | 0 | 17923 |
| workspace_context | runtime | 1/1 | 1.0000 | 440 | 0 | 0 | 0 |
| stream_contract | stream | 0/1 | 0.9091 | 37733 | 0 | 0 | 0 |

## Mail Search

- Cases with mail search metrics: `25`
- Recall@k average: `0.9400`
- Full recall: `22/25`
- Precision@k average: `0.6280`
- MRR average: `0.8730`
- Forbidden clean: `3/8`
- Forbidden@k average: `0.3750`

## Git Status At Capture

```text
 M .gitignore
 M app/core/agent_turn.py
 M app/core/llm/__init__.py
 M app/core/llm/audit.py
 M app/core/llm/errors.py
 M app/core/llm/openai_compatible.py
 M app/core/runtime.py
 M app/tool_packages/matter.py
 M docs/api_contract.md
 M docs/backend_engineering_guide.md
 M docs/current_system_architecture.md
 M docs/mvp_todolist.md
 M tests/test_agent_turn.py
 M tests/test_llm_audit.py
 M tests/test_mail_service.py
?? evals/
?? tests/test_evals_smoke.py
```
