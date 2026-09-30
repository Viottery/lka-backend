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
  "max_chars_per_chunk": 420
}
```

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
  package 展开动作会作为 functions 发给 provider。没有 function call 表示进入独立 `answer`
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

### 10.4 Append Session Message

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
  正文证据片段；空 query 搭配 `order_by=source_time_desc` 返回最新邮件。
- 邮件能力通过 Tool Package 暴露给后续通用 Agent turn：`mail.search`、
  `mail.load_messages`、`mail.sync`；`mail.load_messages` 仅用于最多三封邮件的精确原文查阅。
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
