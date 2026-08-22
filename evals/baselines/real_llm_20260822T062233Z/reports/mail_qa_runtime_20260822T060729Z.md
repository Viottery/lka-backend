# Eval Report: mail_qa

- subject: `runtime`
- generated_at: `2026-08-22T06:07:29.743098+00:00`
- score: `0.8824`
- passed: `0/11`

## Cases

### ntuso_requirements

- score: `0.9412`
- passed: `False`
- wall_time_ms: `29828.045`
- selected_package: `mail`
- answer_preview: 根据我查到的 NTUSO（南洋理工大学交响乐团）邮件，audition 的要求如下：

**核心要求：**
1. **G 大调音阶**（G major scale）
2. **一首自备曲目**（one prepared piece）
3. **视奏**（sight reading）
4. **提前 15 分钟到达** audition 现场
5. audition 房间为 **Music Studio 3**

**另外，来自更新邮件的

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_1e836c3e6c9e"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_1e836c3e6c9e", "mail_msg_df04b623c7e8"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15851, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 29828.045, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### ntuso_latest_schedule

- score: `0.9118`
- passed: `False`
- wall_time_ms: `25151.897`
- selected_package: `mail`
- answer_preview: 根据最新收到的 NTUSO 邮件（2026-08-12 更新的 audition schedule），信息如下：

**面试时间：**
- 2026年8月28日（周五）下午 2:00（14:00）
- 适用对象：小提琴（violin）候选人

**要带的东西：**
- 学生证（student card）
- 打印的曲目单（printed repertoire list）

另外，根据 2026 年 audition requiremen

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.75` | `False` | `{"missing": ["2026-08-28"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_df04b623c7e8"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_1e836c3e6c9e", "mail_msg_df04b623c7e8"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15015, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 25151.897, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### ica_checklist

- score: `0.8941`
- passed: `False`
- wall_time_ms: `73354.18`
- selected_package: `mail`
- answer_preview: 根据你邮箱中收到的一封 ICA 邮件（主题为 "ICA Student Pass completion checklist"，发件人 ica@example.gov.sg，收到时间 2026-08-07），完成 ICA Student Pass 手续需要准备以下材料：

1. **IPA letter**（原则批准信，In-Principle Approval letter）
2. **护照**（Passport）
3. **SG Ar

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.6` | `False` | `{"missing": ["passport", "recent photo"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_a8143b23db5c"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_a8143b23db5c"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 20132, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 73354.18, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### attachment_metadata

- score: `0.9412`
- passed: `False`
- wall_time_ms: `38736.22`
- selected_package: `mail`
- answer_preview: 根据邮件记录，**Lab onboarding documents**（发件人：lab-admin@example.edu，收件时间 2026-08-11）中包含以下 **2 个附件**：

1. **lab_safety_guide.pdf** — PDF 文件，约 240 KB（245,760 字节）
2. **workstation_policy.docx** — Word 文档，约 96 KB（98,304 字节）

邮件正文也

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_b6649df353b4"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_b6649df353b4"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15540, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 38736.22, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### scholarship_deadline_with_noise

- score: `0.7148`
- passed: `False`
- wall_time_ms: `62182.151`
- selected_package: `mail`
- answer_preview: 根据收到的邮件通知（来自 financial-aid@example.edu，发送于 2026-08-13），奖学金申请的 **延期截止日期为 2026 年 8 月 25 日 23:59（新加坡时间 SGT）**。

所需材料包括：

1. **成绩单**（transcript）
2. **个人陈述**（personal statement）
3. **推荐人联系方式**（referee contact）

补充说明：今天是 8 月 2

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `0.0` | `False` | `{"actual": ["mail.search", "mail.search", "mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `0.6667` | `False` | `{"actual": ["mail.search", "mail.search", "mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.6` | `False` | `{"missing": ["2026-08-25", "23:59 SGT"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_695e19ddc134"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_695e19ddc134"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 4, "total": 4}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `0.0` | `False` | `{"actual": 11, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 34108, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 62182.151, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### rescheduled_event_overrides_original

- score: `0.9074`
- passed: `False`
- wall_time_ms: `33468.641`
- selected_package: `mail`
- answer_preview: Quantum seminar 最终改到 **2026年8月21日（周五）15:30**，地点为 **Seminar Room B**。

根据 seminars@example.edu 在 8月14日发出的改期邮件，原定时间（8月19日 10:00，LT5）已不再有效，请以新时间和新地点为准。

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.6666666666666667` | `False` | `{"missing": ["2026-08-21"]}` |
| `answer_excludes_all` | `1.0` | `True` | `{"violations": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_4978f1c297d9"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_4978f1c297d9", "mail_msg_f21903f3eedd"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 16703, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 33468.641, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### cancelled_event

- score: `0.8824`
- passed: `False`
- wall_time_ms: `29556.088`
- selected_package: `mail`
- answer_preview: 不需要去了。

我查了一下你邮箱里的通知：**career@example.edu** 在 8 月 9 日发来邮件，标题是 *"Career fair booth session cancelled"*，里面明确写着：

> 原定于 **2026-08-16** 的 career fair booth training 已取消，且**没有安排替代场次**。

所以这次培训你不用参加了。如果后续有新的安排（比如重新发布培训通知），我可以再

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.5` | `False` | `{"missing": ["no replacement session"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_476d619cd345"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_476d619cd345"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 14208, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 29556.088, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### bilingual_orientation

- score: `0.9176`
- passed: `False`
- wall_time_ms: `37871.794`
- selected_package: `mail`
- answer_preview: 根据学生生活处（student-life@example.edu）8月15日发送的邮件通知：

**迎新活动时间：** 2026年8月22日（今天）上午 09:00，地点在 **Auditorium A**（A礼堂）。

**需要携带的物品：**
- 学生卡（student card）
- 水壶（water bottle）

提醒一下，活动时间是今天上午9点，记得提前到达并带好上述物品。如需更多细节，可以回复那封邮件或联系学生生活处确认

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.8` | `False` | `{"missing": ["2026-08-22"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_61c431de4042"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_61c431de4042"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15130, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 37871.794, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### library_overdue

- score: `0.7137`
- passed: `False`
- wall_time_ms: `53100.707`
- selected_package: `mail`
- answer_preview: 根据本地邮箱中的图书馆逾期警告邮件（发件人 library@example.edu，发送于 2026-08-16），关键信息如下：

- **涉及图书**：《Distributed Systems》
- **最晚归还日期**：**2026-08-19**（在此之前归还可避免罚款）
- **逾期罚款标准**：**SGD 3.00 / 天**

今天是 **2026-08-22**，如果这本书尚未归还，那么从 8 月 20 日起已逾期：



| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `0.0` | `False` | `{"actual": ["mail.search", "mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `0.8` | `False` | `{"actual": ["mail.search", "mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `0.6666666666666667` | `False` | `{"missing": ["SGD 3.00 per day"]}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_1d5b26037f6c"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_1d5b26037f6c"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 3, "total": 3}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `0.0` | `False` | `{"actual": 9, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 26282, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 53100.707, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### timezone_event

- score: `0.9412`
- passed: `False`
- wall_time_ms: `34877.124`
- selected_package: `mail`
- answer_preview: 根据收件箱中 advisor@example.edu 发来的邮件（主题：Research meeting timezone clarification），Research meeting 的安排如下：

- **时区**：SGT（新加坡时间，UTC+8），邮件明确说明是 09:00 SGT，**不是 UTC**。
- **时间**：2026-08-23 09:00 SGT
- **议程**：草稿评审（draft review）和实验计划

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_fa4fa5b86641"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_fa4fa5b86641"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 15538, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 34877.124, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |

### admin_attachment_bundle

- score: `0.9412`
- passed: `False`
- wall_time_ms: `28472.715`
- selected_package: `mail`
- answer_preview: 邮件 **“Admin forms attachment bundle”**（发件人：admin@example.edu，2026-08-18 收到）共包含 **3 个附件**：

1. **consent_form.pdf**（PDF，约 110 KB）
2. **reimbursement_form.xlsx**（Excel 表格，约 36 KB）
3. **room_booking_policy.pdf**（PDF，约 88 KB

| metric | score | pass | details |
| --- | ---: | --- | --- |
| `no_runtime_error` | `1.0` | `True` | `{"error": null}` |
| `selected_package_match` | `1.0` | `True` | `{"actual": "mail", "expected": "mail"}` |
| `tool_sequence_exact_match` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `tool_sequence_f1` | `1.0` | `True` | `{"actual": ["mail.search", "mail.load_messages"], "expected": ["mail.search", "mail.load_messages"]}` |
| `answer_contains_all` | `1.0` | `True` | `{"missing": []}` |
| `evidence_recall_at_k` | `1.0` | `True` | `{"expected_message_ids": ["mail_msg_9969d7810898"], "missing": []}` |
| `loaded_required_messages` | `1.0` | `True` | `{"loaded": ["mail_msg_9969d7810898"], "missing": []}` |
| `tool_success_rate` | `1.0` | `True` | `{"completed": 2, "total": 2}` |
| `schema_rejection_count` | `1.0` | `True` | `{"actual": 0, "maximum": 0}` |
| `llm_call_count` | `1.0` | `True` | `{"actual": 7, "maximum": 8}` |
| `reported_token_total` | `1.0` | `True` | `{"actual": 14709, "maximum": null}` |
| `wall_time_ms` | `0.0` | `False` | `{"actual": 28472.715, "maximum": 5000}` |
| `run_log_completeness_rate` | `1.0` | `True` | `{"json_parse_errors": [], "missing_sections": []}` |
