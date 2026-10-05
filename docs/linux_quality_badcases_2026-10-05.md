# Linux 质量迭代历史案例档案（2026-10-05）

机器目录：[linux_quality_cases_2026-10-05.json](../evals/fixtures/linux_quality_cases_2026-10-05.json)。
依据：[逐轮记录](quality_iteration_2026-10-05.md)与[收尾报告](linux_quality_final_report_2026-10-05.md)。
本文整理历史、优化评测选择与核对现有证据；本次没有新增模型调用、网络请求或生产运行。

## 口径与边界

按能独立定位和验收的能力/故障归档，同一故障的多次 run 合为一项；不同缺陷不能因同一次运行而互相洗白。
`foundation` 为基础能力，`regression` 为已暴露的具体缺陷，`challenge` 为整体质量挑战；
tier 不决定通过状态。全部 `exposure=seen`，昔日 heldout 现在也已见，不宣称 blind。

`baseline_passed` 单列基线：未观察到 Agent bad，不计修复。
`fixed_live` 只表示条目中写明的 bounded 验收有实际模型链路证据，不承诺整个任务、所有分支或长期稳定性。
`fixed_offline` 表示补丁与离线反例已验证，缺最终 live 闭环。
`open` 保留已确认失败或未达验收，`inconclusive` 供证据不足而不能判定的条目使用。
离线测试绿、工具 completed、关键词覆盖及父子 run completed 均不是语义通过。
复杂三独立合同仍失败，不能用主要来自同一 child 的六 facts 替代独立审计。

当前状态按最窄已验证范围填写；每项的“后证据”仍保留整体失败与未覆盖分支。
历史数据取自记录，时间变化不是严格性能 A/B。没有可靠 TTFT 的文本任务不填写 TTFT。
原始 prompt、邮件、数据库与回答不复制到这两个文件；私有 artifact 引用只用于本地复核。

## 评测集统计与状态索引

<!-- BEGIN AUTO LINUX QUALITY STATS -->
统计由 JSON 目录及实际文件内容计算；可用 `scripts/eval_quality_catalog.py --profile all --inventory --json` 重新核对。
快照：[linux_quality_catalog_statistics_20261005.json](../evals/fixtures/linux_quality_catalog_statistics_20261005.json)。

**40 个逻辑案例 = 4 基线 + 16 有真实复测的局部修复 + 12 仅离线修复 + 8 仍 bad；证据不确定条目 0。**
历史 bad 为 36 项（不含 4 基线）。这是历史案例状态，不是本次重新运行的模型准确率。

| 分组 | 合计 | 基础基线 | 真实复测修复 | 仅离线修复 | 仍 bad |
| --- | ---: | ---: | ---: | ---: | ---: |
| 文件 / Bash / 编码 | 6 | 3 | 1 | 2 | 0 |
| 邮件 | 5 | 0 | 4 | 1 | 0 |
| 网页 | 5 | 0 | 0 | 2 | 3 |
| RAG | 6 | 0 | 3 | 1 | 2 |
| 多 Agent | 5 | 0 | 2 | 1 | 2 |
| 记忆 | 3 | 0 | 2 | 1 | 0 |
| 后台 | 2 | 0 | 1 | 1 | 0 |
| 上下文 / 工具 | 6 | 1 | 1 | 3 | 1 |
| 持续关注 | 2 | 0 | 2 | 0 | 0 |

| 选择 profile | 逻辑项数 | 用途 |
| --- | ---: | --- |
| `focus`（默认） | 20 | 8 未解决 + 12 待真实验证；跳过基础与已验证修复 |
| `regression` | 29 | 具体缺陷回归，含未解决的显式 opt-in 红例；不保证全绿 |
| `foundation` | 4 | 基础能力；模块改动或发布前按需补测 |
| `historical_holdout` | 10 | 曾用于迁移检验，现在全部 seen，不是 blind |
| `all` | 40 | 全档案，不会自动执行 |

Profiles 有交集，不能相加。40 项都有相关 checker/回归引用；这不意味着这些单测覆盖全部语义验收。
全目录去重后 83 个 pytest 文件/节点选择目标，26 项带场景再测入口、合并为 19 个不同再测计划，
14 项需要手动配置/选择或只有离线反例。多个逻辑问题共享一次真实场景时只派发一次，不把一次证据算多次。

现有资源另行计数，不与逻辑案例相加：**17 个 suite / 56 个不同 case 定义；27 个 goal-only 场景；
4 个公开数据集 / 1,128 个问题 / 5,949 个文档记录**。这些是可用资源，不表示本轮全部实际执行。

| 公开数据集 | 实际问题数 | 文档记录数 |
| --- | ---: | ---: |
| multihop-rag | 528 | 355 |
| hotpotqa | 200 | 1952 |
| 2wikimultihopqa | 200 | 1447 |
| musique | 200 | 2195 |

`multihop_rag_600.jsonl` 实际是 528 问题，不能按文件名统计成 600。不同数据集文档未做跨集去重。
新模型调用 0，搜索 0；完整历史测试费用仍见上一轮报告，不把筛选/统计当成新质量测量。

### 已有真实复测支持的修复（16）

- `background_async_compaction_outbox`：硬溢出后台outbox与前台继续运行。完整优化方法、前后证据和范围限制见下方同名条目。
- `cached_text_delivery_receipts`：最终provider缓存文本实际交付区间。完整优化方法、前后证据和范围限制见下方同名条目。
- `child_failed_generation_partial_delivery`：子生成失败占位成功及已有摘要固定截断。完整优化方法、前后证据和范围限制见下方同名条目。
- `knowledge_full_authority_empty_grant`：知识full authority空显式grant误拒绝。完整优化方法、前后证据和范围限制见下方同名条目。
- `long_log_tail_delivery`：长日志尾部证据与严格决策恢复。完整优化方法、前后证据和范围限制见下方同名条目。
- `mail_body_updates_no_duplicate_reads`：正文变更行动项及父级重复读取。完整优化方法、前后证据和范围限制见下方同名条目。
- `mail_cached_full_group_count`：缓存全量邮件精确分组计数。完整优化方法、前后证据和范围限制见下方同名条目。
- `mail_expert_identity_audit`：邮件专家路由身份与实际工作流审计。完整优化方法、前后证据和范围限制见下方同名条目。
- `mail_stable_pagination`：私有邮件稳定分页标识。完整优化方法、前后证据和范围限制见下方同名条目。
- `memory_terminal_period_alias`：句号异形候选两独立来源晋升。完整优化方法、前后证据和范围限制见下方同名条目。
- `memory_withdrawal_late_worker_fence`：撤回后晚到worker不得复活旧偏好。完整优化方法、前后证据和范围限制见下方同名条目。
- `rag_chunk_tail_conditions`：知识块尾部条件静默截断与续读。完整优化方法、前后证据和范围限制见下方同名条目。
- `replacement_contract_degradation_gate`：替代执行不得冒充原合同覆盖。完整优化方法、前后证据和范围限制见下方同名条目。
- `requested_json_answer_format`：请求短JSON被中文prose覆盖。完整优化方法、前后证据和范围限制见下方同名条目。
- `watch_current_state_warning`：关注当前值与旧值矛盾及覆盖告警隐藏。完整优化方法、前后证据和范围限制见下方同名条目。
- `watch_grounded_partial_identity`：关注有据partial交付与跨来源同事项比较。完整优化方法、前后证据和范围限制见下方同名条目。

### 现在仍 bad（8）

- `catalog_routing_efficiency`：目录精简后仍有错误路由和延迟。
- `degraded_history_partial_missing`：skip/degrade后历史partial和说明丢失。
- `parallel_three_independent_contracts`：复杂多Agent三独立审计整体失败。
- `public_qa_relation_reasoning`：公开配对QA缺跳、关系推理与弃答。
- `public_retrieval_all_hop_regression`：公开多跳检索融合覆盖负面结果。
- `web_python_multisource_conditions`：Python多来源核查默认构建与支持限定。
- `web_release_unproved_causes`：现场版本搜索的缓存与跳转归因。
- `web_sqlite_condition_brevity`：SQLite互证后仍遗漏WAL增长条件。

### 仅离线修复，尚不能列入真实通过（12）

- `bash_readonly_arguments`：Bash参数级只读边界。
- `bash_shell_lexing_hooks`：引号注释绕过与继承执行钩子。
- `benchmark_query_prefix_profile`：公开检索query prefix显式接线。
- `incomplete_control_not_completion`：原生控制与SSE不完整响应不得授权或完成。
- `mail_low_priority_action_handoff`：低优先级必需行动不应在专家handoff消失。
- `memory_extractive_grounding`：记忆候选原文支撑、否定条件与scope。
- `preexecution_rejection_audit`：执行前拒绝与持久化bool副作用审计。
- `replan_atomic_attempt_recovery`：重规划格式恢复与原子attempt发表。
- `summary_prefix_provenance_inheritance`：摘要跨prefix来源误拒导致local fallback。
- `tool_discovery_read_truth_table`：条件只读发现与严格动态分类。
- `web_bounded_paging_find_recovery`：网页长文分页、窗口去重与零匹配恢复。
- `web_html_readability`：HTML可读抽取噪声及隐藏标签。
<!-- END AUTO LINUX QUALITY STATS -->

## 基线（不计修复）

### `baseline_port_diagnosis` — 配置与日志端口冲突诊断基线

分组 `files_bash_code` · tier `foundation` · 现在状态 `baseline_passed` · `exposure=seen` · 数据 `synthetic`。

之前症状：本任务未观察到 bad；配置端口与实际监听端口不同。

原因：不适用：已有证据足以定位冲突。

优化思路与方法：不计修复；保留只读诊断和独立事实核对。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R1：17.325s/5 calls，人工核对正确
- docs/quality_iteration_2026-10-05.md — R4：24.397s/8 calls仍正确；没有提速

验收标准：

- 正确区分配置5433与实际5432；不修改文件

回归或再测入口：[tests/test_eval_realworld.py::test_fixture_runtime_isolated_and_artifact_retained_without_api](../tests/test_eval_realworld.py)。

现有再测选择元数据：runner=`realworld`，case=`bash_diagnosis`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`baseline`、`no_prior_bad`。

### `baseline_native_code_repair` — 原生小代码修复与不可用测试入口基线

分组 `files_bash_code` · tier `foundation` · 现在状态 `baseline_passed` · `exposure=seen` · 数据 `synthetic`。

之前症状：评测任务中代码原有缺陷，但 Agent 修复能力未出现已确认 bad。

原因：分页、mutable-default或名称归一化是fixture缺陷；不能当Agent历史失败。

优化思路与方法：不计修复；独立运行原测试、禁止改测试或安装依赖。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R1：分页修复28.583s/10 calls，4 unittest通过
- docs/quality_iteration_2026-10-05.md — R8：mutable-default留出21.893s；R12：测试入口留出25.479s/10 calls，4原测试通过

验收标准：

- 原测试独立通过；只修改目标代码
- 不可用旧runner时使用本地unittest；命令exit被echo遮蔽不能抹掉先前失败

回归或再测入口：[tests/test_eval_realworld.py::test_goals_do_not_prescribe_registered_tools](../tests/test_eval_realworld.py)；[tests/test_eval_realworld.py::test_fixture_runtime_isolated_and_artifact_retained_without_api](../tests/test_eval_realworld.py)。

现有再测选择元数据：runner=`realworld`，case=`heldout_broken_test_runner`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`baseline`、`no_prior_bad`、`bounded_small_project`、`heldout`。

### `baseline_receipt_copy` — 按内容日期复制与同名收据消歧基线

分组 `files_bash_code` · tier `foundation` · 现在状态 `baseline_passed` · `exposure=seen` · 数据 `synthetic`。

之前症状：从未观察到该整理任务的 Agent bad；含中文、空格、误导文件名与缺日期。

原因：不适用；内容日期为月份依据，reference用于同名消歧。

优化思路与方法：不计修复；核对原件/副本哈希与最终文件集合。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R14：17.315s/7 calls/3 Bash，三副本哈希通过
- docs/quality_iteration_2026-10-05.md — R16：29.746s/8 calls/4 Bash，跨目录同名、CRLF与缺日期留出通过；未测同票号异内容或并发修改

验收标准：

- 有效副本逐字节一致；所有原件及备注保持
- 缺日期不推断；不得覆盖、漏文件或产生额外文件

回归或再测入口：[tests/test_eval_realworld.py::test_copy_verification_catches_mutation_missing_copy_and_extra_files](../tests/test_eval_realworld.py)。

现有再测选择元数据：runner=`realworld`，case=`heldout_file_collision`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`baseline`、`no_prior_bad`、`bounded_static_csv`、`heldout`。

### `baseline_same_session_reuse` — 持久会话短追问复用基线

分组 `context_tools` · tier `foundation` · 现在状态 `baseline_passed` · `exposure=seen` · 数据 `synthetic`。

之前症状：原任务与同会话标识追问未观察到 bad。

原因：不适用；已有会话工具证据可复用。

优化思路与方法：不计修复；核对实际session、调用痕迹和源文件。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R15：首轮12.452s/6 calls/2 tools；追问2.877s/2 calls/0 tools，正确且源文件未改

验收标准：

- 同会话追问正确返回标识且0 tools；无写
- 不得把此短历史基线当压缩后摘要连续发表验收

回归或再测入口：[tests/test_eval_realworld.py::test_followup_reuses_same_session_and_keeps_its_timeout_trace](../tests/test_eval_realworld.py)。

现有再测选择元数据：runner=`realworld`，case=`heldout_context_reuse`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`baseline`、`no_prior_bad`、`heldout`。

## 文件、Bash 与代码（`files_bash_code`）

### `long_log_tail_delivery` — 长日志尾部证据与严格决策恢复

分组 `files_bash_code` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：原始工具已有末次失败，但模型只见头部；非法动作被当完成。

原因：宽松JSON截取把嵌套参数当envelope；500字符预览遗漏尾部。

优化思路与方法：验证完整决策envelope；首尾预览附区间/遗漏，原缓存可完整回读。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R1：13.001s/4 calls失败；R2：6红例后严格解析相关62通过
- docs/quality_iteration_2026-10-05.md — R2：同题19.130s/5 calls，失败结论、v3.9与CHECK_783正确；R8中段留出37.059s

验收标准：

- 返回最后失败、版本及标识；不提前完成
- 中段未见须续读，不能把首尾当全文件

回归或再测入口：[tests/test_decision_protocol_quality.py](../tests/test_decision_protocol_quality.py)；[tests/test_tool_result_gate.py](../tests/test_tool_result_gate.py)。

现有再测选择元数据：runner=`realworld`，case=`long_file_tail`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_log_diagnosis`。

### `bash_readonly_arguments` — Bash参数级只读边界

分组 `files_bash_code` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：READ executor放行sed -i等写参数，隔离文件alpha被改beta。

原因：旧白名单只看命令名，忽略输出、可执行路径与展开。

优化思路与方法：逐命令保守校验参数与shell展开；写或unknown走审批，READ拒绝。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R19：25新例先失败；参数/缩写/输出/变量补充后80联合通过
- docs/quality_iteration_2026-10-05.md — R20-S：最终shell/参数/既有Bash/目录/诊断153通过

验收标准：

- 写参数和未知展开在READ下零执行
- 普通数字/末行sed print等只读仍可执行；不冒称OS sandbox

回归或再测入口：[tests/test_bash_readonly_argument_quality.py](../tests/test_bash_readonly_argument_quality.py)；[tests/test_bash_tools.py::test_bash_non_read_only_command_requires_safety_review](../tests/test_bash_tools.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`authorization`。

### `bash_shell_lexing_hooks` — 引号注释绕过与继承执行钩子

分组 `files_bash_code` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：quoted separator或单词内#隐藏输出参数；pwd也可经BASH_ENV写marker。

原因：去引号后再切分与shlex注释语义不匹配；READ继承启动/rg钩子环境。

优化思路与方法：扫描原始shell词法，逐换行检查；READ同步/后台去除RIPGREP_CONFIG_PATH、BASH_ENV、ENV、导出函数。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R20-S：5实际副作用红例；启动文件/导出函数×同步/后台4红例后修复
- docs/quality_iteration_2026-10-05.md — R20-S：Root153 passed/8.42s

验收标准：

- READ下quoted分隔符/注释不能隐藏写参；钩子零副作用
- 审批后的非只读语义保持；不认证PATH/login profile/Git配置

回归或再测入口：[tests/test_bash_shell_lexing_quality.py](../tests/test_bash_shell_lexing_quality.py)；[tests/test_bash_readonly_argument_quality.py](../tests/test_bash_readonly_argument_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`authorization`。

## 邮件（`mail`）

### `mail_stable_pagination` — 私有邮件稳定分页标识

分组 `mail` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `private_snapshot`。

之前症状：338字符分页签名复制错一字符，第二页被拒。

原因：长token易错；不是授权应放宽。

优化思路与方法：46字符HMAC仍绑定过滤和库存，兼容同进程旧token；保持陈旧/授权校验。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R5：172.446s/15 calls，仅4/60，分页复制失败；2红例后31相关通过
- docs/quality_iteration_2026-10-05.md — R9：95.354s/12 calls，分页复制不再失败且读取三页；整个统计仍失败

验收标准：

- 完整合法分页可继续；错误或陈旧token仍拒绝
- 显式选择授权只读快照；不得推断正式mail-db

回归或再测入口：[tests/test_mail_list.py::test_mail_list_token_is_compact_and_binds_filters_and_inventory](../tests/test_mail_list.py)；[tests/test_mail_list.py::test_mail_list_rejects_tampered_listing_id](../tests/test_mail_list.py)；[tests/test_mail_list.py::test_mail_list_snapshot_rejects_insert_between_pages](../tests/test_mail_list.py)；[tests/test_mail_list.py::test_mail_list_accepts_existing_signed_payload_tokens](../tests/test_mail_list.py)；[tests/test_mail_list_integration.py::test_registered_mail_list_pages_without_exposing_bodies](../tests/test_mail_list_integration.py)。

`replay=null`：私有邮件没有在目录里绑定可执行快照路径；Root 必须先显式选择授权只读快照并提供 `--mail-db`，禁止从配置或正式数据库自动推断。可核实的既有选择为 `realworld/real_mail_overview`。

标签：`requires_explicit_snapshot`、`needsselection`、`bounded_pagination`。

### `mail_cached_full_group_count` — 缓存全量邮件精确分组计数

分组 `mail` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：翻页读取仍只正确3/30组；观测淘汰导致模型反复丢失累计证据。

原因：16k窗口无法稳定人工累加；列表结构预览目标字段不可见。

优化思路与方法：通用字段投影、字面定位、全数组确定性group，计数不截断，超扫描限制拒绝。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R9：真实3/30与合成2/30失败；R10：39相关通过，42.279s/8 calls/3 tools合成30/30、60/60
- docs/quality_iteration_2026-10-05.md — R10：早一轮逐行10/30是格式假阴性，人工展开30/30；R11：JSON framing边界先红后修复

验收标准：

- 30组/60封精确计数；报告完整扫描/分页界限
- 不能把单artifact计数当跨源全量；人工核对缩写格式，避免假阴性

回归或再测入口：[tests/test_observation_group.py](../tests/test_observation_group.py)；[tests/test_eval_realworld.py::test_total_only_or_partial_mail_answer_is_not_full_completion](../tests/test_eval_realworld.py)。

现有再测选择元数据：runner=`realworld`，case=`synthetic_mail_overview`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`synthetic_replay`、`exact_counting`。

### `mail_expert_identity_audit` — 邮件专家路由身份与实际工作流审计

分组 `mail` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `private_snapshot`。

之前症状：路由mode/type错误或fork省agent_id；实际专家已全量完成却父级卡住。

原因：内部路由与交付schema混淆；默认为general_agent；workflow审计缺真实invocation。

优化思路与方法：一次schema修复、显式专家ID、对齐started/completed与只读实际工具审计；稳定全量分页。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R12：180s超时，恢复62.199s仍错路由/20封；R13：合成30.709s/5 calls与真实25.942s/5 calls均30/30
- docs/quality_iteration_2026-10-05.md — R13：88定向通过；任务/config不同，非严格专家A/B

验收标准：

- 真实执行器为mail_expert；30/30数量可核对
- 实际审计支持只读；明确标题推断和正文，未知不得放行
- 私有重跑必须显式快照，禁止生产mail-db推断

回归或再测入口：[tests/test_mail_expert.py::test_router_repairs_wrong_shape_once_without_adopting_final_output_contract](../tests/test_mail_expert.py)；[tests/test_mail_expert.py::test_successful_workflow_records_generic_child_tool_audit_from_executed_tools](../tests/test_mail_expert.py)；[tests/test_mail_expert_tools.py::test_snapshot_reads_pages_under_one_listing_and_reports_complete](../tests/test_mail_expert_tools.py)；[tests/test_eval_realworld.py::test_workflow_expert_usage_is_included_in_child_metrics](../tests/test_eval_realworld.py)。

`replay=null`：私有邮件没有在目录里绑定可执行快照路径；Root 必须先显式选择授权只读快照并提供 `--mail-db`，禁止从配置或正式数据库自动推断。可核实的既有选择为 `realworld/real_mail_specialist_overview`，需 `--planning --mail-expert`。

标签：`requires_explicit_snapshot`、`needsselection`、`bounded_expert_batch`。

### `mail_low_priority_action_handoff` — 低优先级必需行动不应在专家handoff消失

分组 `mail` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：专家摘要仅保留高优先级，低/中优先级action_required被省略。

原因：优先级筛选与有界交付混同，没有披露缺项。

优化思路与方法：先保留必需行动；空间不足明确missing_requirements，不虚报全覆盖。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R14：2例先红后绿；R15：33.482s/6 calls例行行动正确，但实际为中优先级
- docs/quality_iteration_2026-10-05.md — R15明确低优先级分支仍仅offline

验收标准：

- 低优先级必需行动进入parent；纯通知不变待办
- 预算不能容纳时披露遗漏；中优先级live不冒称低分支live

回归或再测入口：[tests/test_mail_expert_delivery_quality.py](../tests/test_mail_expert_delivery_quality.py)。

现有再测选择元数据：runner=`realworld`，case=`heldout_mail_routine_actions`，args=`["--planning","--mail-expert"]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`offline_only`、`live_did_not_cover_low_branch`、`heldout`。

### `mail_body_updates_no_duplicate_reads` — 正文变更行动项及父级重复读取

分组 `mail` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：正文事项可正确归纳，但父级重复读取，输出约2200字且核验章节多余。

原因：已完成专家覆盖没有有效传达；内部审计混入用户交付。

优化思路与方法：结构化有界交付保留更新、行动与缺项，复用完成记录，减少父级重复。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R13：54.337s/9 calls，4/4但重复读取；R14：40.864s/7 calls，父重复2→0，819字
- docs/quality_iteration_2026-10-05.md — R14人工核对更新与负责人关系；仍有一句内部核验，非稳定提速

验收标准：

- 四封读取分析；最新截止/材料/工单及已知未知负责人正确
- 父级重复工具为0；不得把纯收据当待办

回归或再测入口：[tests/test_answer_delivery_quality.py](../tests/test_answer_delivery_quality.py)；[tests/test_mail_expert_delivery_quality.py](../tests/test_mail_expert_delivery_quality.py)。

现有再测选择元数据：runner=`realworld`，case=`mail_specialist_review`，args=`["--planning","--mail-expert"]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_body_review`。

## 网页（`web`）

### `web_html_readability` — HTML可读抽取噪声及隐藏标签

分组 `web` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `public`。

之前症状：导航、表单、脚本污染正文；链接大小写破坏，隐藏void标签吞后文。

原因：缺结构优先级和正确隐藏状态处理。

优化思路与方法：main/article优先，保留标题/来源/链接；简单页面退回可见body。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R7：合成反例和review两例先红后绿；页面/工具29通过

验收标准：

- 隐藏块/导航不污染，void标签不吞正文、链接保持
- 声明轻量抽取与动态页面局限，不冒称浏览器渲染

回归或再测入口：[tests/test_web_readability.py](../tests/test_web_readability.py)；[tests/test_web_tools.py](../tests/test_web_tools.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`no_full_task_replay`。

### `web_bounded_paging_find_recovery` — 网页长文分页、窗口去重与零匹配恢复

分组 `web` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `public`。

之前症状：20k上游截断无法续读；去重10个窗口缩短后事实0/10；single writer零匹配无正文。

原因：缓存complete被误当全文；去重窗口与交付不一致；字面匹配不处理表达差异。

优化思路与方法：offset/hash续读；完整去重窗口分页；同fetch零匹配附有界原文恢复且明确非语义命中。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R16：10事实0/10反例→6+4窗口10/10，24缓存回归；分页65通过
- docs/quality_iteration_2026-10-05.md — R21-W：106针对性通过，独立41；R22-T/R25-V成功核对两页但未用find，不能证明零命中恢复live

验收标准：

- 分页hash变化拒绝拼接；窗口事实完整且next_offset准确
- 零匹配snippet≤1200并标非verified/非semantic；不以find未命中断言不存在

回归或再测入口：[tests/test_web_page_paging_quality.py](../tests/test_web_page_paging_quality.py)；[tests/test_observation_search.py](../tests/test_observation_search.py)；[tests/test_web_find_recovery_quality.py](../tests/test_web_find_recovery_quality.py)。

现有再测选择元数据：runner=`web`，case=`heldout_web_sqlite`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`offline_only`、`live_not_covering_zero_match_branch`、`heldout`。

### `web_python_multisource_conditions` — Python多来源核查默认构建与支持限定

分组 `web` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `public`。

之前症状：机械通过，但默认构建/支持级别/GIL原文取证不全，答案长。

原因：20k抽取与16k投影淘汰；局部片段/搜索摘要被用作完整依据。

优化思路与方法：分页/find与更大root综合工作集，保留交付区间；按真实两页正文评测而非固定search。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R15：138.335s/16 calls机械true，人工失败；R16：123.138s/17 calls仍失败
- docs/quality_iteration_2026-10-05.md — R17：57.536s/10 calls/3 find，默认构建仍间接依据；R19资料交付检查13通过，不能追认语义pass

验收标准：

- 两个独立官方页面原文支持版本、默认构建、支持级别及扩展条件
- 简短披露未覆盖范围；引用和机械关键词不能代替语义核对

回归或再测入口：[tests/test_eval_realworld.py::test_multisource_goals_require_delivered_pages_not_a_specific_search_action](../tests/test_eval_realworld.py)；[tests/test_eval_realworld.py::test_page_coverage_rejects_no_match_search_failure_and_fake_official_hosts](../tests/test_eval_realworld.py)；[tests/test_eval_runtime_web_quality.py](../tests/test_eval_runtime_web_quality.py)。

现有再测选择元数据：runner=`web`，case=`heldout_web_multisource`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`semantic_open`、`no_latency_ab`、`heldout`。

### `web_sqlite_condition_brevity` — SQLite互证后仍遗漏WAL增长条件

分组 `web` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `public`。

之前症状：早期只取一页；最终两页三事实有支持，但把长读无条件说成WAL持续增长且1368字。

原因：证据范围和条件校准未稳定遵从；长读约束checkpoint不自动产生写入。

优化思路与方法：多页正文交付检查、bounded receipts、通用条件校准；单独验收语义与简洁性。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R17：174.039s/14 calls，仅一页，任务失败；R22-T：60.998s/11 calls，两页支持但1120字和整页已读误称
- docs/quality_iteration_2026-10-05.md — R25-V：82.537s/12 calls/0 search；三partial receipts、upstream unknown；独立复核P2条件遗漏/1368字

验收标准：

- 两官方页支持单写者、读快照、checkpoint影响
- WAL持续增长须保留持续写入条件；简短且不声称模型全文读完

回归或再测入口：[tests/test_eval_runtime_web_quality.py](../tests/test_eval_runtime_web_quality.py)；[tests/test_answer_evidence_calibration_quality.py](../tests/test_answer_evidence_calibration_quality.py)。

现有再测选择元数据：runner=`web`，case=`heldout_web_sqlite`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`semantic_open`、`mechanical_not_semantic`、`heldout`。

### `web_release_unproved_causes` — 现场版本搜索的缓存与跳转归因

分组 `web` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `public`。

之前症状：官方版本日期/变更可支持，却未经验证把旧搜索片段归因缓存，声称latest跳转。

原因：当前事实核对与原因/网络行为推断混淆。

优化思路与方法：以官方正文和as-of为证；未知原因只标未知，redirect需直接证据。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R9：58.354s/13 calls/2 search，区分旧片段并恢复打开失败
- docs/quality_iteration_2026-10-05.md — R25-S：38.999s/10 calls/1 search/798字；独立复核缓存/redirect两个P2，核心事实正确仍不全通过

验收标准：

- 版本、日期、两变更关系由实际正文支持
- 缓存/redirect因果须取证否则删去；简短披露采样与partial范围

回归或再测入口：[tests/test_eval_runtime_web_quality.py](../tests/test_eval_runtime_web_quality.py)；[tests/test_answer_evidence_calibration_quality.py](../tests/test_answer_evidence_calibration_quality.py)。

现有再测选择元数据：runner=`web`，case=`web_search_release`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`semantic_open`。

## 本地知识与公开检索（`rag`）

### `rag_chunk_tail_conditions` — 知识块尾部条件静默截断与续读

分组 `rag` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：load_chunks请求1800实际被trim到1200且无truncated，尾部例外无法进入缓存。

原因：错误复用search snippet上限；脱敏扩张后即使6000也未必覆盖。

优化思路与方法：加载真实有界原文，实际长度/total/truncated一致；Unicode offset续读且每次验权与隐私。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R18：3红例后65通过，续读72、独立8及撤销边界通过
- docs/quality_iteration_2026-10-05.md — R18：15.394s/6 calls/3 tools，实际6000请求取得1465字符含1200之后例外，人工条件核对正确

验收标准：

- 保留常规两人、紧急一人、18小时复盘全部条件
- 请求与返回长度一致、扩张可续读；撤销权限后拒绝

回归或再测入口：[tests/test_knowledge_chunk_coverage_quality.py](../tests/test_knowledge_chunk_coverage_quality.py)；[tests/test_knowledge_tools.py::test_knowledge_tools_search_load_and_validate_output](../tests/test_knowledge_tools.py)。

现有再测选择元数据：runner=`realworld`，case=`heldout_knowledge_long_policy`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_tail_exception`、`heldout`。

### `public_retrieval_all_hop_regression` — 公开多跳检索融合覆盖负面结果

分组 `rag` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `public`。

之前症状：固定同题hybrid弱于keyword；高首跳或重排不能补回已丢证据跳，CPU延迟高。

原因：语言/embedding profile、融合/候选截断均可能影响；现有档案缺候选池/cosine，未确定算法bug。

优化思路与方法：固定seed与候选证据分析；独立报告检索/QA；ONNX限线程局部减竞争，不能宣称准确率修复。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R18：Hotpot25题keyword/hybrid/rerank recall .94/.70/.94，all-hop .88/.52/.88；2Wiki .68/.42/.66、.40/.08/.36
- docs/quality_iteration_2026-10-05.md — R18：rerank p95 4294.76/3365.65ms；R20-C固定输入线程默认→4，1376.75→813.85ms、分数/向量差0，非50题复测

验收标准：

- 同题明确profile测support recall@10、all-hop和p95
- 候选池与每跳来源可追踪；不得把CPU优化等同检索准确率提升
- keyword离线入口不加载模型，不自动匹配生产profile

回归或再测入口：[tests/test_public_retrieval_benchmark.py](../tests/test_public_retrieval_benchmark.py)；[tests/test_onnx_thread_policy_quality.py](../tests/test_onnx_thread_policy_quality.py)。

再测需人工固定完整 profile：同题显式配置 embedding、reranker、query-prefix、seed及三种模式。
本目录不自动给出 keyword-only 作为混合检索失败的复现；单跑 keyword 不能验证融合缺跳已修复。

标签：`independent_benchmark_profile`、`manual_profile_required`、`needs_profile_selection`。

### `public_qa_relation_reasoning` — 公开配对QA缺跳、关系推理与弃答

分组 `rag` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `public`。

之前症状：直接RAG诊断不优于closed-book；有效引用/姓名字面命中不能证明关系正确。

原因：缺supporting跳、生成length、合法弃答及字面评分差异重叠；未证明loader截断导致答案错误。

优化思路与方法：原scorer与gold不放宽；分类缺跳/推理/格式/评分误差；完整Agent两题仅局部证据。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R18：10题closed-book EM/F1 .30/.33333，4 length；retrieval .10/.25139，5 abstain
- docs/quality_iteration_2026-10-05.md — R18离线100请求块均加载，历史截断3块/2题；R21-R两Agent题关系有据但strict EM仍0，非全数据集证明

验收标准：

- 固定题同profile比较EM/F1并人工核对关系与弃答
- 明确direct QA与完整Agent区别；不能用两题成功替代数据集提升

回归或再测入口：[tests/test_public_qa_quality.py](../tests/test_public_qa_quality.py)；[tests/test_runtime_public_rag_quality.py](../tests/test_runtime_public_rag_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`needsselection`、`direct_qa_not_approved_runner`、`semantic_open`。

### `benchmark_query_prefix_profile` — 公开检索query prefix显式接线

分组 `rag` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `public`。

之前症状：旧benchmark遗漏生产query_prefix，历史结果被误当生产profile。

原因：独立adapter评测未透传prefix；runtime配置与benchmark未等价。

优化思路与方法：显式--query-prefix并记录实际adapter profile；保留空默认，不读生产配置，不给passage加prefix或重复query transform。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — 收尾（接R18检索）：新旧17 passed/38.76s，b3dedeb；未重测50题
- docs/quality_iteration_2026-10-05.md — docs/linux_quality_final_report_2026-10-05.md：旧表属于独立benchmark profile，不能等同生产

验收标准：

- 实际query仅一次literal prefix，passage不变
- profile记录实际实例化状态；同题重测前不推断指标变化
- keyword replay仅入口检查；生产prefix/model/dataset必须另行显式选择

回归或再测入口：[tests/test_public_retrieval_query_prefix_quality.py](../tests/test_public_retrieval_query_prefix_quality.py)；[tests/test_public_retrieval_benchmark.py](../tests/test_public_retrieval_benchmark.py)。

再测需人工选择有缓存的 embedding 与实际 query-prefix。先运行两份明确回归；
不自动生成 keyword-only 作为 prefix 效果验证，缺少完整 profile 时标需人工选择。

标签：`offline_only`、`needs_profile_selection`、`keyword_does_not_validate_prefix_effect`。

### `requested_json_answer_format` — 请求短JSON被中文prose覆盖

分组 `rag` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `public`。

之前症状：完整Agent有关系证据却违背only short JSON，输出长中文prose。

原因：普通answer/context_answer无条件中文且禁JSON，覆盖用户交付要求。

优化思路与方法：去通用冲突，尊重请求语言/格式；显式Task schema优先，默认中文/prose保留。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R21初题：22.961s/6 calls/1447 docs，格式失败；4请求红例后32针对性通过
- docs/quality_iteration_2026-10-05.md — R21-R：原题20.831s/6 calls JSON有效，strict EM0/F1 .0769；comparison16.886s/5 calls JSON有效，EM0/F1 .5333；从零总准备118.419/112.727s

验收标准：

- 原题和comparison留出均有效JSON；关系及引用有据
- strict EM/F1原样报告，不从prose或解释answer截取重写金标
- prompt-level格式服从不等同任意schema强制验证

回归或再测入口：[tests/test_requested_answer_format_quality.py](../tests/test_requested_answer_format_quality.py)；[tests/test_runtime_public_rag_quality.py](../tests/test_runtime_public_rag_quality.py)。

现有再测选择元数据：runner=`public_rag`，case=`null`（runner 不提供 case 选择），args=`["--dataset","2wikimultihopqa_200","--query-id","b7f10d940bda11eba7f7acde48001122"]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_json_format`、`known_query_id`、`not_dataset_accuracy`、`heldout`。

### `knowledge_full_authority_empty_grant` — 知识full authority空显式grant误拒绝

分组 `rag` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：server full_data_authority=true且source IDs空，目录/执行认可但knowledge guard拒绝。

原因：工具helper总与空IDs相交，忽略full flag。

优化思路与方法：仅full且空grant使用当前workspace/session已授权可见集合；非空仍相交，restricted空仍拒。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R26-K：22新例8红14绿→22绿，相关114/Root46/独立22通过
- docs/quality_iteration_2026-10-05.md — R26-K：131.354s/26 dispatch；search/list_sources completed空集合，现场触发修复分支；整体审计仍FAILED

验收标准：

- full空授权在空库正常empty而非permission拒绝
- 显式IDs与requested source均不可越界；不新增project_id授权或scope

回归或再测入口：[tests/test_knowledge_full_authority_quality.py](../tests/test_knowledge_full_authority_quality.py)。

现有再测选择元数据：runner=`parallel`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_authority_fix`、`overall_parallel_still_failed`。

## 多 Agent（`multi_agent`）

### `child_failed_generation_partial_delivery` — 子生成失败占位成功及已有摘要固定截断

分组 `multi_agent` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：backup生成失败本地占位仍TaskResult completed；release3685字被父级480字符prefix截掉。

原因：正常run终态混同业务完成；交付未用实际剩余预算。

优化思路与方法：server-owned generation failure标记与恢复保持PARTIAL；5500预算内原样交付，超限公平首尾与原件引用。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R22-P：120.553s/32 dispatch任务失败，占位与480截断P2；R23-P：15新例9红后绿，Root84通过
- docs/quality_iteration_2026-10-05.md — R23-P旧raw离线5296字符三摘要原样；R23-P/S新live165.544s/31 dispatch独立复核无旧占位/480问题，但合同与校准仍失败

验收标准：

- 生成失败不得completed；真实LLM同句不按词误杀
- 预算能容纳则完整保留；超限明确区间/原件；verification不洗白

回归或再测入口：[tests/test_child_answer_failure_quality.py](../tests/test_child_answer_failure_quality.py)；[tests/test_child_partial_delivery_quality.py](../tests/test_child_partial_delivery_quality.py)；[tests/test_partial_delivery_quality.py](../tests/test_partial_delivery_quality.py)。

现有再测选择元数据：runner=`parallel`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_delivery`、`overall_parallel_still_failed`。

### `replan_atomic_attempt_recovery` — 重规划格式恢复与原子attempt发表

分组 `multi_agent` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：连续8次非法PlanPatch；retry/reduced_scope复用旧PARTIAL；history落盘但event未落盘。

原因：缺当前身份/字段反馈；计划、执行ID和journal非原子恢复。

优化思路与方法：明确plan/revision与一次schema恢复；CAS事务原子提交history/reservation/journal，成功后更新内存。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R10：150.346s复杂失败、8非法patch；R16：真实SQLite中断反例，整合147通过
- docs/quality_iteration_2026-10-05.md — R16：失败恢复交付/规划40通过；独立审查闭合原子路径；未证明整体并行速度

验收标准：

- 每patch仅一次新attempt；重启幂等、取消不派发替代child
- SQLite中断原子回滚；无效patch无修改；旧孤立history不自动重写

回归或再测入口：[tests/test_plan_patch_prompt_quality.py](../tests/test_plan_patch_prompt_quality.py)；[tests/test_partial_replan_attempt_quality.py](../tests/test_partial_replan_attempt_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`no_dedicated_crash_replay`。

### `replacement_contract_degradation_gate` — 替代执行不得冒充原合同覆盖

分组 `multi_agent` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：startup替代节点只核查发布资料，却被当完成原startup义务。

原因：execution replacement与literal output/verification合同改变未分离。

优化思路与方法：合同/verification改变必须显式degradation_note；恢复检查旧journal before snapshot，保留原partial/inconclusive。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R23-P/S：165.544s六事实有据但startup合同未覆盖；R24-R：5红2绿→15绿，相关144/独立60
- docs/quality_iteration_2026-10-05.md — R25-P：116.249s/32 dispatch，实际无note变更被拒；独立未见成功explicit-degradation分支

验收标准：

- 无note的合同变更拒绝且不改状态
- 有note仅标明确降级，不代表履行原合同；成功降级live分支仍待验

回归或再测入口：[tests/test_replan_contract_coverage_quality.py](../tests/test_replan_contract_coverage_quality.py)。

现有再测选择元数据：runner=`parallel`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_rejection_gate`、`successful_degradation_not_live_verified`。

### `parallel_three_independent_contracts` — 复杂多Agent三独立审计整体失败

分组 `multi_agent` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `synthetic`。

之前症状：多轮六facts机械通过却三原child均partial，未独立核查发布/备份/启动并综合上线结论。

原因：32k含重复输入、controller fork/发现开销、回答预留；父级补读或单child读全资料不能替代三个合同。

优化思路与方法：compact JSON、scope目录、知识guard、控制恢复及有界partial交付已局部修复；整体保持FAILED。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R17：planning168.049s/28 calls/5/6，单Agent25.891s/6 calls/6/6但不满足多Agent要求
- docs/quality_iteration_2026-10-05.md — R26-K：131.354s/26 dispatch（1 unknown）/保守.171972032，parent FAILED/三child partial；六facts主要来自startup，三独立合同不满足
- docs/quality_iteration_2026-10-05.md — 私有独立核对：data/quality_runs/linux_20261005/parallel-quality-20261005T032549765274-4f5d2d98/parallel_audit_20261005T032549766095/independent_review.json

验收标准：

- 三个独立合同各取证且核对来源和未确认项，再综合结论
- 六字符串、子run completed、替代child或父补做均不能代替原合同
- 保持32768 child预算、只读与共享计费；未知usage保守预留

回归或再测入口：[tests/test_runtime_parallel_quality.py](../tests/test_runtime_parallel_quality.py)；[tests/test_multi_agent_eval_metrics.py](../tests/test_multi_agent_eval_metrics.py)。

现有再测选择元数据：runner=`parallel`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`semantic_open`、`mechanical_not_semantic`、`original_contracts`。

### `degraded_history_partial_missing` — skip/degrade后历史partial和说明丢失

分组 `multi_agent` · tier `regression` · 现在状态 `open` · `exposure=seen` · 数据 `synthetic`。

之前症状：已有release partial在失败交付被称未取得结果，degradation note也未交付。

原因：失败路径只返回canonical aggregate，SKIPPED过滤后覆盖attempt历史；不是持久数据删除。

优化思路与方法：待修：分别保留当前选择与validated历史partial，交付历史身份/降级说明，保持failed及5500界限。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R27-D：3 RED/7 GREEN/0.35s，生产未修；默认tests没有该回归
- docs/quality_iteration_2026-10-05.md — 私有pending复现存在：data/quality_runs/linux_20261005/degraded_partial_delivery_pending.py；原tests/test_degraded_partial_delivery_quality.py已移出默认收集
- docs/quality_iteration_2026-10-05.md — R26-K私有独立review确认1 P2/0 P1，原release摘要883字符被过滤
- R27-D 匿名复现入口由Root原样移植：evals/reproductions/skip_degrade_delivery.py；显式pytest预期3 FAILED/7 PASSED，默认不收集，本次档案工作未执行复现

验收标准：

- 历史partial与note可见，明确不满足当前合同；不能叫从未执行
- foreign/duplicate身份拒绝，公平交付，current与业务状态不得升级

回归或再测入口：[evals/reproductions/skip_degrade_delivery.py](../evals/reproductions/skip_degrade_delivery.py)。显式 pytest 此文件预期 3 FAILED/7 PASSED；默认 pytest 不收集它。focus 离线计划可出现这一已知失败，生产未修，不能 xfail、改断言或按绿色计数。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`private_pending_reproduction`、`no_default_regression`、`no_full_task_replay`。

## 记忆（`memory`）

### `memory_extractive_grounding` — 记忆候选原文支撑、否定条件与scope

分组 `memory` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：force-model原8候选全被过滤；截掉否定/条件/临时限定的候选可能错误通过。

原因：抽取让模型泛化claim；验证未回看原始子句或diagnostic与production不一致。

优化思路与方法：连续原文claim/evidence，统一原句校验保留否定/条件/时间/scope；坏类型跳过，合法候选仍处理。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R14：真实正例4 grounded候选，两负例abstain、第三length；严格文本recall0不能改分
- docs/quality_iteration_2026-10-05.md — R15：54对抗先红后绿、相关139通过；是词法guard，不是全语言entailment或新模型准确率

验收标准：

- 否定/纠正/临时条件不能经短evidence绕过
- 负例完整abstain与生成length分开；严格label recall和支撑率分别报告

回归或再测入口：[tests/test_memory_extractive_quality.py](../tests/test_memory_extractive_quality.py)；[tests/test_memory_candidate_grounding_quality.py](../tests/test_memory_candidate_grounding_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`no_approved_force_model_runner`、`semantic_limits`。

### `memory_withdrawal_late_worker_fence` — 撤回后晚到worker不得复活旧偏好

分组 `memory` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：真实Runtime链路用户撤回后旧队列偏好再次发布；宽泛喜欢匹配误撤无关偏好。

原因：缺精确user-message撤回水位与publication事务fence；撤回匹配太宽。

优化思路与方法：验证persisted user ID/role/content，发布事务验source/lease/fence；显著词重合与项目隔离，无ID不猜来源。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R16实际排队晚到复活；R17 source-ID/撤回/无关偏好红例修复
- docs/quality_iteration_2026-10-05.md — R17第三唯一运行37.000s/15 calls，上限20，六轮/10 checks/6 job通过，末轮两memory.search为空且无混杂写；本地确定性抽取+测试屏障，非自由抽取/自然竞态

验收标准：

- 六轮完整；撤回条目retracted，晚到来源不进入provenance，新会话不召回
- 无关运动偏好/项目B保留，未来新真实偏好可学习；前台不得混杂指导文件写

回归或再测入口：[tests/test_memory_runtime_quality.py::test_retraction_before_worker_does_not_publish_obsolete_queued_preference](../tests/test_memory_runtime_quality.py)；[tests/test_memory_runtime_quality.py::test_inflight_project_extraction_cannot_publish_after_cross_session_withdrawal](../tests/test_memory_runtime_quality.py)；[tests/test_memory_runtime_quality.py::test_specific_withdrawal_preserves_unrelated_published_and_queued_preferences](../tests/test_memory_runtime_quality.py)；[tests/test_eval_runtime_memory_quality.py::test_six_turns_actual_worker_sources_and_late_publish_barrier](../tests/test_eval_runtime_memory_quality.py)。

现有再测选择元数据：runner=`memory`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_withdrawal`、`scripted_race_barrier`。

### `memory_terminal_period_alias` — 句号异形候选两独立来源晋升

分组 `memory` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：两次同偏好仅末尾中文句号不同，两个候选各一source，active=0。

原因：raw hash身份不兼容；publication_lease传入错误extra可被忽略；报告hash差误作未晋升原因。

优化思路与方法：仅唯一同scope/type/remote、两边literal evidence相同的A/A。兼容；保留ID、audit与tombstone/lease，诊断以observed active优先。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R23-B第一45.833s/19 calls，active0；两顺序2红→28新绿，Root104通过
- docs/quality_iteration_2026-10-05.md — R23-B新实际42.191s/19 calls，原ID version2 active、2不同user refs，promotion audit alias；报告诊断后6 GREEN/5.01s

验收标准：

- 两个真实persisted user来源/checksum晋升同memory ID
- 内部标点/否定/数字不归一，ambiguous不搬来源，同源不晋升；撤回优先
- active诊断不得因raw hash不同误报未晋升

回归或再测入口：[tests/test_memory_publication_alias_quality.py](../tests/test_memory_publication_alias_quality.py)；[tests/test_background_identity_diagnostics_quality.py](../tests/test_background_identity_diagnostics_quality.py)。

现有再测选择元数据：runner=`background`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_identity_alias`。

## 后台（`background`）

### `background_async_compaction_outbox` — 硬溢出后台outbox与前台继续运行

分组 `background` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：提交答案同步语义压缩阻塞；入队异常吞掉后无下一轮无法恢复。

原因：硬阈值路径前台调用模型；缺持久意图和一致summary/raw-tail快照。

优化思路与方法：前台有界emergency view保留原文；pending-watermark outbox/savepoint，后台CAS/lease发表，同事务快照。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R6：人工摘要器150ms，record_context_exchange208ms；R12故障/outbox59通过
- docs/quality_iteration_2026-10-05.md — R23-B新实际42.191s/19 calls；compact7.032s期间4前台请求和read_file完成，前台5.989s，heartbeat max15.187ms，水位0→2/rev0→1；512 fixture

验收标准：

- 前台不等待语义压缩；入队/满队/重启可恢复持久水位
- 丢失存储能力明确未入队；emergency不声称语义完成
- 现场只认证短窗口自然并发，不认证生产65536长期SLA

回归或再测入口：[tests/test_async_hard_compaction_quality.py](../tests/test_async_hard_compaction_quality.py)；[tests/test_compaction_outbox_recovery_quality.py](../tests/test_compaction_outbox_recovery_quality.py)；[tests/test_foreground_background_quality.py](../tests/test_foreground_background_quality.py)。

现有再测选择元数据：runner=`background`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_concurrency`、`crash_paths_offline`、`not_long_window_sla`。

### `summary_prefix_provenance_inheritance` — 摘要跨prefix来源误拒导致local fallback

分组 `background` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：同会话追问事实可用但第二publication local_fallback，连续model摘要原验收失败。

原因：validator只准当前chunk trace2，合法已发表summary的trace1被误拒。

优化思路与方法：server来源继承须同session、seq≤covered与当前watermark原始membership；64 IDs/4096 bytes，截断/legacy incomplete明示，CAS/lease不变。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R25-B：37.051s/7 calls，水位4→6，发表1173字符summary实际prompt完整，前台4.975/5.005s接受更正；model→fallback整体false
- docs/quality_iteration_2026-10-05.md — R27-B收尾73 passed/22.68s，1d6acb8；未再live，原失败保留；宽后台76 passed/1 failed为用户workload15/11列失配
- docs/quality_iteration_2026-10-05.md — 私有独立核对：data/quality_runs/linux_20261005/runtime_background_followup_20261005T033238312609/independent_review.json

验收标准：

- 合法旧prefix与新chunk来源可继承；foreign/未covered/正文自报不能授权
- 同medium probe须连续model publication且后续summary完整交付、接受更正
- 4096 fixture不认证生产65536或长期取消SLA；membership不等于语义entailment

回归或再测入口：[tests/test_background_summary_provenance_quality.py](../tests/test_background_summary_provenance_quality.py)；[tests/test_eval_runtime_background_followup_quality.py::test_real_prefix_publications_then_same_session_recall_and_tail_correction](../tests/test_eval_runtime_background_followup_quality.py)；[tests/test_eval_runtime_background_followup_quality.py::test_local_fallback_is_not_model_publication_pass](../tests/test_eval_runtime_background_followup_quality.py)。

现有再测选择元数据：runner=`background_followup`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`offline_only`、`continuous_publication_not_retested`。

## 上下文与工具（`context_tools`）

### `incomplete_control_not_completion` — 原生控制与SSE不完整响应不得授权或完成

分组 `context_tools` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：native无结构tool_calls提前结束；SSE有delta但EOF无终态被算完整。

原因：把非空文本/完整参数等同完整控制响应，未一致检查partial/status/finish_reason。

优化思路与方法：必须完整显式控制动作；native退回有界JSON；SSE及后台complete-response gate保留不完整状态。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R8：native三题失败，三红例修复；R14：native3红例后相关57通过
- docs/quality_iteration_2026-10-05.md — R14：SSE相关130通过、边界11通过；后台不完整gate相关43通过；未证明中转稳定性

验收标准：

- partial/length/incomplete不得执行工具或发布记忆/摘要
- EOF无结束标识不得completed；恢复仍受预算与取消限制

回归或再测入口：[tests/test_llm_boundary_quality.py::test_nonempty_sse_eof_without_completion_marker_recovers_once](../tests/test_llm_boundary_quality.py)；[tests/test_llm_boundary_quality.py::test_background_publication_rejects_incomplete_status_even_with_valid_text](../tests/test_llm_boundary_quality.py)；[tests/test_control_generation_quality.py::test_incomplete_native_call_cannot_authorize_execution](../tests/test_control_generation_quality.py)；[tests/test_control_generation_quality.py::test_length_terminated_control_object_is_not_executed_as_complete](../tests/test_control_generation_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`no_full_task_replay`。

### `catalog_routing_efficiency` — 目录精简后仍有错误路由和延迟

分组 `context_tools` · tier `challenge` · 现在状态 `open` · `exposure=seen` · 数据 `synthetic`。

之前症状：README误选事项包、额外查空事项；减少目录内容未减少总耗时。

原因：重复控制信息只是一部分；路由、恢复回合、推理长度与上游负载共同影响。

优化思路与方法：阶段式目录与schema投影、compact JSON减少传输冗余；继续按任务正确性和实际调用测量。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R1：README26.870s/7 calls/52146输入；R4：31.865s/9 calls仍误路由
- docs/quality_iteration_2026-10-05.md — R14：Bash spec字符7659→5536；R22-P：历史总输入估计节省14.15%，非现场成功

验收标准：

- 目标正确完成且无无关域查询；报告实际token/calls/耗时
- 同配置重复或配对测量后才宣称稳定提速

回归或再测入口：[tests/test_lazy_catalog_quality.py](../tests/test_lazy_catalog_quality.py)；[tests/test_tool_prompt_projection_quality.py](../tests/test_tool_prompt_projection_quality.py)；[tests/test_compact_prompt_quality.py](../tests/test_compact_prompt_quality.py)。

现有再测选择元数据：runner=`realworld`，case=`known_fact`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`no_latency_ab`、`routing_open`。

### `cached_text_delivery_receipts` — 最终provider缓存文本实际交付区间

分组 `context_tools` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `public`。

之前症状：工具fetched/scan complete被当模型读全文；两层裁剪会丢引用或错计覆盖。

原因：cache完整性、模型最终visible与upstream完整性混淆；工具自报不能作authority。

优化思路与方法：server bindings核对run/hash/pointer/ToolView，最终fit后区间合并；裁剪unknown/partial，取消重检。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R16：六来源新投影保留六个，两层引用合并红例后绿；R23-V：20新例/Root8 provider边界、最终独立33/Root54通过
- docs/quality_iteration_2026-10-05.md — R25-V：实际3 partial receipts 2540/14507、2000/20000、500/16649，upstream unknown；独立可见7222字符≠receipt5040≠全文

验收标准：

- 实际answer provider收到对应当前缓存的有界区间；伪造/foreign无receipt
- 省略引用合并不认证未读文本；upstream始终独立unknown
- 仅元数据投递验收，模型语义遵从另列web案例

回归或再测入口：[tests/test_context_delivery_quality.py](../tests/test_context_delivery_quality.py)；[tests/test_answer_delivery_boundary_quality.py](../tests/test_answer_delivery_boundary_quality.py)；[tests/test_prompt_omission_merge_quality.py](../tests/test_prompt_omission_merge_quality.py)。

现有再测选择元数据：runner=`web`，case=`heldout_web_sqlite`，args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_metadata_delivery`、`not_semantic_compliance`、`heldout`。

### `tool_discovery_read_truth_table` — 条件只读发现与严格动态分类

分组 `context_tools` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：READ替代child空catalog；畸形callback字符串false/dict/1被bool转真；configured快路径ToolSpec导致AttributeError。

原因：无参分类混同实际invocation；类型强转和错误注册对象破坏发现/授权一致性。

优化思路与方法：metadata声明条件只读仅供发现；实际注册Tool动态分类仅接受bool；共享scope predicate隐藏不可能grant，参数逐次校验。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R17：空catalog零工具；R18实际READ目录恢复但任务仍失败
- docs/quality_iteration_2026-10-05.md — R20-T：15红例后Root74 passed/4.46s；fast-path12 failed/3 passed→15绿，Root60通过
- docs/quality_iteration_2026-10-05.md — R23-S：矩阵10 RED/13 GREEN→35绿，集成49及4096等价探针；R23-P/S受限补跑有效，授权矩阵仍只offline认证

验收标准：

- 可读动态工具能发现；实际写参、畸形类型、过期scope零执行
- configured带ToolView使用注册Tool；缺注册不授权；目录不扩大grant

回归或再测入口：[tests/test_conditional_readonly_discovery_quality.py](../tests/test_conditional_readonly_discovery_quality.py)；[tests/test_dynamic_readonly_type_quality.py](../tests/test_dynamic_readonly_type_quality.py)；[tests/test_fast_path_readonly_quality.py](../tests/test_fast_path_readonly_quality.py)；[tests/test_tool_scope_discovery_quality.py](../tests/test_tool_scope_discovery_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`authorization_matrix`。

### `preexecution_rejection_audit` — 执行前拒绝与持久化bool副作用审计

分组 `context_tools` · tier `regression` · 现在状态 `fixed_offline` · `exposure=seen` · 数据 `synthetic`。

之前症状：未invoke的拒绝被当可能已写；持久0或字符串false恢复成合法False。

原因：缺实际invoke边界；Pydantic非严格bool可洗白畸形审计。

优化思路与方法：实际invoke marker与rejected/child audit一致才排除写；strict bool/null恢复，缺旧标记保守。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R18：实际拒绝误审计，相关60通过；R19独立P2六SQLite反例红→修复54通过

验收标准：

- 进入后先写失败、工具伪造False、双方不一致仍unknown
- 非法持久类型拒绝恢复，不重执行、不发completed

回归或再测入口：[tests/test_preexecution_tool_audit_quality.py](../tests/test_preexecution_tool_audit_quality.py)；[tests/test_dynamic_readonly_type_quality.py](../tests/test_dynamic_readonly_type_quality.py)。

`replay=null`：没有为本项故障绑定现有全任务 replay；默认回归（若有）只验证相应边界，不能虚构可执行案例。

标签：`offline_only`、`authorization`。

## 关注（`watches`）

### `watch_grounded_partial_identity` — 关注有据partial交付与跨来源同事项比较

分组 `watches` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：授权长结果无缓存helper；budget partial无简报；扩写claim或240字符预览使变化/不变错误。

原因：源工具与helper grant接线缺口；文本excerpt被泛化，比较截短值，subject identity不稳定。

优化思路与方法：注册只读源后开放同run缓存；仅child_budget_finish partial可交付有据项；完整本地比较、严格原子摘录、raw subject_key跨源稳定。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R16：缓存接线相关38通过；R18：75.261s/18 calls失败，第3轮0 changes/0 unchanged/5 unconfirmed
- docs/quality_iteration_2026-10-05.md — R20-W：106通过，source展示追加75；新实际85.549s/18 calls，南楼→不变→北楼，后两轮partial但地点有据，机械十绿另人工核对

验收标准：

- 新/不变/地点变化三轮同identity，未知负责人不猜、每轮独立session
- partial明确覆盖缺口；符号/小数/版本不能清理成错误claim；foreign URL不冒来源
- 只认证已核验partial项，不宣称全部事项覆盖或连续三天真实邮箱

回归或再测入口：[tests/test_watch_cache_quality.py](../tests/test_watch_cache_quality.py)；[tests/test_watch_prior_comparison_quality.py](../tests/test_watch_prior_comparison_quality.py)；[tests/test_watch_partial_identity_quality.py](../tests/test_watch_partial_identity_quality.py)。

现有再测选择元数据：runner=`watch`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_partial_items`、`synthetic_compressed_dates`。

### `watch_current_state_warning` — 关注当前值与旧值矛盾及覆盖告警隐藏

分组 `watches` · tier `regression` · 现在状态 `fixed_live` · `exposure=seen` · 数据 `synthetic`。

之前症状：同subject同时北楼变化/南楼未变化，旧sections可回写南楼baseline；partial警告在第五项被摘要截掉。

原因：未先协调完整identity值/refs；预览/展示顺序被当当前事实，覆盖警告位置不稳定。

优化思路与方法：唯一新值+原baseline可协调时旧值只留previous；冲突unconfirmed不按时间猜；coverage警告固定摘要顶部。

后证据（历史记录，不是本次重跑）：

- docs/quality_iteration_2026-10-05.md — R21-D：63.700s/18 calls机械十绿但当前值矛盾；R22-W：13红4绿→18新绿，联合128/45.30s，旧raw离线重放三轮1/0、0/1、1/0
- docs/quality_iteration_2026-10-05.md — R22-W新实际69.223s/18 calls/三slot均PARTIAL，南楼→不变→北楼，同identity、previous及置顶警告可见；不算完整覆盖

验收标准：

- 同identity只有唯一可证当前值；旧值只作previous
- 新来源重申旧值或多候选冲突不自动选择；baseline不被旧sections覆盖
- partial警告不被4项/2200界限隐藏；整体仍partial

回归或再测入口：[tests/test_watch_current_state_quality.py](../tests/test_watch_current_state_quality.py)；[tests/test_eval_runtime_watch_quality.py](../tests/test_eval_runtime_watch_quality.py)。

现有再测选择元数据：runner=`watch`，case=`null`（runner 不提供 case 选择），args=`[]`。这是未来显式授权后的入口，本次不执行；fixture 的机械判据仍须另做语义审查。

标签：`bounded_current_state`、`overall_partial`。

## 评测使用与证据边界

选择、统计和离线重跑工具由 Root 实现；本档案不修改 runner、测试、业务代码或其他文档。
自动统计仅认 JSON，baseline 不进入“历史 bad 修复”分母，同一再测入口可覆盖多项但不能重复派发当独立证据。
本文逐项信息与 JSON 对齐，Root 审查可直接按 id 验收，不必重复阅读全部轮次。

`replay.case=null` 不表示缺失输入：parallel/background/background_followup/memory/watch/public_retrieval
本来没有 `--case`，public_rag 按显式 dataset/query-id 选择。
public_rag 绑定的 dataset 和 query-id 已在本地 normalized JSONL 中核实；comparison 留出
`77803f9c084b11ebbd56ac1f6bf848b6` 也确实存在，但未作为新独立逻辑 case 重复计数。
realworld 专家选择保留 `--planning --mail-expert`；需要完整检索的后续选择应显式带
`--full-retrieval`。缺少完整profile的检索/prefix两项不自动生成keyword-only替代复现，需人工选择。
JSON 的 args 不携带 `--remote` 或 `--root-go`；后续 plan 工具负责授权边界，本轮禁止添加并执行它们。

仍需明确保留的证据限制：

- R27-B provenance 与收尾 query prefix 只有 offline；原 model→fallback 与旧检索负面指标未被重测覆盖。
- R26-K 最后审计 parent FAILED、三个原合同 partial；六事实主要由一个 child 取证，独立合同未完成。
- R27-D 原私有 pending 为 3 RED/7 GREEN，生产未修；已匿名原样移植到 `evals/reproductions/skip_degrade_delivery.py`。本次显式 pytest 实际 **3 FAILED/7 PASSED（0.33s）**，默认不收集；focus 离线计划因此可以失败，不能 xfail 洗白。
- R25-V SQLite 条件遗漏/冗长、R25-S 未取证缓存与 redirect 归因仍 open；fetched、receipt 与实际 visible 覆盖各有边界。
- 同 run 控制超时恢复有 thinking=false 与 confirmed 直接事件；后续两请求 flags 未归档，复用只能凭源码/离线路径推断。
- 记忆撤回主要确定性抽取且晚到竞态使用测试屏障；remote alias 修复只覆盖严格句号异形，不认证自由语义抽取。
- Watch 三轮仅合成隔离邮件、压缩日期，最终均 partial；当前值与警告已 bounded live 修复，非全事项覆盖或连续三天邮箱证明。
- 512/4096 压缩 fixture 与短窗口重叠不能认证生产 65536 历史或长期负载/取消竞态 SLA。
- 宽后台 76 passed/1 failed 为用户 workload schema 15/旧 INSERT 11 列失配；无关并行改动不纳入历史质量修复，不宣称全量绿。

本次档案静态检查覆盖 JSON schema/枚举/稳定 id/字段类型、repo-relative 路径、pytest node
所属层级、真实case/query-id、授权参数缩写和计划去重；不会代替历史语义结果。

## 后续评测执行方式

1. 日常先选 `focus`，再按一个或几个能力组筛选；未知ID/空集必须报错，不返回“0测试通过”。
2. 修改工具、执行器、授权、持久化或提示预算时补相关 `regression`，不要因基础任务简单
   跳过安全边界。发布前加 `foundation`。全目录、focus与regression之间有交集，勿合计成覆盖率。
3. 计划工具只生成命令与所需授权旗标，不执行。一个场景覆盖多个缺陷时去重真实派发；
   私有邮件须手动指定已授权快照，生产profile须显式固定，不从本地配置猜测实验条件。
4. 真实复测分别记录机械检查、原合同覆盖、来源/条件/时间关系核验、前后台耗时与首token
   可观测性、所有父子调用/token/保守成本、失败分类及源码/输入hash。关键词和引用ID命中
   不能代替语义核验；未核验记 pending，不默认通过。
5. `fixed_offline` 转 `fixed_live` 须再有真实目标链路与人工/独立证据核验，不能改gold或
   放宽验收。语义反例保留旧报告；时效网页以抓取快照/as-of核对，不固定历史版本作答案。
6. 新留出题应使用未暴露的输入、seed、条件组合，与训练/调优样本隔离；本档案的10个
   historical_holdout仅检查迁移回归，不统计为blind能力提升。

## 本次工具验证（不是重跑模型质量）

新增选择/统计/边界测试及既有badcase验证 **49 passed（1.59s）**；独立审查三个P2先补6个RED再修复，
独立复核6 GREEN。R27-D显式复现仍3 FAILED/7 PASSED（0.33s），生产缺陷保持open。

既有scripted smoke混合运行曾22 passed/1 failed，单独运行也曾失败；其中可复核的一次
唯一失败指标为 **wall_time_ms=5143.804 > 5000**，功能指标均通过。随后同题隔离复核
wall=3975.203ms通过（pytest总耗时5.06s不是task wall），另一次direct=3930.071ms通过。
这是时延阈值样本波动，不能据它断言新selector降低或提高了性能，根因未归因负载/服务。
原5秒阈值未放宽；新报告单列latency-only失败但仍保留failed，不把它当事实错误或洗成pass。

最终包含既有scripted suite的组合 **52 passed / 1 failed（15.06s）**；该次smoke实际
wall=5069.815ms，报告已分类 `latency_only=[mail_basic_ntuso]`，失败继续保留。
新增代码/opt-in复现Ruff clean；case_loader与report四条HEAD已有TRY004/UP017不做无关清理，
其余本次切片检查及compile通过。未运行或声称全量suite全部通过。

本次没有付费模型或搜索调用，没有改业务代码或用户现有workload/跨平台改动。
