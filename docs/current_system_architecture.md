# 当前系统业务逻辑与代码架构说明

本文档记录 Local Knowledge Agent OS 当前已经实现的业务处理逻辑、模块分层、代码结构、运行链路和已知边界。它描述的是当前代码真实状态，不是远期理想架构。

最后更新日期：2026-08-14

---

## 1. 系统当前定位

Local Knowledge Agent OS 当前已经从“邮件问答 Demo”推进到一个最小可运行的本地 Agent Harness。

当前系统的核心目标是：

1. 通过本地持久化数据管理用户知识，当前主要是邮件和事务。
2. 用一个主 Agent turn loop 处理用户单次输入。
3. 让 LLM 自主选择工具包、调用工具、读取结果、继续决策，并最终回答。
4. 把每次运行的完整过程写成本地自然语言友好的运行日志。
5. 支持多会话、多轮上下文窗口、会话切换。
6. 为后续更强的 harness、risk control、skill evolution、多 agent 架构保留扩展边界。

当前最重要的架构原则是：

- 邮件不是一个独立 Agent。
- 邮件能力只以工具包形式暴露给主 Agent。
- 邮件服务只负责数据读取、同步、搜索、加载，不负责调用 LLM。
- 事务系统独立于邮件系统。
- LLM 决策和工具执行统一发生在 `AgentTurnLoop` 中。
- 工具结果必须回传给 LLM，LLM 根据 observation 继续下一步。
- 运行过程不进入会话上下文，只写入本地日志。
- 会话上下文只保留用户输入和 Agent 最终回答。

---

## 2. 当前业务功能总览

### 2.1 已实现能力

当前后端已经实现以下能力：

| 能力 | 当前状态 | 入口 |
| --- | --- | --- |
| 健康检查 | 已实现 | `GET /health` |
| workspace 索引调试 | 已实现基础版 | `POST /workspaces/index` |
| 能力列表 | 已实现 | `GET /capabilities` |
| legacy runtime debug loop | 已保留 | `POST /runtime/debug` |
| 主 Agent turn | 已实现 | `POST /agent/turn` |
| 会话创建/列表/详情/追加消息 | 已实现 | `/sessions` |
| 邮件导入 | 已实现 | `POST /mail/import` |
| 邮件搜索 | 已实现 | `GET /mail/search` |
| Outlook Device Code 登录 | 已实现 | `/mail/outlook/auth/start`, `/mail/outlook/auth/complete` |
| Outlook 邮件同步 | 已实现 | `POST /mail/outlook/sync` |
| 邮件本地搜索工具 | 已实现 | `mail.search` |
| 邮件原文加载工具 | 已实现 | `mail.load_messages` |
| 邮件同步工具 | 已实现 | `mail.sync` |
| 独立事务创建 | 已实现 | `matter.create`, `POST /matters` |
| 批量事务创建 | 已实现 | `matter.create_many` |
| 事务搜索/列表 | 已实现 | `matter.search`, `matter.list`, `/matters/search`, `GET /matters` |
| 事务更新 | 已实现 | `matter.update`, `PATCH /matters/{matter_id}` |
| 事务来源链接 | 已实现 | `matter.link_source`, `POST /matters/{matter_id}/source-links` |
| 显式文件读取 | 已实现 | `filesystem.read_file` |
| 显式文件编辑 | 已实现 | `filesystem.edit_file` |
| 实时时间上下文 | 已实现 | `session_context_window.current_time` |
| 多轮会话 | 已实现基础版 | `SessionService` |
| 会话切换 | 已实现基础版 | 显式 `session_id` |
| 上下文窗口 | 已实现，默认 65536 token 估算预算 | `SessionService.get_context_window` |
| 上下文压缩 | 已实现基础版，窗口超预算时触发 LLM 摘要 | `record_context_exchange` |
| 工具观察会话缓存 | 已实现基础版 | 从 Tool Package metadata 标记的历史工具事件恢复 |
| LLM 错误分类与重试 | 已实现基础版 | `app/core/llm.py`, `AgentTurnLoop._complete_text_with_retry` |
| 工具输入校验 | 已实现 | `ToolExecutor._validate_input` |
| 工具反馈机制 | 已实现基础版 | 本地输出协议校验，异常时 `tool_result_check` |
| 最终回答校验 | 已实现基础版 | `AgentTurnLoop._verify_final_answer` |
| Markdown 运行日志 | 已实现 | `data/agent_logs/*.md` |
| Windows 桌宠前端适配 | 已在外部前端目录完成 | `/mnt/d/agent-bot-frontend` |

### 2.2 当前未实现或只做了基础版的能力

| 能力 | 当前状态 |
| --- | --- |
| 强制确认门 | 未完成 |
| 高风险工具权限策略 | 未完成 |
| 严格 JSON-only LLM 输出拒收 | 未强制 |
| 工具结果分页/压缩 | 未完成 |
| Agent 主动检索历史运行日志 | 未完成 |
| embedding / semantic search | 已实现 FastEmbed + sqlite-vec V1；无索引时保守降级关键词检索 |
| 自动邮件触发 LLM 处理 | 未完成 |
| skill 形成与沉淀 | 未完成 |
| planner / brain 多 agent 分层 | 延后 |
| multi-agent 协同执行 | 延后 |
| 真正的事务提醒调度器 | 未完成 |
| 日历集成 | 未完成 |
| 数据库直接暴露给 LLM 查询 | 未开放 |

---

## 3. 代码目录分层

当前代码按以下层次组织：

```text
app/
  api/                 FastAPI HTTP 层
    main.py            应用创建、router 注册、runtime 生命周期
    schemas.py         HTTP request/response schema
    routes/            各业务路由

  core/                Agent Harness 核心、LLM、会话、上下文、trace、配置
    agent_turn.py      当前主 Agent turn loop
    runtime.py         后端运行时组装器
    tools.py           工具协议、工具包、注册表、执行器
    sessions.py        会话与上下文窗口服务
    llm.py             LLM client、错误分类、provider 调用
    runtime_context.py 当前时间等确定性运行时上下文 helper
    local_config.py    config/local.toml 解析
    config.py          应用设置
    runtime_loop.py    legacy debug loop
    tracing.py         legacy trace recorder
    retrieval.py       workspace debug retrieval
    context.py         legacy context models

  domains/             确定性领域服务和领域数据模型
    mail.py            邮件领域服务
    matters.py         独立事务领域服务

  integrations/        外部 provider 接入
    outlook.py         Microsoft Graph / Outlook 同步服务

  tool_packages/       Agent 可展开的工具包实现
    mail.py            邮件工具适配层
    matter.py          事务工具适配层
    runtime.py         运行时工具，例如当前时间

  platform/            本地平台与文件系统抽象
    detect.py
    filesystem.py
    paths.py
    base.py

  storage/
    db.py              SQLite schema、连接、轻量迁移

docs/                  项目文档
scripts/               开发与 smoke test 脚本
tests/                 pytest 测试
```

最核心的当前运行链路涉及这些文件：

```text
app/api/main.py
  -> app/core/runtime.py
    -> app/core/agent_turn.py
      -> app/core/tools.py
      -> app/tool_packages/mail.py
      -> app/tool_packages/matter.py
      -> app/tool_packages/runtime.py
      -> app/core/sessions.py
      -> app/core/llm.py
      -> app/domains/mail.py
      -> app/domains/matters.py
      -> app/storage/db.py
```

---

## 4. HTTP 层

### 4.1 应用入口

文件：`app/api/main.py`

职责：

1. 创建 FastAPI app。
2. 加载 `Settings`。
3. 创建 `LocalKnowledgeAgentRuntime`。
4. 注册所有 router。
5. 在服务启动时调用 runtime startup hook。
6. 在服务关闭时停止后台同步线程。

HTTP 层不直接实现业务逻辑，只把请求转交给 runtime 或 domain service。

### 4.2 请求和响应模型

文件：`app/api/schemas.py`

核心 schema：

- `AgentTurnRequest`
  - `session_id: str | None`
  - `user_input: str`
- `AgentTurnResponse`
  - 继承 `AgentTurnResult`
- `SessionCreateRequest`
- `SessionAppendMessageRequest`
- `MailImportRequest`
- `OutlookSyncRequest`
- `MatterCreateRequest`
- `MatterUpdateRequest`
- `MatterLinkSourceRequest`

这个文件只定义 API contract，不做业务判断。

### 4.3 主 Agent API

文件：`app/api/routes/agent.py`

入口：

```text
POST /agent/turn
```

请求体：

```json
{
  "session_id": "optional-session-id",
  "user_input": "用户输入"
}
```

处理逻辑：

1. FastAPI 校验请求体。
2. route 获取 runtime。
3. 调用 `runtime.run_agent_turn(...)`。
4. 返回完整 `AgentTurnResult`，包括：
   - `answer`
   - `trace_id`
   - `selected_package`
   - `package_catalog`
   - `session_context_window`
   - `expanded_tools`
   - `decision_events`
   - `tool_events`
   - `progress_events`
   - `verification_warnings`
   - `llm_events`
   - `log_path`

前端右键菜单和浏览器交互最终都应该调用这个接口。

---

## 5. Runtime 组装层

### 5.1 入口类

文件：`app/core/runtime.py`

类：`LocalKnowledgeAgentRuntime`

这是当前后端的对象组装中心。它不应该成为 Agent 决策逻辑本身，而是负责把服务、工具、LLM、数据库和后台任务连接起来。

初始化过程：

1. 读取 `Settings`。
2. 计算 SQLite 路径：
   - `data/lka.sqlite3`
3. 调用 `init_db(...)` 初始化数据库表。
4. 检测当前平台。
5. 创建 path resolver 和 filesystem scanner。
6. 创建核心领域服务：
   - `SessionService`
   - `MailService`
   - `MatterService`
   - `OutlookService`
7. 加载 `config/local.toml`。
8. 创建 `ToolRegistry`。
9. 注册工具包：
   - `mail`
   - `matter`
   - `runtime`
10. 注册具体工具：
    - `mail.search`
    - `mail.load_messages`
    - `mail.sync`
    - `matter.create`
    - `matter.create_many`
    - `matter.search`
    - `matter.list`
    - `matter.update`
    - `matter.link_source`
    - `filesystem.read_file`
    - `filesystem.edit_file`
11. 创建 `ToolExecutor`。
12. 根据本地配置创建真实或 mock LLM client。
13. 创建 `AgentTurnLoop`。
14. 创建 legacy `RuntimeLoop`，供 `/runtime/debug` 使用。

### 5.2 启动时邮件同步

`LocalKnowledgeAgentRuntime.start()` 做两件事：

1. `_run_startup_mail_sync()`
2. `_start_background_mail_sync()`

启动同步行为由 `config/local.toml` 中 Outlook 配置控制：

- `enabled`：Outlook 同步总开关；关闭后 startup、background、HTTP API 和 `mail.sync`
  均不会访问远程 Outlook provider。
- `startup_sync_enabled`
- `background_sync_enabled`
- `sync_interval_seconds`
- `sync_folder`
- `sync_limit`
- `sync_max_pages`

如果 Outlook 未启用或启动同步关闭，runtime 会把 `last_mail_sync_result` 标记为 skipped。

### 5.3 后台邮件同步

后台同步通过 daemon thread 实现：

```text
_start_background_mail_sync
  -> _background_mail_sync_loop
    -> _sync_outlook_mail_safely(trigger="background")
```

同步失败不会让 API 服务崩溃，而是写入 `last_mail_sync_result`。

---

## 6. SQLite 本地持久化层

### 6.1 数据库入口

文件：`app/storage/db.py`

主要函数：

- `get_db_path(data_dir)`
- `connect(db_path)`
- `init_db(db_path)`

当前数据库默认路径：

```text
data/lka.sqlite3
```

### 6.2 表结构总览

当前 SQLite 表：

| 表 | 用途 |
| --- | --- |
| `workspaces` | workspace 索引调试记录 |
| `runtime_events` | legacy runtime event |
| `traces` | legacy debug trace |
| `agent_sessions` | Agent 会话 |
| `agent_session_messages` | 会话消息 |
| `agent_session_context_windows` | 每个 session 的上下文窗口 |
| `mail_accounts` | 邮件账户 |
| `mail_messages` | 邮件主表 |
| `mail_attachments` | 邮件附件元数据 |
| `mail_chunks` | 邮件正文分块 |
| `mail_messages_fts` | 邮件全文检索 FTS5 表 |
| `mail_matters` | 旧邮件事务表，保留但不作为当前主入口 |
| `mail_matter_links` | 旧邮件事务和邮件链接表 |
| `mail_sync_state` | Outlook 同步状态 |
| `matters` | 独立事务主表 |
| `matters_fts` | 独立事务 FTS5 表 |
| `matter_source_links` | 独立事务来源链接 |

### 6.3 关键数据边界

邮件本地化：

- 原始邮件写入 `mail_messages`。
- 附件元数据写入 `mail_attachments`。
- 正文分块写入 `mail_chunks`。
- 可检索文本写入 `mail_messages_fts`。
- Graph delta / next state 写入 `mail_sync_state`。

事务系统：

- 事务写入 `matters`。
- 可检索字段写入 `matters_fts`。
- 和邮件等证据来源的关系写入 `matter_source_links`。

会话系统：

- 会话元数据写入 `agent_sessions`。
- 用户消息和 Agent 最终回答写入 `agent_session_messages`。
- 摘要和 recent messages 写入 `agent_session_context_windows`。
- 工具运行细节不写入 recent messages，而是进入消息 payload 和 markdown log。

---

## 7. Agent Turn 业务主链路

### 7.1 入口

文件：`app/core/agent_turn.py`

类：`AgentTurnLoop`

方法：

```python
run(session_id: str | None, user_input: str) -> AgentTurnResult
```

这是当前真正的主 Agent loop。它负责：

1. 会话绑定。
2. 上下文读取。
3. 工具包路由。
4. 工具包展开。
5. LLM ReAct 决策循环。
6. 工具执行。
7. 工具反馈。
8. 最终回答。
9. 最终回答校验。
10. 上下文更新。
11. 运行日志写入。

### 7.2 单次请求完整流程

一次 `/agent/turn` 的完整业务流程如下：

```text
用户输入
  -> HTTP POST /agent/turn
    -> AgentTurnLoop.run
      -> ensure_session
      -> append user message
      -> get_context_window
      -> 注入 current_time
      -> 注入 cached_tool_observations
      -> 获取 package_catalog
      -> LLM route 选择一个工具包或 null
      -> 记录 package_selected/no_package progress
      -> 如果选中 package:
           展开该 package 的 tools
           进入 ReAct decision loop
             -> LLM 输出 operation
             -> 解析 operation
             -> 校验 tool 是否允许
             -> 校验 tool_input
             -> 执行 tool
             -> 记录 tool_started/tool_completed
             -> 生成 tool_feedback
             -> 把 observation 和 feedback 回传给 LLM
             -> 继续下一轮
           直到 final_answer 或轮数耗尽
         否则:
           LLM 直接基于 context 回答
      -> verify final answer
      -> record_context_exchange
      -> write markdown log
      -> append agent message
      -> 返回 AgentTurnResult
```

### 7.3 AgentTurnResult

`AgentTurnResult` 是当前调试和前端展示的核心响应对象。

字段：

- `session_id`
- `trace_id`
- `answer`
- `selected_package`
- `package_catalog`
- `session_context_window`
- `expanded_tools`
- `decision_events`
- `tool_events`
- `progress_events`
- `verification_warnings`
- `llm_events`
- `log_path`

前端“运行过程”展示主要来自：

- `progress_events`
- `decision_events`
- `tool_events`
- `llm_events`
- `log_path`

---

## 8. 工具包路由逻辑

### 8.1 为什么先选 package

当前系统不把所有工具一次性塞给 LLM，而是先只暴露粗粒度工具包：

- `mail`
- `matter`
- `runtime`

原因：

1. 降低 prompt 体积。
2. 降低工具选择噪声。
3. 后续可以把更多能力按 package / skill / plugin 组织。
4. 更贴近 harness 架构，而不是简单 function calling 列表。

### 8.2 route prompt

当前 route 阶段 system prompt 的核心语义是：

```text
You are the Main Agent Brain for Local Knowledge Agent OS.
Choose at most one tool package for the current turn.
Do not choose concrete tools yet.
Return only null when the provided session context window, including cached local resources,
is sufficient to answer without another tool call.
Return only strict JSON:
{"selected_package":"mail|matter|runtime|null","reason":"...","search_query":"..."}
Use mail for email evidence, matter for local tasks/events, and runtime for direct current-time questions.
```

route 阶段 user prompt 包含：

- `user_input`
- `session_context_window`
- `package_catalog`

### 8.3 route 输出

期望输出：

```json
{
  "selected_package": "mail",
  "reason": "User asks about email evidence.",
  "search_query": "NTUSO audition"
}
```

如果 LLM 不可用或 route 输出不可解析，系统会走本地 fallback：

- 邮件相关关键词命中 `mail`
- 事务相关关键词命中 `matter`
- 当前时间相关关键词命中 `runtime`
- 都不命中则 `selected_package = null`

这个 fallback 只用于提高可用性，不代表工具是 heuristic 自动命中的。只要真实 LLM 可用，决策来源会记录为 `source="llm"`。

---

## 9. Operation-first ReAct 决策循环

### 9.1 当前决策协议

工具包展开后，LLM 不应该先输出“我已经做了什么”。当前协议要求 LLM 先输出结构化 operation，再给可显示消息。

期望 JSON：

```json
{
  "operation": {
    "type": "tool_call",
    "package_name": null,
    "tool_name": "mail.search",
    "tool_input": {
      "query": "NTUSO audition",
      "limit": 8
    },
    "final_answer": null,
    "reason": "Need relevant local email evidence.",
    "confidence": "high"
  },
  "assistant_message": "正在搜索本地邮箱中与 NTUSO audition 相关的邮件。"
}
```

支持的 operation type：

| type | 用途 |
| --- | --- |
| `tool_call` | 调用一个已经展开并允许的工具 |
| `expand_package` | 请求展开另一个工具包，当前实现受限 |
| `final_answer` | 给出最终回答 |
| `request_confirmation` | 请求用户确认，当前未完整接入确认门 |
| `no_op` | 不执行工具，通常用于无法继续 |

### 9.2 为什么 assistant_message 不能驱动工具

`assistant_message` 只用于前端展示运行进度，不能作为控制信号。

控制信号只能来自：

```text
operation.type
operation.tool_name
operation.tool_input
operation.final_answer
```

这样避免以下问题：

- LLM 在自然语言中说“我正在读取邮件”，但实际没有调用工具。
- 前端把中间进度误当最终答案。
- 解析器从普通文本中误提取行为。

当前规则是：

- `operation.type=tool_call` 时执行工具。
- `operation.type=final_answer` 时必须读取 `operation.final_answer`。
- `assistant_message` 可以展示，但不能替代 final answer。

### 9.3 决策循环步骤

一次工具包内 ReAct loop 大致如下：

```text
step 1:
  LLM sees user_input + context_window + expanded_tools + observations[]
  LLM outputs operation=tool_call(mail.search)
  backend validates and executes mail.search
  backend records observation and feedback

step 2:
  LLM sees previous observation
  LLM outputs operation=tool_call(mail.load_messages)
  backend validates and executes mail.load_messages
  backend records full mail bodies and feedback

step 3:
  LLM sees loaded message bodies
  LLM outputs operation=final_answer
  backend returns final answer
```

当前最大决策步数：

```text
max_decision_steps = 10
```

该值可通过 `config/local.toml` 的 `[agent].max_decision_steps` 调整。

如果没有得到 final answer，系统会返回当前能形成的回答或错误状态。

---

## 10. 工具系统

### 10.1 工具协议

文件：`app/core/tools.py`

核心模型：

- `ToolPackageSpec`
- `ToolSpec`
- `ToolInvocation`
- `ToolContext`
- `ToolResult`

核心类：

- `ToolRegistry`
- `ToolExecutor`

### 10.2 ToolPackageSpec

`ToolPackageSpec` 描述一个工具包：

```python
class ToolPackageSpec(BaseModel):
    name: str
    description: str
    risk: str = "low"
    requires_expansion: bool = True
    tool_names: list[str] = Field(default_factory=list)
```

当前工具包：

```text
mail
matter
runtime
```

### 10.3 ToolSpec

`ToolSpec` 描述一个具体工具：

```python
class ToolSpec(BaseModel):
    name: str
    type: str
    description: str
    package: str | None = None
    risk: str = "low"
    requires_confirmation: bool = False
    read_only: bool | None = None
    side_effects: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
```

重要字段：

- `name`：工具名，例如 `mail.search`
- `package`：所属工具包
- `risk`：风险等级
- `requires_confirmation`：前端展示和兼容字段
- `read_only`：工具是否显式只读；`read_only != true` 必须进入 safety review
- `side_effects`：读写本地库、读远程邮件等副作用描述
- `input_schema`：后端执行前校验
- `output_schema`：给 LLM 和开发者理解返回结构

### 10.4 ToolExecutor 执行逻辑

`ToolExecutor.execute(...)` 流程：

```text
get_tool_or_none
  -> 未注册则 ToolResult(status="rejected")
validate_input
  -> 输入不合法则 ToolResult(status="rejected")
构造 ToolInvocation
调用 tool.invoke(...)
  -> 正常返回 ToolResult
  -> 异常捕获为 ToolResult(status="failed")
```

工具执行不会直接抛出到 Agent 主链路，而是统一归一成 `ToolResult`。

### 10.5 工具输入校验

当前支持的 schema 能力：

- `type`
- `required`
- `properties`
- `items`
- `allowed_values`
- `minimum`
- `object`
- `array`
- `string`
- `integer`
- `number`
- `boolean`
- `null`
- 联合 type，例如 `["string", "null"]`

输入不合格时：

```json
{
  "status": "rejected",
  "error": "Tool input failed schema validation.",
  "output": {
    "validation_errors": [...]
  }
}
```

---

## 11. Mail 工具包

### 11.1 工具包定义

文件：`app/tool_packages/mail.py`

```python
MAIL_PACKAGE = ToolPackageSpec(
    name="mail",
    description="Search, load, and sync local mail knowledge. Matter persistence lives in the matter package.",
    risk="low_to_medium",
    requires_expansion=True,
)
```

关键点：

- mail package 只处理邮件知识。
- matter persistence 不属于 mail package。
- 旧的 `mail.persist_matters` 类还在代码中，但 runtime 不注册它。
- 当前 Agent 可见邮件工具只有：
  - `mail.search`
  - `mail.load_messages`（仅精确查阅，最多三封）
  - `mail.sync`

### 11.2 mail.search

工具类：`SearchMailTool`

职责：

- 搜索本地知识库中 `source_type=mail_message` 的邮件证据。
- 复用知识库关键词、语义和混合检索，以及隐私过滤和 chunk 截断。
- 返回邮件元数据、受预算约束的正文证据片段和稳定溯源。

输入：

```json
{
  "query": "NTUSO audition",
  "limit": 8,
  "mode": "hybrid",
  "order_by": "relevance"
}
```

输出：

```json
{
  "query": "NTUSO audition",
  "messages": [
    {
      "message_id": "...",
      "subject": "...",
      "sender": "...",
      "received_at": "...",
      "snippet": "...",
      "document_id": "...",
      "chunk_id": "...",
      "source_ref": "mail_message:..."
    }
  ]
}
```

业务含义：

- 常规任务直接使用检索结果中的正文证据，不再把“候选元数据”和正文批量加载拆成两步。
- 空 query 加 `order_by=source_time_desc` 用于最新邮件列表。
- 仅在用户明确要求原文或精确引文且证据片段不足时才调用 `mail.load_messages`。

### 11.3 mail.load_messages

工具类：`LoadMailMessagesTool`

职责：

- 根据已知 `message_ids` 查阅少量邮件原文。
- 最多三封；Agent 后续 prompt 仍受通用 observation 总预算保护。

输入：

```json
{
  "message_ids": ["message_id_001"]
}
```

输出：

```json
{
  "messages": [
    {
      "message_id": "...",
      "subject": "...",
      "sender": "...",
      "recipients": [...],
      "cc": [...],
      "received_at": "...",
      "body_text": "...",
      "attachments": [...]
    }
  ]
}
```

业务含义：

- 这是 LLM 获取邮件完整内容的主要工具。
- 如果对应 Tool Package metadata 标记该工具结果可缓存，加载过的邮件会通过会话历史
  payload 在后续 turn 中被恢复为 `cached_tool_observations`，减少重复读取。

### 11.4 mail.sync

工具类：`SyncMailTool`

职责：

- 触发 Outlook 同步。
- 把远程邮件变化写入本地 SQLite。

输入：

```json
{
  "folder": "Inbox",
  "limit": 25,
  "max_pages": 1
}
```

输出包含：

- `provider`
- `folder`
- `imported_messages`
- `imported_attachments`
- `status`
- `sync_mode`
- `next_link`
- `delta_link`

业务含义：

- 这是一个有远程读取和本地写入副作用的工具。
- 当前 `requires_confirmation=False`，后续 risk control 应重新评估。

---

## 12. MailService 邮件领域服务

### 12.1 文件职责

文件：`app/domains/mail.py`

`MailService` 是纯邮件领域服务，不负责 LLM，不负责 Agent 决策。

它负责：

1. upsert 邮件账户。
2. 导入邮件。
3. 写入邮件附件元数据。
4. 写入正文 chunk。
5. 写入 FTS。
6. 搜索邮件。
7. 加载完整邮件。
8. 保留旧 mail matters 相关能力。

### 12.2 业务边界

`MailService` 可以做：

- 从数据库读邮件。
- 向数据库写邮件。
- 做 FTS 查询。
- 返回结构化邮件对象。

`MailService` 不应该做：

- 调用 LLM。
- 判断用户意图。
- 决定是否创建事务。
- 决定最终回答。
- 维护会话上下文。

这些都属于 `AgentTurnLoop` 和后续 harness 层。

---

## 13. Outlook 同步服务

### 13.1 文件职责

文件：`app/integrations/outlook.py`

`OutlookService` 负责 Microsoft Graph / Outlook 邮件同步。

当前支持：

- Device Code Flow 开始登录。
- Device Code Flow 完成登录。
- token 存储和刷新。
- 拉取邮件。
- 导入本地 `MailService`。
- 维护 `mail_sync_state`。

### 13.2 同步入口

HTTP 入口：

```text
POST /mail/outlook/sync
```

工具入口：

```text
mail.sync
```

runtime 启动入口：

```text
LocalKnowledgeAgentRuntime.start()
```

后台入口：

```text
_background_mail_sync_loop()
```

### 13.3 当前同步策略

当前策略是：

1. 服务启动时自检并同步最新邮件。
2. 服务运行期间可以按配置定时同步。
3. 用户请求中如果 LLM 判断需要更新邮件，也可以调用 `mail.sync`。
4. Graph 同步状态存储在 `mail_sync_state`。
5. 同步失败被记录为结构化状态，不直接杀死服务。

---

## 14. Matter 工具包与事务系统

### 14.1 事务为什么独立于邮件

用户事务不是邮件的附属物。

邮件只是事务来源之一。未来事务还可能来自：

- 手动输入
- 日历
- 文件
- 网页
- 聊天记录
- 系统通知
- 其他工具输出

因此当前系统把事务建模为独立 `matter` domain。

### 14.2 matter package

文件：`app/tool_packages/matter.py`

当前工具：

- `matter.create`
- `matter.create_many`
- `matter.search`
- `matter.list`
- `matter.update`
- `matter.link_source`

### 14.3 matter.create

用途：

- 创建单个独立事务。

输入关键字段：

- `title`
- `summary`
- `status`
- `priority`
- `due_at`
- `tags`
- `source_links`
- `metadata`

`status` 允许值：

```text
open, in_progress, waiting, done, cancelled
```

`priority` 允许值：

```text
low, normal, high, urgent
```

### 14.4 matter.create_many

用途：

- 在一个受控批次中创建多个事务。

适合场景：

- LLM 读完多封邮件后，提取多个确定日程。
- 用户要求整理一批待办。

### 14.5 matter.search / matter.list

用途：

- `matter.search` 用 FTS 搜索事务。
- `matter.list` 按 due date 和更新时间列出事务。

### 14.6 matter.update

用途：

- 更新已有事务。

可以更新：

- title
- summary
- status
- priority
- due_at
- tags
- metadata

### 14.7 matter.link_source

用途：

- 给事务补充证据来源。

例如从邮件创建事务后，添加：

```json
{
  "source_type": "mail_message",
  "source_id": "message_id_001",
  "reason": "Extracted from loaded local evidence."
}
```

---

## 15. MatterService 事务领域服务

文件：`app/domains/matters.py`

职责：

1. 创建事务。
2. 批量创建事务时由工具层循环调用创建。
3. 搜索事务。
4. 列出事务。
5. 更新事务。
6. 维护事务 FTS。
7. 创建 source link。

业务边界：

- `MatterService` 只做事务数据管理。
- 它不读取邮件。
- 它不调用 LLM。
- 它不判断要不要从邮件中创建事务。
- 从邮件证据到事务创建的决策由 `AgentTurnLoop + LLM + tools` 完成。

---

## 16. Runtime 工具包

文件：`app/tool_packages/runtime.py`

`AgentTurnLoop.run()` 每次都会把 `current_time_payload()` 注入 `session_context_window`。

这意味着 LLM 不需要显式调用工具，也能看到当前时间上下文。`runtime.now` 暂不注册为 Agent
可见工具，避免在当前邮件处理 MVP 中干扰工具路由。

---

## 17. 会话与上下文管理

### 17.1 文件职责

文件：`app/core/sessions.py`

核心服务：

```text
SessionService
```

职责：

1. 创建或确保 session。
2. 追加 user / agent message。
3. 查询 session list。
4. 查询 session detail。
5. 维护每个 session 的 context window。
6. 触发上下文摘要。

### 17.2 多轮会话

每次 `/agent/turn` 都可以传入 `session_id`。

如果传入已有 session：

- 新 user message 写入该 session。
- 读取该 session 的 context window。
- Agent 回答后，agent message 写回该 session。
- 后续追问可以看到该 session 的 summary 和 recent messages。

如果不传 session：

- 后端创建新 session。

### 17.3 会话切换

会话切换通过显式 `session_id` 实现。

不同 session 拥有：

- 独立消息列表。
- 独立 context window。
- 独立 recent messages。
- 独立 mail message cache 恢复范围。

因此一个 session 中加载过的邮件，不会自动污染另一个 session。

### 17.4 上下文窗口结构

当前上下文窗口包含：

- `session_id`
- `token_budget`
- `summary`
- `recent_messages`
- `token_estimate`
- `updated_at`
- 运行时注入的 `current_time`
- 运行时注入的 `cached_tool_observations`

默认 token budget：

```text
65536
```

当前 token 计算是估算值，不是 provider tokenizer 的精确值。

### 17.5 recent messages

之前曾使用过 `core_messages` 命名，后来已改为 `recent_messages`，避免“核心消息”语义歧义。

recent messages 只保存核心对话内容：

- 用户输入
- Agent 最终回答

不保存：

- LLM 完整 prompt
- LLM 中间输出
- tool result
- progress events
- run log

这些信息在本地日志和 message payload 中保留。

### 17.6 上下文压缩

当上下文窗口超过预算时触发摘要。

当前策略：

1. 保留最近两条消息。
2. 其余历史消息进入 summary 输入。
3. 调用独立 LLM summary stage。
4. 输出新的 summary。
5. 把 summary + retained recent messages 写回 `agent_session_context_windows`。

摘要 LLM 调用会记录在 `llm_events`，stage 通常是 context summary 相关阶段。

---

## 18. 会话内工具观察缓存

### 18.1 目的

之前发现一个问题：

用户追问时，前一轮已经通过某个工具加载过完整本地证据，后一轮仍然可能重复调用等价工具。

当前做法是：

- 不把完整运行过程写进 context。
- 但从当前 session 的历史 agent message payload 中恢复已被 Tool Package metadata
  标记为可缓存的工具观察。
- 将这些观察以 `cached_tool_observations` 注入当前 context window。

### 18.2 缓存边界

缓存是 session-scoped：

- 同一 session 可以复用。
- 不同 session 不共享。
- 缓存来源是历史工具事件，不是全局内存。

### 18.3 当前限制

当前缓存仍是基础版：

- 没有复杂失效策略。
- 没有按正文长度压缩。
- 没有精细引用计数。
- 如果上下文太大，后续需要 result paging / result compaction。

---

## 19. LLM 接入层

### 19.1 文件职责

文件：`app/core/llm.py`

职责：

1. 定义统一 LLM client protocol。
2. 实现 mock LLM。
3. 实现真实 text LLM client。
4. 从 `config/local.toml` 构造 provider client。
5. 识别不同 LLM 错误类型。

### 19.2 当前错误类型

当前代码中已经区分：

- `LLMClientError`
- `LLMRateLimitError`
- `LLMAuthenticationError`
- `LLMNetworkError`
- `LLMTimeoutError`
- `LLMProviderHTTPError`
- `LLMResponseParseError`

### 19.3 Agent 调用 LLM 的统一入口

`AgentTurnLoop` 中的 LLM 调用走：

```text
_complete_text_with_retry(...)
```

它负责：

1. 发起 LLM 请求。
2. 记录 `AgentTurnLLMEvent`。
3. 对 rate limit 等错误做等待后重试。
4. 对认证、网络、超时、HTTP、解析错误做分类记录。
5. 返回 `LLMResponse` 或 `None`。

当前生成 token budget：

```python
self.llm_generation_token_budget: int | None = None
```

即默认不在本地强行设置输出 token 上限，避免截断。

### 19.4 当前 LLM 调用阶段

一次 turn 中可能出现以下 LLM stage：

- `route`
- package 内 decision
- tool feedback check
- context summary
- answer from context

所有 prompt 和 output 都会进入 `llm_events` 和 markdown run log。

---

## 20. 工具反馈机制

### 20.1 为什么需要工具反馈

工具调用后，不能只把 raw result 扔回去就结束。当前系统会为每个工具调用生成反馈，让 LLM 知道：

- 工具是否真的成功执行。
- 结果是否符合调用意图。
- 是否找到数据。
- 是否被后端 schema 拒绝。
- 是否发生领域层面的空结果或失败。

### 20.2 反馈来源

当前有两层反馈：

1. 本地 deterministic feedback。
2. 可选 LLM checker feedback。

本地反馈函数会根据 `ToolResult.status` 产生基础判断。

LLM checker 用于更细地判断工具结果是否满足预期，但有一条重要规则：

```text
如果 ToolResult.status 不是 completed，LLM checker 不能把它升级为 accepted。
```

### 20.3 工具反馈 summary

Tool Package 或 domain service 可以在工具输出或反馈中提供领域 summary，帮助 LLM 区分：

- tool execution status
- business object status
- 对象数量
- 领域状态计数
- rejected 时的 validation errors

这类 summary 不由 Agent core 硬编码；如果某个 package 需要领域摘要，应由该 package
或 domain service 生成，再作为工具结果 / metadata 输入 Agent Loop。

---

## 21. 最终回答校验

### 21.1 文件位置

文件：`app/core/agent_turn.py`

函数：

```text
_verify_final_answer(...)
```

### 21.2 当前校验目标

当前校验是基础版，主要检查最终回答是否有明显不支持的声明。

例如：

- 没有调用事务写入工具，却声称已经创建事务。
- 没有调用日历工具，却声称已经写入日历。

如果发现风险，会生成 `AgentTurnVerificationWarning`，并写入：

- API response
- progress events
- markdown run log
- session agent message payload

---

## 22. 运行日志与 Trace

### 22.1 日志位置

当前每次 Agent turn 会生成 markdown 日志：

```text
data/agent_logs/agent_turn_xxx.md
```

### 22.2 日志内容

运行日志由后端脚本生成，不需要 LLM 介入。

日志应包含：

- trace id
- session id
- 用户输入
- package catalog
- session context window
- expanded tools
- 每次 LLM 调用的 system prompt
- 每次 LLM 调用的 user prompt
- 每次 LLM 输出
- route decision
- decision events
- tool started
- tool completed
- tool input
- tool result
- tool feedback
- progress events
- final answer
- verification warnings

### 22.3 日志和上下文的区别

运行日志是完整审计记录。

上下文窗口是给后续 LLM 对话使用的精简上下文。

二者不能混在一起：

- 日志越完整越好。
- 上下文越核心越好。

---

## 23. 典型业务链路示例

### 23.1 查询 NTUSO audition 要求

用户：

```text
搜索一下NTUSO的audition要求，我要怎么做？
```

期望链路：

```text
AgentTurnLoop.run
  -> route 选择 mail package
  -> 展开 mail tools
  -> LLM operation=tool_call(mail.search)
       input: {"query": "NTUSO audition", "limit": 8}
  -> ToolExecutor 校验 input
  -> SearchMailTool 调 MailKnowledgeMirror.search -> KnowledgeService.search
  -> 返回带正文证据片段和来源的邮件卡，例如 "Fw: NTUSO Audition"
  -> 工具反馈：找到了相关邮件
  -> LLM operation=final_answer
       final_answer: 总结 audition 要求、准备材料、建议
  -> verify final answer
  -> 写 context
  -> 写 run log
  -> 返回 response
```

### 23.2 查询 ICA student pass 进度

用户：

```text
帮我查看我的ICA相关的学生签证现在是什么进度，已经完成了什么，还需要做什么。
```

可能链路：

```text
route -> mail
mail.search("ICA student pass")
final_answer
```

如果相关事务已经被创建，后续追问也可能：

```text
route -> matter
matter.search("ICA student pass")
final_answer
```

这里取决于 LLM 根据上下文判断需要查邮件证据还是查已整理事务。

### 23.3 从邮件提取事务并写入

用户：

```text
搜索 NTU、ICA 等相关邮件，提取有确定时间的日程并加入事务。
```

合理链路：

```text
route -> mail
mail.search("NTU ICA")
mail.load_messages([...])
LLM 分析哪些内容有确定时间
expand_package 或后续选择 matter 工具
matter.create_many([...])
tool feedback
final_answer
```

当前系统对跨 package 的能力已经有方向，但仍需要继续强化，使 LLM 更自然地从 mail package 切到 matter package。

---

## 24. 当前前端交互链路

前端仓库不在当前后端目录内，位置是：

```text
/mnt/d/agent-bot-frontend
```

当前前端状态：

- Windows 侧运行。
- 后端仍在 WSL 中运行。
- Java Spine 桌宠使用正确的“真理”动画资源。
- 右键菜单已迁移浏览器聊天 UI 的主要功能。
- 右键菜单调用 WSL 后端 `/agent/turn`。
- JavaFX WebView 请求体丢失问题通过桌面 bridge 侧规避。
- 前端可展示多轮历史对话。
- 前端可展示运行过程。

启动脚本：

```text
D:\agent-bot-frontend\run-lka-windows.ps1
```

后端需要先在 WSL 启动，前端从 Windows 侧访问后端 HTTP。

---

## 25. 配置结构

### 25.1 示例配置

文件：

```text
config/local.example.toml
```

真实本地配置：

```text
config/local.toml
```

真实配置应被 git ignore，不提交 API key。

### 25.2 LLM 配置

当前 LLM 调用由应用级 LLM service 管理：

```text
Settings.load_local_config()
build_text_llm_client(self.local_app_config.llm)
  -> LLMClientRegistry
  -> LLMService
```

构造。

关键配置一般包括：

- named client
- provider type
- base_url
- api_key env
- default model
- available models
- timeout
- stream / JSON mode capability

旧的单 provider 配置仍保持兼容；新的配置可以声明多个 `llm.clients`。单次
`/agent/turn` 请求可以通过 `llm.client_name` 和 `llm.model` 覆盖默认选择，使 model
成为用户可切换选项，而不是后端代码里的固定值。

当前 LLM provider 调用已经通过 async service 执行；runtime 也提供 async agent turn 入口，
可把现有同步 turn 放入后台线程，避免阻塞 async 调用链。`LLMService` 已提供 stream event 抽象，但用户可见
SSE `/agent/turn/stream` 尚未实现。

### 25.3 Outlook 配置

Outlook 配置用于：

- 是否启用 Graph 邮件同步。
- Device Code Flow。
- 启动同步。
- 后台同步。
- 同步 folder、limit、max_pages。

---

## 26. legacy debug loop

当前还保留一个 legacy 调试链路：

```text
POST /runtime/debug
```

对应：

```text
app/core/runtime_loop.py
```

它使用：

- `ContextAssembler`
- `LocalDebugRetrievalProvider`
- `MockLLMClient`
- `MockToolExecutor`
- `TraceRecorder`

这个链路用于早期 scaffold 和 debug，不是当前主 Agent 入口。

当前主入口应该是：

```text
POST /agent/turn
```

---

## 27. 测试与验证

### 27.1 当前测试目录

```text
tests/
  test_agent_turn.py
  test_api_cors.py
  test_local_config.py
  test_mail_service.py
  test_matters.py
  test_outlook_service.py
  test_platform_support.py
  test_runtime_debug.py
  test_runtime_mail_sync.py
  test_sessions.py
```

### 27.2 常用验证命令

完整测试：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
```

lint：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check app tests scripts
```

CLI smoke：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/run_agent_turn.py \
  --session-id smoke_ntuso \
  "搜索一下NTUSO的audition要求，我要怎么做？"
```

JSON smoke：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/run_agent_turn.py \
  --session-id smoke_ntuso \
  --json \
  "搜索一下NTUSO的audition要求，我要怎么做？"
```

---

## 28. 当前架构边界与风险点

### 28.1 LLM 输出协议仍需继续收紧

当前已经采用 operation-first JSON envelope，但仍保留一定容错：

- 可以从带前后缀的输出中解析 JSON。
- strict JSON-only 违规还没有完全拒收。

后续建议：

- 对 decision stage 强制 JSON-only。
- 非 JSON 直接当作 invalid decision。
- 允许一次 format repair，但 repair 也必须只输出 JSON。

### 28.2 工具安全审查门

`ToolSpec` 已有：

- `risk`
- `requires_confirmation`
- `read_only`
- `side_effects`

当前已经有强制 safety review gate：

- 所有 Agent-visible 工具必须显式声明 `read_only`。
- `read_only != true` 的工具调用在执行前必须生成 safety review record。
- review mode 支持 `skip`、`llm`、`manual`。
- `skip` 仍会记录审查并自动通过。
- `llm` 由配置的 LLM 审查，通过可解析批准才执行。
- `manual` 会把 run 标记为 `waiting_confirmation`，前端通过
  `/agent/safety-reviews/{review_id}/decision` 放行或拒绝。
- `ToolExecutor` 也会拒绝未带 approved safety review context 的非只读工具，
  防止绕过 Agent 层直接执行写工具。

`request_confirmation` 仍是 LLM 决策输出类型，但真实执行控制以 runtime safety review gate
为准，不依赖 LLM 自觉请求确认。

### 28.3 跨 package 决策还需要强化

当前 route 阶段只选择一个 package。

实际复杂任务可能需要：

```text
mail.search -> mail.load_messages -> matter.create_many
```

这要求 Agent 在一个任务中跨 package 使用工具。当前已有 `expand_package` operation 方向，但还需要继续完善权限、上下文和工具集合更新逻辑。

### 28.4 工具结果太长时需要分页/压缩

邮件原文可能很长，多封邮件一起加载时容易撑大上下文。

后续需要：

- tool result paging
- message body chunk loading
- result summaries
- source citation
- 按需展开完整正文

### 28.5 邮件同步到自动处理尚未打通

当前可以：

- 启动同步。
- 后台同步。
- 手动同步。
- LLM 调用 `mail.sync`。

但尚未实现：

- 新邮件触发 Agent 自动归纳。
- 自动创建事务。
- 自动提醒。
- 风险确认策略。

### 28.6 Skill evolution 尚未开始

当前有工具包和 harness 雏形，但还没有：

- 历史任务模式归纳。
- skill 草稿生成。
- skill 验证。
- skill 注册。
- skill 调用。

---

## 29. 推荐下一阶段开发顺序

结合当前系统状态，建议优先级如下：

1. 强化单 Agent harness，而不是立刻做 multi-agent。
2. 完成跨 package tool loop，让一次任务可以自然地从 mail 切到 matter。
3. 加入 confirmation gate 和风险控制基础设施。
4. 实现 tool result paging / compaction，解决长邮件和多邮件读取。
5. 增加 trace/log 查询 API，让前端无需直接读文件。
6. 增加自动邮件同步后的处理队列，但先只做低风险摘要和候选事项。
7. 做事务提醒调度器。
8. 等基础设施稳定后，再引入 skill evolution。
9. 最后再考虑 planner / brain / sub-agent 多 agent 架构。

---

## 30. 当前系统一句话总结

当前系统已经具备一个最小但真实可用的本地 Agent Harness：FastAPI 接收用户输入，`AgentTurnLoop` 维护会话上下文并驱动 LLM operation-first 决策，工具注册表按 package 懒展开能力，工具执行器负责 schema 校验和失败归一，邮件和事务作为独立领域服务落到 SQLite，本地运行日志完整记录每次 LLM prompt/output、工具调用、反馈和最终回答。系统还不是完整自主桌面助理，但已经具备继续强化 harness、风险控制、自动同步处理和 skill evolution 的基础。
