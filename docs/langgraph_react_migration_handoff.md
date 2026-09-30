# LangGraph ReAct Migration Initial Handoff

更新时间：2026-09-05

本文保留 LangGraph 重构启动时的交接快照，记录当时的需求、代码事实、
未完成范围、实施顺序和验收标准。它不是当前实现状态；完整重构过程和后续
修复记录见 `docs/langgraph_react_refactor_record.md`，正式架构方案仍以
`docs/langgraph_core_migration_plan.md` 为准。

## 1. 当前需求

将现有 `AgentTurnLoop` 中的 ReAct 控制流尽可能按当前异步和流式语义拆解，
重新实现为 LangGraph 图。

目标图至少包含：

```text
initialize_run
  -> prepare_context
  -> route_package
  -> expand_package
  -> decide_next_operation
  -> validate_operation
  -> safety_gate
  -> execute_tool
  -> build_observation
  -> decide_next_operation
  -> answer
  -> verify_answer
  -> finalize_run
```

条件边必须处理 `expand_package`、`call_tool`、`final_answer`、无效决策、
取消、失败和步数耗尽。节点和边不得硬编码具体 package、tool 或领域流程。

## 2. 必须保持的现有语义

- 保留现有 `LLMService`、provider-native Function Calling 和 JSON fallback。
- 所有 LLM 阶段继续发送 token delta；不能等节点结束后才批量发布 SSE。
- 保留 `/agent/turn`、`/agent/turn/stream`、run event 和 safety review API 契约。
- 每个 Agent step 只能展开 package、调用一个工具或进入回答阶段。
- 工具调用必须经过 `ToolExecutor` schema 校验。
- `read_only != true` 必须产生 safety review；支持 `skip`、`llm`、`manual`。
- Agent core 不得包含 package/tool/domain 特例。
- `selected_package` 仅作为兼容日志字段，不进入 LLM 上下文。
- 完整 prompt、邮件正文和长工具结果不得进入普通 graph checkpoint。
- session workspace、Windows/WSL 路径和现有工具环境变量语义不得改变。

## 3. 当前已经实现

### 3.1 LangGraph 外层 scaffold

`app/core/agent_graph.py` 已实现：

```text
initialize_run -> execute_turn -> finalize_run
```

- 支持 memory/SQLite checkpointer。
- graph thread 使用单次 `run_id`，不复用 conversation `session_id`。
- checkpoint state 保存有界 request、run snapshot、结果摘要和 artifact 引用。
- 可查询 state/history，并提供显式 resume API。

但 `execute_turn` 仍调用完整的 `AgentTurnLoop.run_async()`。因此它只是 bridge，
不是外层 ReAct 迁移完成。

### 3.2 持久化运行基础设施

`app/core/agent_storage.py` 和 `app/storage/db.py` 已增加：

- durable Agent run；
- durable run events；
- durable safety reviews；
- large run/tool artifacts；
- durable tool invocation claims。

高频 `llm_delta` 由内存队列批量写入 SQLite；其他生命周期事件同步落盘。
取消请求和 review 决策可在进程重启后恢复。

### 3.3 工具生命周期子图

`app/core/agent_tool_graph.py` 已实现：

```text
safety_gate -> execute_tool -> build_observation
```

- 未绕过现有安全审查和 `ToolExecutor`。
- invocation ID 由 `run_id + step + tool + canonical input` 稳定派生。
- 执行前 claim，完成后保存结果。
- completed claim 复用已有结果，不重复执行。
- 遗留 `executing` claim 视为副作用不确定，拒绝自动重放。
- 完整 ToolResult 保存为 artifact，不进入持久化 graph state。

当前 manual review 仍使用原有阻塞等待机制，尚未改为 LangGraph interrupt。

### 3.4 Working set 数据结构

`app/core/agent_turn.py` 已新增 `AgentTurnWorkingSet`，包含：

- run/session/trace identity；
- route 和 initial/active/expanded/used package 状态；
- step index 和 pending decision；
- pending invocation/review ID；
- observation artifact references；
- terminal answer/reason。

该类型目前只是已定义的数据契约，尚未接入 `AgentGraphRunner`，不能将其视为
外层图已迁移。

## 4. 当前仍未实现

- `prepare_context` LangGraph 节点。
- `route_package` LangGraph 节点。
- 单步 `decide_next_operation` 节点。
- `validate_operation` 节点及数据驱动条件边。
- package expansion 节点和回到 decision 的循环边。
- tool result 到 observation artifact/compact observation 的独立节点衔接。
- answer、verify、session commit、run finalize 的独立节点。
- manual safety review 的 LangGraph `interrupt/resume`。
- 外层节点级崩溃恢复。
- 删除 `execute_turn -> AgentTurnLoop.run_async()` bridge。

当前恢复保护只允许从 `initialized -> execute_turn` 的执行前 checkpoint 恢复。
进入 legacy bridge 后会拒绝重放，避免重复写操作。

## 5. 推荐实施顺序

1. 将 `AgentTurnLoop._run()` 的准备和收尾拆成可复用阶段服务：
   `prepare_context`、`commit_result`。
2. 将 `_run_llm_decision_loop()` 改为单步函数：输入 working set，输出一个
   normalized operation；函数内部不得循环。
3. 将扩包、工具可用性、重复调用检查拆为纯本地 validation/transition 服务。
4. 在 `AgentGraphRunner` 中建立真实条件边：
   decision -> expand/tool/answer/error；expand/observation -> decision。
5. 用 artifact ID 保存完整 context、observations、LLM events 和 tool results；
   checkpoint 只保存 working set 和引用。
6. 将现有工具生命周期子图接入外层 `execute_tool` 路径。
7. 将 manual review 改为 durable review + LangGraph interrupt；API 决策后通过
   `Command(resume=...)` 恢复原 graph thread。
8. 删除 bridge 和仅为 bridge 存在的保守 resume 限制。
9. parity 通过后再考虑将 `agent.orchestrator` 默认值从 legacy 改为 langgraph。

## 6. 异步与 SSE 约束

- 当前 provider 调用和 legacy loop 通过工作线程避免阻塞 FastAPI event loop。
- 拆分后，同步阶段节点应继续在线程中执行或提供 async wrapper。
- 现有 LLM delta callback 必须在 provider 产出时立即写入 run event hot cache；
  不等待 LangGraph node 返回。
- LangGraph checkpoint 和 durable delta 批处理不能成为首 token 的同步前置步骤。
- run completion event 必须排在该 run 的最后一个 delta 之后；关闭 runtime 时必须
  flush delta writer。

## 7. 完成验收标准

只有同时满足以下条件，才可声明外层迁移完成：

- `AgentGraphRunner` 不再调用完整 `AgentTurnLoop.run/run_async`。
- Python `for/while` 不再拥有 ReAct 的跨步骤控制；循环由 LangGraph 条件边拥有。
- route、decision、expand、validate、tool、observation、answer、verify、finalize
  都能在 state history 中看到明确节点边界。
- 每个非只读工具调用仍有 safety review 记录。
- manual review 可跨进程重启恢复，并且批准后只执行一次工具。
- 崩溃发生在工具 claim 后、结果记录前时不会自动重放副作用。
- SSE token delta 时序和现有 API payload 保持兼容。
- legacy/langgraph parity 测试、checkpoint/restart 测试、manual review 测试通过。
- graph checkpoint 中不存在完整邮件正文、无界 observations 或完整 prompt。

## 8. 当前测试证据

最近已通过的相关测试：

- `tests/test_agent_runs.py`：7 passed。
- manual/skip safety review 与 `tests/test_agent_turn.py` 组合：32 passed。
- 工具生命周期图 completed claim 测试确认同一 invocation 不会重复执行。
- `ruff` 对新增的 tool graph/storage/tests 通过。

完整组合测试曾受 60 秒命令超时影响，不能据此声明整个迁移验证完成。

## 9. 当前 Git 工作区

本轮重构尚未提交。已修改/新增的任务相关文件包括：

```text
app/core/agent_checkpoints.py
app/core/agent_graph.py
app/core/agent_runs.py
app/core/agent_storage.py
app/core/agent_tool_graph.py
app/core/agent_turn.py
app/core/local_config.py
app/core/runtime.py
app/core/safety.py
app/storage/db.py
config/local.example.toml
docs/langgraph_core_migration_plan.md
pyproject.toml
tests/test_agent_runs.py
tests/test_agent_turn.py
tests/test_local_config.py
tests/test_safety_reviews.py
uv.lock
```

当时未跟踪的 `:memory:.ses` 和 `todolist_2026-08-31.txt` 不属于该阶段任务。

迁移前快照位于：

```text
/tmp/lka_backend_pre_langgraph_20260905T025220Z
```
