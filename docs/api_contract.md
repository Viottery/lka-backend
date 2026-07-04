# Local Knowledge Agent OS：HTTP API Contract

> 本文档定义 Linux Backend Server 与 Windows/Linux Frontend 之间的最小 HTTP API 契约。
>
> 同时也记录当前实现与未来目标之间的边界，避免接口和愿景脱节。

## Base URL

MVP 默认：

```text
http://127.0.0.1:8765
```

后端默认只绑定本机，不开放公网或局域网。

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
  "workspace": "/mnt/c/Users/chuan/Documents/NTU",
  "source_frontend": "windows-cli",
  "options": {
    "recursive": true,
    "include_code": true,
    "include_pdf": true
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
- `options` 字段保留给前端扩展使用。

---

## 3. Plan Task

```http
POST /tasks/plan
```

请求：

```json
{
  "task": "整理这个文件夹并提取 TODO",
  "workspace": "/mnt/c/Users/chuan/Documents/NTU",
  "frontend": "windows-cli"
}
```

响应：

```json
{
  "intent": "file_organization",
  "plan": [
    "scan workspace",
    "retrieve relevant context",
    "summarize folder",
    "extract tasks",
    "generate organization suggestions"
  ],
  "suggested_capabilities": [
    "summarize_folder",
    "extract_tasks",
    "organize_files"
  ],
  "risk": "low"
}
```

### 设计说明

- `intent` 是对任务的高层分类。
- `plan` 是给人和系统都能理解的执行步骤。
- `suggested_capabilities` 用于后续能力选择。
- `risk` 用于决定是否需要确认流程。

---

## 4. Run Task

```http
POST /tasks/run
```

请求：

```json
{
  "task": "整理这个文件夹并提取 TODO",
  "workspace": "/mnt/c/Users/chuan/Documents/NTU",
  "frontend": "windows-cli",
  "mode": "interactive"
}
```

完成响应：

```json
{
  "task_id": "task_001",
  "status": "completed",
  "summary": "完成文件夹摘要、待办提取和整理建议。",
  "trace_id": "trace_001",
  "requires_user_action": false,
  "artifacts": []
}
```

需要前端动作的响应：

```json
{
  "task_id": "task_002",
  "status": "requires_frontend_action",
  "action_request": {
    "action": "open_file",
    "path": "C:\\Users\\chuan\\Documents\\report.pdf",
    "reason": "需要用户查看生成报告"
  }
}
```

需要确认的响应：

```json
{
  "task_id": "task_003",
  "status": "waiting_for_confirmation",
  "confirmation_id": "confirm_001",
  "reason": "该操作可能修改 23 个文件。",
  "proposed_actions": [
    {
      "type": "modify_files",
      "count": 23,
      "risk": "high"
    }
  ]
}
```

### 设计说明

- `run` 的目标不是直接“什么都做完”，而是把任务拆成可执行、可控、可回放的流程。
- 需要用户配合的步骤应明确返回 `requires_frontend_action`。
- 高风险动作应优先进入 `waiting_for_confirmation`。

---

## 5. Get Task

```http
GET /tasks/{task_id}
```

响应：

```json
{
  "task_id": "task_001",
  "status": "completed",
  "summary": "...",
  "trace_id": "trace_001"
}
```

---

## 6. List Capabilities

```http
GET /capabilities
```

响应：

```json
{
  "capabilities": [
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

## 7. Trace APIs

```http
GET /traces
GET /traces/{trace_id}
```

`GET /traces/{trace_id}` 返回完整执行轨迹：

```json
{
  "trace_id": "trace_001",
  "user_goal": "...",
  "intent": "...",
  "plan": [],
  "context_summary": "...",
  "capabilities_used": [],
  "verification_result": {},
  "success": true
}
```

`GET /traces` 返回精简轨迹列表，便于前端浏览历史记录。

### 设计说明

- Trace 是后端的可解释性核心。
- 后续 verifier、diff checker、test runner 都应把结果写入 trace。

---

## 8. Confirmation API

```http
POST /confirmations/{confirmation_id}
```

请求：

```json
{
  "decision": "approved",
  "frontend": "windows-cli"
}
```

或：

```json
{
  "decision": "rejected",
  "frontend": "windows-cli"
}
```

响应：

```json
{
  "confirmation_id": "confirm_001",
  "decision": "approved",
  "status": "resolved"
}
```

### 设计说明

- 确认 API 是高风险动作的闸门。
- 前端只负责收集决策，后端负责记录状态和后续执行条件。

---

## 9. Compatibility Notes

- 当前后端实现是轻量骨架，因此部分返回值是规则化输出而非真实 agent 结果。
- 这份契约保留了未来完整系统需要的字段，便于逐步替换实现。
- 如果某个字段暂时未被使用，应优先保留而不是删除，避免前后端接口反复抖动。

