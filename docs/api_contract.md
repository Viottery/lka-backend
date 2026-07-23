# Local Knowledge Agent OS：HTTP API Contract

> 本文档定义 Linux Backend Server 与 Windows/Linux Frontend 之间的最小 HTTP API 契约。
>
> 当前阶段只保留基础服务能力，不包含任务规划、任务执行、轨迹或确认流。

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
- 未来 workspace 索引还会补充文件构成索引，用于在不依赖向量检索时提升效率和准确率。
- `options` 字段保留给前端扩展使用。

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

## 4. Compatibility Notes

- 当前后端实现是轻量骨架，因此部分返回值是规则化输出而非真实 agent 结果。
- 这份契约保留了未来完整系统需要的字段，便于逐步替换实现。
- 如果某个字段暂时未被使用，应优先保留而不是删除，避免前后端接口反复抖动。
