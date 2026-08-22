# Eval Report: runtime_tools

- subject: `runtime`
- generated_at: `2026-08-22T06:18:03.530728+00:00`
- score: `0.4118`
- passed: `0/1`

## Cases

### runtime_now_tool

- score: `0.4118`
- passed: `False`
- wall_time_ms: `5532.248`
- selected_package: `None`
- answer_preview: 确定性运行时日期时间为：

- UTC：2026-08-22 06:17:58.505867
- 本地时间（Asia/Shanghai，UTC+8）：2026-08-22 14:17:58.505867

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `0.0` | `False` | `{"actual": null, "expected": "runtime"}` |
| `tool_sequence_exact_match` | `0.0` | `False` | `{"actual": [], "expected": ["runtime.now"]}` |
| `tool_sequence_f1` | `0.0` | `False` | `{"actual": [], "expected": ["runtime.now"]}` |
| `required_tool_called` | `0.0` | `False` | `{"actual": [], "missing": ["runtime.now"]}` |
| `answer_contains_all` | `0.0` | `False` | `{"missing": ["runtime.now"]}` |
| `tool_output_contains_all` | `0.0` | `False` | `{"missing": ["local", "timezone", "utc", "date"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 2, "maximum": 5}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 1701, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 5532.248, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
