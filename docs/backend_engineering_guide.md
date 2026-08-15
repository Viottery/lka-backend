# Backend Core：Engineering Guide

## 1. 项目定位

Backend Core 是整个 Local Knowledge Agent OS 的核心执行层。它承载 HTTP API、
运行时调度、知识上下文构建、能力选择、技能执行、验证与轨迹记录等核心职责。

Backend Core 目标是支持 Windows 和 Linux 原生 Python 运行。当前项目不再维护 Docker
运行路径；WSL 可以作为可选运行环境，但不应成为 Windows 支持的前提。

这个文档既描述当前已实现的后端骨架，也保留项目的中长期愿景，方便后续分阶段落地。

---

## 2. 核心职责

Backend Core 目前只负责五个基础动作：

```text
HTTP API
  ↓
Workspace Index
  ↓
Platform Path / Filesystem Adapter
  ↓
Workspace File Structure Index
  ↓
SQLite 持久化
  ↓
Static Capability Catalog
```

当前阶段先把服务、索引和能力目录做稳，不再保留基于规则的任务规划或执行闭环。

Workspace Index 同时需要记录 workspace 的结构化元数据和文件构成索引。文件构成索引包括目录层级、文件名、扩展名、路径位置、README、配置文件、测试命令和其他高价值元数据，用于在不依赖向量检索时提升效率和准确率。

邮件优先 MVP 中，Agent Harness 的执行边界必须保持清晰：

```text
HTTP API
  ↓
Agent Loop / Session
  ↓
Tool Package Registry
  ↓
Tool Executor
  ↓
Mail Tools
  ↓
MailService / SQLite
```

`MailService` 只提供确定性的存储、查询、加载和持久化方法，不调用 LLM，不选择执行步骤。
LLM 推理、工具选择、观察工具结果和反馈循环属于 Agent Loop。
第一版通用 Agent turn 使用单次查询内的 step-limited harness：先选择 Tool Package，
再展开具体工具，然后反复执行 `decision -> tool call -> observation`，直到 Agent
给出最终回答或达到步数上限。每一步只能调用一个工具或回答，避免在领域服务里隐藏
多步骤自动化。
ReAct 过程中可以通过 `expand_package` decision 继续展开其他 Tool Package；第一次 route
只决定起始 package，不应把整个 turn 锁死在单一领域工具里。典型邮件事务流程应是先展开
`mail` 检索 / 加载证据，再展开 `matter` 调用 `matter.create` 或 `matter.create_many`
写入独立事务。
Tool Executor 在真正执行工具前会基于 Tool Registry 中的 `input_schema` 做统一校验，当前
支持必填字段、基础类型、数组元素类型、嵌套 object、枚举值和最小数值。校验失败时工具不会
执行，而是返回 `status=rejected`、`validation_errors` 和本地失败反馈；Agent Loop 会把这类
结果作为 observation 反馈给 LLM，让其修正参数或改选其他工具。
Agent Harness core 必须保持 package / domain agnostic。`app/core/` 的 prompt 和 fallback
逻辑不能写死具体 package 名、工具名、领域流程、路由关键词或工具调用示例；具体领域策略必须
放在 `app/tool_packages/` 的 package metadata、tool description、input/output schema、
routing hints、decision hints 和 cache policy 中。Agent Loop 可以把 Tool Registry metadata
注入 prompt，但不能在 core 里重新编码某个 package 的调用顺序、读写边界或跨 package 工作流。
decision 输出必须区分自然语言和内部操作，并采用 operation-first envelope：`operation`
是唯一控制通道，保存 `tool_call` / `final_answer` / `request_confirmation` 等结构化动作；
`assistant_message` 只保存用户可见的过程说明，不能选择工具，也不能补成最终回答。
`decision.operation.type == "final_answer"` 只表示证据已足够、可以进入独立 `answer`
stage；`operation.final_answer` 即使存在也不能作为最终用户答案返回。真正展示给用户的
最终自然语言回答只能来自独立 `answer` 或 `context_answer` LLM stage。这样工具调用中途的
模型文本可以被展示和记录，但不会和真实执行动作或最终回答混在同一个字段里。
如果 LLM 判断当前 turn 不需要展开任何 Tool Package，Agent Loop 仍应允许 LLM 基于当前
session context window 直接回答；“不需要工具”和“系统无法处理”不能混为一谈。
当 provider 返回不完整 JSON 但明确选择了某个 package 时，Agent Loop 可以做保守恢复，
并在 run log 中保留原始输出，避免模型格式问题直接破坏工具链路。
如果 decision 阶段返回非 JSON 的自然语言文本，Agent Loop 必须记录为
`invalid_plain_text_decision`，不能将其恢复为最终回答。Agent Loop 应先做一次格式重试，
在重试 prompt 中带上上一条 plain-text 输出并强调必须返回 operation-first JSON。若重试仍
失败但本轮已经通过工具读取到足够证据，Agent Loop 可以进入独立 `answer` 阶段让 LLM 基于
观察结果重新生成自然语言回答；否则应停止本轮工具执行，并返回可追踪的结构化决策失败提示。
如果 decision 阶段返回的非 JSON 内容疑似工具调用，例如包含 `tool_name`、`tool_input`
或 `tool_call`，Agent Loop 必须 fail closed：先尝试 `decision_repair` 修复为合法
operation，修复失败则停止本轮执行，不能把工具调用残片当作最终 answer。
每次真实工具调用后都必须生成反馈 observation。反馈至少包含执行成功 / 失败状态和可读
message；当 LLM 可用时，还要追加独立 `tool_result_check` 调用，让 LLM 检查工具结果是否
符合上一条 tool-call decision。底层 `ToolResult.status` 已失败时，反馈不能被升级为成功。
工具包可以按自身 metadata 或工具输出提供额外 summary，但这不是 Agent core 的领域特判。
Agent core 只能把工具返回的原始结果、通用反馈和 package metadata 交给 LLM；如果某个领域
需要状态计数、去重提示、读写边界或业务摘要，应由对应 Tool Package 或 domain service 生成。
Agent Loop 还会由本地 harness 生成 `progress_events`，用于前端展示用户友好的运行过程：
package 选择、LLM 的 `assistant_message`、package 展开、工具开始 / 完成、工具反馈、最终回答
和校验 warning 都会进入该事件流。`progress_events` 不参与后续上下文窗口压缩，也不由 LLM
生成，避免把运行流水混入对话记忆。
HTTP stream endpoint 使用 SSE 输出统一的 Agent Run event，不维护另一套事件模型。stream
输出会用 `stream_part` 标注 lifecycle、progress、tool_result、llm_audit、llm_delta 和
final_answer 等结构。`POST /agent/turn/stream` 默认使用 stream response mode，Agent
runtime 中所有 LLM stage 都通过 `LLMService.stream()` 接收 provider token delta，并作为
`llm_delta` 事件发送。route、decision、decision_repair、tool_result_check 等 JSON stage
只流式接收和审计 token，必须累计完整输出后再解析 JSON 和执行工具。自然语言输出 stage
会标记为 `assistant_answer`，其他 LLM stage 会标记为 `agent_process`。前端应只把
`llm_delta.payload.display_target == "assistant_answer"` 的 delta 当作最终回答实时输出；
`final_answer` event 只作为最终校准 / 补全事件。
最终回答会经过一层本地轻量校验，结果写入 `verification_warnings`。当前校验只记录 warning，
不自动改写答案；例如模型声称已经写入日历但本轮没有 calendar tool 完成时，会标记
`unsupported_calendar_claim`，供前端和后续 verifier 使用。

会话基础设施当前由本地 SQLite 管理，使用显式 `session_id` 支撑平行会话和多轮会话。
后端不维护隐式全局当前会话；前端切换会话时必须把目标 `session_id` 传给运行入口。
Session Service 只负责创建会话、追加消息、读取历史和更新时间，不调用 LLM，也不选择工具。
每个 session 还维护一个本地 context window，默认预算为 `65536` token。窗口只保存
前文摘要和近期 user / agent 问答，用于下一轮 prompt 注入；完整工具结果、LLM prompt /
output、错误和运行过程保留在 `data/agent_logs/`，必要时再通过日志或历史检索恢复。
窗口未满时不触发摘要；超过预算时由独立 `context_summarize` LLM 调用重新生成 summary，
并只保留最近两条 user / agent 消息原文。
Agent Loop 会把同一 session 中已加载过、且被 Tool Package metadata 标记为可缓存的本地
资源作为 session-scoped resource cache 重新注入后续 turn。缓存以通用
`cached_tool_observations` 形式暴露给 route / decision prompt；后续追问如果缓存已经足够，
Agent 应直接基于缓存回答，或把缓存作为已有 observation 使用，而不是重复调用等价工具。
缓存归 harness 管理，工具仍保持一次性、无会话状态；哪些工具结果可缓存由 Tool Package
metadata 决定，不能在 Agent core 中写死。
邮件数据源是全局本地知识源，不存在独立的“邮件会话引擎”。Mail tools 只在某个 Agent turn
中按当前 `session_id` 读取一次性信息并返回观察结果；是否把用户输入、工具观察、`run_id`
或 `log_path` 写入会话历史，必须由通用 Agent turn / Session 层显式决定，邮件工具和
`MailService` 不应自动写会话消息。
远程邮箱同步由 runtime 管理，不属于独立邮件 Agent。服务启动时 runtime 可以执行一次
Outlook 自检同步，把最新邮件写入本地 SQLite；服务运行期间可以通过后台轮询继续同步。
按需同步则暴露为 `mail.sync` 工具，由 Main Agent Brain 在用户要求“最新/同步/当前邮箱”
或本地邮件可能过期时选择调用。无论触发来源是 startup、background、API 还是 tool，
同步过程都只负责更新本地邮件知识源，后续检索、加载、概括和事务整理仍通过 Agent Loop
中的 mail tools 完成。
事务管理是独立 domain service，不从属于邮件。`MatterService` 负责 `matter` 的本地
持久化、关键词检索、列表、状态更新和来源链接；`mail`、`agent_trace`、`file`、后续
`calendar_event` 等都只是 `matter_source_links` 中的 source。Agent 从邮件中提取待办
时，应先通过 mail tools 获取证据，再通过 matter tools 创建或更新事务，不能让
`MailService` 直接替用户做事务决策。
Agent 可见的 matter tool schema 必须明确必填字段、枚举和示例。写入类工具只能使用
`open`、`in_progress`、`waiting`、`done`、`cancelled` 作为 status，只能使用 `low`、
`normal`、`high`、`urgent` 作为 priority。创建事项前如果存在重复风险，Agent 应先调用
`matter.search` 检查已有事项；发现相似事项时，应选择 update、skip/no_op，或明确说明为何
它是独立新事项后再 create。
旧的 `mail.persist_matters` / `mail_matters` 是邮件优先 MVP 早期遗留能力，只作为历史
数据和迁移路径保留，不再注册进 Agent 可见的 Tool Registry。
runtime context 也通过工具和上下文双路径提供：`runtime.now` 是可调用工具；每次
Agent turn 还会把 `current_time` 注入 session context window，包含 UTC、本地时间、
时区和日期，用于处理“今天”“明天”“8月5号之后”这类相对时间。

每次 Agent Loop 运行都必须生成本地自然语言友好的 run log。run log 由代码模板生成，
不调用 LLM，至少记录：

- 运行时间、`run_id`、`session_id` 和用户输入。
- 第一层 Tool Package catalog 和实际展开的 package。
- 本轮使用的 session context window 快照。
- Agent 每一步 decision，包括 action、assistant_message、operation、reason、选中的
  tool、tool input 或最终 answer。
- 每个 tool 的选择时间、输入、输出、状态、错误和反馈。
- 本地生成的 progress events 和最终回答 verification warnings。
- 给 LLM 的完整 system prompt、user prompt 和 LLM 完整输出。
- 最终结构化结果。

这些日志默认写入 `data/agent_logs/`。由于日志可能包含完整邮件正文和个人信息，
它们必须保持本地持久化，不应进入 git，也不应默认同步到外部服务。

LLM provider 的失败必须进入 run log。HTTP `429` 限流应被单独识别为
`rate_limited`，记录 `status_code`、`retry_after`、完整 prompt 和错误输出，并由
Agent Loop 按 `Retry-After` 或本地默认等待时间重试；重试耗尽后再降级到本地 heuristic
或其他后备策略，而不是让领域服务自行处理；对于没有明确后备策略的工具执行阶段，应停止
继续调用工具并返回可追踪失败提示。认证失败、网络失败、超时、非 429 HTTP 错误和 provider
响应解析失败也必须拆分记录，便于后续调试和策略调整。
每次 LLM 调用还会生成结构化审计记录，当前落点是 `AgentTurnResult.llm_events`、
Agent Run event payload 和 markdown run log。审计字段包括 `llm_call_id`、`run_id`、
`trace_id`、`session_id`、stage、client、provider、model、response mode、状态、耗时、
HTTP status、provider request id、`Retry-After`、OpenAI / OpenAI-compatible error body
字段、canonical `error_category`、`is_retriable`、finish reason、token usage、
content length、prompt summary 和隐私受控 metadata。完整 prompt / output 保留在本地
run log 中，SQLite `llm_calls` 表留到第二版再落地。

---

## 3. 推荐目录结构

```text
backend/
  pyproject.toml
  .env.example

  app/
    api/
      main.py
      routes/
        health.py
        workspaces.py
        capabilities.py

    core/
      agent_turn.py
      llm.py
      runtime.py
      context.py
      retrieval.py
      runtime_loop.py
      tools.py
      sessions.py
      runtime_context.py
      tracing.py

    domains/
      mail.py
      matters.py

    integrations/
      outlook.py

    tool_packages/
      mail.py
      matter.py
      runtime.py

    platform/
      base.py
      detect.py
      paths.py
      filesystem.py

    storage/
      db.py
      models.py
      vector_store.py
```

### 说明

- 上面这份结构是“目标结构”，不是当前实现的全部内容。
- 当前仓库只实现了其中很小一部分，但目录规划保留了后续演进路径。
- 这份结构的价值在于：它让后续扩展不会每次都重新发明分层方式。
- `app/core/` 只放 Agent Harness 核心结构、LLM 调用、会话 / 上下文、安全校验 / trace
  等通用运行时能力；具体领域服务和工具包实现不放在 `core`。
- `app/domains/` 放确定性领域服务与领域数据模型，例如邮件和事务。领域服务不调用 LLM，
  不选择工具，不隐藏多步骤 agent 行为。
- `app/tool_packages/` 放 Agent 可见的工具包适配层，例如 `mail`、`matter`、`runtime`。
  这些模块可以依赖领域服务和核心 tool 协议，但不应把具体工具实现放回 `core`。
- `app/integrations/` 放 Outlook / Graph 等外部 provider 接入，避免 provider 代码污染
  Agent Harness 核心层。
- `app/platform/` 是跨平台边界，用于集中处理路径、文件系统和后续命令执行差异。

### 存储与检索约定

- SQLite 是当前 MVP 的主存储方案，用于 workspace 索引元数据、任务记录、trace、confirmation 和其他结构化状态。
- Qdrant 只作为可选语义检索扩展，不作为 MVP 必需项。
- 非向量检索应优先依赖 workspace 的结构化索引、文件构成索引和本地文件系统检索。
- Workspace 路径必须先经过 `PathResolver`，文件扫描必须优先经过 `FilesystemScanner`。
- 后续本地命令、专家工具、测试命令应经过统一 `CommandRunner`，避免业务代码写死 shell。
- Capability Registry 第一层应优先暴露 Tool Package，而不是一次性暴露所有具体工具。
- 具体工具应在 Agent 决定展开某个 package 后再进入上下文。
- 不维护全局 intent 枚举；Agent 应输出面向下一步执行的 routing / execution decision。

---

## 4. 核心接口

```python
class LocalKnowledgeAgentRuntime:
    def index_workspace(self, workspace: str, options: dict | None = None) -> dict:
        ...

    def list_capabilities(self) -> list[dict]:
        ...
```

### 设计意图

- `index_workspace`：把 workspace 变成可检索、可统计、可追踪的知识入口。
- `list_capabilities`：返回当前对外公开的静态能力目录。

---

## 5. 后端模块优先级

### P0

- FastAPI skeleton
- SQLite
- workspace index
- static capability catalog
- local-only binding
- native Windows/Linux path resolution
- cross-platform read-only filesystem scanning

### P1

- KnowledgeObject
- TaskContext
- retrieval
- Capabilities registry redesign
- Native Skills 接口抽象
- CommandRunner 平台抽象

### P2

- SubAgent abstraction
- organize_files
- analyze_repo
- delegate_to_coding_agent manual mode
- Verifier diff/test
- Claude/Codex CLI 自动调用
- SSE/WebSocket 任务流
- Skill Evolution proposal

---

## 6. 当前实现与愿景的关系

当前代码实现的是一个轻量骨架，主要覆盖：

- Health Check
- Workspace index 的基础统计
- Capability 列表返回
- Runtime Debug 结构化链路
- SQLite trace / runtime event 持久化
- 平台识别、workspace 路径解析和只读文件扫描

这意味着：

- 你在愿景里定义的模块没有丢，它们是后续的目标结构。
- 当前实现只是把最基础的 HTTP 服务和索引先跑起来，避免一开始就陷入复杂度。
- 未来可按 P0 / P1 / P2 逐步增加真正的知识层和能力层。

---

## 7. 设计原则

- 先有最小闭环，再补智能能力。
- 先把服务、索引、能力目录打牢，再做更复杂的 agent 协作。
- 先保留显式能力注册，再考虑自动选择和自动调度。
