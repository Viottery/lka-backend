# LangGraph ReAct 重构记录

更新时间：2026-09-09  
范围：将通用 Agent turn 的外层 ReAct 编排迁移到 LangGraph，并完成持久化、恢复、SSE 和隐私边界收尾。

本文是实施记录与后续交接材料。架构约束以 `AGENTS.md`、
`docs/backend_engineering_guide.md` 和 `docs/langgraph_core_migration_plan.md` 为准。

## 1. 重构需求与边界

最初需求不是“套用一个 LangGraph Agent 模板”，而是：

1. 保持原 Agent 的自由度、LLM 决策逻辑、package-first 工具模型、Tool Executor、安全策略和领域实现。
2. 由 LangGraph 接管跨步骤状态、条件分支、checkpoint、中断和恢复。
3. 保留项目自己的 LLM provider、session/context、tool package、domain service、run log 和 API 事件模型。
4. 让写工具继续经过 `ToolExecutor` 和 safety review；manual review 必须可恢复且不得重复副作用。
5. 不在 `app/core/` 中引入具体 package、tool 或领域流程特例。

因此，LangGraph 的定位是**外层执行状态机和 durable-control substrate**，不是对现有 Agent
组件的替换。

## 2. 迁移前基线

迁移前的 `AgentTurnLoop` 在 Python 控制流中执行完整循环：

```text
route -> expand package -> decision -> tool -> observation -> decision -> answer
```

它已具备的项目语义包括：

- package-first registry 与按需工具展开；
- 每一步仅展开一个 package、调用一个工具或进入 answer；
- provider-native Function Calling 和 JSON operation-first fallback；
- Tool Executor input/output schema 校验；
- `skip`、`llm`、`manual` 三种 safety review；
- session context、工具观察压缩、LLM audit、Markdown run log；
- SSE run events 与 LLM token delta。

最初的 LangGraph scaffold 只有：

```text
initialize_run -> execute_turn -> finalize_run
```

其中 `execute_turn` 仍整体调用 legacy loop，因此不能算完成外层 ReAct 迁移。

## 3. 实施步骤

### 3.1 建立运行协议与持久化基础

新增/完善的项目自有基础设施：

- `AgentTurnRunner`：允许 runtime 在 `legacy` 与 `langgraph` 编排器之间切换；
- `AgentRunRecord`、run events、safety reviews、artifacts 和 tool invocation claims；
- SQLite run store 与 SQLite LangGraph checkpointer；
- `AgentTurnWorkingSet`：只保存有界的控制状态与 artifact 引用；
- 稳定的 run、invocation、review、session effect 标识。

图 checkpoint 不保存完整 prompt、邮件正文、完整 tool result 或无界 observations。完整审计内容
留在 project-owned artifact、session payload 和本地 Markdown run log 中。

### 3.2 替换 outer ReAct bridge

`AgentGraphRunner` 被拆为显式节点：

```text
START
  -> initialize_run
  -> prepare_context
  -> route_package
  -> expand_package / answer
  -> decide_next_operation
  -> validate_operation
  -> safety_gate
  -> manual_review_interrupt (仅 manual pending)
  -> execute_tool
  -> build_observation
  -> decide_next_operation
  -> answer
  -> verify_answer
  -> finalize_run
  -> END
```

条件边只根据 operation、phase、review/cancellation 状态决定，不按 package 或 tool 名称分支。
`AgentTurnLoop` 的已有方法仍负责 route、prompt、LLM 调用、operation validation、回答、验证与
run log；LangGraph 只拥有循环、状态迁移和恢复边界。

### 3.3 工具生命周期与安全审查

工具调用接入项目自有的 nested graph：

```text
safety_gate -> execute_tool -> build_observation
```

其关键约束：

- 通过 Tool Executor 执行，未绕过 schema 校验；
- 使用 `run_id + step + tool + canonical input` 派生稳定 invocation ID；
- 执行前 claim；完成后持久化 ToolResult；
- 已完成 claim 复用结果；遗留 executing claim 视为副作用不确定，不自动重放；
- write-capable tool 必须创建 review，read-only 语义通过注册表/工具 metadata 判定。

### 3.4 恢复与幂等副作用

节点在 checkpoint 前可能已经完成外部写入，因此为以下副作用加入稳定幂等标识或去重：

- user/agent session message；
- context exchange/summary；
- `run_started` 与 `run_completed` lifecycle event；
- tool invocation claim/result；
- safety review decision transition。

runtime artifact 采用 `run_id + content_hash` 形式的不可变 ID，旧 checkpoint 不会指向后续节点
写入的“未来”内容。

### 3.5 SSE、恢复和公开 API

运行和 SSE 连接解耦：首次 stream 启动后台 run task；SSE 只读取按 sequence 排序的 run events。
断连不再隐式取消 run。增加：

```http
GET  /agent/runs/{run_id}
GET  /agent/runs/{run_id}/events?after_sequence=N
GET  /agent/runs/{run_id}/stream?after_sequence=N
POST /agent/runs/{run_id}/cancel
POST /agent/runs/{run_id}/resume
```

`cancel` 会立即发布 cancelled run 状态并写入 durable cancellation request；节点边界会检查并停止
后续 LLM/tool/finalize 操作。已经进入 provider 的请求无法保证被强制中止，但其迟到结果不会让
run 回到 completed。finalize 会先持久化 result artifact，再原子获取 completion claim；取消和完成
只能有一个赢家，避免产生 cancelled 状态却带有 `run_completed` event 或 agent answer 的记录。
manual review 的审批 API 仅在 review 从 `pending` 首次转换到 `approved/rejected` 时调度 resume。
服务重启留下的 `running` LangGraph run 可通过 resume API 显式恢复；同一进程内的重复 resume 会
合并为一个恢复任务。

### 3.6 API 隐私边界与 delta 存储

内部 `AgentTurnResult` 保留完整本地审计数据；公开 `AgentTurnResponse` 改为显式 DTO：

- 保留 answer、run/session/trace ID、package 状态、工具状态摘要、progress 摘要和 LLM 审计摘要；
- 移除 session context、expanded tool schema、decision raw output、工具输入/原始输出、完整 prompt、
  LLM output、audit record 和本地 log path；
- SSE/事件查询同样剥离普通工具 lifecycle event 中的原始 input/result metadata。
- safety-review API 与 `safety_review_*` SSE event 仅公开 review 摘要；原始 `tool_input` 与
  审查 LLM output 只保留在本地审计记录。

对于 streaming：内存中的 SSE event 仍保留 `content_snapshot` 兼容旧前端；SQLite 只保存 `delta`，
恢复/回放时按 `llm_call_id` 聚合重建 snapshot，避免长输出的平方级磁盘增长。
每次正常终结前都会 flush 异步 delta writer；flush 失败会让 run 失败而不会发布 `run_completed`。

## 4. 审查问题、原因与解决方案

| 问题 | 原因 | 解决方案 | 状态 |
| --- | --- | --- | --- |
| tool event 反序列化使用 `typing.Any.model_validate` | graph result restore 使用了错误的模型类型 | 分别使用 `AgentTurnToolEvent` 与 `AgentTurnVerificationWarning` | 已修复 |
| 请求级 LLM client/model/stream mode 丢失 | graph 执行时覆盖为默认 TEXT/None | request 写入 graph state，并在节点调用中通过 ContextVar 恢复 override | 已修复 |
| manual review 没有完整 resume 链路 | review API 只更新状态，未恢复 graph | 审批 endpoint 在首次状态转换时调度 `resume_agent_run_async`；non-stream 返回 202 waiting 状态 | 已修复 |
| runtime artifact 覆盖旧 checkpoint | artifact ID 固定为 run ID | 按内容 hash 使用不可变 artifact ID | 已修复 |
| prepare/finalize replay 重复写 session/context/run event | 副作用发生在节点 checkpoint 前 | session message、context exchange、lifecycle event 使用稳定 effect ID 或去重 | 已修复 |
| safety reject 未进入 observation | rejected result 直接进入 answer | 生成 tool event、feedback 和 observation，再让 Agent 决策/回答 | 已修复 |
| assistant/verification 事件语义缺失 | graph 节点未镜像 legacy event 生成 | 追加 assistant progress、verification warning event；无 package 使用 context-answer 路径 | 已修复 |
| review 重复提交会并发 resume | endpoint 对已决 review 仍会创建 resume task | `decide_safety_review_with_transition` 仅允许首次 pending-to-terminal 触发恢复 | 已修复 |
| `initialize_run` 重放重复 `run_started` 并修改 started time | 节点副作用无去重 | 仅 queued run 标记 running；按既有 event 去重 `run_started` | 已修复 |
| `/agent/turn` 泄露完整 prompt、context、工具结果 | response 继承内部 `AgentTurnResult` 并直接 `model_dump` | 引入公开 DTO 和 transport event 脱敏；完整数据仅在本地审计存储 | 已修复 |
| completed run 在 result artifact 前标记完成 | `_finalize` 的持久化顺序错误 | 先写 result artifact，在 run snapshot 保存 artifact ref，再 `complete_run`；恢复增加 fallback | 已修复 |
| SSE 断连取消运行且无法重连 | task 由 generator 持有，断连路径调用 cancel；无 event API | 后台 task 与连接分离；新增 status/event/reconnect/cancel API | 已修复 |
| 重启后事件无法回放 | hot cache 未恢复 durable event | run restore 从 SQLite 读取并恢复 ordered events | 已修复 |
| delta snapshot 持久化 O(n²) | 每个 token event 保存不断增长的全文 snapshot | SQLite 去除 snapshot，只保存 delta；rehydrate 重建 snapshot | 已修复 |
| cancel 与 finalize 竞态产生 cancelled + completed 混合记录 | cancel/complete 分别更新状态，finalize 内部副作用没有终结仲裁 | result artifact 后写入 durable completion claim；取消和完成互斥，终结前再发布 completed event | 已修复 |
| safety review 公开 API/SSE 泄露原始工具输入 | public DTO 继承内部 review record，event filter 未处理 review | 独立公开摘要 DTO，并在 transport filter 中只保留安全字段 | 已修复 |
| terminal event 前 delta 未确保落库 | `llm_delta` 走异步 writer，正常完成未 flush | `run_completed` 前 flush；持久化失败转为 failed run | 已修复 |
| 重启后的 running run 没有 HTTP 恢复入口 | runtime recovery 未接入 API 控制面 | 新增 `POST /agent/runs/{run_id}/resume`，等待审查和终态返回 409 | 已修复 |
| manual review 初始 progress 不在 checkpoint | `interrupt()` 在 `_save()` 前中断节点返回 | 拆为 `safety_gate` 持久化节点和后续 `manual_review_interrupt` 节点 | 已修复 |
| Agent `trace_id` 未写入旧 `traces` 表 | 旧 TraceRecorder 仅为 debug RuntimeLoop 设计 | 新增 Agent run 查询以 trace_id 关联 run；不双写 debug traces，避免不一致/原始事件复制 | 有意保持分离 |

## 5. 当前行为与已知边界

- LangGraph 模式已不再将完整 legacy loop 当作 bridge node 调用。
- legacy orchestrator 仍保留为显式兼容选项；是否将默认值切换到 LangGraph 是独立发布决策。
- process restart 后不会自动恢复全部 incomplete run；恢复通过 `Runtime.resume_agent_run(run_id)` 或
  safety-review decision 明确触发。
- SSE reconnect 能重放持久化事件；对已经在 provider 内部执行的调用，取消仍是协作式，不能保证
  强制中止远端请求。
- debug `traces` 表和 Agent run audit 继续分离。若未来需要全局 trace 检索，应新增统一 read model，
  而不是复制 Agent 的原始 events 到旧表。

## 6. 验证记录

最终验证采用分组执行，避免单个命令运行时间受环境限制：

```bash
uv --cache-dir .uv-cache run pytest -q tests/test_agent_turn.py -k '...'
uv --cache-dir .uv-cache run pytest -q tests/test_agent_runs.py tests/test_agent_stream.py tests/test_safety_reviews.py
uv --cache-dir .uv-cache run pytest -q tests/test_api_cors.py tests/test_cli.py tests/test_filesystem_tools.py tests/test_knowledge_semantic.py tests/test_knowledge_service.py tests/test_knowledge_tools.py
uv --cache-dir .uv-cache run pytest -q tests/test_bash_tools.py
uv --cache-dir .uv-cache run pytest -q tests/test_llm_audit.py tests/test_llm_service.py tests/test_local_config.py tests/test_mail_service.py tests/test_matters.py tests/test_outlook_service.py
uv --cache-dir .uv-cache run pytest -q tests/test_evals_smoke.py tests/test_platform_support.py tests/test_public_dataset_adapter.py tests/test_public_document_snapshot.py tests/test_runtime_debug.py tests/test_runtime_mail_sync.py tests/test_sessions.py tests/test_start_backend.py
uv --cache-dir .uv-cache run ruff check app/api/routes/agent.py app/api/schemas.py app/core/agent_graph.py app/core/agent_runs.py app/core/agent_storage.py tests/test_agent_turn.py tests/test_agent_runs.py tests/test_agent_stream.py tests/test_safety_reviews.py
python -m py_compile app/api/routes/agent.py app/api/schemas.py app/core/agent_graph.py app/core/agent_runs.py app/core/agent_storage.py
git diff --check
```

结果：178 个测试通过；ruff、py_compile 和 `git diff --check` 通过。

## 6.1 本地部署测试配置

默认 orchestrator 继续保持 `legacy`，这是兼容发布策略，不表示 LangGraph 未完成。进行本地
部署测试时，在未提交的 `config/local.toml` 中显式启用：

```toml
[agent]
orchestrator = "langgraph"
checkpoint_backend = "sqlite"
max_decision_steps = 10
```

首轮验收应至少覆盖：

1. 无工具回答、只读工具、跨 package 工具和写工具 manual review；
2. 浏览器/SSE 断连后按 `run_id` 和 `after_sequence` 重连；
3. `POST /agent/runs/{run_id}/cancel` 在 route、decision、answer 前取消，以及 provider
   调用期间取消后的终态保持；
4. 重启后恢复 waiting manual review、incomplete checkpoint 和 completed result artifact；
5. 检查 `/agent/turn`、run status、event query 不含完整 prompt、session context、原始 tool
   result 或 artifact reference。

当前代码已满足进入上述**受控本地部署测试**的条件；是否将默认 orchestrator 从 `legacy`
切换为 `langgraph` 应在该验收完成后作为单独发布决策处理。

## 7. Git 记录

迁移和收尾分别保存为：

```text
4e8c0d8 Migrate agent orchestration to LangGraph
1f41e8f Harden agent run recovery and API privacy
```

本文档创建后应作为单独、可审查的文档提交，不包含本地 run log、个人数据或未跟踪临时文件。
