# Local Knowledge Agent OS：HTTP API Contract

> 本文档定义 Backend Core 与 Windows/Linux Frontend 之间的最小 HTTP API 契约。
>
> 当前阶段保留基础服务能力，并新增第一版本地会话 API，用于支撑平行会话、
> 多轮消息记录和后续 session-scoped trace。

## Base URL

MVP 默认：

```text
http://127.0.0.1:8765
```

后端默认只绑定本机，不开放公网或局域网。
该默认值必须可通过配置覆盖，不应写死。

---

## 1. Health Check

```http
GET /health
```

响应：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "service": "local-knowledge-agent-os"
}
```

---

## 2. Index Workspace

```http
POST /workspaces/index
```

请求：

```json
{
  "workspace": "/home/chuan/Documents/NTU",
  "source_frontend": "linux-native",
  "options": {
    "recursive": true,
    "skip_hidden": true,
    "allow_symlinks": false,
    "max_files": 50000
  }
}
```

Windows 原生后端示例：

```json
{
  "workspace": "C:/Users/chuan/Documents/NTU",
  "source_frontend": "windows-native",
  "options": {
    "recursive": true,
    "skip_hidden": true,
    "allow_symlinks": false,
    "max_files": 50000
  }
}
```

响应：

```json
{
  "workspace_id": "ws_001",
  "status": "completed",
  "indexed_files": 128,
  "indexed_chunks": 934
}
```

### 设计说明

- 这是 workspace 索引的最小合同。
- 当前实现先统计文件数量和估算 chunk 数量，后续再接入真正的知识抽取与检索。
- 未来 workspace 索引还会补充文件构成索引，用于在不依赖向量检索时提升效率和准确率。
- `workspace` 表示后端进程可访问的本地路径。Windows 原生后端应传 Windows 路径，
  Linux 原生后端应传 POSIX 路径。
- Windows JSON 推荐使用 `C:/Users/...` 写法，避免反斜杠转义。
- 当前 `options` 支持 `recursive`、`skip_hidden`、`allow_symlinks`、`max_files`
  和 `sample_limit`。

---

## 3. List Capabilities

```http
GET /capabilities
```

响应：

```json
{
  "capabilities": [
    {
      "name": "mail",
      "type": "tool_package",
      "risk": "low_to_medium",
      "requires_confirmation": true,
      "read_only": false
    },
    {
      "name": "matter",
      "type": "tool_package",
      "risk": "low_to_medium",
      "requires_confirmation": true,
      "read_only": false
    },
    {
      "name": "filesystem",
      "type": "tool_package",
      "risk": "medium",
      "requires_confirmation": true,
      "read_only": false
    },
    {
      "name": "summarize_folder",
      "type": "native_skill",
      "risk": "low",
      "requires_confirmation": false
    },
    {
      "name": "claude_code",
      "type": "expert_tool",
      "risk": "medium",
      "requires_confirmation": true
    }
  ]
}
```

### 设计说明

- 能力清单是显式注册制，不是隐式能力发现。
- `native_skill` 与 `expert_tool` 的风险模型不同。
- `read_only` 表示该 capability 是否整体只读。任一工具 `read_only != true` 时，
  package 的 `requires_confirmation` 为 `true`。
- `requires_confirmation` 用于前端展示；运行时强制审查以具体工具的 `read_only`
  标签为准。

---

## 4. Runtime Debug Entry

```http
POST /runtime/debug
```

请求：

```json
{
  "session_id": "session_001",
  "workspace": "/home/chuan/Documents/NTU",
  "user_input": "Analyze this workspace for runtime debugging."
}
```

响应：

```json
{
  "trace_id": "trace_xxx",
  "session_context": {},
  "task_context": {},
  "events": [],
  "retrieval_result": {},
  "tool_invocation": {},
  "tool_result": {},
  "llm_response": {},
  "trace": {}
}
```

### 设计说明

- 这是 2.2 阶段的调试入口，用于验证运行时结构化链路。
- 当前实现只使用 mock/local provider，不调用真实 LLM，不执行真实文件修改工具。
- 该接口用于验证 `SessionContext -> TaskContext -> Retrieval -> Tool/LLM -> Trace`，
  不等同于后续 `/tasks/plan` 或 `/tasks/run`。

---

## 5. Agent Turn

```http
POST /agent/turn
```

请求：

```json
{
  "session_id": "session_001",
  "user_input": "帮我查询 NTUSO 的乐团考试相关要求",
  "llm": {
    "client_name": "packyapi",
    "model": "deepseek-v4-flash",
    "response_mode": "json"
  }
}
```

`llm` 是可选字段；不传时使用会话 / 配置默认值。`model` 是运行时可切换选项，不应由
后端代码写死。当前 `/agent/turn` 仍返回完整非流式结果；`response_mode=stream` 先作为
LLM service 层能力预留，用户可见 stream endpoint 后续单独提供。
每次 LLM 调用都会在响应的 `llm_events` 中带上 `llm_call_id`、provider / model、
response mode、耗时、usage、finish reason、provider request id、rate-limit headers、
错误分类和 retry 判断等审计字段。完整 prompt / output 仍只写入本地 markdown run log；
第一版暂不新增 SQLite `llm_calls` 表。

响应：

```json
{
  "session_id": "session_001",
  "run_id": "agent_run_xxx",
  "trace_id": "agent_turn_xxx",
  "answer": "我已调用本地 mail tools...",
  "selected_package": "mail",
  "package_catalog": [],
  "expanded_tools": [],
  "decision_events": [],
  "tool_events": [],
  "progress_events": [],
  "verification_warnings": [],
  "llm_events": [],
  "log_path": "data/agent_logs/agent_turn_xxx.md"
}
```

### 设计说明

- 这是第一版通用 Agent turn 入口，不是邮件专属 agent endpoint。
- 每次调用都会创建独立 Agent Run；`run_id` 用于后续 stream、取消、确认、重试和事件查询。
- Agent 第一层只读取 Tool Package catalog；可展开 package 和可调用工具来自 Tool Registry。
- 所有非只读工具调用都会先进入 safety review。审查模式由本地配置选择：
  `skip` 记录并自动通过、`llm` 调用 LLM 审查、`manual` 进入等待前端确认状态。
- Agent core 不硬编码具体 package 名、工具名、领域流程、路由关键词或工具调用示例。
  具体策略必须由 package metadata、tool description、input/output schema、routing hints、
  decision hints 和 cache policy 提供。
- 展开 package 后，Agent 会进入单次 turn 内的 step-limited loop：每一步生成一个
  `decision_event`，动作可以是调用一个工具、展开 package、请求确认，或进入最终回答阶段；
  工具结果作为 observation 进入下一步。
- `decision_event` 会同时记录 `assistant_message` 和 `operation`。`assistant_message`
  是用户可见的过程文本；`operation` 是内部结构化动作，例如
  `tool_call`、`expand_package`、`final_answer`、`request_confirmation` 或 `no_op`。
  `decision.operation.type == "final_answer"` 只表示后端应进入独立 `answer` stage，
  不能把 `operation.final_answer` 或旧格式 `answer` 当作最终用户答案。前端展示过程文本时
  可读取 `assistant_message`，不要把 decision JSON 当作用户最终回答展示。
- route 只决定起始 package；在 ReAct loop 中，如果已展开工具不足，LLM 可以输出
  `expand_package` 来展开另一个已注册 package。跨 package 工作流必须来自 registry
  metadata 和 tool schema，而不是 Agent core 特判。
- 如果 LLM 选择不展开 package，Agent 可以进入 `context_answer` 阶段，基于当前
  session context window 直接回答，不应把“无需工具”当成“无法处理”。
- 如果 LLM 返回不完整 JSON 但原始输出明确选择了某个已注册 package，Agent 会保守恢复该
  package 选择，并继续记录原始 LLM 输出以便回放。
- 如果 decision 阶段返回非 JSON 的自然语言文本，Agent 会记录
  `invalid_plain_text_decision`，不能把该文本直接恢复为最终回答。Agent 会先做一次格式
  重试，重试 prompt 会包含上一条 plain-text 输出并强调必须返回 operation-first JSON；
  若重试仍失败但本轮已经读取到足够工具证据，可进入独立 answer 阶段重新生成自然语言回答，
  否则停止继续工具调用并返回结构化决策失败提示。
- 如果 decision 阶段返回疑似工具调用的损坏 JSON，Agent 会先进入 `decision_repair`
  阶段尝试修复；修复失败时记录 `malformed_tool_call` 并停止执行，不会把该残片恢复为
  `answer`。
- 每个真实 `tool_event` 都包含 `feedback`。反馈至少包含执行成功 / 失败状态和可读
  message。Agent 会先做本地协议校验：工具声明 `output_schema` 时按该 schema 校验
  `ToolResult.output`；未声明时只检查 `ToolResult` 是完整 JSON 对象形状。只有工具执行
  失败、被拒绝、输出协议不匹配或本地无法确认时，才记录 `tool_result_check` LLM 事件。
- `progress_events` 是由 harness 本地代码生成的自然语言运行过程流，不调用 LLM。它会记录
  package 选择、模型过程文本、package 展开、工具开始 / 完成、工具反馈、最终回答和校验
  warning，供前端展示“系统正在做什么”。
- `verification_warnings` 是本地最终回答检查结果。当前只做 warning，不自动改写 LLM
  最终回答；领域级校验规则必须通过 package metadata 或独立 verifier 注册，不能在
  Agent core 中写死。
- 展开工具时，`expanded_tools[*].input_schema` 会尽量暴露 required fields、allowed values
  和 examples。LLM decision prompt 要求模型严格遵循这些 schema；具体枚举值、示例和
  领域约束由对应 Tool Package schema 提供。
- 每个 session 维护一个本地 context window，默认预算为 `65536` token。Agent prompt
  只注入前文摘要和近期 user / agent 问答；完整工具调用、LLM prompt/output 和运行过程
  保存在本地 run log，不进入后续 prompt。
- 每次 Agent turn 会把确定性的 `current_time` 注入 session context window，包含 UTC、
  本地时间、时区和当前日期；这让 LLM 能处理“今天/明天/8月5号之后”等相对时间。
- Agent 会从同一 session 的历史工具结果中恢复由 Tool Package metadata 标记为可缓存的
  本地资源。缓存以 `cached_tool_observations` 注入 context window；如果追问可以由缓存
  observation 回答，Agent 应直接回答或把缓存作为已有 observation 使用，不再重复调用
  等价工具。
- 如果某个 package 需要同步、刷新或跨 package 工作流，这些策略必须写在该 package 的
  metadata / tool descriptions / schemas 中，由 Agent core 注入 prompt 后让 LLM 决策。
- context window 未满时不做摘要；超过预算时触发独立 `context_summarize` LLM 调用，
  将旧 summary 和除最近两条消息外的历史问答重写为新 summary，原文只保留最近两条消息。
- 每次调用会显式追加 user / agent session message，并写入本地 markdown run log。
- run log 由代码模板生成，包含用户输入、package catalog、逐步展开工具、决策事件、
  工具调用输入输出、工具反馈、progress events、verification warnings、LLM 完整 prompt /
  output / 错误分类和最终回答。
- 未配置真实 LLM 或 LLM 决策失败时，Agent turn 不会用 package 专属 heuristic 伪造
  多步骤工具结果；它会记录本地 `answer` decision，说明运行时没有获得有效结构化决策。
  route 阶段仍可做保守本地 package 选择恢复，但工具执行阶段必须依赖合法 decision。

---

## 6. Agent Safety Reviews

```http
GET /agent/runs/{run_id}/safety-reviews
```

返回指定 run 下的审查记录：

```json
{
  "reviews": [
    {
      "review_id": "safety_review_xxx",
      "run_id": "agent_run_xxx",
      "session_id": "session_xxx",
      "trace_id": "agent_turn_xxx",
      "invocation_id": "tool_invocation_xxx",
      "tool_name": "filesystem.edit_file",
      "tool_input": {},
      "tool_risk": "medium",
      "side_effects": ["write_local_file"],
      "read_only": false,
      "mode": "manual",
      "reason": "Tool is explicitly non-read-only.",
      "status": "pending",
      "decided_by": null,
      "decision_reason": null
    }
  ]
}
```

```http
GET /agent/safety-reviews/{review_id}
```

返回单个审查记录。

```http
POST /agent/safety-reviews/{review_id}/decision
```

请求体：

```json
{
  "decision": "approve",
  "reason": "User approved this scoped local write.",
  "decided_by": "user"
}
```

`decision` 只能是 `approve` 或 `reject`。`approve` 会恢复对应 run 的执行；
`reject` 会把工具调用作为 rejected observation 反馈给 Agent，Agent 可以生成解释性回答。

---

## 7. Agent Turn Stream

```http
POST /agent/turn/stream
Accept: text/event-stream
```

请求体与 `/agent/turn` 相同。第一版 stream endpoint 复用 Agent Run event，不单独发明事件模型。
LLM client / model 解析仍使用当前规则：请求级 `llm` override 优先，否则使用配置默认值；
暂不读取 session metadata 中的 LLM preference。

响应：

```text
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

每个 SSE frame：

```text
event: <event_type>
id: <run_id>:<sequence>
data: <AgentRunEvent JSON plus stream_part>

```

第一版会输出 run/progress/tool/LLM/safety/final answer 事件，例如 `run_started`、
`package_selected`、`tool_started`、`tool_completed`、`llm_started`、`llm_completed`、
`llm_failed`、`llm_delta`、`safety_review_required`、`safety_review_decided`、
`final_answer`、`run_completed`、`run_failed`。
每个 SSE `data` 都会带 `stream_part`，用于前端区分结构：

```text
lifecycle
progress
tool_result
llm_audit
llm_delta
safety_review
final_answer
```

当审查模式为 `manual` 时，stream 会在 `safety_review_required` 后保持打开；
前端应调用 `POST /agent/safety-reviews/{review_id}/decision` 提交用户决策。

`POST /agent/turn/stream` 默认以 `llm.response_mode=stream` 运行。所有 Agent runtime
中的 LLM stage 都可以发送 provider token delta，包括 route、decision、decision_repair、
异常工具检查的 tool_result_check、context_summarize、context_answer 和 answer。JSON 决策阶段只流式接收
并审计 token；Agent 必须等完整内容累计完成后再解析 JSON 和执行下一步，避免半截 JSON
驱动工具调用，也不能用 JSON stage 的 delta 解析或显示最终答案。最终自然语言回答必须来自
`answer` / `context_answer` stage 的 `llm_delta`。`llm_delta.payload` 至少包含：

```json
{
  "stream_part": "llm_delta",
  "content_role": "final_answer",
  "display_target": "assistant_answer",
  "delta": "增量文本",
  "content_snapshot": "截至当前的完整文本"
}
```

非最终回答类 stage 的 `display_target` 为 `agent_process`，例如 `route_decision`、
`agent_decision`、`decision_repair` 和异常工具检查的 `tool_result_check`；最终自然语言回答使用
`assistant_answer`。前端只应把 `display_target == "assistant_answer"` 的 `llm_delta`
作为最终回答实时输出；`final_answer` event 只作为最终校准 / 补全事件，不是首个显示最终
答案的主要来源。

### 6.1 Linux CLI Frontend

仓库内 `debug_frontend/` 提供一个最小 Linux 命令行 HTTP 前端。它与后端保持前后端分离，
只通过 HTTP / SSE 调用已有 API，不导入或调用 `app/core` 内部运行时代码：

```bash
uv run lka health
uv run lka capabilities
uv run lka sessions list
uv run lka ask --session-id cli_smoke "帮我查询 NTUSO 的乐团考试相关要求"
uv run lka chat --session-id cli_chat
```

CLI 默认连接 `http://127.0.0.1:8765`，也可以通过全局参数或环境变量覆盖：

```bash
uv run lka --base-url http://127.0.0.1:8765 health
LKA_BASE_URL=http://127.0.0.1:8765 uv run lka health
```

`lka ask` 默认调用 `POST /agent/turn/stream`，并将最终回答 token delta 输出到 stdout，
将 Agent 过程事件输出到 stderr，便于管道只消费最终回答。过程事件支持三种显示模式：

```bash
uv run lka ask --agent-events hidden "只显示最终回答"
uv run lka ask --agent-events collapsed "显示折叠过程和最终回答"
uv run lka ask --agent-events expanded "显示完整事件 payload"
```

CLI 不会在前端模拟逐字符输出。stdout 只显示后端实际到达的 `llm_delta` chunk 或最终
`final_answer`；如果 provider 或后端本轮没有产生 token delta，就不会伪装成逐 token
输出。需要观察完整运行过程时使用 expanded Agent events：

```bash
uv run lka ask --agent-events expanded "展示完整运行过程"
```

交互式 `lka chat` 会复用同一个 `session_id` 进行多轮对话，并支持 `/agent
hidden|collapsed|expanded` 在会话中切换过程事件显示方式。

---

## 8. Sessions

### 8.1 Create Session

```http
POST /sessions
```

请求：

```json
{
  "title": "Coliwoo follow-up",
  "initial_message": "Check Coliwoo notices.",
  "metadata": {
    "frontend": "linux-native"
  }
}
```

响应：

```json
{
  "session": {
    "session_id": "session_xxx",
    "title": "Coliwoo follow-up",
    "status": "active",
    "metadata": {
      "frontend": "linux-native"
    },
    "created_at": "2026-08-04T00:00:00Z",
    "updated_at": "2026-08-04T00:00:00Z"
  },
  "messages": [
    {
      "message_id": "session_msg_xxx",
      "session_id": "session_xxx",
      "role": "user",
      "content": "Check Coliwoo notices.",
      "payload": {
        "source": "session_create"
      },
      "created_at": "2026-08-04T00:00:00Z"
    }
  ]
}
```

### 8.2 List Sessions

```http
GET /sessions?limit=50
```

响应：

```json
{
  "sessions": [
    {
      "session_id": "session_xxx",
      "title": "Coliwoo follow-up",
      "status": "active",
      "metadata": {},
      "created_at": "2026-08-04T00:00:00Z",
      "updated_at": "2026-08-04T00:00:00Z"
    }
  ]
}
```

### 8.3 Get Session

```http
GET /sessions/session_xxx
```

响应：

```json
{
  "session": {},
  "messages": []
}
```

### 8.4 Append Session Message

```http
POST /sessions/session_xxx/messages
```

请求：

```json
{
  "role": "user",
  "content": "Continue with the Coliwoo thread.",
  "payload": {}
}
```

说明：

- `role` 当前支持 `user`、`agent`、`system`、`tool`。
- 会话 API 只负责本地持久化和读取，不调用 LLM。
- Agent turn 会额外维护 session context window，用于后续多轮 prompt 注入；它只保存近期
  问答和前文摘要，不保存完整 run log。
- 前端切换会话时应使用显式 `session_id`，后端不维护隐式全局当前会话。
- 会话消息必须由 Session / 通用 Agent turn 显式追加；mail tools 不会因为被调用而自动
  写入 session history。

---

## 9. Mail Import

```http
POST /mail/import
```

请求：

```json
{
  "account": {
    "provider": "local_json",
    "email_address": "user@example.com",
    "display_name": "User"
  },
  "messages": [
    {
      "external_id": "msg_001",
      "folder": "Inbox",
      "subject": "Visa document reminder",
      "sender": "admin@example.com",
      "to": ["user@example.com"],
      "cc": [],
      "received_at": "2026-08-03T09:30:00Z",
      "body_text": "Please submit the missing document by Friday.",
      "attachments": [
        {
          "external_id": "att_001",
          "name": "checklist.pdf",
          "content_type": "application/pdf",
          "size": 12345
        }
      ]
    }
  ]
}
```

响应：

```json
{
  "account_id": "mail_account_xxx",
  "imported_messages": 1,
  "imported_attachments": 1
}
```

---

## 10. Mail Search

```http
GET /mail/search?q=document&limit=10
```

响应：

```json
{
  "query": "document",
  "messages": [
    {
      "message_id": "message_id_001",
      "subject": "Visa document reminder",
      "sender": "admin@example.com",
      "folder": "Inbox",
      "received_at": "2026-08-03T09:30:00Z",
      "snippet": "Please submit the missing document by Friday."
    }
  ]
}
```

---

说明：

- 当前没有 `/mail/process` 或其他邮件专属 agent endpoint。
- 邮件能力通过 Tool Package 暴露给后续通用 Agent turn：`mail.search`、
  `mail.load_messages`、`mail.sync`。
- 邮件整理、概括、匹配等行为应由通用 Agent turn 决定是否调用 mail tools，而不是通过
  mail 路由直接启动独立 agent loop。

---

## 11. List Mail Matters

```http
GET /mail/matters
```

响应：

```json
{
  "matters": [
    {
      "matter_id": "mail_matter_xxx",
      "title": "Visa document reminder",
      "status": "open",
      "priority": "normal",
      "summary": "Please submit the missing document by Friday."
    }
  ]
}
```

---

## 12. Matters

独立事务系统用于保存任务、事件、待办和提醒候选项，不从属于邮件。邮件、Agent trace、
本地文件或后续日历对象都可以作为 `source_links` 关联到同一个 matter。

### 12.1 Create Matter

```http
POST /matters
```

请求：

```json
{
  "title": "Submit required application documents",
  "summary": "Prepare required documents before the deadline.",
  "status": "open",
  "priority": "high",
  "due_at": "2026-08-10T09:00:00+08:00",
  "tags": ["application"],
  "source_links": [
    {
      "source_type": "mail_message",
      "source_id": "message_id_001",
      "reason": "Extracted from loaded local evidence."
    }
  ],
  "metadata": {}
}
```

响应：返回完整 `matter` 记录。

### 12.2 List / Search / Update

```http
GET /matters?limit=50&status=open
GET /matters/search?q=ICA&limit=10
PATCH /matters/{matter_id}
POST /matters/{matter_id}/source-links
```

说明：

- `matter` 表是独立本地持久化层，`mail_matters` 只是旧邮件内视图。
- `matter_source_links` 负责把 matter 关联到邮件、trace、文件或后续其他数据源。
- Agent 可以通过 `matter` tool package 创建、查询、更新和链接事务。
- 第一版只做本地持久化和检索，不做主动提醒调度；提醒会在下一阶段接入。

---

## 13. Outlook Auth Start

```http
POST /mail/outlook/auth/start
```

响应：

```json
{
  "device_code": "device-code",
  "user_code": "ABCD-EFGH",
  "verification_uri": "https://microsoft.com/devicelogin",
  "expires_in": 900,
  "interval": 5,
  "message": "Open the verification URL and enter the code."
}
```

说明：

- 使用 Microsoft Graph Device Code Flow。
- 只读同步路径要求 `User.Read Mail.Read offline_access`。
- `client_id` 可在 `config/local.toml` 中直接配置，也可通过 `client_id_env`
  指定环境变量读取，默认环境变量名是 `MS_GRAPH_CLIENT_ID`。

---

## 14. Outlook Auth Complete

```http
POST /mail/outlook/auth/complete
```

请求：

```json
{
  "device_code": "device-code"
}
```

响应：

```json
{
  "status": "authorized",
  "expires_at": 1785749400,
  "error": null
}
```

如果用户尚未在浏览器完成授权，响应中的 `status` 为 `pending`。
授权成功后，token 会保存到本地配置指定的 `token_store_path`。

---

## 15. Outlook Sync

```http
POST /mail/outlook/sync
```

请求：

```json
{
  "folder": "Inbox",
  "limit": 25,
  "max_pages": 1
}
```

响应：

```json
{
  "account_id": "mail_account_xxx",
  "folder": "Inbox",
  "imported_messages": 25,
  "imported_attachments": 3,
  "status": "completed",
  "next_link": null,
  "delta_link": "https://graph.microsoft.com/...",
  "sync_mode": "delta"
}
```

说明：

- 该接口只读拉取 Outlook 邮件正文和附件 metadata。
- 附件内容不在第一版下载，数据库中的附件记录保持 `is_downloaded = 0`。
- Outlook 同步使用 Microsoft Graph delta query；`mail_sync_state` 会记录最近一次
  同步结果、`next_link` 和 `delta_link`，后续同步优先从保存的 delta 状态继续。
- 除该手动 API 外，服务启动时可按配置执行一次启动自检同步；服务运行期间可按配置后台
  轮询同步；Agent 也可通过 `mail.sync` 工具按需触发同步。
- Webhook / push notification 后续再实现。

---

## 16. Compatibility Notes

- 当前后端实现是轻量骨架，因此部分返回值是规则化输出而非真实 agent 结果。
- 这份契约保留了未来完整系统需要的字段，便于逐步替换实现。
- 如果某个字段暂时未被使用，应优先保留而不是删除，避免前后端接口反复抖动。
