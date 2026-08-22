# Eval Report: hard_mail_tasks

- subject: `runtime`
- generated_at: `2026-08-22T06:12:37.094917+00:00`
- score: `0.8646`
- passed: `0/6`

## Cases

### multi_deadline_digest_with_distractors

- score: `0.8725`
- passed: `False`
- wall_time_ms: `41538.745`
- selected_package: `mail`
- answer_preview: 根据邮件检索和内容核实，以下是当前（2026-08-22）需要关注的**学生事务截止日期汇总**（已排除广告、诈骗及旅行推广类邮件）：

### 1. 课程注册截止
- **截止时间：2026-08-18 17:00（已截止）**
- 来源：教务处 registrar@example.edu
- 事项：课程注册已关闭；如缺少必修模块，需尽快联系教务提交申诉（申诉需在截止前提出，现已逾期，建议立即联系说明情况）。

### 2. ICA 

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.6` | `False` | `{"missing": ["2026-08-19", "Library overdue"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `0.75` | `False` | `{"expected_message_ids": ["mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc", "mail_msg_695e19ddc134", "mail_msg_1d5b26037f6c"], "missing": ["mail_msg_1d5b26037f6c"]}` |
| `loaded_required_messages` | `0.75` | `False` | `{"loaded": ["mail_msg_695e19ddc134", "mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc"], "missing": ["mail_msg_1d5b26037f6c"]}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_695e19ddc134", "mail_msg_a8143b23db5c", "mail_msg_b55458eeaadc"], "violations": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 17946, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 41538.745, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### conflicting_thesis_defense_updates

- score: `0.9074`
- passed: `False`
- wall_time_ms: `34172.083`
- selected_package: `mail`
- answer_preview: 根据研究生院最新邮件（2026-08-20 发送的"Final thesis defense update"），最终答辩信息如下：

- **时间**：2026-08-24（周一）10:30
- **地点**：Room C

这封邮件是最近一封官方更新，已覆盖之前 08-04（原定 09:00）和 08-12（房间变更）等旧信息，请以本次为准。另外邮件还提到，幻灯片需在 2026-08-23 10:30 前上传，别忘了准备。

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_adc1d792637a", "mail_msg_f134db29cac4", "mail_msg_3646512b39eb"], "missing": []}` |
| `loaded_required_messages` | `0.33333333333333337` | `False` | `{"loaded": ["mail_msg_3646512b39eb"], "missing": ["mail_msg_adc1d792637a", "mail_msg_f134db29cac4"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16797, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 34172.083, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### finance_reimbursement_scam_filter

- score: `0.85`
- passed: `False`
- wall_time_ms: `36187.794`
- selected_package: `mail`
- answer_preview: 根据本地邮件记录，Finance workshop 报销需要提交以下材料：

**必需材料**
1. 原始收据（original receipt）
2. 银行账户证明（bank account proof）
3. 报销单（claim form）

**提交截止时间**：2026-08-26 17:00

**来源说明**：以上要求来自合法邮件，发件人为 `finance-office@example.edu`，位于收件箱，主题是 "Fi

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_78bed1c74d79"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_682a6feb15fd", "mail_msg_78bed1c74d79"], "missing": []}` |
| `forbidden_evidence_not_loaded` | `0.0` | `False` | `{"loaded": ["mail_msg_682a6feb15fd", "mail_msg_78bed1c74d79"], "violations": ["mail_msg_682a6feb15fd"]}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16563, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 36187.794, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### old_vs_current_visa_photo_requirement

- score: `0.875`
- passed: `False`
- wall_time_ms: `28125.941`
- selected_package: `mail`
- answer_preview: 根据 2026-08-19 收到的签证办公室邮件（发件人：visa-office@example.gov.sg），当前 Visa 照片重新提交的要求如下：

1. **背景**：必须为白色背景
2. **尺寸**：35×45 mm
3. **眼镜**：不允许佩戴眼镜
4. **上传截止时间**：2026-08-21 12:00

需要提醒的是：截至当前时间（2026-08-22），邮件中规定的上传截止时间（8 月 21 日 12:00）

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.25` | `False` | `{"missing": ["white background", "35x45 mm", "no glasses"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_9a166b0197d7"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_9a166b0197d7"], "missing": []}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_9a166b0197d7"], "violations": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15002, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 28125.941, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### capstone_multisource_to_matter

- score: `0.7329`
- passed: `False`
- wall_time_ms: `77522.902`
- selected_package: `mail`
- answer_preview: 已完成 ✅ 已把 Capstone pitch 相关事项整理为一个本地事务，并关联了邮件证据。

**事务信息**
- 事务 ID：`matter_1aac594564c3`
- 标题：Capstone pitch 准备与排练
- 状态：进行中（in_progress）｜优先级：高
- 截止时间：2026-08-22 16:00（Asia/Shanghai）
- 标签：`capstone`、`pitch`

**事务要点**
1. 制作

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `0.0` | `False` | `{"actual": ["mail.search", "mail.load_messages", "matter.search", "matter.create"], "expected": ["mail.search", "mail.load_messages", "matter.create"]}` |
| `tool_sequence_f1` | `0.8571` | `False` | `{"actual": ["mail.search", "mail.load_messages", "matter.search", "matter.create"], "expected": ["mail.search", "mail.load_messages", "matter.create"]}` |
| `answer_contains_all` | `0.5` | `False` | `{"missing": ["Prepare capstone pitch package"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_46195099f9d9", "mail_msg_34139bb38969"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"], "missing": []}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"], "violations": []}` |
| `matter_write_precision` | `0.5` | `False` | `{"created_count": 1, "missing": ["Prepare capstone pitch package", "printed rubrics"]}` |
| `matter_source_link_recall` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_34139bb38969", "mail_msg_46195099f9d9"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 4, "total": 4}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `0.0` | `False` | `{"actual": 12, "maximum": 10}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 47088, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 77522.902, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### lab_incident_specific_vs_general_training

- score: `0.95`
- passed: `False`
- wall_time_ms: `30791.378`
- selected_package: `mail`
- answer_preview: 根据实验室安全邮件（发件人 lab-safety@example.edu，主题 "Lab incident report required"），提交 **Lab incident report（编号 LIR-2026-08）** 需要包含以下内容：

1. **witness name**（证人姓名）
2. **equipment ID**（设备编号）
3. **corrective action**（纠正措施）

另外邮件提醒的提交截

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_35c6a3dc6487"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_35c6a3dc6487"], "missing": []}` |
| `forbidden_evidence_not_loaded` | `1.0` | `True` | `{"loaded": ["mail_msg_35c6a3dc6487"], "violations": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 8, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 18179, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 30791.378, "maximum": 7000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
