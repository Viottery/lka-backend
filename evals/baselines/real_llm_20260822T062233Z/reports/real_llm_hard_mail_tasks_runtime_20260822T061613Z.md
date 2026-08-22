# Eval Report: real_llm_hard_mail_tasks

- subject: `runtime`
- generated_at: `2026-08-22T06:16:13.473100+00:00`
- score: `0.7894`
- passed: `1/4`

## Cases

### real_multi_deadline_digest_with_distractors

- score: `0.9306`
- passed: `False`
- wall_time_ms: `29618.823`
- selected_package: `mail`
- answer_preview: 根据邮件检索结果，已从收件箱中筛选出 3 封与学生事务相关的邮件，均已排除广告、诈骗和旅行信息。汇总如下：

## 📋 学生事务 Deadline 汇总

| 事项 | 截止时间 | 状态（按当前时间 2026-08-22） | 说明 |
|---|---|---|---|
| **课程注册**（Course registration） | 2026-08-18 17:00 | ⚠️ 已过期 | 若缺少必修模块，需在截止前提交申诉；现已

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `required_tool_called` | `1.0` | `True` | `{"actual": ["mail.load_messages", "mail.search"], "missing": []}` |
| `answer_contains_all` | `0.75` | `False` | `{"missing": ["2026-08-19"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `0.75` | `False` | `{"expected_message_ids": ["mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc", "mail_msg_695e19ddc134", "mail_msg_1d5b26037f6c"], "missing": ["mail_msg_1d5b26037f6c"]}` |
| `loaded_required_messages` | `0.75` | `False` | `{"loaded": ["mail_msg_695e19ddc134", "mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc"], "missing": ["mail_msg_1d5b26037f6c"]}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_695e19ddc134", "mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc"], "violations": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 12}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16501, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 29618.823, "maximum": 60000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### real_conflicting_thesis_defense_updates

- score: `0.9271`
- passed: `False`
- wall_time_ms: `39406.59`
- selected_package: `mail`
- answer_preview: 根据最新的邮件通知（**Final thesis defense update**，2026-08-20 收到），你的论文答辩最终安排如下：

- **时间**：2026年8月24日（周一）上午 **10:30**
- **地点**：**Room C**

这是邮件中确认的最终版本，不含之前较早邮件里的旧时间（例如 09:00 或换个房间之类的信息），以这条最新通知为准。另外邮件还提醒：请在 2026-08-23 10:30 前上传答辩

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `required_tool_called` | `1.0` | `True` | `{"actual": ["mail.load_messages", "mail.search"], "missing": []}` |
| `answer_contains_all` | `0.75` | `False` | `{"missing": ["2026-08-24"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_adc1d792637a", "mail_msg_f134db29cac4", "mail_msg_3646512b39eb"], "missing": []}` |
| `loaded_required_messages` | `0.33333333333333337` | `False` | `{"loaded": ["mail_msg_3646512b39eb"], "missing": ["mail_msg_adc1d792637a", "mail_msg_f134db29cac4"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 12}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16902, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 39406.59, "maximum": 60000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### real_finance_reimbursement_scam_filter

- score: `1.0`
- passed: `True`
- wall_time_ms: `32268.583`
- selected_package: `mail`
- answer_preview: 根据官方邮件（发件人：finance-office@example.edu，2026-08-18 收到），Finance workshop 报销需要提交以下材料：

1. **原始收据**（original receipt）
2. **银行账户证明**（bank account proof）
3. **报销申请表单**（claim form）

提交截止时间为 **2026-08-26 17:00**。

关于可疑邮件：请勿采信来自 *

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `required_tool_called` | `1.0` | `True` | `{"actual": ["mail.load_messages", "mail.search"], "missing": []}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_78bed1c74d79"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_78bed1c74d79"], "missing": []}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_78bed1c74d79"], "violations": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 12}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15511, "maximum": null}` |
| `wall_time_ms` | `1.0` | `True` | `{"actual": 32268.583, "maximum": 60000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### real_capstone_multisource_to_matter

- score: `0.3`
- passed: `False`
- wall_time_ms: `114694.811`
- selected_package: `None`
- answer_preview: 

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `0.0` | `False` | `{"error": {"message": "Remote end closed connection without response", "type": "RemoteDisconnected"}}` |
| `selected_package_match` | `0.0` | `False` | `{"actual": null, "expected": "mail"}` |
| `required_tool_called` | `0.0` | `False` | `{"actual": [], "missing": ["mail.load_messages", "mail.search", "matter.create"]}` |
| `answer_contains_any` | `0.0` | `False` | `{"candidates": ["Capstone", "capstone", "事务", "matter"], "matched": []}` |
| `evidence_recall_at_k` | `0.0` | `False` | `{"expected_message_ids": ["mail_msg_46195099f9d9", "mail_msg_34139bb38969"], "missing": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"]}` |
| `loaded_required_messages` | `0.0` | `False` | `{"loaded": [], "missing": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"]}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": [], "violations": []}` |
| `matter_write_precision` | `0.0` | `False` | `{"created_count": 0, "missing": ["Capstone", "2026-08-22"]}` |
| `matter_source_link_recall` | `0.0` | `False` | `{"expected_message_ids": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"], "missing": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"tool_event_count": 0}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 0, "maximum": 14}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 0, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 114694.811, "maximum": 60000}` |
| `run_log_completeness_rate` | `0.0` | `False` | `{"json_parse_errors": [], "missing_sections": ["User Input", "Package Catalog", "Session Context Window", "Expanded Tools", "Decision Events", "Tool Events", "Progress Events", "Verification Warnings", "LLM Events", "Answer"]}` |
