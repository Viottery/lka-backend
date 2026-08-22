# Eval Report: matter_tools

- subject: `runtime`
- generated_at: `2026-08-22T06:17:57.173896+00:00`
- score: `0.4118`
- passed: `0/1`

## Cases

### matter_create_search_update_link

- score: `0.4118`
- passed: `False`
- wall_time_ms: `102772.664`
- selected_package: `None`
- answer_preview: 当前无法执行“创建并更新本地事务、关联证据来源”的操作，因为本次请求的上下文中**没有选择任何工具包**（`selected_package` 为 `null`），且会话上下文窗口为空（无历史消息、无摘要、无可用的本地工具定义）。

缺少的关键信息包括：

1. **事务（Matter）字段**：名称/标题、类型、状态、自定义属性等具体内容；
2. **更新操作**：要更新哪个字段、从什么值改成什么值；
3. **证据来源（Eviden

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `0.0` | `False` | `{"actual": null, "expected": "matter"}` |
| `tool_sequence_exact_match` | `0.0` | `False` | `{"actual": [], "expected": ["matter.create", "matter.search", "matter.update", "matter.link_source"]}` |
| `tool_sequence_f1` | `0.0` | `False` | `{"actual": [], "expected": ["matter.create", "matter.search", "matter.update", "matter.link_source"]}` |
| `required_tool_called` | `0.0` | `False` | `{"actual": [], "missing": ["matter.create", "matter.link_source", "matter.search", "matter.update"]}` |
| `answer_contains_all` | `0.0` | `False` | `{"missing": ["Prepare evaluation report"]}` |
| `tool_output_contains_all` | `0.0` | `False` | `{"missing": ["Prepare evaluation report", "in_progress", "high", "eval_source", "source_eval_report_001"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 2, "maximum": 12}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 858, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 102772.664, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
