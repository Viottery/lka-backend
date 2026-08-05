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
      "requires_confirmation": false
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
- `requires_confirmation` 用于前端和运行时共同判断。

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
  "user_input": "帮我查询 NTUSO 的乐团考试相关要求"
}
```

响应：

```json
{
  "session_id": "session_001",
  "trace_id": "agent_turn_xxx",
  "answer": "我已调用本地 mail tools...",
  "selected_package": "mail",
  "package_catalog": [],
  "expanded_tools": [],
  "decision_events": [],
  "tool_events": [],
  "llm_events": [],
  "log_path": "data/agent_logs/agent_turn_xxx.md"
}
```

### 设计说明

- 这是第一版通用 Agent turn 入口，不是邮件专属 agent endpoint。
- Agent 第一层只读取 Tool Package catalog；当前已实现的可展开 package 是 `mail`。
- 当 turn 判断用户目标需要本地邮件上下文时，才展开 `mail.search`、
  `mail.load_messages`、`mail.sync`、`mail.persist_matters` 等具体工具。
- 展开 package 后，Agent 会进入单次 turn 内的 step-limited loop：每一步生成一个
  `decision_event`，动作可以是调用一个工具或直接回答；工具结果作为 observation 进入下一步。
- 如果 LLM 选择不展开 package，Agent 可以进入 `context_answer` 阶段，基于当前
  session context window 直接回答，不应把“无需工具”当成“无法处理”。
- 如果 LLM 返回不完整 JSON 但原始输出明确选择了 `mail` package，Agent 会保守恢复该
  package 选择，并继续记录原始 LLM 输出以便回放。
- 如果 decision 阶段返回非 JSON 的自然语言最终回答，Agent 会把该文本恢复为 `answer`
  decision，避免已有回答被本地 fallback 覆盖。
- 每个 session 维护一个本地 context window，默认预算为 `65536` token。Agent prompt
  只注入前文摘要和近期 user / agent 问答；完整工具调用、LLM prompt/output 和运行过程
  保存在本地 run log，不进入后续 prompt。
- Agent 会从同一 session 的历史工具结果中恢复已加载过的本地资源缓存。当前邮件缓存以
  `cached_mail_messages` 注入 context window；如果追问可以由缓存邮件正文回答，Agent
  应直接回答或把缓存作为已有 observation 使用，不再重复调用 `mail.load_messages`。
- 如果用户明确要求同步、询问最新邮箱状态，或 Agent 判断本地邮件可能过期，可以在
  mail package 展开后调用 `mail.sync`。同步完成后，Agent 应继续通过 `mail.search`
  和 `mail.load_messages` 使用本地持久化邮件，而不是让同步工具直接生成答案。
- context window 未满时不做摘要；超过预算时触发独立 `context_summarize` LLM 调用，
  将旧 summary 和除最近两条消息外的历史问答重写为新 summary，原文只保留最近两条消息。
- 每次调用会显式追加 user / agent session message，并写入本地 markdown run log。
- run log 由代码模板生成，包含用户输入、package catalog、展开工具、决策事件、
  工具调用输入输出、LLM 完整 prompt / output / 错误分类和最终回答。
- 未配置真实 LLM 或 LLM 调用失败时，Agent turn 会降级到本地 heuristic，保持链路可运行。

---

## 6. Sessions

### 6.1 Create Session

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

### 6.2 List Sessions

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

### 6.3 Get Session

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

### 6.4 Append Session Message

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

## 7. Mail Import

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

## 8. Mail Search

```http
GET /mail/search?q=document&limit=10
```

响应：

```json
{
  "query": "document",
  "messages": [
    {
      "message_id": "mail_msg_xxx",
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
  `mail.load_messages`、`mail.sync`、`mail.persist_matters`。
- 邮件整理、概括、匹配等行为应由通用 Agent turn 决定是否调用 mail tools，而不是通过
  mail 路由直接启动独立 agent loop。

---

## 9. List Mail Matters

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

## 10. Outlook Auth Start

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

## 11. Outlook Auth Complete

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

## 12. Outlook Sync

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

## 13. Compatibility Notes

- 当前后端实现是轻量骨架，因此部分返回值是规则化输出而非真实 agent 结果。
- 这份契约保留了未来完整系统需要的字段，便于逐步替换实现。
- 如果某个字段暂时未被使用，应优先保留而不是删除，避免前后端接口反复抖动。
