# Eval Report: stream_contract

- subject: `stream`
- generated_at: `2026-08-22T06:22:33.990433+00:00`
- score: `0.9091`
- passed: `0/1`

## Cases

### stream_mail_basic_ntuso

- score: `0.9091`
- passed: `False`
- wall_time_ms: `37733.298`
- selected_package: `mail`
- answer_preview: 根据你的邮箱中的相关邮件，我帮你整理出 NTUSO audition 的考试要求如下：

**考试内容**
- G 大调音阶（G major scale）
- 一首自选曲目（prepared piece）
- 视奏（sight reading）

**考场与时间**
- 考场：Music Studio 3
- 小提琴考生请于 **2026-08-28 14:00** 参加考试

**其他要求**
- 请**提前 15 分钟**到达考场


| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `answer_contains_any` | `1.0` | `True` | `{"candidates": ["NTUSO", "audition"], "matched": ["NTUSO", "audition"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 37733.298, "maximum": 30000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
| `sse_sequence_valid` | `1.0` | `True` | `{"events": ["run_started", "llm_started", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta", "llm_delta"` |
