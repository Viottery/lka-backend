# Eval Report: mail_to_matter

- subject: `runtime`
- generated_at: `2026-08-22T06:08:28.401954+00:00`
- score: `0.8788`
- passed: `0/1`

## Cases

### course_deadline_to_matter

- score: `0.8788`
- passed: `False`
- wall_time_ms: `58246.996`
- selected_package: `mail`
- answer_preview: 已完成 ✅

我已将邮件 **“Course registration deadline reminder”**（发件人：registrar@example.edu）整理成待办事项：

**📝 待办事项详情**
- **标题**：Course registration deadline
- **截止时间**：2026-08-18 17:00
- **优先级**：高
- **状态**：open
- **标签**：`course`、`reg

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages", "matter.create"], "expected": ["mail.search", "mail.load_messages", "matter.create"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages", "matter.create"], "expected": ["mail.search", "mail.load_messages", "matter.create"]}` |
| `required_tool_called` | `1.0` | `True` | `{"actual": ["mail.load_messages", "mail.search", "matter.create"], "missing": []}` |
| `answer_contains_all` | `0.5` | `False` | `{"missing": ["创建本地事务"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_b55458eeaadc"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_b55458eeaadc"], "missing": []}` |
| `matter_write_precision` | `0.6666666666666667` | `False` | `{"created_count": 1, "missing": ["Submit course registration"]}` |
| `matter_source_link_recall` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_b55458eeaadc"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 3, "total": 3}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 10, "maximum": 10}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 33260, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 58246.996, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
