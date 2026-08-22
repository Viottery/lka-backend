# Eval Report: failure_recovery

- subject: `runtime`
- generated_at: `2026-08-22T06:19:26.030332+00:00`
- score: `0.9412`
- passed: `0/1`

## Cases

### no_schema_rejections_on_valid_mail_qa

- score: `0.9412`
- passed: `False`
- wall_time_ms: `46998.346`
- selected_package: `mail`
- answer_preview: 根据邮箱中 ICA（ica@example.gov.sg）于 **2026-08-07** 发送的邮件《ICA Student Pass completion checklist》，完整的 **ICA Student Pass 办理清单**如下：

1. **IPA letter**（原则上批准信）
2. **Passport**（护照）
3. **SG Arrival Card**（新加坡入境卡）
4. **Recent photo*

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.5` | `False` | `{"missing": ["passport"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_a8143b23db5c"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_a8143b23db5c"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15794, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 46998.346, "maximum": null}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
