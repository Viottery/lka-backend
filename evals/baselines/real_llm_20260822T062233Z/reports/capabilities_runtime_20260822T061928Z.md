# Eval Report: capabilities

- subject: `runtime`
- generated_at: `2026-08-22T06:19:28.672946+00:00`
- score: `1.0`
- passed: `2/2`

## Cases

### health_contract

- score: `1.0`
- passed: `True`
- wall_time_ms: `424.67`
- selected_package: `None`
- answer_preview: 

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `output_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 424.67, "maximum": 2000}` |

### capability_catalog_contract

- score: `1.0`
- passed: `True`
- wall_time_ms: `408.353`
- selected_package: `None`
- answer_preview: 

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `output_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `capabilities_include` | `1.0` | `True` | `{"actual": ["claude_code", "codex", "extract_tasks", "mail", "matter", "organize_files", "runtime", "summarize_folder"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 408.353, "maximum": 2000}` |
