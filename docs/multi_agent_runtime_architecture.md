# 动态多 Agent Runtime 架构设计

## 1. 设计目标

本项目不把多 Agent 限定为 RAG、邮件或某一种固定业务流程。目标是建立一个稳定的 Agent Runtime：

```text
固定运行时骨架 + 动态任务分解 + 策略约束下的子 Agent fork
```

Planner 可以根据任务动态创建、并行、等待、重试和取消子任务，但不能修改系统级权限、预算、安全规则或运行时调度约束。

本设计称为：

> Policy-Constrained Dynamic Agent Orchestration（策略约束下的动态多 Agent 编排）

## 2. 核心原则

1. Agent 决策层动态，Runtime 执行层稳定。
2. 子 Agent 通过结构化任务和结果通信，不通过自由文本直接控制彼此。
3. 子 Agent 使用从父上下文派生的不可变快照，不共享可变的全局 session memory。
4. 所有工具调用继续经过 Tool Registry、Tool Executor、schema validation 和 safety gate。
5. RAG、mail、filesystem、code、matter 等都是可注册的 capability，不定义多 Agent Runtime 的领域逻辑。
6. 读取任务可以并行；同一资源上的写操作默认串行，并由资源锁或冲突检查保护。
7. SSE 负责进度展示，持久化 run / event / artifact 才是可靠状态来源。
8. 每个子 Agent 都必须有预算、超时、取消边界和输出合同。

## 3. 固定 Runtime 骨架

```text
User Request
    ↓
Main Agent / Planner
    ↓
Plan + Task DAG
    ↓
Fork Policy / Scheduler
    ├─ Child Agent A
    ├─ Child Agent B
    ├─ Child Agent C
    └─ 受规则限制的递归 fork
    ↓
Result Aggregator
    ↓
Verifier / Critic
    ↓
Planner 继续调整或 Final Answer
```

固定的是生命周期、协议和安全边界，不是子 Agent 的业务角色或数量。

### 3.1 Planner

Planner 负责理解目标、创建和调整 Plan、决定是否 fork、指定依赖和验收标准。Planner 可以提出候选 capability 和资源需求，但不能直接授予权限，也不能绕过工具执行链。

### 3.2 Scheduler

Scheduler 将 Plan 转换为可运行的 Child Run，负责 DAG 依赖、并发上限、队列、超时、取消传播、重试、资源锁和部分失败处理。Scheduler 不负责领域推理。

### 3.3 Child Agent

Child Agent 只执行一个受限 Subtask。它可以是叶子 Agent，也可以是允许有限递归 fork 的 Coordinator Agent：

- `leaf`：不能继续 fork，只能完成当前任务。
- `coordinator`：可以创建子任务，但受更小的深度、数量和预算限制。
- `root_planner`：负责顶层计划和重新规划。

### 3.4 Aggregator

Aggregator 合并结构化结果、artifact 和 evidence 引用，发现重复、冲突和缺口；不能把失败或未知包装成成功。

### 3.5 Verifier

Verifier 按 `VerificationContext` 检查结果是否满足计划中的验收标准，包括证据充分性、测试结果、文件变化、权限和副作用。验证失败时可以返回补充任务建议，但不能自行扩大范围。

执行审计区分“发起调用”与“进入工具实现”：`ToolResult.execution_started` 是 Executor
持有的可选标记，进入 `tool.invoke` 前设为 true，并覆盖工具自身返回的同名字段。只在
实际实现调用前拒绝时标 false；进入后失败/拒绝/异常仍可能产生部分副作用。旧记录缺失
标记不推断为未执行，工具 output 内的同名数据不作为审计凭据。
标记必须是实际 bool 或 null；SQLite 恢复不得把 `0`、`"false"` 等强制转换为未执行证明。
非法持久结果停止恢复，不重新调用工具，也不发布洗白后的 completion。

聚合时标记必须与持久化 tool completion 和 child audit 的 invocation/status 对齐；只在
一致的 false + rejected 记录下排除该调用的实际副作用，不能借此把没有证据的子任务升级
为完成。SQLite 恢复保留原标记且不重新执行。这个判断不改变 ToolView、审批或权限规则。

ToolView 拒绝结果另外携带 `output.authorization_denial`：区分过期、tool/package 未授权
和本次参数未被证明只读。它解释当前调用，不能授权重试或扩大 scope。一个支持条件只读
的工具被拒绝一次，不等于所有合法读法都被禁用；后续参数仍经过全部原始检查。

## 4. Plan 与动态 fork

Plan 是任务图，不是固定工作流。每个 `PlanStep` 至少包含：

```text
step_id
objective
role / capability
depends_on
parallel_group
allowed_packages
input_refs
output_contract
verification_criteria
budget
side_effect_level
```

角色由服务端沿运行 lineage 派生，不接受 Planner 自行指定：root Planner 的首层委派固定为 Coordinator；Coordinator 在单独的较小深度、子项数及运行预算内可再委派 Leaf；Leaf 不可 fork。`coordinator_depth` 计的是 Coordinator 可发起的 fork 代数（首层 root→Coordinator 不计入），总体 DAG 深度仍由 `max_depth` 约束。Alternative-step replan 继承被替换步骤的角色和角色预算上限。

Planner 的 fork 操作应是结构化 operation，例如：

```json
{
  "operation": "fork_subtasks",
  "parent_task_id": "task_1",
  "subtasks": [
    {
      "id": "task_1_a",
      "objective": "分析项目架构",
      "allowed_packages": ["knowledge", "filesystem"],
      "depends_on": [],
      "parallel_group": "research",
      "output_contract": "analysis_report"
    },
    {
      "id": "task_1_b",
      "objective": "检查测试状态",
      "allowed_packages": ["filesystem", "bash"],
      "depends_on": [],
      "parallel_group": "research",
      "output_contract": "verification_report"
    }
  ]
}
```

系统在创建 Child Run 前必须校验 schema、fork 深度、任务数量、权限范围、资源冲突、预算和确认要求。Agent 可以决定“要做什么”，不能决定“系统允许消耗多少资源”。

## 5. 上下文隔离与传递

```text
SessionContext
    ↓
TaskContext
    ↓ 按子任务裁剪
ContextSnapshot / ExecutionContext
    ↓
TaskResult / Artifact / EvidenceRef
    ↓
VerificationContext
```

每个 Child Agent 获得独立的 `ContextSnapshot`，至少绑定：

- parent run、plan、subtask 和 session 标识；
- 用户目标的最小必要摘要；
- 子任务目标和依赖结果引用；
- workspace、source、account 等访问 scope；
- allowed packages、tools 和 side-effect level；
- token、LLM 调用次数、检索轮数和耗时预算；
- 输出合同和验收标准；
- 相关 evidence / artifact 引用及 freshness。

子 Agent 不直接修改父计划、session context 或其他 Agent 的 working set。所有结果先写为不可变 artifact，再由 Aggregator / Planner 决定是否合并。大段正文应通过引用传递，避免在每个 prompt 中复制。

子 Agent 与 root Agent 使用同一 Tool Registry 和 ReAct package 懒展开机制；package/tool 不按角色静态降级。`allowed_packages` / `allowed_tools` 是 Planner 的路由提示，不是授权来源。实际工具权限由不可变 snapshot 中 parent、session、workspace 和 server policy 的交集决定，数据工具还必须在工具包 / Tool Executor 层执行 source/account/path 范围校验。无法证明受限 workspace 下任意命令安全时，`bash.run` fail closed；在完整继承的 workspace 范围内仍走同一 SafetyGate，写操作不得绕过确认。

## 6. Agent 通信协议

通信分为四类：

```text
命令：TaskAssignment / Cancel / Retry
事件：Started / Progress / ToolUsed / Completed / Failed
结果：TaskResult / Artifact / VerificationResult
引用：ContextRef / EvidenceRef / ArtifactRef
```

事件用于 UI、监控和取消；结果和引用写入持久化存储，供 Planner、Aggregator 和 Verifier 消费。SSE 不是可靠的系统间通信协议。

建议的生命周期事件包括：

```text
plan_created
subtask_created
subtask_queued
subtask_started
subtask_progress
subtask_waiting
subtask_completed
subtask_failed
subtask_cancelled
aggregation_completed
verification_completed
plan_revised
```

## 7. 并发、非阻塞和资源冲突

并行执行采用有界 DAG 调度，而不是无条件 `asyncio.gather`：

- 没有未完成依赖的任务才进入 runnable 队列。
- 全局、session、provider、package、workspace 分别设置并发上限。
- 同一文件、matter、邮箱账户或其他可变资源的写操作默认串行。
- 每个 Child Run 有独立 timeout、cancel token、token budget 和 retry policy。
- parent cancel 必须传播给所有后代 Child Run。
- 单个子任务失败默认只影响依赖它的节点；无关节点继续执行。
- CPU 密集型 embedding、reranker、文件扫描等不能直接阻塞 FastAPI event loop。
- 长任务通过持久化 run 和后台执行恢复，不让 HTTP 请求一直等待。

```text
HTTP request → 创建 parent run → 返回 run_id / stream
                         ↓
                   后台 Scheduler
                         ↓
             Child Runs + events + artifacts
                         ↓
                  Aggregator / Verifier
```

## 8. Policy、预算与安全边界

每次 fork 都需要经过系统侧策略检查：

```text
max_depth
max_children
max_concurrency
max_total_tokens
max_wall_time
allowed_packages
allowed_scopes
allowed_side_effects
confirmation_requirement
```

子 Agent 不得通过 prompt、工具参数或返回结果扩大这些限制。高风险工具仍必须经过既有 safety review 和 confirmation 流程。

## 9. RAG 与其他能力的关系

RAG 是 capability / Context Provider，而不是多 Agent Runtime 的上层控制器：

- Planner 可以检索任务背景。
- Research Agent 可以在授权 source scope 内检索证据。
- Code Agent 可以检索代码规范、README 和历史上下文。
- Mail Agent 可以检索邮件和事务。
- Verifier 可以检索验收约束和相关证据。

`RetrievalPlan` 与 `ExecutionPlan` 必须保持分离：前者描述如何获取证据，后者描述如何完成任务；两者可以通过 evidence gap 和 verification criteria 互相影响，但不能合并为一个领域专用图。

## 10. 与当前项目的映射

当前 LangGraph ReAct Loop 继续作为单个 Agent 的执行内核：

```text
Parent Planner / Scheduler
        ↓ 创建 Child Run
Child Agent 使用现有 Agent Turn / ReAct Loop
        ↓ Tool Registry / Executor / Safety Gate
TaskResult / Artifact / Trace Event
        ↓
Aggregator / Verifier
```

不为每个领域创建独立 StateGraph。多 Agent Runtime 负责 parent-child lifecycle 和调度；Agent Loop 负责单个 Agent 的 route、decision、tool execution、observation 和 answer。

## 11. 当前实现状态、启用方式与限制

### 启用多 Agent

默认配置保持关闭。复制 `config/local.example.toml` 为 `config/local.toml`，在 `[agent]` 下设置：

```toml
orchestrator = "langgraph"
checkpoint_backend = "sqlite"
multi_agent_planning_enabled = true
```

启用人工审批时，在 `[safety]` 下设置：

```toml
tool_review_mode = "manual"
```

随后重启后端。多 Agent planning 要求 LangGraph checkpoint runner；runtime 会在配置不兼容时拒绝启动。`manual` 模式下非只读工具会等待 safety-review 决策，用户通过安全审批 API 决定后继续恢复 run。

### 已落地的后端能力

- Plan / fork 校验、ContextSnapshot 与 ToolView、子 Agent 现有 ReAct / Tool Executor 执行路径。
- 有界 DAG scheduler、依赖处理、重试/取消、结果聚合和 verifier / 支持的 PlanPatch replan。
- parent snapshot / child tree、run event cursor、child cancel / retry、safety review 和 checkpoint resume API。
- 同一 runtime 实例内的 per-run graph lease 避免同一 checkpoint 被并发恢复；scheduler 与 HTTP resume 共用 runner。

具体勾选与剩余工作见 [多 Agent Runtime 开发路线](multi_agent_todolist.md)。

### 重要限制

- 这套 scheduler、Tool Executor admission/resource locks 与 per-run graph lease 是**单进程内**协调；多 worker 或多进程部署尚无分布式锁保证。
- 取消和 timeout 会阻止后续调度并在可检查边界生效，但已经进入同步写工具的调用不能被强制终止，可能先完成副作用。
- child 的 wall-time、tool-call 和 LLM-call 边界已接入。`max_tokens` 以审计用量（缺失时按字符数回退）加 prompt 字符估算，并限制下一次请求输出 token；这是近似预算，不是 tokenizer 精确计量。
- `ASK_USER` 使用独立的 `WAITING_USER` 状态、私有 answer journal 和 `/runs/{run_id}/continue`，不复用 safety approval 队列；根 parent checkpoint 的重启/重复 continuation 有集成覆盖。嵌套 child-question 的 wait-set 与祖先恢复已做 runtime 链路测试，但该测试 stub 了 graph runner，完整嵌套图 E2E 仍待补。
- Phase 10 已接入 server-configured `single_agent` 策略 telemetry；它不跳过 Planner / ReAct LLM 步骤，不属于延迟优化。显式 Planner fork 请求记录升级后走原有 scheduler。其他 fast-path 模板因缺少可信 selector / evidence contract 而保持禁用。Phase 11 的 root → coordinator → leaf 执行与审批恢复有定向集成覆盖；跨层取消 E2E 尚待验证。
- 后端只提供 API / SSE 协议；现有前端位于 `/mnt/d/agent-bot-frontend`，使用原生 JS 页面，不要求 React 框架。前端需实现 snapshot + event cursor 恢复和状态展示。
- Safety review 决策与 run 恢复有持久化记录；审批通过不会绕过原工具执行时的 Tool Executor 和 Safety Gate 边界。

### 专家执行器的当前边界

`PlanStep.role` 仍表示编排权限；`agent_id` 则独立指定服务端注册的执行实现。未指定时为 `general_agent`，沿用现有 ReAct Child Agent。Scheduler 在创建 ChildRun 前解析并冻结专家版本；未知或禁用专家会被拒绝。`mock_workflow` 是不调用 ReAct 的确定性示例，仅在本地配置 `mock_workflow_agent_enabled = true` 时可被 Planner 选择；其结果是模拟内容，不应用于真实任务答案。当前专家定义和事件适配只覆盖最小执行闭环，尚不构成完整的专家输入/输出、模型、审批、取消和恢复合同。

Codex app-server 目前只有可注入传输的 JSON-RPC 协议客户端和 fake-peer 测试，没有进程启动、隔离工作区、持久化审批桥或副作用观测，因此没有注册为可执行专家。缺少这些安全边界时必须保持禁用。

### 11.1 协议与执行边界

`app/core/multi_agent.py` 提供版本化 Plan、PlanStep、ChildRun、ContextSnapshot、TaskResult、
Artifact、VerificationResult 和事件合同；Plan DAG 与 fork policy 在创建子运行前由服务端校验。
请求/拒绝/validated plan、子任务生命周期及结果写入 run metadata / durable events。Scheduler 会把
validated steps 转为独立 Child Run 和 Snapshot，经现有 ReAct / Tool Executor 执行，之后聚合和验证。

fork 的权限来自 requested scope 与 parent effective、session、workspace、policy scope 的交集；
planner 的 package/tool 选择仅作为路由约束，不作为权限授予。child 工具调用再次由 ToolView、
Tool Executor 和 Safety Gate 校验。配置默认关闭，启用需使用 LangGraph orchestrator。

工具发现与调用授权分开：`ToolSpec.supports_read_only_invocations` 默认 false，支持按参数
分类只读的工具可明确声明 true，让 READ 子 Agent 看到工具而不拿空参数误判隐藏。
这仅影响 catalog，静态 read_only 保持原值；执行仍用真实参数检查冻结 ToolView、作用域、
安全门和来源约束。声明不能允许写调用、绕过 NONE/expiry，或将 READ 提升为 EXTERNAL。

动态分类回调只有实际 `bool` 才能决定本次只读属性；字符串、整数、容器、null 或普通
分类异常视为 unknown（`None`），不能用 truthiness 或静态 true 回退授予 READ。
unknown 在 READ/NONE child 中被拒绝，顶层有审批的调用仍可走原安全门；取消继续传播。

当前验证结果只对明确机器可检查的 artifact/evidence/side-effect 状态给出确定结论；自然语言合同
需要外部 verifier outcome，未提供时保持 inconclusive。文件变更范围和测试/lint/schema 结果需由外部
验证器或调用方提供，内置 aggregator 不会自行运行这些检查。

## 12. 验收标准

- 不同业务可以复用同一套 Planner、Scheduler、Child Run 和 Result 协议。
- 子 Agent 不能访问父上下文之外的数据或工具。
- 子 Agent 不能绕过 Tool Executor 和 safety gate。
- 无依赖的只读任务能够并行执行。
- parent cancel 能传播到所有子任务。
- 一个子任务失败不会丢失无关任务结果。
- 计划、依赖、上下文快照、事件、artifact 和验证结果可回放。
- 最终结果可以追溯到具体 Agent、工具调用、证据或文件变更。
- RAG、mail、filesystem、code 等 capability 可以独立接入，不改变 Runtime 核心。

## 13. 暂不做

- 任意 Agent 自由创建无限子 Agent。
- Agent 之间无 schema 的自由聊天作为控制协议。
- 全局可变共享 memory。
- 让模型自行扩大权限、预算、并发数或 source scope。
- 为每个领域复制一套 Agent StateGraph。
- 在没有实际场景验证前建设完全通用的 Agent 市场、动态发现和跨进程自治网络。

ContextDriver 的详细设计、Snapshot 字段、上下文视图、缓存失效和确定性核心与可选 LLM 辅助见：[docs/context_driver_design.md](context_driver_design.md)。
