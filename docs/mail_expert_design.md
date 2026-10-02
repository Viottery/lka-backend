# 邮件专家 Agent：只读批处理工作流

## 启用和边界

在 `config/local.toml` 的 `[agent]` 下设置 `mail_expert_enabled = true`，重启后端。默认关闭；启用仅注册 `mail_expert@1` 执行器、`mail.snapshot` 和 `mail.batch_load`，并在开启多 Agent 调度时把专家加入服务端允许列表。它**不改变**顶层 route/fork 选择逻辑，也不新增邮件 HTTP 端点。Planner 仍须选择专家并授予 `mail` 包、相应只读工具、具体账户及来源 scope；缺少任一数据授权时调用被拒绝，不会把空结果误报为空邮箱。

专家执行五类只读请求：局部检索问答（`search`）、明确时区的半开时间窗元数据清单（`list`）、同时间窗批量分析（`review`）、按发件地址分组（`group_sender`）、与已有事项候选对齐（`align_matter`）。一次有界 LLM 调用把子任务 objective 转成结构化意图；无法安全确定时间边界、同步/发送/删除或写入事务会返回不支持。当前时间提示同时提供 UTC 和本地 `Asia/Shanghai` 时间，区间最长一年。简短检索走现有 `mail.search`，不做邮箱全量扫描。

## 固定批量流程

`list/review` 先经 `mail.snapshot` 读取元数据。工具内部以 20 封为一页，单次最多返回 500 封；专家记录总数和不透明 `listing_id`，按 `next_range.start_rank` 续读，直到总数覆盖或预算停止。后续读取须沿用同一 token；本地集合变化时工具拒绝旧 token，专家失败而不宣称清单完整。每页校验总数、数量、ID 唯一性和连续 rank。扫描顺序执行以维护同一快照合同；同步工具由后台线程运行，不阻塞事件循环。

`list` 在元数据完整时结束，公共摘要最多展示前 15 封，完整清单留本地 artifact。`review` 再按 100 个 ID 调 `mail.batch_load`；每封正文最多 1200 字符，输出包含 `body_truncated`。专家把正文缩成每封最多 650 字符的证据片段，每 20 封发一批给 LLM 做逐封摘要、优先级和行动标记。每批输出必须一一对应输入 ID，不接受额外、重复或缺失 ID；总体优先级统计由已验证的逐封结果确定性生成。邮件正文是非可信数据，提示中明确禁止把正文当指令。

所有工具调用仍通过 `ToolExecutor` 的 schema、ToolView、scope 和安全门；不调用领域服务绕过授权。每次完整 `ToolResult` 单独保存在本地 `tool_result` artifact，最终卡片、分析与覆盖账本保存在 `mail_expert_result` artifact；公开 `TaskResult` 只有有界摘要、缺口和 artifact ID，不包含批量正文。事件记录阶段、调用计数、耗时、模型/提供商及失败类型。只读子运行不在原位恢复，重试须由调度器创建新 attempt。

## 发件人和事项整理

`group_sender` 只用元数据，按规范化发件邮箱聚合；无效发件人分开保留，避免误合并。完整分组、消息 ID 和主题样本留在本地产物，公开摘要只显示前 15 组，无额外 LLM 分析调用。它是发件地址分组，不是联系人实体消歧。

`align_matter` 必须由 Planner 同时授予 `matter` 包和 `matter.list` 工具；专家不会自行扩大权限。它读取至多 100 个当前 scope 可见事项，先认已有 `mail_message` 来源链接，再按主题与事项文本的词项交集做候选，模糊候选由 LLM 在小批次内判定，输出事项 ID 必须属于对应消息的候选集。没有候选、模型放弃匹配或事项列表达到上限时，不能断言“全局没有事项”；列表达到上限明确标为部分覆盖。该流程只读，不创建、更新或链接事项；对齐结果是供主 Agent 核验的建议。

发邮件目前**不可用**：现有 `OutlookService` 只有同步/读取路径，`mail` 工具包没有发送工具。未来若开发，须新增单独的写能力、OAuth 发信权限、收件人与正文预览、明确确认和幂等/审计；不能把只读邮件专家的分类结果当作发送授权，也不能在本次整理流程里暗中发信。

## 覆盖与限制

完成只表示**本地**时间窗内已成功枚举/分析，不能证明 Outlook 等远端邮箱已经同步完整。覆盖账本分别记录 `local_total`、`enumerated`、`metadata_complete`、`body_loaded`、`body_untruncated`、`semantically_analyzed` 和 `remote_sync_complete=unknown`。达到工具或 LLM 预算、正文缺失/截断、模型分片输出无效时返回 `partial` 和缺口，不会把未处理邮件计为完成。默认子 Agent 的 8 次 LLM、16k token 和 10 次工具调用预算可能不足以逐封分析数百封邮件；元数据清单可续读，但对大批量 `review` 应扩大被授权的子任务预算或拆分时间窗，并以覆盖账本核实结果。搜索结果有候选数上限，`possible_more` 不是精确总数。

本阶段不自动同步邮箱、不发送/删除邮件、不创建事项。针对真实模型的**合成意图路由**对照见 [评测记录](mail_expert_laya_evaluation.md)；它不等于真实邮箱端到端质量基准。
