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

### 会话工作目录

```http
PUT /sessions/{session_id}/workspace
```

```json
{
  "path": "/home/chuan/Documents/project",
  "platform": "linux"
}
```

响应中的 `workspace` 会同时出现在该会话的 `GET /sessions/{session_id}` 结果中。它是
`bash` 和 `filesystem` 的默认根目录：相对路径、`$workspace_root` 和命令环境变量均按此目录
解析。`filesystem` 会拒绝目录之外的路径；`bash` 将其作为受校验的 `cwd`，但不是 OS 级
sandbox，shell 命令本身仍须经过既有安全审查。

路径协议：

- `platform` 必须是 `linux`、`windows` 或 `macos`。通常必须等于后端实际运行平台；但 Linux
  backend 支持 WSL 的 Windows drive-path 桥接。
- Linux/macOS 使用绝对 POSIX 路径；Windows 使用绝对盘符或 UNC 路径，JSON 推荐 `C:/...`。
- Linux backend 在 WSL 中接收 `C:/Users/...` 时，会按 `LKA_WSL_WINDOWS_MOUNT_ROOT`（默认
  `/mnt`）映射为 `/mnt/c/Users/...` 后执行；响应保留原 Windows 路径，并额外返回实际的
  `backend_path`。默认映射不支持 Windows UNC 路径（如 `//server/share/...`）。
- 前端可选择和切换目录，但只能选择后端进程实际可访问的本地目录；Windows backend 不能直接
  执行 `/home/...`，非 WSL Linux backend 的 Windows 路径也必须有可访问的对应挂载点。
- 配置了 `LKA_WORKSPACE_ROOTS` 时，所选目录必须位于其中之一；未配置时，用户显式选择的目录
  本身成为该会话唯一工具根目录。
- 该设置影响后续工具调用。已启动的 `bash` 后台终端保留创建时的 `cwd`，不会在切换目录时被
  静默迁移。
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
      "name": "knowledge",
      "type": "tool_package",
      "risk": "low_to_medium",
      "requires_confirmation": false,
      "read_only": true
    },
    {
      "name": "filesystem",
      "type": "tool_package",
      "risk": "medium",
      "requires_confirmation": true,
      "read_only": false
    },
    {
      "name": "bash",
      "type": "tool_package",
      "risk": "high",
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

## 4. Knowledge Import And Retrieval

`knowledge` 是 source-agnostic 的本地证据库。第一版支持导入 Markdown/TXT 本地文档，
写入 SQLite FTS，并通过只读工具提供最小必要片段给 Agent。

网页抓取不由 Agent 工具自动执行。当前公开网页样本先由外部脚本转换为分层本地 Markdown，
例如 `data/knowledge_samples/minecraft/mobs/creeper.md`，再以
`source_type="local_document"` 导入；原 URL、抓取时间、collection、section 和校验和仅
保存在 metadata 中作为证据溯源。网页来源模型和 crawler 是后续数据源能力，不是当前
`knowledge` 的前提。

本地邮件会在 `/mail/import` 和 `/mail/outlook/sync` 成功写入后自动镜像为
`source_type="mail_message"` 的知识文档。镜像使用 `mail_message:<message_id>` 作为
document-level `source_ref`，chunk 结果会追加 `#chunk=<n>`；`uri` 为稳定的
`mail://<account_id>/messages/<message_id>`。邮件内容默认属于 `personal`，并使用
`redact` 远程策略。邮件专属筛选仍应使用 `/mail/search`，统一跨来源检索可通过
`source_type=mail_message` 过滤 `/knowledge/search`。

### 4.1 Import Knowledge Document

```http
POST /knowledge/import
```

请求：

```json
{
  "source": {
    "source_type": "local_document",
    "display_name": "prts.wiki.md",
    "uri": "data/knowledge_samples/prts.wiki.md",
    "metadata": {"seed_site": "prts"},
    "sensitivity": "public",
    "remote_policy": "allow"
  },
  "title": "prts.wiki.md",
  "uri": "data/knowledge_samples/prts.wiki.md",
  "text": "页面正文文本...",
  "mime_type": "text/markdown",
  "metadata": {"original_url": "https://prts.wiki", "ingestion_kind": "public_page_snapshot"},
  "sensitivity": "public",
  "remote_policy": "allow"
}
```

响应：

```json
{
  "source_id": "knowledge_source_xxx",
  "document_id": "knowledge_doc_xxx",
  "imported_chunks": 12,
  "checksum": "sha256...",
  "sensitivity": "public",
  "remote_policy": "allow",
  "secret_chunks_redacted": 0
}
```

### 4.2 Search Knowledge

```http
GET /knowledge/search?q=红石&limit=10&source_type=local_document&mode=hybrid
```

响应：

```json
{
  "query": "红石",
  "query_id": "knowledge_query_xxx",
  "requested_mode": "hybrid",
  "applied_mode": "hybrid",
  "retrieval_warning": null,
  "rerank_applied": false,
  "results": [
    {
      "chunk_id": "knowledge_chunk_xxx",
      "document_id": "knowledge_doc_xxx",
      "source_id": "knowledge_source_xxx",
      "title": "Minecraft Wiki",
      "source_type": "local_document",
      "uri": "data/knowledge_samples/zh.minecraft.wiki_w_Minecraft_Wiki.md",
      "chunk_index": 0,
      "snippet": "最小必要片段...",
      "source_ref": "local_document:data/knowledge_samples/zh.minecraft.wiki_w_Minecraft_Wiki.md#chunk=0",
      "sensitivity": "public",
      "remote_policy": "allow",
      "policy_decision": "allowed",
      "retrieval_channels": ["keyword", "semantic"],
      "retrieval_score": 0.0328,
      "rerank_score": null,
      "untrusted_data": true
    }
  ],
  "filtered_count": 0
}
```

`mode` 可选值为 `keyword`、`semantic` 和 `hybrid`。后端通过可替换的 retrieval
接口选择实现；当语义索引尚未同步时，`semantic` / `hybrid` 会保守回退到关键词检索，并在
`retrieval_warning` 中明确返回原因。
当前默认配置采用 `keyword`；只有经本地数据集验证的模型与索引准备就绪后，
才建议把默认值切换到 `hybrid`。`rerank_applied`、`rerank_score` 与
`retrieval_warning` 是兼容扩展；本地重排未启用或不可用时保持融合排序。

### 4.3 Rebuild Mail Knowledge Mirror

```http
POST /knowledge/mail-mirror/sync
```

请求可限定一个本地邮件账户：

```json
{"account_id": "mail_account_xxx"}
```

不传 `account_id` 时会重建所有本地持久化邮件的镜像。该接口只投影本地邮件数据，
不访问远程邮箱；向量索引同步仍使用 `/knowledge/semantic-index/sync`，或由本地
`auto_index_on_import` 配置控制。

响应：

```json
{
  "scope": "account",
  "scanned_messages": 25,
  "mirrored_messages": 25,
  "imported_chunks": 31,
  "document_ids": ["knowledge_doc_xxx"]
}
```

### 4.4 Sync Local Semantic Index

```http
POST /knowledge/semantic-index/sync
```

显式为已导入、非 `secret` 且非 `deny` 的 chunk 生成本地 embedding，并同步已配置的本地
语义索引。默认实现为 FastEmbed 本地 ONNX embedding 和 sqlite-vec；原文不发送到远程
embedding 服务。

首次初始化模型时，必须显式请求下载：

```json
{"allow_model_download": true}
```

默认 `allow_model_download=false`，已缓存模型后的索引同步和所有查询均为离线操作。

```json
{
  "enabled": true,
  "index_key": "fastembed:BAAI/bge-small-zh-v1.5:512",
  "scanned_chunks": 46,
  "embedded_chunks": 46,
  "updated_chunks": 0,
  "removed_chunks": 0,
  "indexed_chunks": 46
}
```

### 4.5 Load Knowledge Chunks

```http
POST /knowledge/chunks/load
```

请求：

```json
{
  "chunk_ids": ["knowledge_chunk_xxx"],
  "max_chars_per_chunk": 420,
  "offset": 0
}
```

默认仍为每块 420 字符；可请求 1–6000 字符（一个完整知识块的上限），不再暗中受
搜索摘要 1200 字符上限限制。`chunks[].char_count` 是实际返回字符数，`total_chars`
是隐私过滤后该块的总字符数，`truncated` 明确是否省略；需要完整依据时可提高读取上限。
`offset` 为每个块隐私过滤文本的 Unicode 字符偏移；按该块的 `next_offset`、单独该块 ID
续读直到 next_offset=null，脱敏后扩长的块也可完整读取。每次重新检查来源授权/隐私；
不是跨请求冻结隐私策略的快照。`truncated=true` 描述本次片段不等于整个块，不代表后面必有内容。
授权、隐私过滤和最多 20 块的边界不变；长工具结果仍由通用结果 gate 缓存及摘选。

### 4.6 Load Knowledge Document

```http
GET /knowledge/documents/{document_id}?include_text=false
```

默认只返回 metadata 和 `chunk_ids`。只有显式 `include_text=true` 时才返回有界、
privacy-filtered 的文本视图。

### 设计说明

- `knowledge.list_sources`、`knowledge.search`、`knowledge.load_chunks`、
  `knowledge.load_document` 都是 Agent 可见
  只读工具。
- `knowledge.list_sources` 返回当前 Agent scope 中可见的来源 id、类型、名称和文档数；
  `knowledge.search` 可选 `source_ids`，与子 Agent 服务端授权范围取交集。
- `knowledge.search` 只返回 Top-K 最小片段，不返回整篇文档。
- 关键词、语义和 hybrid 融合策略不是 Agent core 或工具协议中的硬编码；它们由本地
  retrieval implementation 和配置决定。
- 默认 FastEmbed/sqlite-vec 实现完全本地运行。`secret` / `deny` chunk 不生成向量。
- 所有返回给 Agent 的 retrieved content 都标记为 `untrusted_data=true`。
- `sensitivity="secret"` 或 `remote_policy="deny"` 不进入 Agent prompt 输出。
- 导入时检测到 secret-like chunk 时，默认只保存占位内容和 metadata，不保存原 secret 原文。
- `mail_message` 镜像保留邮件特有查询能力的边界；邮件同步、按文件夹/发件人精确筛选和
  完整邮件加载仍通过 `mail` package 完成。

---

## 5. Runtime Debug Entry

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

## 6. Agent Turn

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
后端代码写死。`/agent/turn` 返回用户可见结果和脱敏审计摘要；`POST /agent/turn/stream`
提供用户可见的 SSE token-delta 输出。

桌面前端可用 `GET /agent/models` 读取当前配置的 client 与模型列表；接口会尝试服务商
兼容的 `/models`，不可用时退回本地 `available_models` 与默认模型。`?refresh=true` 强制
刷新，响应不包含 API key。`GET /agent/ui-defaults` 和 `PUT /agent/ui-defaults` 读取及保存
本机共享的前端默认设置，字段为 `llm_client`、`llm_model`、`safety_mode`、`stream`、
`workspace_parent`。默认设置只影响前端之后发出的请求或新建工作区，不修改已有会话。
请求可选带 `safety_review_mode`（`skip`、`llm`、`manual`）。后端取请求与本地配置中
更严格的模式，因此前端可以提高审查级别，但不能绕过后端配置；省略时使用本地配置。
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
  "initial_package": "mail",
  "expanded_packages": ["mail", "matter"],
  "used_packages": ["mail", "matter"],
  "active_package": "matter",
  "tool_events": [{"tool_name": "mail.search", "status": "completed"}],
  "progress_events": [],
  "verification_warnings": [],
  "llm_events": [{"llm_call_id": "llm_call_xxx", "stage": "answer", "status": "completed"}]
}
```

完整 session context、decision raw output、工具输入/原始输出、LLM 完整 prompt / output、
audit record 和本地日志路径均不属于正常 HTTP 响应；它们只保留在本地 run log 与受控审计存储。

### 设计说明

- 这是第一版通用 Agent turn 入口，不是邮件专属 agent endpoint。
- 每次调用都会创建独立 Agent Run；`run_id` 用于后续 stream、取消、确认、重试和事件查询。
- Agent 第一层只读取 Tool Package catalog；可展开 package 和可调用工具来自 Tool Registry。
- 长工具结果在 prompt 中只显示带 `_result_cache.artifact_id` 的规则预览；展开
  `observation` package 后可调用 `observation.read`，传入 `artifact_id`、可选的
  JSON Pointer `path`（默认根路径）、`offset`（默认 0）及 `limit`（1–20，默认 5）。
  返回类型、总量、分页 `has_more`/`next_offset`；字符串每次最多回读 4000 字符。
  字符串可用 `max_chars`（1–4000，旧默认仍4000）控制窗口；≤1200 更适合短证据交付。
  `fields` 投影中的截断字段附 `read_path` 与字段内 `next_offset`，可省略 fields 按该路径续读。
  数组 `has_more=false` 只表示记录分页结束，不表示所有字段已完整读取。
  artifact 只在产生它的同一个 run 内可读，不是跨会话或子 Agent 的共享引用。
- 所有非只读工具调用都会先进入 safety review。审查模式由本地配置选择：
  `skip` 记录并自动通过、`llm` 调用 LLM 审查、`manual` 进入等待前端确认状态。
- `bash.run` 会按具体命令动态判断只读性。白名单只读命令可直接执行；白名单之外、
  写入、信号、stdin 交互等都按非只读处理并进入 safety review。
- Agent core 不硬编码具体 package 名、工具名、领域流程、路由关键词或工具调用示例。
  具体策略必须由 package metadata、tool description、input/output schema、routing hints、
  decision hints 和 cache policy 提供。
- 展开 package 后，Agent 会进入单次 turn 内的 step-limited loop：每一步生成一个
  `decision_event`，动作可以是调用一个工具、展开 package、请求确认，或进入最终回答阶段；
  工具结果作为 observation 进入下一步。默认 step budget 为 10，可通过
  `config/local.toml` 的 `[agent].max_decision_steps` 调整。
- 当当前 LLM client 在 `config/local.toml` 声明 `supports_function_calling = true` 时，
  decision stage 优先使用 provider-native Function Calling：已展开工具的 input schema 与通用
  package 展开、`fork_subtasks`、`plan_patch` 和结束决策动作会作为 functions 发给 provider。
  仅当 endpoint 也支持 `tool_choice = "required"` 时设置
  `supports_required_tool_choice = true`，此时每步决策要求恰好一个 function call；不支持该能力的
  client 仍使用 `auto`，没有 function call 表示进入独立 `answer`
  stage；最终用户答案仍只来自 `answer` / `context_answer` 的 token-delta。所有 native 调用仍会
  经过既有 Tool Executor、input schema 与 safety review。未声明该 capability、provider 拒绝
  tools，或 native 响应不可用时，自动回退 operation-first JSON 决策协议。对支持严格 schema
  的 provider 可额外设置 `function_calling_strict = true`；该选项依赖 provider/base URL 支持。
- `decision_event` 会同时记录 `assistant_message` 和 `operation`。`assistant_message`
  是用户可见的过程文本；`operation` 是内部结构化动作，例如
  `tool_call`、`expand_package`、`final_answer`、`request_confirmation` 或 `no_op`。
  `decision.operation.type == "final_answer"` 只表示后端应进入独立 `answer` stage，
  不能把 `operation.final_answer` 或旧格式 `answer` 当作最终用户答案。前端展示过程文本时
  可读取 `assistant_message`，不要把 decision JSON 当作用户最终回答展示。
- route 只决定起始 package；在 ReAct loop 中，如果已展开工具不足，LLM 可以输出
  `expand_package` 来展开另一个已注册 package。跨 package 工作流必须来自 registry
  metadata 和 tool schema，而不是 Agent core 特判。
- `selected_package` 是兼容字段，语义等同于 `initial_package`，只用于 route 质量评估、
  日志和旧客户端兼容；后续 decision、decision_repair、tool_result_check、answer 等
  LLM 上下文不再接收该字段。前端展示跨 package 状态应使用 `initial_package`、
  `expanded_packages`、`used_packages` 和 `active_package`。
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
- decision_repair 的单个完整对象若仅多一个尾闭合括号，可本地确定性恢复并记录
  decision_repair_suffix_normalized；多对象、夹带文本、重复键或更大修复仍拒绝。
  普通decision严格解析与ToolExecutor schema/safety gate不因此放宽。
- 每个真实 `tool_event` 都包含 `feedback`。反馈至少包含执行成功 / 失败状态和可读
  message。Agent 会先做本地协议校验：工具声明 `output_schema` 时按该 schema 校验
  `ToolResult.output`；未声明时只检查 `ToolResult` 是完整 JSON 对象形状。只有工具执行
  失败、被拒绝、输出协议不匹配或本地无法确认时，才记录 `tool_result_check` LLM 事件。
- 长工具结果会在进入后续 LLM prompt 的 `observations` 前做通用压缩，并用
  `_prompt_compacted=true` 标记。完整工具结果仍保留在内部 `tool_events`、session payload 和
  本地 markdown run log 中；正常 API 和 transport event 仅暴露摘要。
- 每轮 decision prompt 还会包含 `completed_tool_calls`，以简洁形式列出当前 turn 已成功的
  调用和其输入。模型在已有结果覆盖请求时必须进入 `final_answer`，不得仅为增加置信度而重放
  相同工具和输入；如用户明确要求重复执行，模型必须在 operation 中显式设置
  `repeat_successful_call=true`。无此标记的重复成功调用会被 harness 拦截，并直接以已有证据
  进入 answer stage。
- `progress_events` 是由 harness 本地代码生成的自然语言运行过程流，不调用 LLM。它会记录
  package 选择、模型过程文本、package 展开、工具开始 / 完成、工具反馈、最终回答和校验
  warning，供前端展示“系统正在做什么”。
- `verification_warnings` 是本地最终回答检查结果。当前只做 warning，不自动改写 LLM
  最终回答；领域级校验规则必须通过 package metadata 或独立 verifier 注册，不能在
  Agent core 中写死。
- finish 的可选 `answer_checks` 支持 `requirement_id` 与
  `status=pending|supported|blocked`，旧 reason-only 调用保持兼容。已声明要求保存在
  Graph checkpoint，并以独立 `task_completion` 进入后续 decision/answer；遗漏要求不等于
  解决要求。存在 pending 时可按预算恢复最多两次，无新增成功工具证据时停止并交付
  部分结果；blocked 必须有原因。`supported` 只是模型声明，不是后端语义校验。
  服务端 `task_completion_handoff` 记录交付时的缺口，child `TaskResult` 据此保留
  `partial/missing_requirements`，包括终态重放；Graph `final_answer` progress 会标记
  partial。简单任务不必填这些字段，也不增加默认模型调用。未声明要求或仅在回答阶段
  才发现的缺口目前不会自动重开执行。设计与验收见
  `docs/task_completion_optimization_2026-10-05.md`。
- 展开工具时，`expanded_tools[*].input_schema` 会尽量暴露 required fields、allowed values
  和 examples。LLM decision prompt 要求模型严格遵循这些 schema；具体枚举值、示例和
  领域约束由对应 Tool Package schema 提供。
- 每个 session 维护一个本地 context window，默认预算为 `65536` token。Agent prompt
  只注入前文摘要和近期 user / agent 问答；完整工具调用、LLM prompt/output 和运行过程
  保存在本地 run log，不进入后续 prompt。
- 全部输入 prompt 另有独立的 `131072` token 目标，不扩大 session 的 `65536` 历史预算。
  实际安全输入上限还会扣除所选模型配置的输出预留和安全余量；未配置容量的远端模型
  会在发请求前明确报错。本地 tokenizer 可配置，未配置时以 UTF-8 字节上界估算。超额时先缩减旧观察、
  低相关记忆、较旧历史与可续读的指导文件预览；系统指令、当前任务、工具 schema 和
  最近两条历史不被静默截断。未显式指定生成上限时，已配置模型的输出预留同时成为
  该次请求的 `max_tokens`，使预留可执行；需更长回答应提高模型配置的预留和容量。
  `llm_started` 事件带输入估算、上限及计数方法。
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

## 7. Bash Tool Protocol

bash 工具只通过 Agent tool-call 暴露，不提供独立 HTTP endpoint。前端通过
`/agent/turn/stream` 观察 tool events 和 safety review events。

`filesystem.read_file` / `filesystem.edit_file` 的 `path` 字段也使用同一 workspace path
基准：相对路径按第一个 configured workspace root 解析，并支持 `$workspace_root`、
`${workspace_root}`、`$WORKSPACE_ROOT`、`${WORKSPACE_ROOT}`、`$LKA_WORKSPACE_ROOT` 和
`${LKA_WORKSPACE_ROOT}` 展开。

路径协议：

- 未传 `cwd` 时，默认使用第一个 configured workspace root。
- `cwd` 可以是绝对路径，也可以是相对路径；相对路径按第一个 workspace root 解析。
- `cwd` 支持 `$workspace_root`、`${workspace_root}`、`$WORKSPACE_ROOT`、
  `${WORKSPACE_ROOT}`、`$LKA_WORKSPACE_ROOT` 和 `${LKA_WORKSPACE_ROOT}` 展开。
- `cwd` 必须位于 configured workspace roots 之内，且必须是已存在目录。
- 命令环境会注入 `workspace_root`、`WORKSPACE_ROOT`、`LKA_WORKSPACE_ROOT` 和
  `LKA_WORKSPACE_ROOTS`。前三者指向默认 workspace root；`LKA_WORKSPACE_ROOTS` 使用分号
  连接所有 configured workspace roots。
- 模型应优先使用相对路径或 `$workspace_root` 定位文件，避免凭空构造占位绝对路径。

### 7.1 `bash.run`

同步命令：

```json
{
  "command": "rg \"needle\" .",
  "cwd": ".",
  "mode": "sync",
  "timeout_seconds": 30,
  "max_output_bytes": 32768
}
```

返回：

```json
{
  "mode": "sync",
  "command": "rg \"needle\" .",
  "cwd": "/home/user/project",
  "workspace_root": "/home/user/project",
  "read_only": true,
  "status": "exited",
  "running": false,
  "exit_code": 0,
  "timed_out": false,
  "stdout": "...",
  "stderr": "",
  "output": "...",
  "session_id": null,
  "next_offset": null
}
```

后台终端：

```json
{
  "command": "npm run dev",
  "cwd": ".",
  "mode": "background"
}
```

返回：

```json
{
  "mode": "background",
  "command": "npm run dev",
  "cwd": "/home/user/project",
  "workspace_root": "/home/user/project",
  "read_only": false,
  "status": "running",
  "running": true,
  "exit_code": null,
  "timed_out": false,
  "stdout": "",
  "stderr": "",
  "output": "",
  "session_id": "bash_session_000001",
  "next_offset": 0
}
```

只读判断是按命令白名单保守执行：`pwd`、`printenv`、`ls`、`find`、`rg`、`grep`、`cat`、
`sed`、`head`、`tail`、`wc`、`git status`、`git diff`、`git log`、`git show`
等可判为只读。无法判断、重定向写入、非白名单命令默认非只读。

### 7.2 `bash.read_session`

```json
{
  "session_id": "bash_session_000001",
  "offset": 0,
  "max_bytes": 32768
}
```

返回：

```json
{
  "session_id": "bash_session_000001",
  "command": "npm run dev",
  "cwd": "/home/user/project",
  "workspace_root": "/home/user/project",
  "read_only": false,
  "status": "running",
  "running": true,
  "exit_code": null,
  "offset": 0,
  "next_offset": 120,
  "output_start_offset": 0,
  "output": "...",
  "truncated": false,
  "reader_error": null
}
```

模型应保存 `next_offset`，下一次从该 offset 继续查询，避免重复读取。

### 7.3 Session Control

- `bash.list_sessions`: 查询所有终端或 `active_only=true` 的活跃终端。
- `bash.write_session`: 向后台终端写入 stdin 文本，例如 `"hello\n"`。
- `bash.interrupt_session`: 发送 Ctrl-C / SIGINT。
- `bash.terminate_session`: 发送 SIGTERM。

`write_session`、`interrupt_session`、`terminate_session` 都是非只读工具调用，必须经过
safety review。

---

## 8. Agent Safety Reviews

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

## 9. Agent Turn Stream

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

SSE 连接本身不是运行的所有者：短暂断开不会取消 run。客户端可以按 sequence 重连或查询
持久化事件：

```http
GET /agent/runs/{run_id}
GET /agent/runs/{run_id}/events?after_sequence=42
GET /agent/runs/{run_id}/stream?after_sequence=42
POST /agent/runs/{run_id}/cancel
POST /agent/runs/{run_id}/resume
```

`cancel` 会立即把 run 标记为 `cancelled` 并记录 durable cancellation request；正在执行的
provider 调用无法保证被强制中止，但其后续 graph node、工具和 finalize 副作用会在下一个节点
边界被阻止。重连的 `llm_delta` 保持 `content_snapshot` 字段兼容性，但 SQLite 只保存 delta 并在
回放时重建快照。完成前会先 flush token delta，之后才发布 `run_completed`。

服务重启后，处于 `running` 状态且保留 LangGraph SQLite checkpoint 的 run 可由
`POST /agent/runs/{run_id}/resume` 显式恢复。`waiting_confirmation` run 必须继续通过对应的
safety-review decision 恢复；终态 run 和 legacy orchestrator run 返回 `409`。同一进程内的重复
resume 请求会合并为同一个恢复任务。

安全审查 API 与 SSE 的 `safety_review_*` 事件只返回公开摘要（review ID、工具、风险、状态、
理由及时间）。原始 `tool_input` 和审查 LLM 输出属于本地审计材料，不会进入普通 HTTP/SSE 响应。

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

### 9.1 Linux CLI Frontend

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

## 10. Sessions

### 10.1 Create Session

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

### 10.2 List Sessions

```http
GET /sessions?limit=50&offset=0&q=invoice
```

`limit` 默认 50、范围 1–200；`offset` 默认 0，按更新时间从新到旧分页。可省略 `q`；
提供非空 `q`（最多 500 个字符）时，会在会话标题和全部历史消息正文中查找不区分
大小写的字面子串，`%` 和 `_` 按普通字符处理。搜索在 SQLite 查询中完成，因此匹配
结果不限于当前页。
`GET /sessions/deleted` 支持相同的 `limit`、`offset` 和 `q` 参数，但只搜索回收站中的会话。
不带新增参数的旧请求保持原有默认行为。

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

### 10.3 Get Session

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

### 10.4 Rename Session

```http
PATCH /sessions/session_xxx
Content-Type: application/json
```

请求体接受 1–40 个字符的 `title`；首尾空白会去除，空白标题无效。成功时返回更新后的
会话及其原有消息，持久化标题并在会话 `metadata` 中设置 `title_is_custom: true`，供
其他窗口和自动标题逻辑识别用户已手动命名。会话不存在或已删除时返回 `404`。

```json
{"title": "Review invoices"}
```

### 10.5 Delete Session

```http
DELETE /sessions/session_xxx
```

成功时返回 `204 No Content`。删除采用软删除：会话状态会设为 `deleted`，并从
`GET /sessions` 和 `GET /sessions/{session_id}` 中隐藏。重复删除或删除不存在的 ID 返回
`404`。历史消息、workspace 文件、run logs 和其他审计数据不会被物理删除；已删除的
`session_id` 也不能通过后续消息写入或 Agent turn 自动重新创建；对已删除会话追加消息返回
`404`。

回收站列表：

```http
GET /sessions/deleted?limit=50
```

响应形状与 `GET /sessions` 相同，最多返回 200 条已删除会话。

恢复会话：

```http
POST /sessions/session_xxx/restore
```

成功时返回恢复后的 session 和原有消息，状态设为 `active`，该会话重新出现在
`GET /sessions` 中，并从回收站列表移除。不是已删除状态或不存在的 ID 返回 `404`。
恢复不会重写消息、workspace、run logs 或其他审计数据。当前不提供自动或定时清理接口。

### 10.6 Append Session Message

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

### 10.7 Browse Session Workspace Files

以下接口只允许本机 loopback 请求，且只读取已绑定到**活动会话**的工作区；已删除、未绑定或不可用的工作区不能浏览。所有 `path` 均为相对工作区的路径，拒绝绝对路径、`..` 路径穿越和符号链接。

工作区必须保持创建绑定时的 canonical 绝对路径；根被替换成符号链接等导致路径改变时
返回 `403`。POSIX 上预览与列表通过逐层不跟随符号链接的目录描述符打开，避免校验后的
祖先目录替换竞态；预览只接受普通文件，FIFO 不等待写入者。Windows 保留路径检查，
尚未提供等价的 native reparse handle 竞态保护。

```http
GET /sessions/session_xxx/files?path=notes
GET /sessions/session_xxx/file?path=notes/todo.md
GET /sessions/session_xxx/file/raw?path=diagram.png
```

`/files` 最多返回当前目录 200 项，每项包含 `name`、相对 `path`、`type`、`size_bytes` 和 `modified_at`，并以 `truncated` 指示是否截断。`/file` 返回最多 256 KiB 的 UTF-8 文本及 `size_bytes`、`truncated`；二进制或非 UTF-8 文件返回 `415`。`/file/raw` 仅允许经文件签名验证的 PNG、JPEG、WebP 和 PDF，最多 8 MiB，以内联响应供工作台预览；不提供任意文件下载或修改接口。

---

### 10.8 项目管理与项目下多会话

项目是持久化的目录身份与显示名称，不是单个会话。项目 ID 复用项目记忆的稳定
`project_id`，新增 `project_profiles` 只保存名称、revision 与时间，不另建身份体系。
项目可以没有会话，但创建时必须指定存在、受允许的目录；不提供无目录的虚拟项目。
以下 `/projects` 接口和显式按项目创建/筛选会话沿用本机 / Bearer token 鉴权。

| 操作 | 接口 |
| --- | --- |
| 创建/登记目录项目 | `POST /projects` |
| 分页列出/搜索项目 | `GET /projects?limit=50&offset=0&q=关键词` |
| 项目详情 | `GET /projects/{project_id}` |
| 修改显示名称 | `PATCH /projects/{project_id}` |
| 项目下的会话 | `GET /projects/{project_id}/sessions?limit=50&offset=0&q=关键词` |
| 现有列表按项目过滤 | `GET /sessions?project_id=project_xxx` |

登记请求示例：

```json
{"name":"LKA 开发","path":"/home/user/lka_backend","platform":"linux"}
```

`name` 可省略，默认目录名；`platform` 可省略，默认后端平台，也支持既有 Windows→WSL
映射。返回 `project_id`、`name`、`revision`、`workspace_path`（规范化后端路径）、
`session_count`（未删除会话数）、`created_at`、`updated_at`。列表另外返回 `next_offset`；
limit 1..200，offset 0..1,000,000，q 最长 500 字符，按字面子串搜索名称/ID。
同目录重复登记返回原项目，不覆盖已修改的名称。未知项目 404，非法/不存在/越界目录 422。

重命名请求为 `{"name":"新名称","expected_revision":1}`，名称 1..120 字符且不能全为空白；
版本冲突 409，成功 revision 自增。**只改显示名称，不重命名文件夹，不改变记忆或会话身份。**
本版不提供项目删除、自动合并、文件夹创建或文件夹搬迁；既有显式 memory project relocation
仍是独立的路径身份操作，不应当当作文件系统移动工具。

新建项目会话可直接调用：

```json
{"title":"检索优化","project_id":"project_xxx"}
```

这是 `POST /sessions` 的新增可选字段，后端自动绑定项目目录，返回 session 中的 `project_id`
与 `workspace`，不用再单独 PUT workspace。不存在的项目 404，目录已消失/失权 422，校验失败
不会创建空会话。每个会话仍有独立 session_id、消息和摘要；共享目录不意味着文件写入隔离。
不传 project_id 时原有接口继续工作。若同时提交 metadata.project_id 且与顶层 ID 不同则 422。

既有 `PUT /sessions/{session_id}/workspace` 自动登记/关联目录项目，并在响应中额外返回
`project_id`；切换目录时重新关联对应项目，不移动历史消息。项目列表/会话计数排除已删除会话，
会话恢复后重新计入。旧数据库的 workspace-only 会话在启动时按受允许目录补齐身份关联，
不改会话时间或顺序、不删除消息、不改项目记忆 ID；无目录的会话继续独立存在。

## 11. Mail Import

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

## 12. Mail Search

```http
GET /mail/search?q=document&limit=10&mode=hybrid&order_by=relevance&max_snippet_chars=420
```

响应：

```json
{
  "query": "document",
  "requested_limit": 10,
  "applied_limit": 10,
  "returned_count": 1,
  "possible_more": false,
  "messages": [
    {
      "message_id": "message_id_001",
      "subject": "Visa document reminder",
      "sender": "admin@example.com",
      "folder": "Inbox",
      "received_at": "2026-08-03T09:30:00Z",
      "snippet": "Please submit the missing document by Friday.",
      "document_id": "knowledge_doc_...",
      "chunk_id": "knowledge_chunk_...",
      "source_ref": "mail_message:message_id_001#chunk=0",
      "excerpt_truncated": false
    }
  ]
}
```

---

说明：

- 当前没有 `/mail/process` 或其他邮件专属 agent endpoint。
- `/mail/search` 通过本地 KnowledgeService 检索 `mail_message` 镜像，返回受预算约束的
  正文证据片段；空 query 搭配 `order_by=source_time_desc` 返回最新邮件，但不能用于穷尽式翻页。
  `requested_limit` 和 `applied_limit` 明示最多 100 条的实际检索上限；`possible_more`
  只表示本次候选页已满，**不是**全量总数或精确 `has_more`。
- 邮件能力通过 Tool Package 暴露给后续通用 Agent turn：`mail.list`、`mail.search`、
  `mail.load_messages`、`mail.sync`；`mail.load_messages` 仅用于最多三封邮件的精确原文查阅。
- `mail.list` 是 Agent 工具，不新增 `/mail/list` HTTP 端点。它按本地已授权邮件的
  `[received_from, received_before)` 时间区间、可选 `folder`，以及 1-based 闭区间
  `start_rank` / `end_rank` 取最多 20 封元数据卡片，不返回正文。首次调用返回
  `listing_id`、`total_matches`、`requested_range`、`returned_count`、`coverage`、
  `has_more` 和 `next_range`；继续取页须传同一 `listing_id`，若期间符合条件的邮件
  发生变化或服务进程重启，工具拒绝旧清单并要求重新开始。`listing_id` 不是授权凭据，
  每页都重新校验 scope。主题/发件人/文件夹最多展示 180/120/80 字符，卡片附有截断标志。
  若需要完整清单，按 `next_range` 逐页读取；提前结束必须报告部分覆盖。例如：

```json
{
  "received_from": "2026-09-23T00:00:00+08:00",
  "received_before": "2026-10-01T00:00:00+08:00",
  "folder": "INBOX",
  "start_rank": 21,
  "end_rank": 40,
  "listing_id": "<上一页返回的 token>"
}
```
- `mail.sync` 返回的远端 `next_link` / `delta_link` 是同步状态，不是 `mail.list` 的本地分页游标。
- 邮件整理、概括、匹配等行为应由通用 Agent turn 决定是否调用 mail tools，而不是通过
  mail 路由直接启动独立 agent loop。

---

## 13. List Mail Matters

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

## 14. Matters

独立事务系统用于保存任务、事件、待办和提醒候选项，不从属于邮件。邮件、Agent trace、
本地文件或后续日历对象都可以作为 `source_links` 关联到同一个 matter。

### 14.1 Create Matter

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

### 14.2 List / Search / Update

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

## 15. Outlook Auth Start

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

## 16. Outlook Auth Complete

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

## 17. Outlook Sync

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

## 18. Compatibility Notes

- 当前后端实现是轻量骨架，因此部分返回值是规则化输出而非真实 agent 结果。
- 这份契约保留了未来完整系统需要的字段，便于逐步替换实现。
- 如果某个字段暂时未被使用，应优先保留而不是删除，避免前后端接口反复抖动。

## 19. Multi-Agent Run Snapshot And Controls

### Enablement

Multi-Agent planning is opt-in. Copy `config/local.example.toml` to
`config/local.toml` and set:

```toml
[agent]
orchestrator = "langgraph"
checkpoint_backend = "sqlite"
multi_agent_planning_enabled = true

[safety]
tool_review_mode = "manual"
```

Restart the backend after changing local configuration. `orchestrator = "langgraph"`
is required when Multi-Agent planning is enabled. Use `tool_review_mode = "manual"`
to queue non-read-only tool calls for user approval; `skip` and `llm` are alternative
review policies, not substitutes for the execution-time Tool Executor checks.

```http
GET /agent/runs/{run_id}/snapshot
```

读取 parent run 的多 Agent 只读快照。计划存在时，服务端会重新校验并返回 Plan；计划尚未
创建时，`plan` 为 `null`。`children` 递归列出 child run 的 ID、step / attempt、run 状态、计划
step 状态和已持久化的 TaskResult 摘要。结果可包含 artifact ID 和 EvidenceRef；不会返回完整
artifact 内容、Agent metadata、ContextSnapshot 或上下文 prompt。

```http
GET /agent/runs/{parent_run_id}/stream?after_sequence=42
```

沿用 parent run 的持久化事件流，包含 scheduler 写入 parent event log 的 `subtask_created`、
`subtask_started`、`subtask_waiting_confirmation`、`subtask_retry_scheduled`、
`subtask_completed`、`subtask_failed` 和 plan progress 事件。`after_sequence` 是 parent run
自身的事件游标，SSE `id` 使用 `{parent_run_id}:{sequence}`；断线后用最后收到的 sequence 重连。
此流提供 scheduler 进度，不合并 child 内部完整的 LLM 或工具事件；需要查看某个 child 的事件时，
使用对应的 `/agent/runs/{child_run_id}/events` 或 `/stream`。

```http
POST /agent/runs/{parent_run_id}/children/{child_run_id}/cancel
```

只取消由该 parent 直接拥有且仍处于 queued、running、waiting_confirmation 或 waiting_user 状态的 child；所有权由
scheduler 再次校验。取消后 scheduler 会恢复 parent 编排，以记录取消结果并处理依赖步骤。其他 parent
的 child、未知 child 或不匹配的 parent 返回 `404` / `409`。

```http
POST /agent/runs/{parent_run_id}/children/{child_run_id}/retry
```

手动重试 parent 直接拥有的最新失败或超时 child attempt。scheduler 校验 parent/child
归属、attempt 是否最新、step 是否处于失败状态，以及是否已有该 step 的活动 attempt；无效状态
返回 `409`，不匹配或不存在的 child 返回 `404`。重试完成后响应 parent 的只读 run snapshot，
并继续 parent scheduler。对同一旧 attempt 重复请求不会再次创建 attempt；scheduler 会按当前
child/plan 状态拒绝不再有效的重试请求。

```http
POST /agent/runs/{child_run_id}/resume
```

沿用通用 run 恢复接口，仅恢复有 LangGraph checkpoint 且状态为 `running` 的 run。等待安全审批的
child 必须先通过 safety-review 队列决定；决定后系统恢复该 child，并在其终态时继续 parent scheduler。
child 恢复与 scheduler 使用同一个 runtime graph runner 和 per-run execution lease；若 child 正被
scheduler 执行，checkpoint 恢复会等待该执行结束，不会并行重放同一 child graph。相同 run 的 HTTP
resume 请求在同一进程内也会合并。该 lease 是进程内同步，不提供多 worker / 多进程互斥。
可重试的 child failure 由 scheduler 按本地 `max_retries` 配置自动重试；
自动重试耗尽后，可使用上面的手动重试 endpoint 重跑最新失败 attempt；不可重试或仍失败的 step
留在失败状态，需后续由 Planner 重新规划。

```http
POST /agent/runs/{run_id}/continue
Content-Type: application/json

{
  "command_id": "client-generated-idempotency-key",
  "answer": "Use the last quarter."
}
```

只接受 `waiting_user` 状态的非终态 run 和非空回答。成功后响应包含 `run_id`、`command_id`、
`question_id`、当前状态、`replayed` 与 `resume_scheduled`；不会回显回答。回答与 command ID 保存在
本地 SQLite continuation journal，run metadata 只保留 command ID 引用；`multi_agent_user_answer_received`
事件不包含回答。重复提交相同 command ID 和相同回答幂等；同一 ID 配不同回答返回 `409`。不同 ID 在
同一问题已被回答后也返回 `409`。回答不进入 Safety Review 队列。

`GET /agent/runs/{run_id}` 和 `/snapshot` 在 run 处于 `waiting_user` 时会返回 allowlisted
`pending_user_question`（question ID、patch ID、面向用户的问题和时间），不会返回 metadata 或回答。
若 runtime continuation hook 尚未接入，回答仍会原子保存并将 run 置为 `running`，但响应明确给出
`resume_scheduled: false`；这不代表 graph 已恢复。相同命令重放可在 hook 可用后重新调度。

取消与超时在 scheduler / 工具检查边界生效；已经进入的同步写工具不能被强制终止，可能完成当前副作用后才观察到取消。

## 网络检索与持续关注（本地后端）

Agent 可展开 `web` 包，再调用只读 `web.search`（参数 `query`、`mode=web|news`、`limit<=10`、可选 `freshness=pd|pw|pm|py`）与 `web.open`/`web.find`。搜索需配置 `BRAVE_SEARCH_API_KEY`，无密钥或达到本地月度请求上限时返回工具失败，不伪装成零结果。页面读取只返回有界纯文本；来源 URL、抓取时间、截断状态必须保留。

生产 runtime 的渐进式网页合同（不新增专用 HTTP 端点）：

- search 默认 `view=compact`，每项短摘要附 `snippet_truncated/snippet_total_chars`、候选
  `ref_id`；整体附 `queried_at/search_id/cache_hit`。`web.search(search_id=..., view=full)`
  只读本地标准化响应，不能同时传 query/filter；无新 provider 请求。
- open/find 恰好传一个 `url/ref_id/snapshot_id`。首次 open 默认概要，可选 query 本地
  选择原文，附位置／有界 outline／省略范围；query+max_chars 仍为相关概要，后者只设预算。
  显式 offset（含0）／view=page 或仅 max_chars 为连续页；page 模式的 query_status 明确
  为 not_applied_page，不会假装执行了 query；view_mode 说明实际视图。
  open offset 是 Unicode 字符，find offset 是匹配序号，二者不混用。
- `snapshot_id` 是不可变的有界可读正文，不代表完整 HTML、模型全文阅读或语义核验。
  同快照 find／续页不再联网；expected_text_sha256 校验版本。
- URL 默认复用≤300秒快照；`refresh=true` 或 `max_age_seconds=0` 新抓取。固定 snapshot_id
  不能 refresh，可用 max_age_seconds 拒绝旧版；过期、淘汰或越权固定引用不静默联网。
  默认 retention 一天、256项／64MB，可在 [web_search] 配置修改，重启生效。
- fetched_at／cache_read_at 与来源 publication 时间分开；cache_hit 只描述本系统。
  根会话可跨 turn／重启续读，child 按 run／权限视图隔离；web artifact 不能用通用
  observation.read 跨 run 读取，完整正文不进入常规 HTTP/SSE。
- 注册生产者的 `output_preview_priority_fields`（最多16个有界 output 顶层字段名）
  优先保留证据／续读／版本信息，JSON 持久化排序不影响优先级；未知工具保持默认。
  此元数据只从实际 ToolSpec 读取，结果正文无法授权。超限先舍弃诊断，再减少预览条数，
  整体 gate 仍≤7000字符；原始结果与当前 run 的缓存访问边界不变。
- 注册生产者可选 `output_preview_text_mode=contiguous_pages`：过长字符串展示最多4个
  连续短原文fragment（每段不超过注册叶子上限，最多1200），附缓存值内的字符区间、
  next_offset、省略量；超额仍按原7k上限减少预览。web.open与observation.read启用，
  未知工具/MCP默认仍head_tail；工具正文无授权。fragment区间是该cached value内的
  Unicode位置，不能直接当网页绝对offset；网页续读还须加output.offset。
  最终context_delivery重新核验原文与拟合后的fragment，仅证明缓存值交付，不认证语义。

详情与离线验证见 [渐进检索 TODO／报告](web_progressive_retrieval_todolist.md)。

`POST /watches` 创建每日关注项，主体包含 `title`、`goal`、IANA `timezone`、本地 `daily_time`、`categories` 与显式 `scope`（如 `web_enabled`、`source_ids`、`account_ids`）。响应给出稳定 `watch_id`，不创建固定会话。每次触发都会产生新的 `Occurrence` 和独立 `session_id`，可以在那次会话继续通过通用 Agent 入口追问。`GET /watches`、`GET /watches/{watch_id}`、`PATCH /watches/{watch_id}`、`DELETE /watches/{watch_id}` 管理关注项；`POST /watches/{watch_id}/pause|resume|run-now` 控制触发，`GET /watches/{watch_id}/runs` 查询执行记录。

`GET /watches/briefings?unread_only=true&watch_id=...` 是本地简报列表，`GET /watches/briefings/{briefing_id}` 取单条，`POST /watches/briefings/{briefing_id}/read` 标为已读。成功执行会把简报摘要写入该次执行的新会话，并在简报返回 `session_id`。前端推送尚未实现，可轮询简报或会话变化。计划任务只授予经工具注册表核对的只读 Child Agent 工具；任何私密账户数据与外部搜索混用须显式设置 `scope.allow_mixed_private_external=true`。搜索/网页证据尚不等于已核验的票务状态。

## 本地记忆与后台任务

### 前端配置、控制与观察

以下接口同样受本机访问 / `LKA_MEMORY_API_TOKEN` Bearer 鉴权保护，不能通过
query string 传递 token；它们不暴露 LLM 密钥、原始 prompt、记忆来源正文。

- `GET /background/config`：返回 `revision`、`active`（当前进程配置）、`desired`
  （待生效配置）、`overrides`、`restart_required`、`pending_restart_fields` 和
  `apply_mode="restart"`。配置包含 `[memory]` 与 `[background]` 的字段，不包含
  provider 密钥或其他领域配置。
- `GET /background/config/schema`：返回 memory/background 配置的 JSON Schema，
  前端可以获取类型、默认值和数值范围；模型目录沿用 `GET /agent/models`。
- `PATCH /background/config`：例如
  `{"expected_revision":0,"memory":{"generation_output_tokens":6000,"allow_remote_extraction":true},"background":{"request_timeout_seconds":45}}`。
  只合并提交字段，持久化到本地 SQLite；版本冲突 409，未知字段、非法数值及未知
  background client/model 422。所有这些配置保存后重启生效，不中断当前请求、worker
  或审批；前端必须展示待重启提示，不应将保存成功显示为已经启用。重启时优先级为
  已保存的字段覆盖项 > 当前 TOML > 默认值，未覆盖字段继续跟随 TOML。
- `DELETE /background/config?expected_revision=N`：清除前端覆盖项，回到当前
  TOML/default；同样需要重启和版本校验，不删除记忆、任务或原始历史。
- `POST /background/jobs/{job_id}/retry` 和 `/cancel`：请求
  `{"expected_updated_at":"任务列表中的 updated_at"}`，以任务状态 CAS 防止过期按钮
  重复操作。404 表示任务不存在，409 表示状态已变化或当前状态不能操作。
  重试只支持 `memory_extract` / `context_compact` 的 failed/cancelled 任务；其他类型
  返回 422，不能借此重放 Watch 或外部动作。过期 deadline、已清理 payload 不可重试。
  重试保留任务身份、累计用量和已完成输入 checkpoint，不重新发布已处理记忆。
  若自动重试次数已耗尽，用户明确重试会额外授予一次尝试，但不会清零次数或 token 用量。
  取消会使旧租约失效并阻止旧 worker 发布；它不保证能中止已发出的 HTTP 调用，
  也不能撤销已经完成的结果。
  已经 cancelled 的任务再次取消返回当前状态（幂等成功，不修改时间或租约）；这一
  无写入分支不要求时间戳匹配。若任务已被重新排队/运行，旧时间戳仍返回 409。
- `GET /background/jobs/{job_id}`：获取精确任务状态、尝试次数、失败分类及时间，
  不返回 payload。遇到 409 时前端应先刷新此状态，再让用户决定下一步。
- `GET /background/events?interval=5`：SSE 立即发送 `background_health`，之后
  在聚合健康快照变化时发送，未变化时发送 keepalive 注释。它是采样状态流，不是
  持久化事件日志；断线重连获取新快照，无 Last-Event-ID 重放承诺。前端收到更新后
  可刷新 `/background/jobs` / `/background/config`。配置 token 时使用 `fetch` 流式
  读取并携带 Authorization，原生 EventSource 不能直接设置该 header。
  `interval` 范围 1..30 秒，默认 5 秒；客户端主动取消流即可断连。
- `GET /sessions/{session_id}/context-status`：只读压缩状态、预算/计数信息、摘要
  方法与水位等元数据，缺失或已删除会话 404；不返回完整摘要、消息正文或 prompt。
  字段包含 `method`（无摘要时为 `none`）、`token_count_method`、`token_budget`、
  `token_estimate`、`estimate_as_of`、`summary_sequence`、`pending_sequence`、
  `revision`、`summary_revision`、`recent_message_count`、`degraded`、`degradation`
  和 `active_compaction_jobs`（queued/retry_wait/running 数量）。token 数是持久化窗口
  在 `estimate_as_of` 时的估算，不是当前完整模型请求的真实用量。

全局/项目自动学习开关仍使用 `PUT /memories/learning`，这是立即生效的学习策略，
不同于上述待重启的系统配置。这里不提供 HTTP 自重启接口，重启由本地应用生命周期管理。

`GET /memories` 默认只列出 active 全局记忆；`scope=project` 需要受允许工作区内的 `workspace_path`。`include_candidates=true` 可查看未发布候选。`POST /memories` 接受 `content`、`memory_type`、`scope`、可选 `workspace_path` 和 `sensitivity`，创建用户确认的记忆。`GET /memories/{memory_id}` 查看一条；项目记忆同样需要匹配的 `workspace_path`。`PATCH /memories/{memory_id}` 接受 `content` 和 `expected_version`，生成可追溯的新版本；`DELETE /memories/{memory_id}?expected_version=N` 撤回，版本冲突返回 409。

`GET /memories/learning` 与 `PUT /memories/learning` 查看/设置全局或项目自动学习开关。`GET /background/jobs` 返回 payload-free 排队/失败状态。`GET /background/health` 返回队列深度、时长、租约恢复、失败分类，以及 `backpressured_input_count` 和 `llm_workloads`（活动调用、前台等待、近24小时调用/token/估算费用和预算配置）；不返回 payload、来源 ID 或 prompt。默认本地提取支持中英文直接长期要求，普通偏好先作候选，独立重复证据可晋升。`[memory].allow_remote_extraction` 是模型成本开关，不是额外内容审批；功能开启即授权已持久化会话处理，秘密/PII 检查针对长期发布。学习关闭不删除历史记忆；`[memory].enabled=false` 也停止注入。

`POST /memories/projects/{project_id}/relocate` 接受 `old_workspace_path`、`new_workspace_path`，两者均须处于配置工作区内；保留项目身份，不移动磁盘文件。旧路径不匹配/新路径已绑定其他项目返回409，未知项目404，越界403。旧路径解绑后被重新使用会取得新身份，不继承移走项目的记忆。

`GET /memories` 支持 `limit`（1..500）和 `offset`（0..1,000,000），返回 `next_offset`；`GET /memories/export` 使用同样的 scope/分页参数，返回带版本/导出时间的 JSON 页，需按 `next_offset` 继续读取。分页不是跨请求冻结数据库快照；并发编辑时应重新导出，完整备份使用 SQLite backup。`GET /memories/{memory_id}/sources` 返回来源 ID/type/ref/checksum/status/expiry/时间，不返原文正文。每页及精确来源查询重新检查项目作用域。

冲突条目可在详情 `metadata.needs_review/conflict_ids/conflict_hints` 查看；待审条目不注入根/child 上下文。新模型候选不得屏蔽旧确认偏好。明确日期期限来自用户原文而非模型凭空指定；仅新确认来源可延长已存期限。同源重试不会增加版本，撤回不自动恢复。

`GET /memories/file` 读取生成的全局或项目 `MEMORY.md`；`POST /memories/file/generate` 在文件未被手改时同步生成；`GET /memories/file/preview` 返回可导入编辑；`POST /memories/file/import` 一次导入一个带原 ID/版本的内容块。未知 ID、丢失条目、元数据改动和版本冲突不会被默默覆盖。所有记忆/后台状态接口默认仅接受 loopback 客户端；需要远程访问时配置 `LKA_MEMORY_API_TOKEN` 并发送 Bearer token，配置后 loopback 也必须带 token。此保护不代表旧 API 已具有同等鉴权。

自动侧写超过1000个活跃条目、人工文件超过8MiB时明确报错并建议分页API，不静默截断、不覆盖原文件；这不是用户 AGENTS.md 的读取上限。会话工作窗口的 summary_metadata 记录来源水位、摘要方式、后台模型和 lossy 风险；原始会话仍保留。历史工具观察带 `_cache`（as_of/age/TTL/版本/历史标记），不能代替本轮读取；未知来源版本不声称 freshness 已验证。

后台健康另含 `pending_watermark_count`，统计满容量/运行中等待续作的最新压缩水位，与待提取输入数分开。根 `recalled_memories.items` 的代表来源有 `source_count/sources_omitted`、`content_truncated/read_ref`；child MemoryReference 增加可选 `source_count` 和默认false的 `content_truncated`，旧快照缺字段时按默认值兼容。完整来源通过本地来源API查看，不由来源数量挤满prompt。

Agent 读取使用只读 `memory.search` / `memory.read`；自动记忆不是 `AGENTS.md`，不授权写工具或扩大权限。子 Agent 不隐式继承记忆：PlanStep `input_refs` 可显式指定 `memory:<id>` 或 `memory:<id>@<version>`，服务端校验父作用域、状态、有效来源与版本，ContextDriver 有界装入冻结快照。读取工具仅允许快照内引用，并重新检查撤回/来源失效。会话软删除撤回相应来源，无其他有效来源的派生记忆失效；原始会话/审计行保留，恢复不自动重新发布。完整边界见 [实现说明](memory_background_implementation.md)。
