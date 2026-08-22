# Eval Report: workspace_context

- subject: `runtime`
- generated_at: `2026-08-22T06:19:27.328806+00:00`
- score: `1.0`
- passed: `1/1`

## Cases

### simple_project_workspace_index

- score: `1.0`
- passed: `True`
- wall_time_ms: `440.15`
- selected_package: `None`
- answer_preview: 

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `output_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `workspace_min_files` | `1.0` | `True` | `{"actual": 5, "minimum": 4}` |
| `workspace_min_chunks` | `1.0` | `True` | `{"actual": 20, "minimum": 4}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 440.15, "maximum": 5000}` |
