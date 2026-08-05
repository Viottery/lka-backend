# Backend Core：Engineering Guide

## 1. 项目定位

Backend Core 是整个 Local Knowledge Agent OS 的核心执行层。它承载 HTTP API、
运行时调度、知识上下文构建、能力选择、技能执行、验证与轨迹记录等核心职责。

Backend Core 目标是支持 Windows 和 Linux 原生 Python 运行。Docker/WSL 可以作为
可选运行方式，但不应成为 Windows 支持的前提。

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
如果 LLM 判断当前 turn 不需要展开任何 Tool Package，Agent Loop 仍应允许 LLM 基于当前
session context window 直接回答；“不需要工具”和“系统无法处理”不能混为一谈。
当 provider 返回不完整 JSON 但明确选择了某个 package 时，Agent Loop 可以做保守恢复，
并在 run log 中保留原始输出，避免模型格式问题直接破坏工具链路。
如果 decision 阶段返回了非 JSON 的自然语言最终回答，Agent Loop 可以将其恢复为
`answer` decision，并记录原始输出，避免已有高质量回答被本地 fallback 覆盖。

会话基础设施当前由本地 SQLite 管理，使用显式 `session_id` 支撑平行会话和多轮会话。
后端不维护隐式全局当前会话；前端切换会话时必须把目标 `session_id` 传给运行入口。
Session Service 只负责创建会话、追加消息、读取历史和更新时间，不调用 LLM，也不选择工具。
每个 session 还维护一个本地 context window，默认预算为 `65536` token。窗口只保存
前文摘要和近期 user / agent 问答，用于下一轮 prompt 注入；完整工具结果、LLM prompt /
output、错误和运行过程保留在 `data/agent_logs/`，必要时再通过日志或历史检索恢复。
窗口未满时不触发摘要；超过预算时由独立 `context_summarize` LLM 调用重新生成 summary，
并只保留最近两条 user / agent 消息原文。
Agent Loop 会把同一 session 中已加载过的本地资源作为 session-scoped resource cache
重新注入后续 turn。以邮件为例，`mail.load_messages` 返回过的完整邮件正文会作为
`cached_mail_messages` 暴露给 route / decision prompt；后续追问如果缓存已经足够，
Agent 应直接基于缓存回答，或把缓存作为已有 observation 使用，而不是重复调用
`mail.search` / `mail.load_messages`。这类似代码 agent 读取本地文件后的上下文缓存：
缓存归 harness 管理，工具仍保持一次性、无会话状态。
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
runtime context 也通过工具和上下文双路径提供：`runtime.now` 是可调用工具；每次
Agent turn 还会把 `current_time` 注入 session context window，包含 UTC、本地时间、
时区和日期，用于处理“今天”“明天”“8月5号之后”这类相对时间。

每次 Agent Loop 运行都必须生成本地自然语言友好的 run log。run log 由代码模板生成，
不调用 LLM，至少记录：

- 运行时间、`run_id`、`session_id` 和用户输入。
- 第一层 Tool Package catalog 和实际展开的 package。
- 本轮使用的 session context window 快照。
- Agent 每一步 decision，包括 action、reason、选中的 tool、tool input 或最终 answer。
- 每个 tool 的选择时间、输入、输出、状态和错误。
- 给 LLM 的完整 system prompt、user prompt 和 LLM 完整输出。
- 最终结构化结果。

这些日志默认写入 `data/agent_logs/`。由于日志可能包含完整邮件正文和个人信息，
它们必须保持本地持久化，不应进入 git，也不应默认同步到外部服务。

LLM provider 的失败必须进入 run log。HTTP `429` 限流应被单独识别为
`rate_limited`，记录 `status_code`、`retry_after`、完整 prompt 和错误输出，并由
Agent Loop 按 `Retry-After` 或本地默认等待时间重试；重试耗尽后再降级到本地 heuristic
或其他后备策略，而不是直接退出或让领域服务自行处理。认证失败、网络失败、超时、非
429 HTTP 错误和 provider 响应解析失败也必须拆分记录，便于后续调试和策略调整。

---

## 3. 推荐目录结构

```text
backend/
  Dockerfile
  docker-compose.yml
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
      runtime.py
      context.py
      retrieval.py
      runtime_loop.py
      tools.py
      tracing.py

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
