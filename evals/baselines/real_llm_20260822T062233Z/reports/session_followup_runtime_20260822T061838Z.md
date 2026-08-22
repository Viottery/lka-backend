# Eval Report: session_followup

- subject: `runtime`
- generated_at: `2026-08-22T06:18:38.204037+00:00`
- score: `0.8824`
- passed: `0/1`

## Cases

### cached_mail_reuse_single_turn_contract

- score: `0.8824`
- passed: `False`
- wall_time_ms: `34124.26`
- selected_package: `mail`
- answer_preview: 已成功读取两封 NTUSO audition 相关邮件，完整内容如下：

---

**📧 邮件一：NTUSO Audition requirements 2026**
- **发件人**：ntuso@example.edu
- **收件时间**：2026-08-05 09:30 UTC
- **内容摘要**：
  - 面试要求包括：**G 大调音阶**、**一首自备曲目**、**视奏**
  - 需**提前 15 分钟到达**
  -

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.0` | `False` | `{"missing": ["G major scale", "one prepared piece"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_1e836c3e6c9e"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_1e836c3e6c9e", "mail_msg_df04b623c7e8"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16133, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 34124.26, "maximum": null}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
