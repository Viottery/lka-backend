# 动态多 Agent Runtime 开发路线

> 本清单面向通用 Agent Harness，不绑定 RAG、邮件、代码或某一种业务流程。
> 目标是在固定 Runtime 骨架下，允许 Planner 动态生成任务 DAG，并在权限、预算、并发、取消和审计规则下 fork 子 Agent。
> Phase 4 的 ReAct Child Agent 是首个执行实现，不是所有子 Agent 的固定形态；后续专家可由 workflow、专用模块或外部 CLI 执行。

相关设计：

- [动态多 Agent Runtime 架构](multi_agent_runtime_architecture.md)
- [ContextDriver 设计](context_driver_design.md)
- [RAG 演化路线](rag_evolution_todolist.md)
- [MVP 执行队列](mvp_todolist.md)

## 0. 执行规则

- [ ] 一次只推进一个阶段中的一个小闭环。
- [ ] 每个条目开始前明确目标、修改文件、风险和验证方式。
- [ ] 每个条目完成后运行针对性测试，再更新本清单。
- [ ] 所有 Planner 输出必须先经过 schema、policy、budget 和 DAG 校验。
- [ ] 子 Agent 不能绕过 Tool Registry、Tool Executor 或 Safety Gate。
- [ ] 不在 `app/core/` 中硬编码 RAG、mail、filesystem 等具体领域策略。
- [ ] 每个 Child Run 必须可取消、可审计、可追踪到 ContextSnapshot。
- [ ] 不把 SSE 当作可靠状态存储；完整状态必须进入 run / event / artifact 存储。
- [ ] 任何持久化 schema、公开 API、跨 workspace 权限或远程出境变化先单独评审。

## 1. 完成定义

一个条目只有在以下条件同时满足时才能标记完成：

- [ ] 实现已落地。
- [ ] 相关协议、schema 和文档已同步。
- [ ] 有自动化测试或可复现验证路径。
- [ ] 失败、取消、超时和权限拒绝行为有明确记录。
- [ ] Git diff 只包含当前条目相关修改。

## 2. Phase 0：现状对齐与协议冻结

目标：在写运行时代码前，确认当前 Agent Loop、Run Manager、Tool Executor、SSE 和持久化边界。

依赖：无。

### 0.1 代码与文档盘点

- [ ] 梳理 `app/core/agent_turn.py` 的单 Agent 执行生命周期。
- [ ] 梳理 `app/core/agent_graph.py` 的 LangGraph 状态和 checkpoint 边界。
- [ ] 梳理 `app/core/agent_runs.py` 的 run、event、cancel 和 terminal status。
- [ ] 梳理 Tool Registry、Tool Executor、Safety Review 和 ToolContext。
- [ ] 梳理当前 SSE progress event 和前端消费协议。
- [ ] 确认 session context、cached observations 和 run log 的数据边界。

验收：

- [ ] 形成 parent run / child run 的映射说明。
- [ ] 明确哪些现有对象可复用，哪些需要扩展。
- [ ] 明确不修改的既有行为和兼容约束。

### 0.2 冻结通用对象协议

- [x] 定义 `Plan`。
- [x] 定义 `PlanStep`。
- [x] 定义 `DependencyEdge`。
- [x] 定义 `ChildRun`。
- [x] 定义 `ContextSnapshot`。
- [x] 定义 `TaskAssignment`。
- [x] 定义 `TaskResult`。
- [x] 定义 `Artifact`、`EvidenceRef` 和 `VerificationResult`。
- [x] 定义 `RunEvent` 的 parent / child / plan / step 字段。
- [x] 定义 object version、schema version 和 correlation id。

验收：

- [x] 所有对象有 JSON schema 或等价的 Pydantic schema。
- [x] schema 能表达成功、失败、取消、超时、部分完成和阻塞。
- [x] schema 不包含具体领域名称或具体工具调用特判。

## 3. Phase 1：Plan DAG 与结构化 fork

目标：让 Planner 能输出可校验的动态任务图，但暂时不并行执行。

依赖：Phase 0.2。

### 1.1 Plan 状态机

- [x] 实现 Plan 状态：`draft`、`validated`、`queued`、`running`、`replanning`、`completed`、`failed`、`cancelled`。
- [x] 实现 PlanStep 状态：`pending`、`blocked`、`ready`、`running`、`waiting`、`completed`、`failed`、`skipped`、`cancelled`。
- [x] 实现依赖合法性检查。
- [x] 拒绝不存在的依赖节点。
- [x] 拒绝循环依赖。
- [x] 拒绝重复 step id 和重复 edge（edge 为从 `depends_on` 派生的只读视图）。

### 1.2 `fork_subtasks` operation

- [x] 为 Planner 增加结构化 `fork_subtasks` operation。
- [x] 校验子任务 objective、output contract、depends_on 和 verification criteria。
- [x] 校验 `max_depth`、`max_children` 和单次 fork 数量。
- [x] 校验 allowed packages、tools、scope 和 side-effect level。
- [x] 拒绝 Planner 修改系统 policy、总预算和安全模式。
- [x] 记录 requested plan 与 validated plan 的差异。

验收：

- [x] Planner 不能通过自然语言触发 fork（只识别 schema 校验通过的 operation-first `fork_subtasks`）。
- [x] 非法 DAG 不会创建 Child Run（拒绝阶段只写拒绝事件）。
- [x] fork 请求、校验结果可写入 run event；validated DAG 同时保存在 run metadata，可从持久化 store 恢复。

## 4. Phase 2：Parent Run / Child Run 生命周期

目标：让一个 PlanStep 可以被独立运行、取消、失败和重试。

依赖：Phase 1。

### 2.1 Child Run 管理

- [x] 为 parent run 增加 `child_run_ids` 和 plan 引用。
- [x] 创建 Child Run 时绑定 `parent_run_id`、`plan_id`、`step_id` 和 `attempt`。
- [x] 记录 child run 的 started / completed / failed / cancelled / timed_out 状态。
- [x] 保留每次 attempt，不覆盖之前的失败运行。
- [x] 实现 parent cancel 到 child run 的取消传播。
- [x] 实现单个 child cancel，不误取消无关 sibling。

### 2.2 事件与持久化

取消 parent/child tree 以及 child timeout 的控制状态与对应事件现以单个 SQLite 事务提交；进程内缓存只在事务成功后更新。跨进程调度和已经进入的同步写工具仍不受该事务强制中断。

- [x] 增加 `subtask_created`、`subtask_queued`、`subtask_started`、`subtask_completed`、`subtask_failed` 和 `subtask_cancelled` 事件。
- [x] 所有 child event 带 parent run、child run、plan 和 step 引用。
- [x] 事件写入持久化 store，SSE 仅作为实时投影。
- [x] 支持按 parent run 查询完整 child tree。

验收：

- [x] 从 parent run 可以重建所有 child run 和 attempt。
- [x] 取消、失败和重试都有独立事件记录。

## 5. Phase 3：ContextDriver 与 Snapshot

目标：让每个 Child Agent 获得最小必要、权限受控、可审计的上下文。

依赖：Phase 0.2、Phase 2.1。

### 3.1 ContextDriver 确定性核心

- [x] 实现 `ContextRequest`。
- [x] 实现 PlanStep、ParentContext 和 DependencyResult 加载（由 ContextRequest 接收已授权、已加载的只读输入）。
- [x] 实现 parent scope、session scope、workspace scope 和 policy scope 的交集计算。
- [x] 实现 allowed packages / tools / paths 的裁剪。
- [x] 实现依赖未完成时的 `blocked_dependency`。
- [x] 实现基础 token / step / time budget。
- [x] 实现 Snapshot 创建和不可变版本。
- [x] 实现 Snapshot expiry 和 stale 标记。

### 3.2 上下文视图

- [x] 实现 `AgentView`。
- [x] 实现 `ToolView`。
- [x] 实现 `PlannerView`。
- [x] 实现 `AuditView`。
- [x] 区分 Reference View、Working View 和 Full View。
- [x] 保证完整 artifact 不被无条件复制到 prompt。
- [x] 保留 source refs、evidence ids 和 compression warnings。

### 3.3 Context 安全测试

- [x] 子 Agent 无法读取 parent scope 外的路径。
- [x] 子 Agent 无法访问未授权 source 或 account。
- [x] retrieved content 不会改变 tool permissions。
- [x] Snapshot 不会跨 session 复用。
- [x] policy / workspace / permission 变化会使 Snapshot 失效。

验收：

- [x] Tool Executor 能根据 ToolView 再次校验工具调用。
- [x] 每个 Child Run 可以追溯到唯一 ContextSnapshot。

## 6. Phase 4：接入现有 Agent Turn

目标：复用现有 ReAct / LangGraph Agent Loop 执行 Child Run，不建立新的领域 StateGraph。

依赖：Phase 2、Phase 3。

- [x] 将 Child Run 转换为现有 Agent Turn 的执行输入（`ChildAgentExecutor`）。
- [x] 将 ContextSnapshot 绑定到独立 child session，并把 ToolView 传入 ToolContext。
- [x] 复用现有 route、decision、validate、safety、execute、observe 和 answer 流程。
- [x] Child Agent 不获得修改 parent plan 的运行时接口。
- [x] 将 child agent 的最终回答转换为 `TaskResult`，而不是直接作为用户最终答案。
- [x] 在 `subtask_result` 事件中记录 tools、packages、evidence、budget 和工具/LLM outcome 分类。
- [x] 区分 provider error、tool rejection、safety denial 和 context failure 的记录类别。

验收：

- [x] Child Run 可以通过真实 Agent Turn 执行并返回结构化 TaskResult（集成测试覆盖 fork、工具审批、恢复及 parent answer）。
- [x] 现有单 Agent 请求行为不变（相关单 Agent 定向回归测试通过）。
- [x] Child Run 失败不会破坏 parent run 的持久化状态（失败、恢复和重试路径有定向覆盖）。

Phase 4 已接入真实 Agent Turn 与 Tool Executor；工具仍受不可变 ToolView、source/account/path 校验和 Safety Gate 约束。子任务的 `max_tool_calls`、`max_llm_calls`、`max_wall_time_seconds` 有运行时检查；`max_tokens` 使用审计 token 用量（缺失时回退到字符数）加 prompt 字符估算，并限制下一次请求的输出 token 上限，因此不是精确 tokenizer 级硬额度。取消不能强行中断已经进入的同步写工具；其可能完成后才被观察到。

## 7. Phase 5：串行 Scheduler

目标：先验证计划、依赖、上下文、执行和结果回写，再引入并行。

依赖：Phase 4。

- [x] 实现 pending、ready、blocked 和 completed 队列。
- [x] 只启动依赖已满足的 step。
- [x] Step 完成后更新下游依赖状态。
- [x] 支持 retry、skip/degrade、wait 和 fail。
- [x] 支持 parent run 的 terminal decision。
- [x] 支持 Aggregator 被触发前的结果收集。

验收：

- [x] A → B → C 的串行 DAG 能正确执行。
- [x] A 失败时 B 被标记 blocked，C 不会误启动。
- [x] A 重试时保留原始 attempt。

## 8. Phase 6：有界并行与资源冲突

目标：并行执行无依赖的只读任务，且不阻塞 FastAPI event loop。

依赖：Phase 5。

### 6.1 并发控制

有界 batch 中某个 step 失败后，Scheduler 继续执行不依赖它的 ready step；依赖失败的节点保持 blocked。已超时 child 的恢复重试会创建新 attempt，不复用终态 child。

- [x] 实现 global、session、provider、package 和 workspace 并发上限（scheduler / Tool Executor admission limits）。
- [x] 以有界 ready batch、semaphore 和 admission limits 限制同时执行量；不使用无界 `asyncio.gather` 启动 DAG。
- [x] 为 Child Run 增加 wall-time timeout、cancel 边界、tool-call / LLM-call 限制和基于使用量估算的 token 上限（非精确 tokenizer 计费）。
- [ ] 将所有同步 I/O、文件扫描、embedding 和 reranker 放入合适的线程池或 worker（目前只覆盖部分调用路径）。
- [ ] 对 LLM provider 429 和过载统一实现退避与排队。

### 6.2 资源锁

- [x] 通过 ToolSpec 声明资源锁 key / lock group。
- [x] 同一资源的写操作可由 Tool Executor 资源锁互斥；写工具仍经过 Safety Gate。
- [x] 记录发生争用的资源锁等待事件与耗时；记录开始、完成或取消及 elapsed ms，不记录原始 lock key。

验收：

- [x] 两个无依赖任务受并发上限控制并可并行执行。
- [x] 已声明相同资源锁的写任务不会同时进入工具执行。
- [x] parent cancel 会传播到排队和运行中的 child；已进入的同步写调用不可强制终止。

并发额度、资源锁和 AgentGraphRunner per-run execution lease 都是单进程内协调；多 worker / 多进程部署没有分布式锁保证。Scheduler lease 防止同一进程内同一 checkpoint 被并发 resume/run。

## 9. Phase 7：Aggregator 与 Verifier

目标：把多个 Child Result 转换为可验证的 parent working set。

依赖：Phase 5、Phase 6。

### 7.1 Aggregator

- [x] 合并 TaskResult、Artifact、EvidenceRef 和 warning。
- [x] 检测重复结果。
- [x] 检测互相冲突的 claim。
- [x] 区分 complete、partial、conflicting、blocked 和 failed。
- [x] 不把失败或未知包装成成功。
- [x] 输出 Planner 可消费的 missing requirements。

### 7.2 Verifier

- [x] 根据 verification criteria 生成结构化检查结果；自然语言 output-contract 需显式 verifier outcome，否则保持 inconclusive。
- [x] 检查 evidence / artifact 引用和副作用 / confirmation 状态；文件变更范围、测试 / lint / schema 目前只能作为外部提供的检查依据，尚未由内置 verifier 自动执行。
- [x] 验证失败时生成 recommended next actions。

验收：

- [x] 一个子任务失败不会丢失其他成功结果。
- [x] 冲突结果会阻止无依据的最终回答，或明确标记冲突。
- [x] Verifier 可以要求 Planner 创建补充任务。

## 10. Phase 8：Planner Feedback 与增量 Re-plan

目标：让 Planner 根据运行结果动态修正 DAG，而不是从头覆盖计划。

进度边界：失败、缺项和 `replan_required` 已结构化回传；过大的 fork 结果会在决策 prompt 中保留可执行的失败/验证摘要，完整结果仍在持久化记录。现有 scripted 测试验证反馈传递、补丁校验和执行恢复，不证明真实 LLM Planner 会选择正确补丁或发现语义遗漏。

小规模真实 LLM 验证：隔离临时 workspace、只读工具下，并行读取两个合成文件的 root → 两个 child → 聚合链路已跑通，两个 child 和 Plan 均为 `completed`。此前合法相对路径在 LangGraph Child ToolContext 中被误判越界，现已按首个配置 workspace root 解析；失败 Plan 的父 Run 不再标为 `completed`，`replan_required` 时直接 `final_answer` 会被拒并要求结构化 PlanPatch。模型仍多次提出无效 fork，实际端到端耗时约两分钟；真实 LLM 的失败后正确补丁选择尚未验证。

依赖：Phase 7。

- [x] 定义 `replan_required` observation。
- [x] 结构化记录主要失败类别及其重规划建议。
- [x] 区分 Runtime 自动重试和必须交给 Planner 的问题。
- [x] 支持 `retry_step`、`retry_with_reduced_scope`、`create_alternative_step`、`skip_and_degrade` 和 `abort`。
- [x] 支持 `ask_user` 后将 run 持久化为 `WAITING_USER`，经独立 `/runs/{run_id}/continue` answer journal 和 LangGraph checkpoint 恢复原 Plan；重启后重复 `command_id` 幂等。此路径不进入安全审批队列。
- [x] Planner 输出 `PlanPatch`，而不是覆盖原始 Plan。
- [x] 对支持的 PlanPatch 重新执行 schema、DAG、policy、budget 和 conflict validation。
- [x] 为重新规划创建新的 Child Run / ContextSnapshot。

验收：

- [x] 临时 provider 失败可以按有界策略自动重试。
- [x] 未授权失败不会通过 re-plan 绕过权限。
- [x] 关键子任务失败时，下游任务不会伪装为完成。
- [x] Plan history 可以显示原计划、失败原因和修订计划。
- [x] 根 parent 的 ASK_USER → 暂停 → 重启 → 重复 continue → Planner 继续已由定向 LangGraph 集成测试覆盖；嵌套 child 等待集的祖先暂停/恢复有 runtime 链路测试，但其 graph runner 在该测试中 stub，完整嵌套图 E2E 尚未覆盖。

## 11. Phase 9：前端外层协议

进度：独立前端现有原生 JS 聊天页已接入 parent snapshot 的计划/子任务状态投影、每会话持久化 event cursor，以及断线后 `GET /runs/{id}/stream?after_sequence=` 的有界续接；不会从重放事件追加重复回答。聊天页和桌宠 `pet.js` 的 Agent 审批入口都改为只对全局 FIFO 队首操作，409 后刷新，后台仍是顺序裁决的权威；聊天页另按 snapshot 状态展示 `waiting_user` 回答、父/子取消及失败的直接子任务重试。嵌套子任务取消使用其直接父 run ID；嵌套重试因缺少直接父计划状态而保守隐藏。尚无浏览器端自动化验收；专属轨迹展开和完整 UI 验收仍待完成。

目标：让现有前端展示 parent / child 状态，并通过后端 command 控制运行；不要求采用 React 框架。

依赖：Phase 2、Phase 5、Phase 6。

- [x] 提供 parent run snapshot、递归 child tree、plan / step 状态与 artifact / evidence 引用查询。
- [x] 提供带 parent event cursor 的 SSE run event stream。
- [x] 提供 parent-owned cancel / retry、manual safety review decision 和 checkpoint resume command。
- [ ] 前端不自行创建 Agent 或推断最终状态（需在独立前端仓库实现）。
- [ ] 在独立前端仓库 `/mnt/d/agent-bot-frontend` 对接 parent snapshot、child tree、plan/DAG、artifact/evidence 引用与 event cursor；前端仅投影后端持久化状态。
- [ ] 在该前端实现同一审批队列的逐项展示与决定，以及 waiting_user、取消、重试和断线重连；不同专家的专属轨迹可分层展开，不要求所有子 Agent 显示 ReAct 步骤。
- [x] 后端支持通过 snapshot + event cursor 恢复断线进度。
- [ ] UI 显示 queued、running、waiting、blocked、failed、cancelled 和 completed（UI 不在本后端仓库）。

验收：

- [ ] 可以在 UI 中看到 parent / child DAG 和实时状态（前端尚未实现于本仓库）。
- [x] 刷新后端查询可从持久化 snapshot / cursor 恢复运行状态。
- [x] 后端记录 cancel / review decision 并提供对应 run events。

## 12. Phase 10：简单任务快速路径

目标：避免所有请求都经过完整 Planner 和多 Agent 调度。

依赖：Phase 4、Phase 5。

- [x] 接入 server-configured `single_agent` policy telemetry（不跳过 Planner / ReAct LLM 步骤，不属于延迟优化）。
- [ ] 将其他 fast-path 模板接入请求运行时（当前仅 `single_agent` 配置路径开放；其他模板因缺少可信 selector / evidence contract 而保持禁用）。
- [ ] 增加 context-answer 与受限 retrieval fast path 的实际执行路径。
- [ ] 为常见模式启用可配置 template plan，例如 `inspect_then_verify`、`retrieve_then_answer`。
- [ ] 只有检测到多工具、多来源、多步骤、冲突、写操作或高风险时才启动完整 Planner。
- [x] 通过持久化 run events 记录 runtime fast-path 命中、完成、升级与错误完成；纯投影器可汇总指标。

验收：

- [ ] 简单查询不创建不必要的 Child Run。
- [ ] fast path 证据不足时能无损升级到完整 ReAct / Planner 流程。
- [ ] fast path 不能绕过 Tool Executor、Safety Gate 或 Egress Gateway。

当前 server 配置可选择 `single_agent` 策略 telemetry，但它不会跳过任何 ReAct/Planner LLM 步骤，不能视为已实现延迟优化。普通 ReAct/router、ToolExecutor、SafetyGate 和答案校验保持原样；显式 Planner fork 请求会先记录升级事件，再进入普通 scheduler。策略校验失败也会记录升级并原样继续。`context_answer`、受限 retrieval、`inspect_then_verify` 与 `retrieve_then_answer` 需要尚未提供的可信模板选择与证据充分性契约，因此保持禁用；不把缓存观察或模型分类当作可信信号。

## 13. Phase 11：递归 fork 与高级能力

目标：在基础 parent-child 生命周期稳定后，开放有限的动态层级。

依赖：Phase 8、Phase 9、Phase 10。

- [x] 实现 `root_planner`、`coordinator` 和 `leaf` 的服务端派生角色；leaf 禁止 fork，Planner/replan 不能自提权。
- [x] 为 coordinator 设置比 root 更小的 fork 深度、子项数和角色预算。
- [x] 验证 root → coordinator → leaf 局部 DAG 经真实 LangGraph / Scheduler / ChildAgentExecutor 执行并从手动审批 checkpoint 恢复；coordinator 局部 Plan 可产出最终结果。
- [x] coordinator 的 leaf 结果经真实 nested scheduler 聚合为结构化 TaskResult 并继续 root parent；同时覆盖 leaf fork 在服务端拒绝、无 grandchild 创建。
- [x] fork 纯校验拒绝深度超限、重复目标及无新增信息循环；真实 leaf ReAct fork 尝试另由上述集成测试覆盖。
- [ ] 跨层 cancel propagation 的递归运行时 E2E 验收。
- [x] Snapshot 记录 parent chain；完整跨层 trace 的 UI 消费仍待实现。
- [ ] 评估是否需要独立 worker process 或分布式 runtime。

Phase 11 已加入服务端角色派生及 coordinator/leaf 策略边界；root 首层委派为 coordinator，coordinator 只可创建 leaf。角色由快照与 plan lineage 恢复，alternative-step 沿用被替换步骤的角色/预算。root → coordinator → leaf 的单层嵌套执行、审批恢复和结果聚合已有真实运行时集成覆盖；更深 coordinator-to-coordinator 递归由角色策略禁止，跨层 cancel propagation 的运行时 E2E 仍待覆盖。Phase 10 仅接入 server-configured `single_agent`，其他模板与 Phase 11 的全量验收仍未完成。

暂不默认开放：

- [ ] 无限递归 fork。
- [ ] Agent 自由发现和创建任意新 Agent。
- [ ] 无 schema 的 Agent-to-Agent 自由聊天作为控制协议。
- [ ] 跨进程分布式 Agent 网络。

## 14. 评估指标

### 14.1 运行效率

- [ ] simple-task latency。
- [ ] planner latency。
- [ ] time-to-first-child-event。
- [ ] time-to-first-useful-result。
- [ ] parallel speedup。
- [ ] queue wait time。
- [ ] provider throttling rate。
- [ ] context construction latency。

### 14.2 计划质量

- [ ] dependency correctness。
- [ ] unnecessary fork rate。
- [ ] missed parallelism rate。
- [ ] invalid plan rate。
- [ ] replanning success rate。
- [ ] premature completion rate。
- [ ] partial completion correctness。

### 14.3 上下文与安全

- [ ] context scope violation rate。
- [ ] cross-session leakage test。
- [ ] stale snapshot usage rate。
- [ ] context budget overflow rate。
- [ ] unsupported claim rate。
- [ ] evidence traceability rate。
- [ ] safety gate bypass rate，目标为零。

## 15. 推荐最小闭环

第一条真正的开发闭环建议是通用只读分析任务，而不是固定 RAG 业务：

```text
用户请求
  ↓
Planner 生成两个无依赖只读 PlanStep
  ↓
Scheduler 创建两个 Child Run
  ↓
ContextDriver 生成两个隔离 Snapshot
  ↓
现有 Agent Turn 并行执行
  ↓
Aggregator 合并 TaskResult
  ↓
Verifier 检查 source refs 和完成条件
  ↓
Planner 直接回答或生成 PlanPatch
```

该闭环必须证明：

- [ ] 两个子任务真正并行且不阻塞 HTTP 请求。
- [ ] 两个子任务拥有不同的上下文和 scope。
- [ ] 一个失败不会丢失另一个结果。
- [ ] parent cancel 能传播到所有 child。
- [ ] 结果可以回溯到 Agent、tool、artifact 和 evidence。
- [ ] 计划可以经过一次增量 re-plan 后继续执行。

## 16. Phase 12：统一专家接入协议与执行器解耦

进度：已加入服务端专家注册表、`general_agent` ReAct 适配器、独立 `agent_id`/版本冻结和 Scheduler 分派；同一 Scheduler 的 mock workflow 定向执行已覆盖。注册表可并存多个版本，旧运行按冻结版本精确解析，新默认版本需显式提升。专家定义的完整输入/输出、scope、审批/取消/恢复合同与跨执行器统一轨迹仍未完成，本阶段不得标记完成。

目标：保留现有 Plan / Scheduler / ChildRun / ContextSnapshot / TaskResult 外层协议，让不同实现的专家共用调度、安全与生命周期。现有 ReAct 注册为 `general_agent`，而不是子 Agent 的唯一实现。

依赖：Phase 4、Phase 5；先完成本阶段，再实现具体专家和外部适配器。

- [ ] 定义版本化 `AgentDefinition` / 专家注册表：稳定 ID、版本、执行器类型、输入/输出合同、适用能力、所需 scope、审批能力、取消/恢复能力和默认评测器；注册由服务端控制，不允许 Planner 注入任意可执行类或命令。
- [ ] 将 `root_planner` / `coordinator` / `leaf` 等 fork 权限角色与 `general_agent` / 专家 ID、`react` / `workflow` / `external_cli` 执行类型拆成正交字段；`PlanStep.role` 不能同时承担三种语义。
- [ ] 定义统一执行请求、执行结果和异步执行器接口，覆盖启动、恢复、取消、超时、进度、等待审批/用户、失败与产物提交；明确不同执行器可选的 checkpoint 能力及不支持恢复时的行为。
- [ ] 从当前 `ChildAgentExecutor` 提取 `general_agent` ReAct 适配器；Scheduler 按已校验的定义选择执行器，不直接访问 `child_executor.runner` 或其私有方法。
- [ ] 在 fork、re-plan、retry、Snapshot 和 TaskAssignment 中解析并冻结专家 ID/版本；未知、禁用、不兼容或越权专家应在创建 ChildRun 前拒绝并留下结构化事件。
- [ ] 定义公共 lifecycle/result/event envelope 和版本迁移；允许执行器附加带命名空间和 schema version 的专属轨迹，不强迫 workflow/CLI 伪造 ReAct decision/tool 序列。
- [ ] 保持旧数据与默认行为兼容：未指定专家的现有计划选择 `general_agent`；持久化的旧 ChildRun/事件可读取，必要时定义显式迁移或兼容读取。
- [ ] 把 parent/session/workspace/policy scope、预算、取消 token、审批队列、Tool Executor/Safety Gate 约束应用于所有执行器，不能因为换专家获得额外权限。

验收：

- [ ] 现有单 Agent 与多 Agent ReAct 路径行为保持兼容；同一 Scheduler 可调用至少两种不同执行器。
- [ ] 专家选择、运行版本、等待/恢复、取消、失败和最终 TaskResult 可持久化并回放；不存在无记录的工具或副作用执行。

## 17. Phase 13：子 Agent 模型与推理配置

进度：LLM 请求、服务和可配置 OpenAI-compatible client 已有可选 reasoning effort，未声明支持的 provider 显式拒绝。服务端 child profile/allowlist、Planner/re-plan 的 ID 校验、Scheduler 首次解析与 client/model/effort 冻结、ContextSnapshot 持久化及 LangGraph→LLMRequest 传递已接通，并有定向测试。现有 session 尚无模型偏好接口；输出 token/费用预算、provider 限流回退、workflow 节点及外部 CLI 参数映射仍未完成。

目标：每个 ChildRun 可选择不同模型及推理深度，同时保持现有请求覆盖、会话偏好、配置默认值与服务端策略的确定性优先级。

依赖：Phase 12。

- [ ] 定义 `ModelPolicy` / `InferenceProfile`：provider/client、model、推理深度或 effort、输出 token、温度、能力要求、回退策略及成本/预算上限；区分 Planner 的偏好、专家默认值和服务端允许范围。
- [ ] 冻结模型选择优先级，并与现有“请求覆盖 → 会话偏好 → 配置默认值”约定兼容；不允许 Planner 通过模型选择提高 scope、预算或安全模式。
- [ ] 由执行器在启动前验证 provider 能力并解析实际模型/参数；不支持的 effort 必须按显式策略拒绝或降级并记录，不能静默假装生效。
- [ ] 将解析后的配置、provider/model 版本、专家定义版本和配置来源写入 ChildRun/ContextSnapshot/运行事件与 LLM 审计，retry/replay 能区分原始配置与实际回退。
- [ ] 将推理参数贯通 LLMRequest、provider adapter 和 `general_agent`；为 workflow 的不同 LLM 节点及外部 CLI 的受支持参数提供独立映射，未支持项明确标注。
- [ ] 为不同模型设置并发、token/费用和超时限制；provider 429/过载服从全局排队、退避与预算，不通过切换模型绕过限制。

验收：

- [ ] 两个 ChildRun 可在同一 Plan 中使用不同的已授权模型/effort；审计能还原最终生效配置。
- [ ] 非法模型、超预算配置和不受支持的推理参数有确定性结果与事件；旧请求不传新字段时行为不变。

## 18. Phase 14：示例专家 Agent（mock workflow）

进度：已实现默认关闭的服务端 mock workflow、专属节点事件和同 Scheduler 分派；本地配置显式开启后才允许 Planner 选择。可信代码可注入成功、部分、失败、合成等待确认、取消和超时场景，并有 SQLite 事件恢复测试；其中合成等待不创建真实 SafetyReview，不能算作审批队列接入。真实审批/恢复及混合 DAG 全分支验收仍需完成。

目标：用不依赖 ReAct 的可运行专家证明统一接口确实支持不同执行轨迹，而不只停留在协议设计。

依赖：Phase 12、Phase 13 的必要模型配置合同。

- [ ] 实现一个服务端注册的 mock workflow 专家：固定的“准备输入 → 执行模拟工作节点 → 验证输出 → 生成 TaskResult”流程，节点事件和 checkpoint 与 ReAct 轨迹不同；无需真实外部 CLI 或真实写操作。
- [ ] 支持确定性的成功、部分完成、失败、等待确认、取消和超时注入；所有分支产生结构化结果及持久化事件。
- [ ] 让 Planner 经 schema 校验后的专家请求可选择该 mock；不匹配的任务/合同、禁用专家和不存在的专家在 dispatch 前拒绝。
- [ ] 以同一个 Plan 混合调度 `general_agent` 与 mock workflow，覆盖依赖、并发、re-plan、结果聚合、父取消和审批恢复。
- [ ] 为后续真实专家提供最小模板/开发说明：注册、输入/输出、事件、scope、模型配置、审批、取消与评测接入点。

验收：

- [ ] mock 专家的执行不调用 ReAct runner，仍能完整走 ChildRun → Snapshot → Scheduler → Aggregator/Verifier。
- [ ] 进度、审批和恢复经持久化查询可重建；仅运行定向 mock/集成测试，不要求大规模测试。

## 19. Phase 15：Codex 外部执行器

进度：已补显式二进制路径的 app-server stdio 进程传输、Git 工作区隔离副本、私有 SQLite 原生事件日志，以及外层专家执行器的启动/中断/结果转换。配置默认关闭；显式开启时才注册 Codex 专家。新版 CLI 使用逐进程命名权限 profile，将命令读取限定为隔离副本、最小运行时和确切 Codex 二进制；真实临时仓库中的只读、暂存写入与区外读取拒绝已验收。文件变更审批仅在 `grantRoot` 可证明落在隔离副本时进入现有持久化 FIFO 队列；命令升级、网络/额外权限、用户输入和 MCP elicitation 暂不授予，未知请求 fail closed。隔离副本中的修改保留为待验收产物并返回 partial，**不会自动应用到源工作区**。重启恢复、独立进程隔离证明、产物应用审批与跨平台真实验收尚未完成，本阶段不得标记完成。

目标：把 Codex 作为可禁用的专家执行器接入。Codex CLI、SDK 和 app-server 是开源的执行框架/集成接口，但模型访问与托管服务另计；针对本系统的持续事件、审批、取消与会话恢复，优先验证 app-server，`codex exec` 仅作为有界批处理的备选。具体协议以实施时官方文档和本机版本为准。Claude Code 执行器不属于本阶段。

依赖：Phase 12–14；默认不启用外部执行器。

### 15.1 共同运行边界

- [ ] 定义外部进程协议：启动/会话标识、结构化输出解析、心跳、退出码、stdout/stderr 脱敏、最大输出、超时、进程树取消、重启后的孤儿进程处理与幂等重试。
- [ ] 用隔离工作目录/工作树和显式 workspace 映射运行；限定文件、网络、凭据和环境变量暴露；拒绝不支持的 scope，不能把通用 CLI 的宽权限当作已通过 ToolView 校验。
- [ ] 建立 CLI 内部工具/命令的审批桥接：审批请求进入现有持久化用户审批队列并按既定顺序处理；等待期间暂停执行，拒绝/超时/取消必须传回 CLI，恢复前再次校验 scope 与安全策略。
- [ ] 明确无法可靠拦截审批、限制工作范围或观测副作用的 CLI 模式为不可用；不得通过自动批准、信任整个进程或仅事后审计绕过 Safety Gate。
- [ ] 记录规范化生命周期、模型调用、工具/命令、文件变更、审批、产物和错误事件，并保留脱敏后的原生轨迹引用；实现 TaskResult/Artifact/EvidenceRef 转换和变更范围验证。
- [ ] 为凭据保管、远程数据出境、依赖安装、用户配置覆盖、路径穿越、prompt injection 和取消后的副作用制定显式策略；独立审查安全与持久化迁移。

### 15.2 Codex 适配器落地

- [ ] Codex 适配器：核验可用的 app-server/SDK/CLI 接口后实现版本探测、会话启动、事件/审批映射、模型与推理参数映射、取消和结果收集；不把 Codex 专有字段写进通用协议。
- [ ] 适配器支持未安装、未认证、版本不兼容、provider 失败、审批拒绝、进程异常退出和部分产物场景；这些状态不能被包装成 completed。
- [ ] 提供默认关闭的配置样例和本地启用说明；不强制现有用户安装 Codex，也不在普通单 Agent 运行时自动启动它。

验收：

- [ ] 用 fake Codex app-server/协议桩验证正常、失败、审批和取消路径；真实 Codex 仅在用户已配置的可丢弃 workspace 中进行小规模手动验收。
- [ ] 无法证明范围隔离或审批桥接时适配器 fail closed；可从 ChildRun 追溯工作目录、模型配置、外部会话、审批及输出产物。

## 20. Phase 16：统一轨迹与自进化输入

进度：Codex 原生通知/请求已有与普通 run event 分离的私有 SQLite 追加日志，可按 ChildRun 分页读取并显式标记记录缺口；普通 API 仍只收到脱敏摘要。跨执行器统一投影、权限化查询/导出、保留策略和自进化反馈合同仍未完成。

目标：为不同执行器保留足够真实、可比较的工作过程，而不把专家轨迹压扁成最终答案。

依赖：Phase 12–15 的事件合同；可先以 mock 专家实现，再接真实 CLI。

- [ ] 定义版本化 canonical trace envelope：parent/child/plan/step/attempt、agent ID/版本、executor 版本、模型配置、时间、事件序号、因果关联、审批、scope、预算、产物和结果；专属 payload 按执行器命名空间保存。
- [ ] 实现轨迹持久化、分页查询、脱敏/保留策略、导出与重放读取；SSE 只做投影，断线后可从持久化事件恢复。
- [ ] 为 workflow 节点、ReAct decision/tool、外部 CLI 消息/命令分别提供适配器；区分观测事实、模型叙述和推断，不伪造不可观测的内部思维链。
- [ ] 产出自进化模块可消费的案例引用与反馈合同：任务、版本、结果、验证、人工纠正、失败类别和证据；从轨迹生成 skill proposal 必须经过离线评测、安全审查及人工批准，不自动提升权限或发布能力。

验收：

- [ ] 同一 parent 下不同执行器的时间线可统一查询，仍能查看各自原生节点/命令；敏感原文不会进入普通 API 响应。
- [ ] 历史案例能固定执行器/模型/策略版本并用于后续评测；回放不会重新执行副作用。

## 21. Phase 17：离线评测与历史 badcase 在线回归

进度：已有显式选取、隐私审定的 badcase manifest 基础，以及从持久化 run/event JSON 计算 DAG、终态、审批和延迟指标的纯函数；缺失证据显示 unavailable。显式文件输入的离线 v1 JSON 报告 CLI 已实现，依赖/终态硬门禁不能把 unavailable 当作通过。另有最小 Planner 决策 oracle suite：分别检查结构/策略有效性和决策匹配；随附 scripted 候选仅验证评测链路，不代表真实模型能力。隔离 multi-Agent runner、真实模型 Planner 评测、在线影子评测、完整指标与跨版本对比仍未实现。

目标：在已有 `evals/` 确定性基准上扩展多 Agent 与专家评测；同时让真实历史失败成为可复现的回归案例。在线评测默认观测或影子运行，不得影响用户真实任务。

依赖：Phase 12–16 的统一结果与轨迹；基础数据集/评分合同可并行设计。

### 17.1 评测数据与离线运行

- [ ] 扩展 suite/case schema：任务、环境/fixture、允许的 scope、专家/执行器版本、模型配置、预算、期望结果、不变量、禁止副作用和评分器版本；保证数据集与报告可版本化。
- [ ] 增加多 Agent 本地隔离 runner：可注入 scripted LLM、fake workflow/CLI 与真实 provider；可重建 Plan、ChildRun、事件、Snapshot、审批和产物，不依赖用户真实邮箱或工作区。
- [ ] 构建分层样本：简单任务、并行 DAG、错误依赖、re-plan、冲突证据、部分失败、审批队列、取消/超时、权限拒绝、模型回退、workflow 专家和 Codex fake 适配器。
- [ ] 复用现有 `evals/` 的安全、工具、答案、延迟与 token 指标；新增任务完成率、证据支持率、DAG 正确性、无效 fork、重规划收益、审批正确性、scope 泄漏、取消后副作用、成本/延迟与并行效率。
- [ ] 将本清单第 14 节列出的效率、计划质量、上下文与安全指标逐项落实为可计算定义、事件来源、样本分母和报告字段；无法可靠观测的指标明确标为 unavailable。
- [ ] 将通用评分与专家专用评分分开：workflow 节点/出口合同、ReAct 工具使用、CLI 文件变更/命令与验证结果各有可插拔 grader；不得要求所有专家匹配同一 ReAct 工具序列。
- [ ] 定义硬门禁（安全、权限、数据泄露、禁止副作用）与软评分（质量、速度、成本）；置信度不足、不可观察和 judge 分歧不得计为通过。

### 17.2 历史 badcase 与在线影子评测

- [ ] 从失败 run、人工纠正、Verifier 冲突和用户反馈中提取 badcase 候选；去标识化、最小化数据、人工审定期望行为与权限后才入库，不直接把原始敏感轨迹复制进评测集。
- [ ] 为 badcase 建立归因标签、严重度、复现 fixture、期望不变量、修复版本和重复聚类；保留失败原版与修复版的对照结果。
- [ ] 实现增量回归选择：代码/策略/模型/专家版本变更时优先运行受影响 badcase 和安全基线；新失败加入隔离队列，复核后再成为门禁。
- [ ] 在线评测默认只读观测或隔离影子运行：不得重发邮件、写真实 workspace、自动审批或复用生产凭据；需要真实副作用的案例只能在明确授权的可丢弃环境执行。
- [ ] 监测版本分组的成功率、安全事件、人工接管/审批等待、token/费用、延迟与失败回归；报告样本量、置信区间和基线差异，避免小样本误判。
- [ ] 报告可定位 case → parent/child → 专家/执行器/模型版本 → 轨迹 → 失败类别 → 建议修复；支持把一次修复前后的离线/影子结果并列比较。

验收：

- [ ] 现有单 Agent suite 继续可运行；新增最小混合执行器 suite、权限/审批 suite 和历史 badcase 回归 suite。
- [ ] 一个脱敏 badcase 可离线复现并检出回归；线上观察不会触发真实写操作或用户审批。
- [ ] 评测报告同时展示任务质量、安全硬门禁、成本/延迟和执行器专属指标，不能只以最终答案正确率判定系统好坏。

## 22. Phase 18：端到端交付与阶段关闭

依赖：Phase 12–17，以及 Phase 6/9/10/11 中对本次交付必需的未完成项。

- [ ] 完成通用 ReAct + mock workflow + Codex fake 适配器混合 DAG 的定向端到端验证；真实 Codex 的验收受本机安装、认证和隔离环境限制，单列结果。
- [ ] 验证跨层 parent cancel、审批队列顺序、重启恢复、terminal winner、超时后禁止新副作用、重复命令幂等及旧数据兼容。
- [ ] 同步架构文档、API/前端契约、配置示例、运维/安全说明与 evals 使用说明；前端实现位于 `/mnt/d/agent-bot-frontend` 的现有原生 JS 页面，跨仓库进度分别验收，不把后端完成误报为 UI 完成。
- [ ] 检查最终 diff、定向测试和剩余风险；不以大规模测试替代关键安全与生命周期验收。

## 23. 后置候选：Claude Code（CC）执行器

当前不实施，也不是 Phase 18 关闭的前提。保留统一执行器协议的扩展点；只有在 Codex 与 mock workflow 的权限、审批、轨迹和评测闭环稳定后，再决定是否立项。

- [ ] 若立项，先核验当时可用的官方 CLI/SDK 协议、授权、模型参数、审批与取消能力，再定义独立适配器；不得照搬 Codex 事件或权限语义。
- [ ] 若立项，复用 Phase 15 的隔离与 fail-closed 安全要求，补充 CC 专属 fake adapter、badcase、轨迹评分及可丢弃 workspace 验收。
