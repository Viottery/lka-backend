# LKA：API 接口说明

本文档供客户端和集成方使用，说明会话、Agent、工具、知识、邮件、消息、记忆及后台控制接口。
安装与功能配置见 [安装指南](getting_started.md) 和 [配置说明](configuration.md)。
本文不记录某台电脑的上线状态或测试过程；运行配置和有效授权决定可用功能。

## Base URL

默认地址：

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

默认只扫描并记录文件数量。传入 `options.index_knowledge=true` 时，才会按文件类型策略
将支持的本地文档转换、分块并写入知识库；类型列表见 `GET /knowledge/file-types`。
该操作只接受允许的工作区，保留 `workspace_file` 来源及工作区权限，不启动持续监听。
单次知识索引最多处理 500 个候选文件（也受服务扫描上限约束），单文件最多 2,000,000
bytes，总读取最多 20,000,000 bytes。转换失败、无可提取正文或达到限额时返回
`knowledge_errors`，状态为 `partial` 或 `failed`，不以空结果替换旧证据，也不执行删除清理。
完整成功的重建才会移除已删除文件的旧索引。

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
解析。`filesystem` 对目录外目标申请本次调用的精确路径审批；`bash` 的目录外 `cwd` 同样
需要审批。所有工具调用均走所选安全审查模式，shell 执行仍不是 OS 级文件 sandbox。

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

`knowledge` 是 source-agnostic 的本地证据库。支持导入文本以及显式工作区索引时转换的
本地文档，写入 SQLite FTS，并通过只读工具提供最小必要片段给 Agent。
PDF 使用原生文字层提取；其他支持格式使用本地 MarkItDown 转换。向量同步仍由现有
语义索引配置控制，文件转换不会自动下载模型或调用 OCR、云服务。

`GET /knowledge/file-types` 返回 `{file_types:[...]}`。每项包含 `extensions`、
`preferred_processor`、`index_supported`、`source_mime_type` 和 `notes`，由统一处理
策略表生成。图片、音视频、ZIP 和旧版 DOC/PPT 会列出处理方向，但当前不自动索引。
未知扩展名不进入工作区索引。

转换文档的 `metadata.extraction` 保存处理器和版本、原文件 SHA-256、原始 MIME 类型及
信息缺失提示。存储的 MIME 类型表示提取后的文本／Markdown，不表示原始二进制格式。
PDF 另有页数、页码字符范围及无文字页列表；检索结果与 chunk 回读的
`metadata.extraction` 保留简短警告、来源字符范围和可定位的 PDF 页码。
这些范围指向转换文本，而非原始文件字节。文件修改后须显式重建；提取成功不代表视觉
内容完整，检索快照也不代表源文件仍未变化。`POST /knowledge/import` 继续接受已提供的
文本，保持原有合同。

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
    "response_mode": "json",
    "reasoning_level": "low"
  }
}
```

`llm` 是可选字段；不传时使用会话 / 配置默认值。`model` 是运行时可切换选项，不应由
后端代码写死。`/agent/turn` 返回用户可见结果和脱敏审计摘要；`POST /agent/turn/stream`
提供用户可见的 SSE token-delta 输出。

`llm.reasoning_level` 接受 `off`、`low`、`high`、`max`，普通请求、SSE 和会话输入队列均支持。
新运行按当前请求 → 会话／管理控制器偏好 → UI 默认值 → `low` 选择；单次覆盖不修改偏好。
运行冻结所选档位，排队、恢复及助理派发的执行会话／子代理沿用冻结值，
旧检查点缺少此字段时保留原配置行为。业务决策、语义修复和用户回答遵循该选择；
纯路由与已决定静默的内部交付整理尝试关闭推理，工具结果检查、摘要、记忆和安全审查使用内部策略。
不支持关闭的模型对内部调用回退到声明的低档；没有声明推理控制能力时记录为未控制。
推理档位不改变工具审批模式或危险操作人工审批。

模型映射按精确 client/model 声明；显式选择不支持的档位会在新请求受理前返回 422，
不会悄悄忽略 `off`。默认 `low` 可在未声明能力的模型上按其原协议运行，审计标记未控制。
`max` 可以映射为模型的最高可用档，审计会标明别名和实际生效档位。
首次调用保存该运行的映射快照，后续恢复不受配置映射变化影响。

### 会话文件附件

先创建会话，再上传文件。`POST /sessions/{session_id}/attachments` 接收原始文件字节；
`X-Filename` 是经 URL percent-encoding 的显示文件名，不作为文件路径。文档必须提供
带支持扩展名的文件名。图片的 `Content-Type` 为 `image/png`、`image/jpeg` 或
`image/webp`；文档使用与扩展名对应的 MIME，也可省略或使用 `application/octet-stream`。
类型列表与 `GET /knowledge/file-types` 中的 `index_supported` 文档策略一致：
TXT/TEXT、MD、PDF、DOCX、PPTX、XLSX/XLS、HTML/HTM、EPUB、CSV、JSON、XML 和 MSG。

成功返回 200 及 `attachment_id`、`kind`（`image`、`document` 或 `audio`）、`filename`、
`media_type`、`size_bytes`、`sha256`。图片另含 `width`、`height`；文档另含
`processor`、`total_chars`、`text_size_bytes`、`text_sha256`、`extraction` 解析信息。
旧图片元数据可不含 `kind`，应按图片处理。图片、文档和音频原文件的上传与下载均最多 256 MiB；图片仍限 400 万像素、
单帧，文档提取文本仍限 200 万字符。类型不匹配或不支持返回 415，
上传超限返回 413，解析失败等格式错误返回 422。这些上传和读取接口仅接受 loopback 客户端。

`GET /sessions/{session_id}/attachments/{attachment_id}` 返回附件元数据；
`GET /sessions/{session_id}/attachments/{attachment_id}/raw` 返回经完整性复核的原文件。
文档以 `Content-Disposition: attachment` 下载，避免 HTML 等上传文件作为页面执行。
附件属于上传时的会话；不存在、跨会话、已删除会话或失效附件均返回 404。

`POST /agent/turn` 和 `/agent/turn/stream` 均接受可选 `attachment_ids`：

```json
{"session_id":"session_001","user_input":"解释这张截图","attachment_ids":["att_0123456789abcdef0123456789abcdef"]}
```

每轮最多四个不同的上传附件 ID，允许图片、文档和音频混合。提供附件时必须指定已有会话；
可省略文字，纯图片以 `[Image input]` 记录，包含文档以 `[File input]` 记录，
无文档且有音频以 `[Audio input]` 记录。
`attachment_ids` 省略或为 null 时沿用当前会话最近一轮 Agent 输入的图片和文档选择；
音频必须显式传 ID，避免纯文本消息自动重听及继续使用音频模型。
传 `[]` 明确清除选择，之后的省略也不再带文件。清除不删除附件，新选择替换旧选择，
旧附件仍可通过显式 ID 再次引用。包含图片而未启用所选模型视觉能力时返回 422，
不会降级为纯文字请求。纯文档不要求视觉能力；文件输入使用当前请求的模型选择。

音频扩展名支持 WAV、MP3、M4A；MIME 分别为 `audio/wav`、`audio/mpeg`、`audio/mp4`，
也接受常见别名及 `application/octet-stream`。后端校验实际容器内容，规范化 MIME，
返回 `audio_format`（发送模型的 `wav` 或 `mp3`）、`duration_ms`、`sample_rate`、`channels`。
M4A 本地解码后以 WAV 或 MP3 发送；大音频自动生成压缩的完整 MP3 输入，
原文件下载不变，单个原音频最多 256 MiB，无需手动切分。转换输出有界。有音频时图片与实际音频输入
合计最多 12 MiB，最终内联模型请求最多 20 MiB，超限拒绝，不自动上传供应商缓存。

显式选择音频时，配置的 `llm.audio_client` / `llm.audio_model` 覆盖本轮模型选择，
音频回复直接写入同一会话；下一轮无音频则回到原模型选择。同音频重新听取需再次
传原 `attachment_id`；仅整理已有分析可传 `[]` 或省略。音频模型未配置或未声明
支持时返回 422，不让纯文本模型猜测文件内容。音频只进入前台相关模型调用，
会话、checkpoint、审计与 API 响应保存引用和来源，不保存 base64。音频中的口头
指令是来源数据，不能改变权限；听感也不等同于精确音高、BPM 或时间测量。

原图进入前台主 Agent 的相关推理与回答请求。文档上传时由已有本地解析器提取文字，
之后以带文件名、哈希、字符范围和解析警告的 `input_sources` 进入同一主 Agent 路线。
初始预览最多 12000 字符，并受完整模型请求预算进一步裁剪；长文本保留
`next_start_char` 和续读入口。Agent 展开只读 `attachments` 工具包后，可用
`attachments.read(attachment_id, start_char=0, max_chars=12000)` 继续读取
（单次最多 64000 字符），或用 `attachments.find(attachment_id, query, start_char=0,
max_matches=20)` 定位正文（大小写敏感的字面搜索，最多 100 个匹配）。工具执行仍经
当前审查策略，且仅能访问当前会话，不能传路径或替换会话身份。

会话和运行恢复仅保存附件引用，不把图片编码或提取全文写入用户消息；模型读取时
重新校验原文件和解析缓存完整性。附件不自动入知识库，也不自动传给后台摘要、
记忆提取或子 Agent；附件中的文字不能提供操作许可。PDF 路线依赖文字层，没有文字层
的扫描 PDF 返回解析失败，不自动 OCR；部分扫描页、图片、布局和 Office 公式可能
未被文本转换覆盖，应结合 `extraction` 警告判断。需要视觉理解时另行提供图片。

桌面前端可用 `GET /agent/models` 读取当前配置的 client 与模型列表；接口会尝试服务商
兼容的 `/models`，不可用时退回本地 `available_models` 与默认模型。`?refresh=true` 强制
刷新，响应不包含 API key。`GET /agent/ui-defaults` 和 `PUT /agent/ui-defaults` 读取及保存
本机共享的前端默认设置，字段为 `llm_client`、`llm_model`、`safety_mode`、`stream`、
`workspace_parent`、`reasoning_level`（默认 `low`）。旧客户端保存设置时省略推理字段会保留原值。
默认设置不修改已有运行的冻结配置。模型目录中的 `reasoning_capabilities` 按模型给出
`controlled`、`supports_off`、`effort_map`、`source` 和 `protocol`，供前端展示档位支持及别名。
请求可选带 `safety_review_mode`（`skip`、`llm`、`manual`）。显式选择优先于继承偏好及本地默认配置；省略时使用会话继承偏好，再回退本地配置。
命中工具的危险操作黑名单时，无论选择 skip 或 llm 均要求人工审批；已有待人工审批记录保持原决策入口。
Bash 黑名单包括递归删除（`rm -r/-rf`、`Remove-Item -Recurse`、`rd /s`、`find -delete`）、磁盘格式化／擦除、原始设备覆写，以及 `git reset --hard`／`git clean -f`。常见包装命令、命令替换和终端输入也检查；这是显式命令检查，不是 OS 沙箱或任意脚本的语义分析。
每次 LLM 调用都会在响应的 `llm_events` 中带上 `llm_call_id`、provider / model、
response mode、耗时、usage、finish reason、provider request id、rate-limit headers、
错误分类和 retry 判断等审计字段。完整 prompt / output 仍只写入本地 markdown run log；
第一版暂不新增 SQLite `llm_calls` 表。
新运行的调用审计 `metadata.reasoning` 包含 `requested`、`effective`、`alias`、
`provider_params`、`policy`、`source`、`status` 和能力映射版本；流式与非流式共用解析。
供应商返回的 reasoning token 计数保留在 usage；没有返回时记为不可用，不推定为零。

响应：

```json
{
  "session_id": "session_001",
  "run_id": "agent_run_xxx",
  "trace_id": "agent_turn_xxx",
  "answer": "我已调用本地 mail tools...",
  "task_outcome": "unknown",
  "stop_reason": null,
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
- 长工具结果默认在 prompt 中显示带 `_result_cache.artifact_id` 的规则预览；展开
  `observation` package 后可调用 `observation.read`，传入 `artifact_id`、可选的
  JSON Pointer `path`（默认根路径）、`offset`（默认 0）及 `limit`（1–20，默认 5）。
  返回类型、总量、分页 `has_more`/`next_offset`；字符串可用 `max_chars`
  （1–16000，默认8000）控制窗口，仍兼容显式小页参数。
  注册主动读取（`output_selected_content` 或后端选页回调）在单结果28k tokens／64k字符内
  完整交付，不立即再次按4k折叠；否则沿用规则预览。决策观察采用32k tokens／128k字符
  工作集，根答案可用64k tokens／256k字符；实际客户端 tokenizer（未配置时保守UTF-8
  字节上界）与完整模型请求预算仍兜底，不是扩大模型上下文窗口或子Agent预算。
  `_result_cache.view_status` 区分 `selected_page_delivered` 与 `partial_preview`。
  旧观察省略时保留本run artifact入口和至多4个原始资源续读合同；后续整请求裁剪也保留
  有界续读入口。可见区间以最终 `context_delivery` 为准，不能只按工具的 next_offset
  推断已阅读范围。所有这些字段只描述交付，不授予权限或证明语义正确。
  `fields` 投影中的截断字段附 `read_path` 与字段内 `next_offset`，可省略 fields 按该路径续读。
  数组 `has_more=false` 只表示记录分页结束，不表示所有字段已完整读取。
  artifact 只在产生它的同一个 run 内可读，不是跨会话或子 Agent 的共享引用。
  `observation.search` 默认 `scope=content`：生产者已声明证据角色时只搜索正文/搜索候选
  路径；不会把零匹配回执中的 `/output/query` 当正文。`scope=raw` 显式搜全部原始字段。
  未声明角色的MCP/工具仍支持结构搜索，但返回 `unknown_structure`／未知证据角色。
- Agent 的每次工具调用都会进入配置的逐调用 safety review，包括只读工具；`skip` 仍创建审查记录并自动通过，`llm` 调用 LLM 审查，`manual` 等待用户决定。外部路径访问和非只读操作也必须通过 Tool Executor 的执行时校验。
- `bash.run` 会按具体命令动态判断只读性。白名单之外、写入、信号、stdin 交互等按非只读处理。只读分类不代表命令的任意文件参数已被沙箱隔离。
- Agent core 不硬编码具体 package 名、工具名、领域流程、路由关键词或工具调用示例。
  具体策略必须由 package metadata、tool description、input/output schema、routing hints、
  decision hints 和 cache policy 提供。
- 展开 package 后，Agent 会进入单次 turn 内的 step-limited loop：每一步生成一个
  `decision_event`，动作可以是调用一个工具、展开 package、请求确认，或进入最终回答阶段；
  工具结果作为 observation 进入下一步。默认 step budget 为 32，可通过
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
- 每轮 decision prompt 包含 `completed_tool_calls` 和有界 `task_working_state`。
  已有证据应复用，用户要求的修改、验证等剩余工作仍需完成。无显式
  `repeat_successful_call=true` 的重复成功调用会跳过执行，反馈原结果引用并继续决策，
  不直接结束任务；成功状态变更后允许相同参数回读。原步骤、调用和安全限制继续有效。
- operation-first JSON 可在 envelope 中附 `working_state` 与 `purpose`；native function
  的可选 `_agent_progress` 携带相同两项，服务端在 Tool Executor 校验前剥离，不能作为工具
  参数或授权。严格 schema 用可为 null 的必填 `_agent_progress` 表示省略笔记，新增嵌套对象
  保持属性全部必填。工作状态保存剩余要求、当前缺口、带本 run invocation 引用的结论和排除假设，
  不保存长推理或复制工具正文；Graph checkpoint 会保留该状态。
  连续三次工具调用未接受新的证据关联里程碑、重复调用、每八次工具调用会提示下一次正常
  decision 复盘；未回应具体缺口与完整 purpose 的工具提议最多暂缓一次并收到字段反馈。
  同一段无新证据关联里程碑的停滞不会反复暂缓；未回应状态继续保留和提示，后续工具仍经过
  原校验和安全审查。回应文字本身不复位暂缓额度，状态随 checkpoint 保留。下一次正常决策
  仍使用原步骤额度。不增加逐步模型审查，不按读取次数强制编辑或收尾。
  换 invocation ID、命令包装或摘要不构成新证据；复盘回应不清零已消耗成本。
  引用和新颖性检查是结构启发式，模型声明的进展不等于语义正确或任务完成。
- 正常响应及持久结果增加 `task_outcome=unknown|incomplete` 和 `stop_reason`。
  `unknown` 不认证任务成功；协议修复耗尽、步骤额度或执行时限收尾明确标记 `incomplete`，
  `completed` 继续表示运行生命周期结束。子任务结果保留这些终止原因及 partial 状态。
  无效操作会得到字段路径反馈，修复仍受原控制调用额度约束，不自动把工具名转换为操作类型。
- 已知传输断开统一记录 `network_failed`，保留可用 request ID、partial、usage 和原因链。
  无流输出时可在现有尝试额度内重试；控制生成与格式修复共用最多两次调用，单次尝试配置
  不放宽网络重试。已有部分流输出时不透明重启，未知用量预留保留，工具不重放。
  可信运行设置的执行 deadline 会限制后续模型和工具派发，并为正常收尾预留时间；
  harness 与模型服务层排队后都会复验 deadline／取消状态，继承原工作负载的额度与派发栅栏；
  普通会话内容或工具文本不能设置该运行控制字段。
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
- 同一 session 的后续独立 root 先获得按当前任务相关度选择的有界进度／证据索引，
  包含来源 run、时间、实际操作句柄、剩余要求和未验证的历史结论，不预装旧工具正文。
  `progress.search(query?,limit?)` 检索最近 16 个 root 索引，limit 默认为 8、上限 12，
  coverage=bounded 不表示遍历全部历史。`progress.load(source_run_id,evidence_id)`
  在复验会话、项目、工作区和当前权限后将允许的旧结果复制为当前 run 的缓存句柄，
  返回可直接执行的 `read_request`；注册元数据声明且实际存在唯一正文时，该请求直达正文字符串。
  多正文或未知结构仍以完整输出对象为入口，`source_content_reads` 提供有界正文路径，
  `container_read_request` 始终保留完整结果入口。正文路径只来自注册声明，不接受来源自报。
  再经 `observation.read/search/group` 分页读取；不会开放 observation 的任意跨 run 读取。
  失败轮的实际成功操作也可继续读取，历史工作记录不证明业务完成，不授权重复写入。
  来源运行撤回、缓存原文变更、权限收窄或 history_reset_at 变化会使旧索引／已复制句柄失效；
  经读取、检索或分组产生的二级及后续工具结果保留服务端来源链，后续读取递归复验来源与版本，
  并继承历史标记；循环或超过有界深度的来源链拒绝读取。来源正文中的自报字段不能建立来源关系。
  child 不继承更广历史范围，跨会话只能经另有服务端归属授权的管理结果工具读取。
  完整读取后仍标 historical_only=true、current_evidence=false，当前状态需另行核验。
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
工具名 `bash.*` 在所有平台保持兼容：Linux 使用 bash；Windows 使用 Windows PowerShell
5.1（UTF-8 输入输出、EncodedCommand），后台终端使用 ConPTY，要求 Windows 10 1809+
或 Server 2019+。Windows 命令使用 PowerShell 语法和 `$env:WORKSPACE_ROOT`，不自动转换
Unix 命令；工具元数据会说明实际 shell。

`filesystem.read_file` / `filesystem.edit_file` / `filesystem.create_file` / `filesystem.list_directory` 的 `path` 字段也使用同一 workspace path
基准：相对路径按当前会话 workspace（未设置时为第一个 configured workspace root）解析，并支持 `$workspace_root`、
`${workspace_root}`、`$WORKSPACE_ROOT`、`${WORKSPACE_ROOT}`、`$LKA_WORKSPACE_ROOT` 和
`${LKA_WORKSPACE_ROOT}` 展开。

`filesystem.list_directory` 是只读的浅层目录发现工具，不读取文件正文或递归跟随符号链接。
`path` 默认 `.`，`limit` 默认 100、最大 200；后续页传 `offset` 和上页的
`snapshot_id`（作为 `expected_snapshot`），名称或类型变化后须重新枚举。
每次最多枚举 2000 个条目；`enumeration_complete=false` / `truncated=true` 表示目录
并未完整枚举，应缩小目录范围。`has_more` 仅表示已枚举条目还有下一页，不能证明目录
所有条目已读取。目录外访问及子运行路径授权与其他文件工具一致。

路径协议：

- 未传 `cwd` 时，默认使用当前会话 workspace；未设置时使用第一个 configured workspace root。
- `cwd` 可以是绝对路径，也可以是相对路径；相对路径按上述默认 root 解析。
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
等可在 Linux 判为只读。Windows 的保守字面命令白名单包括 `Get-Location`、
`Get-ChildItem`、`Get-Content`、`Get-Item`、`Test-Path`、`Select-String`、`Get-Process`、
`Get-Date` 及相应支持的别名。PowerShell 表达式、变量展开、管道和重定向均进入审查；
无法判断、非白名单命令默认非只读。

可选参数 `python_environment: "default"` 使用显式准备的独立 Python 环境，始终按
非只读调用审查。通过 `bash.python_environment` 查询实际可用状态、解释器、Python 与
包版本和脚本／输出目录。Linux 用 `"$LKA_PYTHON" script.py`，Windows 用
`& $env:LKA_PYTHON script.py`。同步、后台、超时和退出码合同保持一致；环境未准备时
明确失败，不回退到后端 Python。继承环境使用系统变量白名单，Linux 不加载 shell profile。

`filesystem.create_file` 参数为 `path`、UTF-8 `content`，仅在父目录存在且目标不存在时创建，
返回 `resolved_path`、`sha256`、`bytes_written`、`created`。调用经过写审查、工作区及子运行
路径范围检查，目录外目标需要本次精确路径批准。

### 7.1.1 PDF 与视觉工具

这些能力同样通过通用 Agent 工具协议调用，无专用任务 HTTP 入口。

| 工具 | 主要参数与行为 |
| --- | --- |
| `documents.open` | `path`；返回 `sha256`、`page_count`、目录和元数据，尚未提取页面文字 |
| `documents.read_pages` | `path`、`expected_sha256`；可选 `start_page`、`start_char`、`max_pages`、`max_chars`、`mode`（`plain`／`layout`）；返回页码、文字与续读 `next` |
| `documents.find` | `path`、`expected_sha256`、`query`；可选起始页／字符、页数和匹配数；返回文字层的区分大小写匹配和续查位置 |
| `documents.render_page` | `path`、`expected_sha256`、`page`、`output_path`；可选 `dpi`；不覆盖地写入 PNG，返回 `image_sha256` 和尺寸，按写操作审查 |
| `vision.inspect_image` | `path`、`question`；可选 `expected_sha256`、`client_name`、`model`、`max_output_tokens`；经过审查向模型发送本地图片，返回解释、来源指纹和模型调用审计 |

PDF 页码从 1 开始，字符偏移从 0 开始；每次调用重新授权路径并校验原文件。
文字层缺失和视觉保真未验证会显式标记，截断内容可继续读取。PDF 上限 64 MiB，
一次读最多 10 页／64000 字符，一次搜索最多 50 页／100 个匹配；加密文件明确失败。
渲染 DPI 为 72–200，单页最多 1600 万像素。视觉工具接受单帧 PNG／JPEG／WebP，
上限 10 MiB／400 万像素，模型须显式配置视觉能力。视觉调用目前不提供给子 Agent，
以免绕过子运行的模型调用和 token 预算；主运行保留共享模型资源控制和工具调用审计。
视觉模型调用的审计与用量位于工具结果的 `llm_call` 字段，目前不并入 Agent 的顶层
`llm_events` 汇总；客户端统计模型总用量时须同时读取这两类记录。容量检查使用保守
图片 token 估计；未配置模型容量时只能依赖提供方拒绝超限请求。
视觉工具默认最多输出 1024 token，可显式设置为 128–4096；回复不完整或达到输出上限
时工具报告失败，保留部分内容和调用审计，避免把截断内容当成完整阅读。

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
- `bash.interrupt_session`: POSIX 发送 SIGINT，Windows 向 ConPTY 发送 Ctrl-C。
- `bash.terminate_session`: 终止后台终端及其子进程树；POSIX 使用进程组，Windows 使用 Job。

同步命令超时、终端终止及后端正常关闭时同样清理对应进程树。后台终端输出可能包含终端
控制字符；前端继续按既有 output/offset 合同消费。

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
      "tool_risk": "medium",
      "side_effects": ["write_local_file"],
      "read_only": false,
      "mode": "manual",
      "reason": "Tool is explicitly non-read-only.",
      "input_fields": ["path", "expected_sha256", "edits"],
      "workspace_access": [],
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

公共审查响应和 SSE 摘要包含 `decision_reason` 与 `workspace_access`，不包含原始 `tool_input`、
实际 `user_request` 或审查 LLM 输出；这些内容仅保存在本地审计记录。审查使用该 run 的实际用户
请求作为上下文。批准只绑定当前调用；之后用户明确授权的操作仍会创建新的逐调用审查。

`workspace_access` 列出本次调用申请访问的工作区外规范化路径。`filesystem.read_file`、
`filesystem.edit_file` 的目标文件，以及 `bash.run` 的 `cwd` 超出当前允许根时，会请求对此
精确路径的审查；工具执行前会重新解析路径，以便符号链接目标变化时失效。Child Agent 的
ContextSnapshot 中收窄的路径范围仍是不可扩大的上限。Bash 的精确路径批准只控制工作目录；
安全审查同时评估整条命令及其间接文件访问。命令在后端 OS 用户权限下执行，不是文件沙箱。
前端应在运行状态和流程视图中展示 review reason 与 `decision_reason`；`manual` 等待用户决定时，
应显示待批准的 `workspace_access` 路径。

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
POST /agent/runs/{run_id}/interrupt
POST /agent/runs/{run_id}/resume
POST /agent/runs/{run_id}/replace
```

`cancel` 会立即把 run 标记为 `cancelled` 并记录 durable cancellation request；正在执行的
provider 调用无法保证被强制中止，但其后续 graph node、工具和 finalize 副作用会在下一个节点
边界被阻止。重连的回答类 `llm_delta` 保持 `content_snapshot` 字段兼容性，但 SQLite 只保存 delta 并在
回放时重建快照。完成前会先 flush token delta，之后才发布 `run_completed`。

服务重启后，处于 `running` 状态且保留 LangGraph SQLite checkpoint 的 run 可由
`POST /agent/runs/{run_id}/resume` 显式恢复。`waiting_confirmation` run 必须继续通过对应的
safety-review decision 恢复；终态 run 和 legacy orchestrator run 返回 `409`。同一进程内的重复
resume 请求会合并为同一个恢复任务。

### 可恢复打断与新消息替换

创建新任务时，可在 `/agent/turn` 或 `/agent/turn/stream` 正文中增加 `"resumable": true`，
让该次任务使用带检查点的 LangGraph。即使默认配置仍为 legacy，也无需修改全局配置；
不会切换模型或修改阶段推理策略。默认不传时沿用原 orchestrator。运行方式绑定在服务端
run 记录中，恢复、重启和替换沿用该绑定。已经开始的 legacy 运行不能补造此前的检查点。
SQLite 检查点支持进程重启恢复；`checkpoint_backend="memory"` 只支持当前进程内继续。

`POST /agent/runs/{run_id}/interrupt` 返回 `202`，请求在 LangGraph 节点之间暂停。
运行中的模型请求或工具步骤不会被强杀；当前步骤结束、检查点写入完成后，状态变为
`paused`，发布 `run_paused`。此前响应的 `pause_requested=true` 只代表请求已登记，
不能当作已经停止。请求和暂停状态持久化；重复请求幂等。终态或不支持的运行返回 `409`，
未知 run 返回 `404`。

打断以根 run 为入口，原子登记整棵任务树的暂停意图，停止新节点派发及新的 fork。
活跃的通用子 Agent 保存各自检查点；尚未派发的子任务停留在原 attempt。
根 run 只有在自己的检查点写完、全部后代已暂停或终结后才发布 `run_paused`。
子任务单独出现 `run_paused` 不等于整棵树停稳。已完成节点和已产生结果保持不变。

`AgentRunResponse` 增加 `paused_at`、`pause_requested`、`paused_from_status` 和
`superseded_by_run_id`。SSE 发布 `run_pause_requested` / `run_paused` / `run_resumed`；
看到 `paused` 后排空已记录事件并关闭连接。暂停不是取消；继续后用原 run ID 和最后的
sequence 重新连接 `/stream`，不会从头回放已完成的工具操作。

暂停后可选择：

- **继续**：沿用 `POST /agent/runs/{run_id}/resume`，从原检查点继续原任务。若暂停前
  正在等待审批或用户回答，则恢复到该等待状态，仍需走原有 decision / continue 接口；
  普通 resume 不代替审批或用户回答。暂停尚未落稳时返回 `409`。重启留下的待暂停请求
  可在空闲检查点处确认后继续；恢复失败保留错误，不宣称任务完成。
  根任务恢复时一并恢复暂停的后代，复用原 DAG、上下文快照及 attempt，不重放已保存的
  工具步骤；预算使用记录不清零。父任务仍在暂停、待暂停或已取消时，单独恢复子任务返回 `409`。
- **用新消息替换**：`POST /agent/runs/{run_id}/replace`，正文沿用 `AgentTurnRequest`，
  另加必填非空 `command_id`（最长 128 字符）。省略 `session_id` 时使用原会话，显式
  指定则必须相同；附件继续遵循原请求的上传和会话校验。模型与审查选项按新请求正常解析。
  仅已暂停且执行租约空闲的根运行可替换；尚在执行时快速返回 `409`，不在 HTTP 请求里
  等待模型结束（仅允许最多 100ms 的旧 worker 收尾等待）。旧 run 标记 `cancelled` 并指向新 run，新 run 在原会话排队执行，返回
  `202 AgentRunResponse`。前端连接**新 run ID**的 `/stream` 观察新任务。

替换的旧状态、新 run 与关联事件在一个 SQLite 事务提交。同一旧 run、同一 command ID、
同一完整请求可重试，返回同一个新 run；变更 ID 或内容返回 `409`。重启后也保持这个关联，
若新 run 尚未启动，重发相同替换请求可重新派发。不会删除原用户消息、工具记录或已发生的
副作用，也不会把取消的旧任务再次恢复。已有写操作**不会回滚**。
事务同时取消暂停的后代，将旧 DAG 标记 `cancelled`、未完成节点标记 `cancelled`，
取消其调度资格而不删除 DAG、父子关联、检查点、审批或 trace。旧树的迟到结果只能留在
旧 run 的记录中，不能完成旧任务或混入新任务。

新任务在模型观察中获得 `replacement_handoff`：先前工具名称、参数预览、报告的结果状态
和效果未知标记。完整操作、旧运行事件及历史 DAG 存入新 run 自己的缓存，模型可展开
`observation`，通过 `observation.read` 读取 `_result_cache.artifact_id` 的
`/output/operations`、`/output/traces` 或 `/output/historical_dag`；不开放任意跨 run 回读。
连续替换保留此前替换链上的操作信息。交接资料不是新的执行授权，不代表写操作已撤销；
无返回结果的调用保守标记未知，回退前应核查现状并取得用户授权。没有修改前快照的操作
不保证可撤销。工具原始参数与结果继续属于模型上下文／本地审计，不新增普通 API 的公开字段。

```json
{
  "command_id": "replace-unique-client-id",
  "user_input": "停止原任务，改为整理已经找到的资料。",
  "llm": {"response_mode": "stream"}
}
```

可恢复打断需要 LangGraph 根运行；`resumable=true` 根任务的通用 ReAct 子任务自动沿用
检查点运行方式。未选择可恢复运行方式的 legacy 根任务继续保留原有 cancel。
当前 `can_resume=false` 的 workflow／外部专家没有可恢复检查点：已获准开始的执行需要
先安全结束，不会伪造“已暂停”或强杀后重放；此期间根任务仍为 `pause_requested`，
不能替换。尚未开始的专家子任务可以一起暂停。外部执行若仍等待审批，需要按原审批
机制解决等待，或选择永久取消；此功能不补造第三方进程的恢复能力。外部专家自己的
私有原生 trace 继续由原有接口保留，通用交接不保证重建其未登记到工具协议的内部调用。
执行租约为进程内互斥，不提供多个后端 worker 对同一图的并行执行保证。

安全审查 API 与 SSE 的 `safety_review_*` 事件只返回公开摘要（review ID、工具、风险、状态、
理由、`decision_reason`、`workspace_access` 及时间）。原始 `tool_input`、run 的实际 `user_request`
和审查 LLM 输出属于本地审计材料，不会进入普通 HTTP/SSE 响应。

`POST /agent/turn/stream` 默认以 `llm.response_mode=stream` 运行。所有 Agent runtime
中的 LLM stage 都可以流式接收并在本地审计 provider token delta。公开 HTTP/SSE 对
`answer` / `context_answer` 以外的 delta 只保留 stage、role、调用标识等状态信息，清除
`message`、`delta`、`content_snapshot` 及其他正文，避免控制 JSON 中的内部工作笔记或来源材料泄露。
Agent 必须等完整内容累计完成后再解析 JSON 和执行下一步，避免半截 JSON
驱动工具调用，也不能用 JSON stage 的 delta 解析或显示最终答案。最终自然语言回答必须来自
`answer` / `context_answer` stage 的 `llm_delta`。回答类 `llm_delta.payload` 至少包含：

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

列表、详情与回收站中的 session 另含服务端保存的 `creation_source` 和 `creator_session_id`：
`user` 表示用户／客户端直接创建，`assistant` 表示助理或子 Agent 创建，`system` 表示定时关注等系统创建，
`unknown` 表示旧记录没有足够的创建证据。该来源独立于 `kind: ordinary|manager`，不会因后来介入、
补充指令、删除或恢复改变；用户创建的主助理会话仍为 `user`，助理派发的执行会话与事务线为 `assistant`。
`creator_session_id` 为发起创建的会话 ID，无会话发起者时为 null。metadata、标题、提示词以及普通
`POST /sessions` 中自报的来源不能改写这些字段；它们不赋予额外读取或执行权限。

`GET /sessions` 和 `GET /sessions/deleted` 可用 `creation_source=user|assistant|system|unknown` 与
`creator_session_id=session_xxx` 独立或组合筛选，筛选在分页前执行，参数非法返回 422。省略时仍返回全部来源。
例如 `/sessions?creation_source=user` 获取人为创建的会话，
`/sessions?creation_source=assistant&creator_session_id=session_xxx` 获取指定助理派发的会话。
旧库启动时自动加列、建立索引，并依据明确的创建命令、委派／子运行及定时关注关系回填；
仅有介入关系或可编辑 metadata 不作为创建证据，其余保留 `unknown`，不删除或重排历史消息。

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

前端清理未使用的空会话时，可传入条件保护：

```http
DELETE /sessions/session_xxx?only_if_empty=true&expected_updated_at=<session.updated_at>
```

`expected_updated_at` 使用会话详情中的原始时间字符串。此模式在同一数据库事务中
检查版本一致、没有任何消息且没有任何 Agent run；缺少版本、版本已变化或已有工作时
返回 `409`，保留会话。正常删除不传这些参数，行为不变。条件删除仍是可恢复的软删除，
不删除工作区文件，也不实现超时物理清理。

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
祖先目录替换竞态；预览只接受普通文件，FIFO 不等待写入者。Windows 使用已打开父目录
相对 `NtCreateFile` 的句柄链、句柄目录枚举和 reparse 校验，避免先检查路径再打开；
拒绝 symlink、junction、OneDrive 占位等所有 reparse point，以及 ADS、DOS 设备别名、
尾随点/空格路径，不退回仅路径检查。UNC 解析支持，但本轮只验证本地 NTFS，SMB 尚未实测。

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

### 10.9 管理特殊会话

会话列表、详情和回收站的 session 增加 `kind: ordinary|manager`，历史记录默认为 ordinary。
普通创建接口或 metadata 中的类型标记不会把会话转换为 manager。

以下接口需启用 `[management].enabled=true`，否则返回 409 `management_disabled`；
沿用现有 loopback／`LKA_MEMORY_API_TOKEN` 校验。未引入账号或设备身份体系。

| 接口 | 行为 |
| --- | --- |
| `POST /managers/default` | 并发幂等获取或首次创建主控会话，返回 200；已删除默认会话返回 409，不自动重建 |
| `POST /managers` | 创建另一管理会话，返回 201；可传 title、make_default、llm_client_name、llm_model、reasoning_level |
| `GET /managers?limit=50&offset=0` | 返回未删除管理会话数组，limit 为 1..200 |
| `GET /managers/{session_id}` | 返回一个管理会话与 controller，不是管理会话则 404 |
| `PATCH /managers/{session_id}` | 修改 status、模型或推理默认值，必须携带 expected_revision；冲突 409 |

单项响应为 `{session: {...}, controller: {...}}`。controller 包含 session_id、
status(active/paused)、revision、llm_client_name、llm_model、reasoning_level、created_at、updated_at、default_execution_workspace。
默认执行父目录通过创建或 PATCH 的 default_execution_workspace:{path,platform} 配置，后端验证并返回 backend_path；null 清除。PATCH 必须使用 expected_revision。POST /managers/default 的目录仅在首次创建时采用，后续不覆盖已有配置。
此字段不赋予主管自身 session.workspace，也不是审批授权。
新建时 paused；模型字段省略保持原值，显式 null 清除。未知 client 返回 422。
`reasoning_level` 接受 off/low/high/max；省略保留偏好，null 清除并使用共享默认值。
make_default=true 表示显式切换默认入口，主控会话无需固定 ID。

对话仍通过通用 `/agent/turn` 或 `/agent/turn/stream`。请求模型覆盖优先于会话默认；
运行冻结其实际会话策略，恢复沿用原设置，替换的新轮读取当前会话设置。
推理档位在替换时默认保留原运行选择，显式传入新档位才覆盖。
主管会话开放 `management`、专用 `history` 包、`matter` 读取及轻量主管写入工具，以及当前注册且已授权的只读操作。支持按参数只读的工具可被发现，但每次调用均强制验证只读；其他写入不能通过这一授权执行。主管复杂工作默认下发；简单搜索、单次操作可直接处理。
主管角色策略在首次路由前装载并冻结，普通执行会话不继承；原有安全审查与审批独立生效。事务线子 agent 的工具能力按委托冻结；默认执行目录与文件访问授权分别处理。
单条事项可在 `matter` 包使用 `management.create_matter` / `management.update_matter`，无需展开整套管理工具。
两者复用 `management.organize_work` 的事务、修订和自主发布检查；未开放普通 `matter.update` 等即时写工具。
controller active 才能发出管理写命令；paused 可对话和读取结果，新轮工具目录仅开放管理读取，并明确提示管理暂停。运行创建后才切为暂停时，管理写工具返回 management_paused、task_accepted=false、retryable=false，不能把安全审查通过解释为任务已受理，也不应更换 command_id 重试。普通任务和 child 不开放管理包。

默认全局助手支持独立的持久用户消息队列；需启用管理功能和 SQLite 检查点。
它不依赖 controller active 或自主运行开关。普通会话及事务线子 agent 不开放此入口。

| 接口 | 行为 |
| --- | --- |
| `POST /sessions/{session_id}/inputs` | 接收并持久保存一条用户消息，返回 202 和入队回执 |
| `GET /sessions/{session_id}/inputs` | 查询队列状态及消息处理回执，不执行模型调用 |
| `POST /sessions/{session_id}/inputs/resume` | 显式继续因失败或取消而暂停的队列，不重试失败消息 |

入队正文包含 `client_message_id`（客户端生成、非空、最长 128 字符）、`user_input`、
`attachment_ids`（无附件传空数组），以及可选 `safety_review_mode` 和
`llm: {response_mode: "stream"|"text"}`。空文字需要有效附件。
可选 `replace_run_id` 为非空、最长 128 字符的运行 ID：显式替换本会话已暂停的根任务，
并将本条输入直接关联替代运行。运行中、等待确认／答复、目标已变化或执行租约仍被占用时返回 409，
不保存新消息或创建替代运行。该字段也属于请求幂等指纹；精确重试始终返回原回执。
同一会话与消息 ID 的相同请求返回原回执，变更内容复用 ID 返回 409。队列最多保留 100 条未完成输入，已满时新输入返回 409；原请求重试仍返回回执。
附件使用既有上传接口，随每条消息独立绑定，不继承另一条消息的附件。

入队响应为 `{input: {...}, queue: {paused, pending_count, active_run_id}}`；
查询和继续响应为 `{inputs: [...], paused, pending_count, active_run_id}`。
每条回执包含 `input_id`、`client_message_id`、`message_id`、`sequence`、`status` 和 `run_id`；
状态为 queued/running/completed/failed/cancelled，尚未创建运行时 run_id 为 null。
已接收仅代表持久保存，不代表模型已读取或已完成请求。

队列按服务器受理顺序处理。当前主运行结束后预留下一轮；该轮首次执行、读取模型上下文前，
原子冻结当时已受理且设置兼容的连续消息，合并为一次普通用户轮。冻结后到达的消息留到后续轮次，
恢复检查点不会扩充原批次，不将新消息注入正在执行的模型请求，也不抢占后台运行。
每条消息保留原文、附件关联及独立幂等回执；同一批回执可以共享 `run_id`，该批统一回复一次。
批次仅合并相同 `safety_review_mode` 和 `llm.response_mode` 的连续输入，去重后的附件合计最多 4 个，
同时遵守既有媒体大小限制及批次文字上限 100000 字符；不兼容时在该条之前结束批次，保持 FIFO，
不丢弃已接收输入。原有单条输入和回执字段保持兼容。
暂停或等待确认／答复的主运行继续保留会话位置；队列继续接口不替代原运行的恢复、审批或答复。
已暂停时带 `replace_run_id` 发送新指令，复用原运行替换语义，取消旧任务并立即派发新轮；
消息保存、替代运行、队列观察位置与派发 outbox 在同一事务提交，其他排队消息保留，随后继续处理；替换轮不会合并其之前排队的旧指令。
原消息、操作记录和已完成的操作保留；不带该字段仍按普通 FIFO 排队。桌面助理在已暂停时发送的第一条新指令会带上该字段，
空输入点击“继续”则通过原 resume 接口恢复原任务。
用户轮失败或取消暂停后续自动处理，保留排队消息供用户显式继续；后台管理轮的失败或策略取消不会暂停用户消息队列。
历史版本已经写入且原因不明的暂停继续保留，需用户显式继续，不自动重放旧指令。
关闭页面不会取消队列，重启后依靠持久状态和原检查点恢复；不重放效果未知的工具操作。
运行观察沿用 `GET /agent/runs/{run_id}/stream`，通过查询队列取得后续 run_id。
原 `/agent/turn` 接口的忙碌冲突合同保持不变。

顶层主管专用历史读取（普通会话和事务线子 agent 均不获授）：

- `management.search_history(query?,kind?,limit?,offset?)`：在未删除的普通／管理会话标题和正文中按区分大小写的字面量子串检索；空 query 列出最近会话。kind 为 all/ordinary/manager，limit 为 1..50。
- `management.read_history(session_id,message_id?,start_char?,order?,limit?,offset?)`：读取 user/agent/tool 历史正文，默认 newest，可选 oldest。单条至多 4000 字符、单页至多 12000 字符；截断消息用 next_message_id/next_char 续读，或沿 next_offset 与 next_char（无值时为 0）继续分页。

每次读取重新验证主管执行身份及来源删除状态，同页来自一致快照。结果不包含消息 payload、运行 metadata、系统指导或工具日志；历史正文仅为引用材料，不授予执行或审批权限。

工具包括 `list_sessions`、`read_session`、`list_runs`、`inspect_run`、`start_task`、
`send_instruction`、`control_run`、`list_goals`、`read_goal`、`read_events` 和 `set_goal`。工具调用继续走包展开、schema 校验和统一安全审查。
写命令必带稳定 `command_id`（1..128 字符），相同参数重试返回同一持久回执，参数改变则冲突。
`start_task` 接收 purpose、可选 title/workspace_path，原子创建 ordinary 会话、独立 LangGraph root、
会话关联和派发记录；回执包含 command_id、session_id、run_id、status=accepted、action。
新回执另含 `snapshot_kind=command_commit`、`snapshot_at` 与 `run_snapshot`；
替换还含 `replaced_run_snapshot`，分别描述新 run 已排队和旧 run 已取消，不能沿用介入前的 paused。
`accepted` 是命令受理状态，不能当作运行完成；暂停请求中的 `pause_requested=true` 不等于已暂停。
回执快照随命令固定，幂等重放保持原值；需查询当前状态时使用新的只读查询。
workspace_path 支持已有会话路径协议（含 Windows 到 WSL 映射），仍验证工作区根。省略时使用 controller.default_execution_workspace 创建独立任务目录；未配置可接受无目录的数据任务，但其冻结路径为空，不开放工作区工具。默认执行目录在受理时冻结，不受后来默认目录或会话目录修改影响，也不回退到后端首工作区。有执行目录的助理任务不把该目录作为文件访问边界；工作区外文件与 Bash cwd 进入现有逐调用安全审查，审批模式继承发起轮选择，命中危险操作黑名单时等待用户审批，工具白名单与数据源授权不变。指定另一位置的新项目时，可选择其已有父目录，让执行会话创建项目目录与文件。
`management.start_task` 的可选 `workspace_path` 可省略或传 null，两者均使用已配置的前端默认独立工作区；显式路径仍须为有效的已有目录，审批照常执行。

`management.read_task_result(run_id,section?,start_char?,max_chars?,evidence_offset?,invocation_id?)` 返回本主管及直属事务线所属独立 root 的执行状态和分页结果，事务线仅能读取自己任务；普通或 child caller 不获授。section 为 answer（默认）、evidence 或 observation。结果还附有界 `execution_receipts`：保留已注册 Matter 写工具明确返回的记录 ID、修订号、标题、状态与日期字段，并标明未确认执行、缺失字段或截断；仅为历史执行回执，不代表当前状态、语义正确或业务事项完成。可按回执的 `read_evidence` 和证据长度继续读取完整的安全字段投影。`mail_coverage_receipts` 经过与历史来源读取相同的权限、来源版本及操作记录校验，提供已执行查询的时间范围、数量、分页状态和各邮件正文交付长度／截断标记，不提供正文、发件人或语义判断；来源不可重验时标为不可用，必要正文沿 `read_observation` 获取。两类回执的默认预览总量有界（不超过 12000 JSON 字符），省略项在分页证据中仍可读取。evidence 的 content 为 JSON 字符片段，按 next_char 拼接后解析；保留实际持久工具证据的白名单字段，不包含系统提示、metadata、工具输入或 shell stdout。每次结果附有界 evidence_index（按 evidence_offset 每页 20 条）、successful_operation_count、error_type、task_outcome 和恢复提示，失败或空回答也可保留成功工具结果；操作计数来自原始工具结果 artifact，不代表业务完成。section=observation 必带 invocation_id，仅为仍授权的只读、非 Bash 原工具结果返回当前 run 的 observation 句柄；缓存 reader 的代理输出不能代替原工具权限，需选择原始生产者的证据条目；其 evidence 回执只保留状态与索引，不投影代理正文；原文通过 observation.read/search/group 按需读取。每次回读复验归属、会话存活、权限和原始／复制内容版本，完整读完仍标 historical_only=true、current_evidence=false。deleted、非归属和 child run 均拒绝，证据不代替新操作审批。
任务／会话已删除或缺失时，结果和状态读取工具返回 task_source_unavailable、retryable=false，不返回已删除数据；这是来源生命周期拒绝，不表示安全审批失败。尚未消费的旧 task_terminal 事件在唤醒前复验所属会话、管理关联及运行终态，来源已不可读取的事件标记为 obsolete，保留审计记录。
消息 payload 的 input_origin/input_envelope 标记内部 manager/background 输入。客户端应保留审计记录并按结构化来源从普通聊天隐藏，不能把所有非 user 角色都渲染为 agent 回答。
`send_instruction` 必带 session_id、purpose、expected_run_id；最后一轮已终态或无历史可启动下一轮，
paused 时原子替换原轮。运行中先 pause 并等暂停确认，等待确认或答复不可替换；目标已变化则拒绝。
`control_run` 接收显式 run_id 和 pause/resume/cancel；resume 只用于 paused，
waiting_confirmation/waiting_user 使用原有确认／答复接口。管理暂停不连带取消独立任务。
已关联独立任务进入等待用户／审批时会捕获去重提醒，唤醒空闲主管；旧问题已答复或取消后不再交付。
兼容存储的 kind=timer、payload.task_waiting=true 表示该提醒，而非目标定时到期。
waiting_user 快照可含有界 pending_user_question 与原 run 的 user_control.continue_path；
waiting_confirmation 仅提示 requires_original_approval，不转交审批权限。主管处理提醒后可以接收其他委托，原工作线仍等待原用户入口。
管理输入的发送方、目的、原话引用和有界调试信息冻结于本地运行记录，非用户输入保存为 tool，
不会作为用户偏好提取；普通运行列表不返回完整提示。

`GET /sessions/{session_id}/runs?limit=30&offset=0` 返回
`{session_id,runs:[{run_id,session_id,trace_id,parent_run_id,status,created_at,started_at,completed_at,paused_at,waiting_since,error_type}],next_offset}`，
只列主运行，按 created_at/run_id 倒序，limit 为 1..100；未启用管理也可查询，删除或不存在会话为 404。
同一真实会话的 queued/running/paused/waiting 主运行占位，竞争的 turn/stream 返回 409，
响应携带 detail、session_id 和占位 run_id；不同会话独立。替换在同事务转移位置，终态释放。
管理工具的会话消息读取使用 limit/offset；长消息返回 next_char，按 message_id/start_char 续读。
`management.list_sessions` 的每项和 `management.read_session` 的结果均包含 `latest_run` 安全摘要
（无 root 时为 null），以及查询的 `observed_at`；消息页和 latest_run 在同一 SQLite 读事务取得。
摘要包含运行 ID、状态、时间及暂停／取消请求标志，不暴露 metadata、完整提示或内部结果。
摘要已满足状态查询时无需再调用 list_runs／inspect_run；历史运行和指定运行仍可分别查询。
`completed` 仅表示运行状态，不能证明当前消息页已包含全部结果；分页／续读合同不变。
派发 worker 在 API 生命周期运行，按检查点恢复原运行；无安全检查点则失败并保留记录。
管理派发要求 SQLite checkpoint；memory 配置拒绝新增派发，幂等命令仍可查原回执。
结果可由后续管理轮按需读取；启用 `autonomous_enabled` 后，任务终态和目标到期还可唤醒自动轮。
绑定到后台执行的管理轮可调用 `management.decide_notification(command_id,notify_user,reason)` 暂存本轮是否交付用户的决定；需通过工具安全审查，前台运行不得调用。每次 execution 只允许一个不可变决定，同 command_id 同参重试幂等。决定随回答、内部上下文、目标提案、事件水位和运行终态在同一事务生效；notify_user=false 保留内部结果，且不创建用户消息或 delivery。新后台轮未决定时默认静默；此前已排队的旧运行沿用原交付行为。结果及 final_answer/run_completed 事件携带 user_visible，客户端应据此隐藏内部处理，普通用户轮保持可见。
外部主动结果通知尚未实现。

自动后台轮仅注入当前事件因果关联的目标 `goal_id` 与 `revision`，不注入全部活跃目标及 objective 预览。一般任务终态未关联目标时 goals 为空；定时、关注及等待指定任务的目标仅携带自身引用。完整要求通过 `management.read_goal` 按需读取，目标列表由显式 `management.list_goals` 查询。关联目标继续接受版本／生命周期复验，无关目标的修改不再使该轮失效。

长期目标与后台记录接口同样沿用本地授权：

| 接口 | 行为 |
| --- | --- |
| `POST /managers/{session_id}/goals` | 创建目标，201；objective 必填（1..16000 字符），status 默认 active，可传 next_wake_at、wait_run_id |
| `GET /managers/{session_id}/goals?limit=50&offset=0` | 返回 goals、next_offset；limit 1..100 |
| `GET /managers/{session_id}/goals/{goal_id}` | 读取完整目标 |
| `PATCH /managers/{session_id}/goals/{goal_id}` | 必须传 expected_revision；冲突 409；省略保持原值，next_wake_at／wait_run_id 显式 null 清除 |
| `GET /managers/{session_id}/events?after=0&limit=50` | 返回 events、next_after，按递增 event_id；保留原事件及 consumed_execution_id |
| `GET /managers/{session_id}/executions?after=0&limit=50` | 返回 executions、next_after；包含稳定 execution_id／run_id、冻结事件和目标版本 |
| `GET /managers/{session_id}/deliveries?after=0&limit=50` | 返回 deliveries、next_after；引用已发布 session／run／message，不包含推送确认 |

目标状态为 active、waiting、paused、completed、cancelled；时间必须含时区，保存为 UTC。
wait_run_id 必须是现存 ordinary root。目标修改增加 revision 与 timer_generation；
到期捕获清除 next_wake_at，但不增加 revision。已编辑或取消目标的迟到事件不能重新激活它。
事件与后台记录的 after 均为非负整数游标；next_after 为 null 表示本次已读完，
持续轮询时客户端仍应保存已收到的最大 event_id 或 cursor。接口不返回原始 LLM prompt。

`management.set_goal` 必带 command_id；创建时 objective 必填，更新时必须带 goal_id 和
expected_revision。人工发起管理轮返回 status=applied；自主轮返回 status=proposed，
先校验并保存提案，最终提交同时发布目标和回答。同一自主轮每个目标只能有一份提案。
回执是历史快照，重复命令不重新应用。旧提案所属轮失败或被替换后，新轮重用该 command_id 会冲突；
应先读取当前目标，再用新 command_id 提交重新考虑后的提案。`list_goals` 只预览 objective 前 500 字符，
`read_goal(goal_id,start_char)` 可继续读取，每页 4000 字符。

Matter 增加一等 `visibility: user|internal`，旧记录为 user。list/search 默认 user，
可显式查询 internal/all；get 已知 ID 仍验证来源权限。sync 保持全量授权变更，
客户端统一投影 user 列表／日历／计数；同 ID visibility 切换以 revision 更新，不能过滤掉删除投影所需的变化。
规划进度只计算 user 子任务，visibility 不改变 ACL，也不以是否有日期推断。

`management.overview` 提供有界工作预览，`inspect_work` 读取事项、可选管理扩展及关系；
`organize_work(command_id,operations)` 在同一事务内执行 1..20 个 create/update/link/bind_goal。
轻量 `create_matter(command_id,title,summary?,...)` 默认 visibility=user，`update_matter(command_id,matter_id,expected_revision,changes)` 只修改显式字段。
它们保留稳定命令去重、来源审查与 CAS；自主轮返回 proposed，最终发布成功才写入。applied 回执已包含结果修订号，无需仅为确认回执再次读取。
排程起止必须成对提供或清空。只有开始时刻而时长未知时，保留准确时刻于 summary 并设置 scheduled_on，不编造结束时间或把约定时间误作截止时间。
create 默认 internal（须启用开关），update 必带 expected_revision，link 支持 supports/depends_on 并拒绝循环。
管理扩展分别保存 purpose、responsible_party、next_action、wait_condition、management_state，
事项允许没有 due_at；不往普通 metadata 存储管理状态或完整提示。
绑定 goal 后，业务标题、摘要和完成状态以 Matter 为准，goal 只保留调度状态；
get/list_goal 返回 matter_id/matter_revision/matter_status，拒绝直接改绑定 goal 的 objective/status。
未绑定历史目标保持原有行为。自主 organize 提案与正式回答一同提交；失败／版本冲突不部分写回。

额外的管理工具（沿用包展开和审查，不增加专用 Agent 执行 HTTP 端点）：

- `readiness(matter_id)`：读取 depends_on 的业务完成与核验条件；supports 不阻塞。
- `start_work(command_id,matter_id,expected_revision,purpose,title?,workspace_path?)`：同事务验证工作与依赖、创建独立任务和绑定；稳定重试返回原受理任务，依赖未就绪不产生任务。独立任务一经受理不因主管轮失败而取消。
- `assign_existing(command_id,matter_id,expected_revision,run_id,expected_run_status?)`：关联本主管已创建的独立任务；提交时拒绝未就绪依赖，不会门控已开始的未绑定运行。
- `result_view(matter_id)`：读取绑定运行及核验状态，实际正文使用 read_session。
- `review(...)`：仅核验已绑定且 completed 的运行，accepted 必须引用同 run 的真实 agent 消息 ID；保存核验并返回 completion_update，不直接完成 Matter。自主核验先发布，再在后续轮使用 organize_work 更新完成状态。任何后续业务编辑会保守使旧完成核验失效。
- `read_policies(limit?,offset?)`：读取近期提醒与周期配置。
- `configure_attention(expected_revision,enabled,timezone?,horizon_days?)`：初次 expected_revision=0，horizon 1..90；默认不启用，只扫描 user 活跃事项。
- `configure_schedule(command_id,goal_id,timezone,kind,...)`：kind 为 one_shot/daily/weekly；一次性用带时区 run_at，日／周用 local_time=HH:MM，周另传 weekday（周一=0）。更新需 schedule_id、expected_revision，并提交完整配置；可传 expires_at、enabled。

排队与关注工具：

- `delegate_work(command_id,objective,title?,workspace_path?,allowed_tools?)`：创建独立持久事务线子 agent，返回 delegate_id、session_id、state、revision。当前主管轮可结束，子 agent 独立推进；默认只授予已注册的非管理只读工具，显式工具清单不能包含管理工具。workspace_path 选择默认执行目录；省略时使用前端默认独立工作区，未配置则无工作目录。已有工作目录不构成文件访问边界，外部访问仍须逐调用审查。子 agent 仅可管理自己创建的普通执行会话，不能递归创建下级主管。
- `list_delegates(limit?,offset?)`／`read_delegate(delegate_id,start_char?)`：读取本主管名下（或子 agent 自身）归属、最新运行和分页目标／报告／证据。来源原文不进入普通返回字段。
- `report_delegate(command_id,kind,summary,evidence?)`：子 agent 报告 progress/completed/needs_decision/blocked；evidence 引用自己名下 session_id、run_id、message_id，服务端复验归属。回执 proposed，只在当前轮最终发布事务成功后提交报告、主管事件与本轮终态；失败不发送。completed 要求没有未终结执行任务。
- `instruct_delegate(command_id,delegate_id,expected_revision,instruction)`：父主管向空闲子 agent 补充指令并唤醒新轮，支持继续已完成事务线。冻结工具清单与该事务线默认目录保持不变；该事务线创建执行会话时可按任务显式选择 workspace_path。忙碌时冲突，不能取代原用户确认或答复。重复命令返回原回执。
- `control_delegate(command_id,delegate_id,expected_revision,action)`：父主管 pause/cancel 事务线，先持久收回后续执行权限，再取消该线与其执行会话中的未终态运行；重复命令重发原取消信号。pause 是停止推进而非保留运行检查点的暂停；原运行终结后可 instruct_delegate 继续，cancel 不可复活。
- 智能体创建的事务线及执行会话继承发起轮的 skip/llm/manual 审批偏好并持久保存，排队、重启、恢复、后续运行和下级派发继续沿用。high 风险或 requires_confirmation 标记本身不强制转人工。命中工具危险操作黑名单时强制人工审批；等待确认保留原 review/run 身份，主管与子 agent 不能代批。
- 显式事务线和回报不依赖 autonomous_enabled；父子 controller 均需 active。回报唤醒主管的新 run，经既有消息／SSE 与 delivery 索引交付；失败／等待用户也持久捕获，重复扫描不重复交付。
- `queue_work(command_id,purpose,matter_id?,expected_revision?,priority?,deadline?,read_only_work?,workspace_path?,write_resources?)`：立即持久受理独立 root，等待验证过的依赖及写资源后才派发。Matter 与 expected_revision 必须同时提供或都省略；无 Matter 的临时任务也可排队。priority 为 low/normal/high/urgent，默认 normal；deadline 必须带时区，只参与排序，不是超时取消。回执仍为 accepted，另含 queue_id、queue_state=pending、queue_revision，不能当作已执行。
- `list_queue(limit?,offset?)`：返回 items、next_offset，可见 pending/paused/blocked/released/finished 及 issue、当前 revision。
- `control_queue(command_id,queue_id,expected_revision,action)`：action=pause/resume，仅控制未派发 pending/paused 项；已运行任务继续用 control_run。取消也使用原 run 控制入口，后台回收该队列项。
- `list_available_watches(q?,limit?,offset?)`：发现既有 watch ID 和有界用途预览，不返回原始来源配置。
- `list_watch_subscriptions(limit?,offset?)`：读取当前管理会话显式订阅。
- `configure_watch(command_id,watch_id,expected_revision?,enabled?,condition?,goal_id?)`：初次 revision=0，绑定既有 watch 的当前授权版本；condition=published 接收之后每份已发布简报，changes 仅接收 changes 非空的简报。配置／重绑从当前发布基线开始，不补配置前历史。
- `read_notification(event_id?|briefing_id?,start_char?,max_chars?)`：二选一引用本会话有效 inbox，返回 JSON 正文 content、next_char、total_chars；默认／上限 4000 字符。不重新抓取来源，不转交原始来源授权。
- `read_delivery_policy()`、`configure_delivery(expected_revision,muted?,snooze_until?,allow_decisions?)`：初次 revision=0；muted 或未来的带时区 snooze_until 抑制普通自动唤醒／交付，捕获水位继续推进。allow_decisions 默认 true，保留工作线提问／审批提醒；false 则一起静音。静音事件不被当作已消费，解除后重验有效性再处理。

队列在接纳时冻结原始来源封装、模型及累计额度，释放时不创建新运行或新预算。
仅就绪项按跨主管持久轮转、主管内优先级／等待老化／软期限排序；未核验依赖不抢占执行槽。
read_only_work 默认 true，实际冻结只读工具白名单并关闭 fork；写工作需显式 workspace_path。
写工作保留已验证工作区及可选精确资源键；父子目录互斥，等待／暂停期间保留逻辑预留，终态才释放。
预留只协调此队列内的工作，不提供操作系统文件隔离，也不覆盖既有直接任务。
排队工作不支持原地替换指令；应取消原 run 后用新 command_id 重新入队，原运行恢复保持冻结权限。
队列不再用 dispatch_workers 限制就绪释放；独立任务动态派发，写预留和业务依赖继续约束释放。手动恢复仍走原用户路径和共享模型资源控制。
事项版本／来源或写工作区变化会阻止旧工作执行，blocked 项须重新核对，不自动重绑定。

未来捕获策略仅允许用户前台委托轮修改，自主轮不能即时改写策略绕过最终提交。
周期按 IANA 时区计算；DST 不存在时刻跳过该日／周，重复时刻固定 fold=0。
每次只捕获最近一次到期 occurrence，事件与触发水位一起提交；暂停、过期标 skipped，恢复不无限补跑。
近期提醒按事项 ID、日期事实、提醒阶段和目的地去重，标题／排序修改不重复提醒；
每次持久推进一页候选，末页回到开头。并发重排可在下一轮补齐，不提供单次扫描快照。
策略取消／修订及业务版本变化都会在绑定和最终发布时重验；未交付的失效事件可在新策略代重建，已交付事实不重播。
这些交付均为本地管理会话消息；未接入外部推送或任意来源的通用变化流。
既有 watch 简报引用通过 watch_subscription_revision 与 scope_version 在捕获、读取、绑定和最终发布时重验；
watch scope 变化后必须显式重新订阅，不能自动把新范围视为已授权。
静音／稍后策略版本同样在最终事务重验；不停止来源捕获，不重置模型额度。

自动管理轮绑定持久事件集合、controller revision、目标 revisions 和绑定 Matter revisions；同会话已有未终态 root
时不创建第二个 root。成功最终事务同时发布消息、上下文、目标与 Matter 提案、final_answer／run_completed
事件、消费水位及 delivery。失败不消费事件。业务已提交但 checkpoint 未完成时，从原 run 的
result artifact 返回已提交结果。等待确认／用户答复和显式暂停保留原 run，仍需原控制入口；
用户输入自动抢占、持久输入排队和外部通知不在此阶段。

重命名、删除和恢复继续使用 `/sessions`。自动空会话清理禁止删除 manager；
手动删除需 controller paused、没有未终态运行或活跃／等待目标，否则 409。
恢复保留 kind 和历史并维持 controller paused，默认指针保留以便恢复原入口。

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
GET /matters?limit=50&offset=0&status=active&kind=task
GET /matters?kind=task&parent_matter_id=matter_plan_id
GET /matters/search?q=ICA&limit=10
GET /matters/sync?after=0&limit=200
GET /matters/{matter_id}
PATCH /matters/{matter_id}
POST /matters/{matter_id}/source-links
```

说明：

- `matter` 表是独立本地持久化层，`mail_matters` 只是旧邮件内视图。
- `matter_source_links` 负责把 matter 关联到邮件、trace、文件或后续其他数据源。
- Agent 可以通过 `matter` tool package 创建、查询、更新和链接事务。
- `kind` 默认为 `task`；`kind=plan` 创建无需目录的独立规划，`summary` 可记录目标与完成标准。
  任务通过 `parent_matter_id` 归属规划，只支持规划下的直接任务，不允许嵌套规划。
  `project_id` 可选关联已有目录项目；任务继承规划目录，显式不一致会被拒绝。
  规划已有任务时不能直接更改目录；先移出任务再调整。`kind` 创建后不可修改。
- 列表返回 `matters`、`total_count` 和 `next_offset`，分页大小为 1..200；并发修改时分页不是冻结快照。
  支持 `offset`、字面标题/说明搜索 `q`、`kind`、`parent_matter_id`、`project_id`、`status`、
  `unscheduled=true`、`due_before`（不含上界）与 `due_after`（含下界）。`status=active` 包含
  `open`、`in_progress`、`waiting`。日期上界与下界允许 ISO 日期或带时区的 ISO 时间；
  全天日期按边界所写的日历日期筛选，精确时间按实际时刻筛选。客户端应保留本地时区偏移。
- `scheduled_on` 是可空的 `YYYY-MM-DD` 计划日期；`scheduled_start` / `scheduled_end` 是可空的
  带时区 ISO 时间，必须成对提供或清空，且结束晚于开始。日程与 `due_at` 截止日期独立，
  旧记录不会自动把截止日期解释为时间安排。`inbox` 为严格布尔值，默认 `false`；客户端快速收集应显式传 `true`。
  `section` 是任务阶段名（最多 120 字符），`sort_order` 是有限数值；规划用 `sections` 保存最多 100 个
  不重复、非空的阶段名，普通任务不能设置非空 `sections`。PATCH 省略字段保持原值。
  列表另支持 `scheduled_on`（精确日期）、`scheduled_after`（含）、`scheduled_before`（不含）、
  `inbox` 和 `section`；`unscheduled=true` 筛选没有计划日期和开始时间的任务，截止日期不影响此条件。
- `/matters/sync` 供完整授权的本地界面建立缓存，不作为 Agent 来源权限的替代接口。
  返回 `{changes: [{sequence, matter}], cursor, has_more}`；`after` 为非负游标，`limit` 范围 1..500。
  首次从 0 分页读到 `has_more=false` 才能标记完整覆盖，后续从已保存游标增量读取。
  同一事项可能重复出现，`matter` 是读取时的完整当前记录，客户端按 `revision` 合并并始终推进游标。
  日志与事项变更在同一事务提交，旧事项首次自动建档。子任务变化不会合成父规划事件，
  完整缓存应从全部子任务计算规划进度。目前没有删除接口或删除事件。
- 详情与列表包含 `revision`；规划额外提供 `progress: {total, done, cancelled}`，覆盖全部直接任务，
  与当前分页无关。子 Agent 只统计其来源/账号权限内的任务。完成或取消规划不自动改变任务状态。
- PATCH 可传 `expected_revision`，冲突返回 409 `matter_revision_conflict`，客户端应保留草稿并重新核对。
  省略 `due_at` 保持原值，显式 `null` 清空；新日期接受 `YYYY-MM-DD` 或带时区的 ISO 时间。
  `parent_matter_id: null` 移出规划，`project_id: null` 清除可选目录关联，仍受上述归属约束。
- Agent 的 `matter.create` / `create_many` / `get` / `list` / `search` / `update` 使用相同规划字段；
  编辑前读取当前 `revision` 并传 `expected_revision`。批量创建仍逐项保存，不保证跨项原子性。
  消息来源事项继续要求专用人工审批回执，通用页面或工具确认不能绕过该限制。
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
is required when Multi-Agent planning is enabled. Every Agent tool call, including read-only calls,
follows `tool_review_mode`: `manual` queues each for user approval, `llm` uses model review, and `skip`
records and automatically approves the review. Executor checks still run.

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

- `web.search/open/find` 声明受控 `external_read`，不是任意本地执行；它们仍按配置进行逐调用
  safety review。工具范围、取消和 SSRF 检查仍生效。
  若某来源策略显式禁止 `external_read`，网络请求仍拒绝；固定 search_id/snapshot_id
  本地续读不联网，继续检查缓存所属会话、版本及当前访问范围。
- 返回资料标为 `untrusted_data` 并保留原文与来源信息。上下文结果 gate 使用中英文启发式
  警示检测常见指令式文本；受字符、节点和深度限制，扫描不完整时标记为 `unknown`。它不
  封禁来源或工具，也不能保证发现所有恶意文本；缓存和折叠不使来源成为指令。
- `search_lang` 支持 `zh-hans`/`zh-hant`，适配器将普通 `zh` 转为简体；country 为
  TW/HK/MO 时转为繁体。常见 zh 地区别名及大小写归一化；显式脚本优先于 country。
  转换发生在第一次请求前，不靠失败重试消耗搜索配额，返回参数记录实际 provider 语言。
- 来源策略拒绝附 `source_constraint_denial`（constraint_id、reason、blocked_effect_domains、
  retryable=false）；只有真实的专用审批操作才附 review_path。不可用能力不会一律指向
  事项审批，普通工具确认也不能清除来源约束。确定性拒绝无需额外模型校验。
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
- web包后端回调提供 `_resource`：快照/搜索身份、版本、来源标签和可调用的本地读取路线。
  web.find回执的原始资源是网页快照，而非该回执artifact；默认优先沿快照查找、续页。
  会话历史先做现有权限/范围检查及资源存活检查，再在最近48个合格观察候选中按资源
  去重、按任务词相关性选择最多8项；历史状态不升级为当前事实，过期引用不静默联网。
  HTML可读正文过稀疏时，可使用有界的隐藏结构记录备用提取，明确标记
  `rendered_visibility=alternative_hidden_structured_text` 与提取方法；不执行脚本，
  不等同于浏览器可见全文。允许 `text/x-wiki` 原始文本，其他非白名单MIME仍拒绝。

使用方式与缓存分工见 [日常使用](usage.md) 和 [系统说明](architecture.md)。

`POST /watches` 创建每日关注项，主体包含 `title`、`goal`、IANA `timezone`、本地 `daily_time`、`categories` 与显式 `scope`（如 `web_enabled`、`source_ids`、`account_ids`）。响应给出稳定 `watch_id`，不创建固定会话。每次触发都会产生新的 `Occurrence` 和独立 `session_id`，可以在那次会话继续通过通用 Agent 入口追问。`GET /watches`、`GET /watches/{watch_id}`、`PATCH /watches/{watch_id}`、`DELETE /watches/{watch_id}` 管理关注项；`POST /watches/{watch_id}/pause|resume|run-now` 控制触发，`GET /watches/{watch_id}/runs` 查询执行记录。

`GET /watches/briefings?unread_only=true&watch_id=...` 是本地简报列表，`GET /watches/briefings/{briefing_id}` 取单条，`POST /watches/briefings/{briefing_id}/read` 标为已读。成功执行会把简报摘要写入该次执行的新会话，并在简报返回 `session_id`。前端推送尚未实现，可轮询简报或会话变化。计划任务只授予经工具注册表核对的只读 Child Agent 工具；任何私密账户数据与外部搜索混用须显式设置 `scope.allow_mixed_private_external=true`。搜索/网页证据尚不等于已核验的票务状态。

## 本地记忆与后台任务

消息采集使用独立于 Agent 会话/个人记忆的消息历史接口，见文末“跨平台消息历史”。

### 前端配置、控制与观察

以下接口同样受本机访问 / `LKA_MEMORY_API_TOKEN` Bearer 鉴权保护，不能通过
query string 传递 token；它们不暴露 LLM 密钥、原始 prompt、记忆来源正文。

- `GET /background/config`：返回 `revision`、`active`（当前进程配置）、`desired`
  （待生效配置）、`overrides`、`restart_required`、`pending_restart_fields` 和
  `apply_mode="restart"`。配置包含 `[memory]`、`[background]` 与 `[message_history]` 的字段，
  不包含 provider 密钥或其他领域配置。message_history 配置 schema_version 固定为 1，
  与消息导入协议的 schema_version 独立。
- `GET /background/config/schema`：返回 memory/background/message_history 配置的 JSON Schema，
  前端可以获取类型、默认值和数值范围；模型目录沿用 `GET /agent/models`。
- `GET /background/health` 的 `llm_workloads.budget_pools` 分别提供记忆、消息、I/O 池的
  `revision`、`reset_at`、`circuit_open`、`opened_at`、`reason` 及小时／日 tokens、calls。
  `last_24h` 保留历史统计，不因额度重置变成零；当前额度应查看 `budget_pools`。
  `budget_events` 为最近30条无正文的重置／熔断记录，熔断也写入服务日志，SSE 同步该状态。
  记忆与自动压缩默认无常规 token 配额；异常阈值见 `limits.memory_emergency_thresholds`。
- `POST /background/budgets/reset`：请求
  `{"expected_revisions":{"background_memory":0,"background_message":0},"reason":"用户确认重置"}`。
  可选池为 background_memory/background_message/background_io；原子校验版本，冲突或
  所选池有在途／未过期预约时409，非法类型／前台池422。只接受记忆管理凭据或未配置
  凭据时的 loopback，不能使用消息配对凭据跨池操作。新计量窗口从操作时开始，并解除
  所选池熔断；不删除历史、记忆、来源、检查点，不重置消息工作 family 的累计额度。
  等待任务按原定时再次尝试；不是任意任务重放接口。结果返回更新后的 workload 健康状态。
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
  重试只支持 `memory_extract` / `context_compact` / `memory_consolidate` / `context_records` 的 failed/cancelled 任务；其他类型
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
  可选 `atomic_context` 包含原子系统的 `configured_mode`、`revision`、`covered_seq`、
  `records`、`lag_messages`、`updated_at` 和 `last_error`；关闭时只提供模式和错误字段。
  这些是原子维护诊断，不表示 shadow 已替换摘要，也不等于当前请求一定采用原子视图。

全局/项目自动学习开关仍使用 `PUT /memories/learning`，这是立即生效的学习策略，
不同于上述待重启的系统配置。这里不提供 HTTP 自重启接口，重启由本地应用生命周期管理。

`GET /memories` 默认只列出 active 全局记忆；`scope=project` 需要受允许工作区内的 `workspace_path`。`include_candidates=true` 可查看未发布候选。`POST /memories` 接受 `content`、`memory_type`、`scope`、可选 `workspace_path` 和 `sensitivity`，创建用户确认的记忆。`GET /memories/{memory_id}` 查看一条；项目记忆同样需要匹配的 `workspace_path`。`PATCH /memories/{memory_id}` 接受 `content` 和 `expected_version`，生成可追溯的新版本；`DELETE /memories/{memory_id}?expected_version=N` 撤回，版本冲突返回 409。

纠正正文和撤回的可选 JSON 正文支持 `feedback_reason`，默认 `unspecified`；
撤回也兼容同名查询参数，正文与查询参数的非默认原因冲突时返回422。
可选值为 `factual_error`、`wording`、`outdated`、`duplicate`、`not_worth_remembering`、
`do_not_store`；`wording` 仅适用纠正，`do_not_store` 仅适用撤回，非法组合返回422。
明确用户操作与反馈在同一版本检查事务中提交，冲突不产生反馈；自动维护不记作用户操作。
`GET /memories/feedback?scope=global&limit=100&offset=0` 返回 `{feedback:[...],next_offset}`，
`limit` 为1..100，`offset` 为0..1,000,000。项目范围须提供允许的 `workspace_path`，
只返回匹配项目记录；采用记忆管理认证，不接受消息凭据。
记录包含操作、原因、服务端标定的入口、时间、前后版本与非敏感纠正文本，不保存完整提示。
`personal` 文本不进入反馈样本；`do_not_store` 同时清空该纠正链已保存的反馈文本。
这不改变记忆软撤回及既有历史／来源审计保留行为。
不补猜历史操作原因，不自动训练模型；模型整理最多参考3条相关偏好纠正、合计2000个
JSON字符。反馈不是当前事实证据或工具权限，仍需本轮用户来源支持。

`GET /memories/learning` 与 `PUT /memories/learning` 查看/设置全局或项目自动学习开关。`GET /background/jobs` 返回 payload-free 排队/失败状态。`GET /background/health` 返回队列深度、时长、租约恢复、失败分类，以及 `backpressured_input_count` 和 `llm_workloads`（活动调用、前台等待、近24小时调用/token/估算费用和预算配置）；不返回 payload、来源 ID 或 prompt。默认本地提取支持中英文直接长期要求，普通偏好先作候选，独立重复证据可晋升。`[memory].allow_remote_extraction` 是模型成本开关，不是额外内容审批；功能开启即授权已持久化会话处理，秘密/PII 检查针对长期发布。学习关闭不删除历史记忆；`[memory].enabled=false` 也停止注入。

`POST /memories/projects/{project_id}/relocate` 接受 `old_workspace_path`、`new_workspace_path`，两者均须处于配置工作区内；保留项目身份，不移动磁盘文件。旧路径不匹配/新路径已绑定其他项目返回409，未知项目404，越界403。旧路径解绑后被重新使用会取得新身份，不继承移走项目的记忆。

`GET /memories` 支持 `limit`（1..500）和 `offset`（0..1,000,000），返回 `next_offset`；`GET /memories/export` 使用同样的 scope/分页参数，返回带版本/导出时间的 JSON 页，需按 `next_offset` 继续读取。分页不是跨请求冻结数据库快照；并发编辑时应重新导出，完整备份使用 SQLite backup。`GET /memories/{memory_id}/sources` 返回来源 ID/type/ref/checksum/status/expiry/时间，不返原文正文。每页及精确来源查询重新检查项目作用域。

冲突条目可在详情 `metadata.needs_review/conflict_ids/conflict_hints` 查看；待审条目不注入根/child 上下文。新模型候选不得屏蔽旧确认偏好。明确日期期限来自用户原文而非模型凭空指定；仅新确认来源可延长已存期限。同源重试不会增加版本，撤回不自动恢复。

`GET /memories/file` 读取生成的全局或项目 `MEMORY.md`；`POST /memories/file/generate` 在文件未被手改时同步生成；`GET /memories/file/preview` 返回可导入编辑；`POST /memories/file/import` 一次导入一个带原 ID/版本的内容块。未知 ID、丢失条目、元数据改动和版本冲突不会被默默覆盖。所有记忆/后台状态接口默认仅接受 loopback 客户端；需要远程访问时配置 `LKA_MEMORY_API_TOKEN` 并发送 Bearer token，配置后 loopback 也必须带 token。此保护不代表旧 API 已具有同等鉴权。

自动侧写超过1000个活跃条目、人工文件超过8MiB时明确报错并建议分页API，不静默截断、不覆盖原文件；这不是用户 AGENTS.md 的读取上限。会话工作窗口的 summary_metadata 记录来源水位、摘要方式、后台模型和 lossy 风险；原始会话仍保留。历史工具观察带 `_cache`（as_of/age/TTL/版本/历史标记），不能代替本轮读取；未知来源版本不声称 freshness 已验证。

后台健康另含 `pending_watermark_count`，统计满容量/运行中等待续作的最新压缩水位，与待提取输入数分开。根 `recalled_memories.items` 的代表来源有 `source_count/sources_omitted`、`content_truncated/read_ref`；child MemoryReference 增加可选 `source_count` 和默认false的 `content_truncated`，旧快照缺字段时按默认值兼容。完整来源通过本地来源API查看，不由来源数量挤满prompt。

Agent 读取使用只读 `memory.search` / `memory.read`；`memory.remember(content?, evidence?)`
提供即时保存回执（非只读，经过安全审查），两个参数至少提供一个。`content` 可描述目标偏好，
`evidence` 兼容旧调用；模型会结合近期对话解析“记住刚才这个要求”，不再要求特定句式。
本地规则仍是快速路径，未命中时使用配置的模型进行有界整理；保留本轮确认与早先要求的
用户消息来源，不把提议文本、助手自述或外部资料自动当作用户事实。
输出包含 `memory`（首条，兼容旧客户端）、`memories`（全部条目）和 `memory_file_status`。
清晰直接/已确认的长期信息可 active，弱推断 candidate；文件冲突时数据库记忆仍可能有效，
必须分别展示。关闭学习或子 Agent 写入会被拒绝。

`memory.allow_remote_extraction` 默认 true，旧 TOML/前端覆盖项显式 false 仍受尊重。

#### 后台记忆维护与合并撤销

- `GET /memories/maintenance?scope=global`：返回 enabled、无正文 state、最近5项整理作业和
  last_error；state 包括已观察／已整理签名、last_completed_at、merged_count、outcome。
  `no_safe_merge` 代表保守跳过，不表示模型质量已验证；`merged_file_conflict` 代表数据库
  整理完成但派生文件有冲突。项目范围须提供允许的 workspace_path，未知项目404。
- `POST /memories/maintenance`：正文 `{scope:"global",workspace_path:null}`，请求一次后台
  整理，返回 job_id/status；仍受学习开关、全局后台开关、来源、租约与异常熔断约束。
  合并只处理已有效的记忆，不直接读取知识库、邮箱或工作区，不同步调用模型等待完成。
- `GET /memories/merges?scope=global&limit=100`：同范围 payload-free 合并元数据，含
  merge_id、target_memory_id、member_ids、after_versions、after_statuses、created_at、undone_at。
- `POST /memories/merges/{merge_id}/undo`：正文 `{expected_versions:{"mem_id":2,...},workspace_path:null}`。
  必须提交所有成员的真实版本整数；非法类型422、后来修改／已撤销409、未知合并404。
  来源失效或期限已过不得恢复。成功返回恢复后的 memories 与 memory_file_status；
  版本递增、历史保留，不回滚其他修改，且阻止同一组合被自动重新合并。

以上接口采用记忆管理认证，不接受消息配对凭据授权记忆修改。
`memory_consolidate` 使用 background_memory 池，并支持既有 `/background/jobs/{id}/retry|cancel`。
同 scope 维护串行，模型输出只能引用当前视图中的完整记录；scope、type、sensitivity、
expires_at 必须相同，不将弱候选晋升或择一覆盖冲突。每个参与成员保留有效来源。
源撤回、项目学习关闭、取消和版本变化在发布事务中复核；重新生成派生文件不改 AGENTS.md。
`extraction_context_messages` 默认 12（2..40）、`extraction_context_chars` 默认 12000
（2000..24000）、`auto_publish_min_confidence` 默认 0.85（0.5..1）；这些字段通过既有
`/background/config` 和 schema 管理，保存后重启生效，不增加新接口。普通提取在后台；
明确立即保存时可能增加一次模型整理等待。使用方式见 [记忆与指导文件说明](usage.md)。

自动记忆不是 `AGENTS.md`，不授权写工具或扩大权限。子 Agent 不隐式继承记忆：
PlanStep `input_refs` 可显式指定 `memory:<id>` 或 `memory:<id>@<version>`，服务端校验父作用域、
状态、有效来源与版本，ContextDriver 有界装入冻结快照。读取工具仅允许快照内引用，并重新
检查撤回/来源失效。会话软删除撤回相应来源，无其他有效来源的派生记忆失效；原始会话/审计行
保留，恢复不自动重新发布。后台工作方式见 [系统说明](architecture.md)。

## 跨平台消息历史

此模块只管理本地收到的消息，不连接 QQ、不暴露 OneBot action，不自动发消息、创建事项
或写入个人长期记忆。采集在 Windows 前端本地服务执行，浏览器只访问后端历史/控制 API。
采集和分析配置见 [配置说明](configuration.md)。

### 白名单与本地控制

所有接口检查 loopback 客户端与 Host。浏览器 Origin 必须匹配后端自身或配置的
`LKA_CORS_ORIGINS`。配置 `LKA_MESSAGES_API_TOKEN` 后，本机请求也必须携带
`Authorization: Bearer <token>`；不接受 query token。响应不缓存。

- `GET /messages/policies` 返回 `{policies:[...]}`，包含已禁用策略，供用户管理。
- `PUT /messages/policies` 创建/替换一个策略，返回 `{policy:{...}}`。首次
  `expected_revision=0`，修改须带当前 revision；并发冲突 409。
  启用会话分析时可仅在首次启用请求中附加 `start_from_now:true`。后端在同一
  SQLite 写事务中把当时最后已入库的 seq 固定为 `analysis_baseline_floor_seq`，后续
  只分析更高 seq 的新消息；省略该字段仍按默认从完整历史开始。该 floor 在禁用/重启后
  保留，不能通过重新启用跳过已开始但未完成的分析工作。
- `GET /messages/conversations?limit=50&offset=0` 返回启用会话及消息/待分析数量。
- `GET /messages/recent?conversation_key=...&since=...&limit=50` 按接收时间查询最近消息。
- `GET /messages/search?query=...&conversation_key=...&sender_id=...` 字面子串搜索，非 FTS。
- `GET /messages/conversations/{key}/history?before_seq=...&limit=100` 按 seq 倒序分页；
  跟随 `next_before_seq`，不以平台发送时间作处理游标。
- `GET /messages/conversations/{key}/summary` 返回 `{summary:{...}}`：滚动摘要、
  legacy `covered_seq` / `pending_count`、`analysis_pending_count`、有限原文尾部 `raw_tail`、
  覆盖限制及 `analysis_job` 状态。`legacy_pending_count` 保留旧覆盖视图，增量分析待处理数
  单独报告，不把 cutover 排除的历史伪装成当前待处理。
- `GET /messages/conversations/{key}/coverage` 返回 `{coverage:{...}}`，包括
  `analysis_baseline_floor_seq`、`analysis_watermark_seq`、`excluded_history_count`、
  `excluded_history_through_seq`、`pending_messages`。cutover 排除数统计 floor 内的入库消息，
  并保持为本次增量流程的排除量；后续 selected generation 不会冲销该计数。
  floor、当前实际 analysis coverage 与未覆盖尾部分别呈现，不能把被排除历史说成已分析。
  未知平台缺口不伪造为零。
- `GET /messages/conversations/{key}/facts?limit=50&offset=0` 返回带原消息引用的信息点。
- `POST /messages/conversations/{key}/analyze` 为不足 N 条的尾批请求分析，不启用已关闭的
  分析策略，也不隐式重试失败任务。
- `POST /messages/conversations/{key}/retry` 接受
  `{"expected_updated_at":"analysis_job.updated_at"}`，只重试该会话当前失败/取消批次；
  成功返回 `{status:"retried",job:{...}}`，状态冲突或已关闭会话分析返回 409，
  已撤销记录返回 404，全局分析关闭返回 503；保留同一 job_id 和累计用量。
  可显式附加 `allow_checkpoint_restart:true`，此选项必须携带配对的
  `LKA_MESSAGES_CONTROL_TOKEN`；允许当前失败的可恢复断点额外重建一次，归档原断点，
  保留同一工作家庭、累计用量与尝试记录，必要时授予一次额外尝试。
  不清空或增加工作 token/call 额度，不放开自动恢复上限；省略此字段保持原行为。
- `POST /messages/conversations/{key}/replay` 接受 `{expected_revision:当前策略revision}`，
  固定请求时的已入库末尾为 `through_seq`，只回放连续水位之后的积压，不重发 cutover 之前历史。
  后台按原有批大小分批推进，绕过通常的数量/间隔触发，不绕过许可、暂停或任何预算。
  返回 `through_seq/analysis_watermark_seq/status/job`；暂停时可登记目标但不派发，恢复后继续。
  策略冲突409、未授权404、非法字段422、全局关闭503；沿用消息管理凭据，不向 Agent 暴露写工具。

策略示例（分析默认关闭，显式开启意味着允许配置的模型处理该会话正文）：

```json
{
  "platform":"qq", "account_id":"当前登录账号", "conversation_type":"group",
  "conversation_id":"群号", "display_name":"项目群", "record_enabled":true,
  "analysis_enabled":false, "media_enabled":false,
  "batch_size":20, "timezone":"Asia/Shanghai", "expected_revision":0
}
```

响应生成独立的 `conversation_key`、`source_id` 和 `account_scope_id`；外部会话 ID
不等于内部 key。关闭记录立即阻止后端导入、分析和新查询，但保留历史；采集端在下次
策略刷新后停止记录。离线时只能使用此前确认的策略，故无法保证离线端即时感知撤销。

新策略的 record_enabled、analysis_enabled、proposals_enabled 默认 false。
local_signals_enabled 默认 true 只是后续本地扫描许可，不表示 R3 扫描器已启用。
策略响应新增 capture_epoch / analysis_epoch / proposals_epoch / processing_revision /
schedule_revision，均从 1 开始。revision 仍用于管理 CAS；记录许可翻转增加 capture_epoch，
分析许可翻转增加 analysis_epoch，batch_size 变化仅增加 schedule_revision，不取消在途分析。
timezone / processing_schema_version 变化增加 processing_revision；前者须为有效 IANA 时区。
minimum_import_version 默认为 1，人工设为 2 后不可降级。processing_schema_version 与配置
schema_version、消息导入版本不同，是会话处理语义修订的输入；它不启用新流水线。

会话调度字段：auto_analyze=true、min_interval_seconds=300、max_wait_seconds=900、
max_batch_messages=200；max_wait 必须不小于 min_interval。batch_size（1..200，默认20）
是触发阈值，不是固定处理条数；每批选择完整输入／工作额度容纳的连续前缀，最多200条。
新消息不重置首条未处理消息的等待时间。调度字段的修改只增加 schedule_revision，不废弃
在途输入；auto_analyze=false 不自动创建新范围工作，但允许已建立工作续跑和人工 analyze。
成功发布仅更新后续意图，由后续有界轮转扫描排队，最短间隔／尾批都是资格条件，不是延迟保证。

### 采集服务导入（独立凭据）

必须设置独立环境变量 `LKA_MESSAGES_IMPORT_TOKEN`；采集服务用 Bearer header 传递。
仅 loopback，无浏览器 Origin。该 token 只允许策略元数据读取和消息导入，不授权正文查询
或策略修改。前端 UI 永远不接触此凭据或 QQ WS token。

- `GET /integrations/messages/policies`：已确认策略列表，采集端持久缓存并在正文入库前检查。
  只返回 platform/account_id/conversation_type/conversation_id/record_enabled/media_enabled/
  revision/capture_epoch/minimum_import_version，不返回正文、分析结果或模型配置。
- `POST /integrations/messages/import`：最多 100 条、请求不超过 8,000,000 bytes。
- `POST /integrations/qq/messages/import`：保留 QQ v1 batch 合同，self_id/display_name
  映射至 account_id/sender_name，返回旧式 self_id/message_id 确认。

```json
{"schema_version":1,"messages":[{
  "platform":"qq","account_id":"123","message_id":"10001",
  "conversation_type":"private","conversation_id":"456","sender_id":"456",
  "sender_name":"张三","text":"请确认开会时间。",
  "sent_at":1790920000,"received_at":1790920001,"content_kind":"text"
}]}
```

`acknowledged` 是事务提交后的 `{platform,account_id,message_id}` 列表，重复消息也确认；
`rejected` 是对应 identity 字段加 `reason`、`permanent`，不回显正文。采集端只能确认本次
提交集合中的 ID，永久拒绝进入本地隔离队列，网络/临时错误重试，不删除既有历史。
同 ID 的不同内容拒绝覆盖。无效 sent_at 存为 null 并标明 timestamp_quality；received_at
是采集时间，ingested_at 是后端接收时间，二者不冒充发送时间。非文本 content_kind=unsupported；
可选 `attachments` 最多 20 项，每项为 `{ordinal,kind,file_name}`，kind 仅 image/video。
序号唯一，文件名为不可信 metadata；不接受原始 CDN URL、本地路径或 OneBot action。
消息与引用在同一事务写入。旧 v1 重试省略 attachments 不会清除已有引用；显式不同引用拒绝覆盖。

#### v2 原生元数据与采集许可

通用 import 同时接受外层 schema_version=2，每条消息须包含 capture_epoch、adapter_id、
adapter_version、metadata_capabilities，可带 mentions/reply_to_message_id/thread_id/content_parts。
例如，在原有 identity/text/time 字段上加入：

```json
{
  "capture_epoch":1,"adapter_id":"qq.onebot11","adapter_version":"2",
  "mentions":[{"kind":"user","user_id":"123"},{"kind":"all"}],
  "reply_to_message_id":"先前平台消息ID","thread_id":null,
  "content_parts":[{"kind":"mention","mention":{"kind":"user","user_id":"123"}}],
  "metadata_capabilities":{"mentions":"supported","reply":"supported",
    "thread":"not_provided","content_parts":"supported"}
}
```

能力状态为 supported/not_provided/unknown，未声明默认 unknown；非空元数据必须声明对应
能力 supported。mentions/content_parts 各至多 100 项；每条序列化内容至多 128 KiB，v2
批次至多 2 MiB，原文本仍至多 16,384 字符。OneBot 数组原生 at/reply 才转换；纯文本或
CQ 字符串不会猜测 @ 目标。content_parts 按原序保留 text/mention/reply/unsupported。

后端重新校验记录许可与 capture_epoch；不匹配永久拒绝 capture_epoch_conflict。
minimum_import_version=2 的会话从通用 v1 或 QQ 兼容入口提交均拒绝 import_version_required。
默认 v1 会话仍可导入，收到 v2 本身不自动升级许可。前端升级后隔离旧队列，不重写其 epoch；
调度版本变化不会隔离合法 v2 行，撤销再授权不会恢复已隔离行。混合版本 outbox 分批同步。
重复消息不能改写原身份或已存 v2 元数据，v1 重试不能清除 v2 信息。

查询返回 provider_message_id 与真实内部 message_id；reply_to_internal_message_id 仅在同平台、
账号、会话找到被引用消息时填充，reply_resolution 为 none/unresolved/resolved。
canonical 来源类型 message_history_message 的域层解析仅接受内部 ID，并重新检查白名单及
source_id/account_scope_id 两项权限；未知 ID 和无权访问不暴露差异。尚无消息转事项写入口。

### 图片与视频本地缓存

`media_enabled` 默认关闭，独立于 analysis_enabled。旧管理客户端省略该字段时保留已有设置；
显式 false 关闭附件访问。采集端只为当前白名单、当前策略版本、已获父消息 ack 的附件下载。
平台下载适配器留在前端本地服务；后端索引采用平台无关的消息身份与 ordinal。

- `POST /integrations/messages/media` 使用导入凭据，正文为
  `{schema_version:1,media:[{platform,account_id,message_id,conversation_type,conversation_id,
  policy_revision,ordinal,state,...}]}`，最多 100 项。返回按上述消息身份及 ordinal 匹配的
  acknowledged/rejected；不返回正文。state 为 pending/cached/failed/expired/unavailable。
- 同一接口接受 schema_version=2；每项另须 capture_epoch，与当前采集许可匹配。
  policy_revision 保留作审计字段，v2 不因单纯调度改动失效；v1 仍受当前 revision 约束。
  已升级至 minimum_import_version=2 的会话拒绝 v1 media；仅在 v1 中补 epoch 也不能绕过。
- cached 必须包含 mime_type、size_bytes、sha256、expires_at；width/height/duration_ms 可选。
  服务端校验父引用、白名单、策略版本和状态迁移，cached metadata 不可被重试修改。
- `GET /messages/attachments?conversation_key=...&kind=image&query=...&limit=50&offset=0`
  返回 `{attachments,next_offset,has_more}`。query 仅匹配文件名或父消息文本，不代表视觉理解。
- `GET /messages/attachments/{id}` 返回 `{attachment}`；
  `GET /messages/attachments/{id}/content` 只读本机共享缓存，过期、禁用、缺失返回 404。
  内容以 attachment/no-store/nosniff 返回，读取时重新检查许可及 TTL，并拒绝链接路径。

前端 `QQ_MEDIA_ENABLED=true` 仍须对应会话 media_enabled=true；缓存默认 72 小时、总额
2 GiB、单图 20 MiB、单视频 200 MiB，以 QQ_MEDIA_TTL_SECONDS / QQ_MEDIA_MAX_BYTES /
QQ_MEDIA_IMAGE_MAX_BYTES / QQ_MEDIA_VIDEO_MAX_BYTES 配置。额度不足优先移除旧缓存；
到期删除字节和私有下载 URL，保留附件索引。已识别表情包忽略，未提供 OCR、embedding 或视频内容分析。
后端设置 `LKA_MESSAGES_MEDIA_CACHE_DIR` 指向同机缓存目录；此路径不传给 Agent。

### 分析与 Agent 查询

`[message_history]` 可独立设置 worker 数、模型、每批 token 上限与分片大小；个人记忆
关闭不影响本模块。N 条唯一消息触发固定 seq 范围；同会话串行，长文本完整分片后原子发布，
异常保留旧摘要和原文。模型只输出摘要/信息候选，没有工具执行权限；输出必须通过 schema、
证据 ID、capture_epoch/analysis_epoch/processing_revision 和任务 lease 校验。
兼容旧任务经加法迁移保留；历史来源、许可和原文不自动删除。消息池的后台 token/费用预算照常生效，
不会占用记忆／自动压缩的额度；历史消息调用按持久工作记录迁移到消息池，不删除用量。

启动时记录实际服务商／模型／prompt 版本的 hash，不保存密钥、URL 或 prompt 正文。
后续模型或处理版本变化取消旧分析；服务商配置身份变化额外关闭分析，须用户重新授权。
这些变化不撤销记录许可，也不重盖采集 epoch。首次引入 hash 绑定保留既有分析许可，
不声称验证历史服务商；运行中修改配置仅改变 desired，重启时才执行上述许可检查。
凭据内容轮换不属于该配置身份检测。旧摘要明确 legacy_only，不代表 R3 话题／重要性已处理。

#### 后台调度与控制（R2）

- `GET /background/services/message-reading`：服务 paused/service_epoch/revision、全局启用状态、
  有界 schedules（最多200，schedules_truncated 标明截断）、预算／工作范围与额度、当前 job
  与 reason；不返回聊天正文、checkpoint、prompt 或凭据。使用消息管理凭据与 loopback/Host/Origin 限制。
- `POST /background/services/message-reading/pause`、`resume`：正文 `{expected_revision:N}`，
  CAS 冲突409、非法类型422，立即生效且保留采集许可。暂停阻止新 dispatch，并使旧调用不能
  写 checkpoint／发布；已经发出的 HTTP 仍可能计费。恢复重用暂停前合法的 checkpoint。
- `POST /messages/analysis-work/{family_id}/limits`：正文
  `{expected_revision:N,max_tokens:40000,max_calls:5}`；只允许增加原 family 上限，保留账本与 job。
  未授权会话或未知 family 为404，revision 冲突／额度下降409，非法字段422。
  tokens 上限500000、calls上限1000；工作耗尽的失败任务在额度实际提高后可续跑，仍受滚动与共享配额。
  对 v3 恢复耗尽／检查点异常，显式增加工作额度也授予一次额外检查点重建；额度不变不授予。
  该人工操作保持用量，归档、累计恢复次数仍递增，不提高服务或会话的滚动限额。

单 handler 最多一次实际模型调用；有效分片的 checkpoint/完整输出及同 job 让出原子提交。
模型校验失败时，修复反馈仅提供已知错误码、字段位置及本地 schema 的允许值或长度限制；
可空字符串／列表同样提供约束，不回传原始消息、非法值或任意异常正文。
让出不等于成功，不消费失败重试次数。格式／截断恢复重新排队且计账，已消费的恢复 dispatch
不能靠普通 retry 再获得。v3 的检查点变化／游标异常／恢复耗尽允许每个 family 有界重建
（维护期间可配置 `fragment_recovery_enabled=true` 允许每个成功推进的分段各修复一次，
默认 false 仍为整批一次；同一分段不重复修复，仍消费同一 family 额度）
（`max_recovery_restarts` 默认1、0禁用、最大3），旧检查点先归档，原 job/family/范围/用量
不变；重建后的调用仍消费原工作累计额度，非免费恢复。取消、权限变化和工作额度耗尽
不自动重建。独立错误码为 checkpoint_input_changed/checkpoint_cursor_invalid/
model_recovery_exhausted/evidence_recovery_exhausted，兼容恢复旧 model_output_invalid。
服务状态增加 pending_messages、analysis_watermark_seq、replay_through_seq、replay_pending、
blocked 及 work.recovery_count；不返回检查点正文。工作完整发布后才推进连续水位，不发布半份摘要。人工 analyze 仅绕过
数量与普通间隔，不绕过权限／暂停／预算；暂停返回 `{jobs:[],status:"paused"}`。

默认完整输入8192 tokens、普通输出2048、一次恢复输出4096；单工作累计32768 tokens／4调用。
消息服务小时／日 token 上限40000/200000、调用8/48；单会话20000/80000、调用4/24。
上述滚动配置的0表示不限，工作额度必须为正。所有层级共享同一原子 reservation；不足时延期，
工作累计耗尽记录 work_budget_exhausted 并拒绝普通 retry。网络失败与潜在已计费超时不免费重做，
旧 task 和清理后的用量按稳定 family 幂等迁移。外部调用不承诺 exactly once，但发布保持幂等。

`[message_history].model_prices` 可配置有界价格表，每项为
`{client_name,model,revision,input_cost_per_million,output_cost_per_million}`，价格须有限且非负、
client/model 组合唯一；未知费用不记为零，金额硬限额开启时必须有匹配模型价格。
后续输入的摘要／旧事实视图有界并标记截断，完整分片输出和原文仍保留；原 input_chunk_bytes
只是二级原文数组限制，不替代完整 prompt 或模型容量检查。模型／分片语义配置重启变更会
失效旧 checkpoint，仍保留 family 已用额度；单纯暂停、调整调度或提高额度不清零。

Agent 包为 `messages`，包含 `messages.list_conversations`、`recent`、`search`、`history`、
`summary`、`facts`、`attachments`。全部 `read_only=true`，只能访问启用的本地历史；child 还须同时获准
对应 source_id 和 account_scope_id。消息、摘要和提取信息都标记为不可信数据。摘要是有损派生
数据；`inbound_only` 不代表完整聊天，更不能据此判断用户没有回复或已完成某项任务。

#### 阅读结果、人工候选与评测（R3–R6）

阅读接口强制使用
`LKA_MESSAGES_API_TOKEN` 或独立 `LKA_MESSAGES_CONTROL_TOKEN`，即使 peer 为 loopback 也不降级。
写入阅读状态／关注配置／候选／决定和本地 badcase 必须使用 CONTROL token。
前端本地服务持有 CONTROL token，通过受限 `/plugins/message-reading/...` 代理调用；
浏览器和 Java 页面不保存该凭据。导入 token 不能用于读取或批准。

- `GET /messages/reading/overview`：有界跨会话概览、分会话覆盖与统计。
- `GET /messages/reading/topics[/{topic_id}]`、`GET /messages/reading/insights[/{insight_id}]`：
  结果与稳定 ID／revision、detector、certainty。列表支持 conversation_key、since/until（含时区 ISO）、
  limit（默认50、最大100）、cursor；insights 另支持 importance、unseen、kind=highlight/importance。
  话题 `heat.participant_count`（兼容字段）与 `heat.recent_participant_count` 和热度分数按最近24小时计算；
  `heat.total_participant_count` 表示该话题全部已关联证据消息的不同作者数，冷却话题仍保留历史总参与人数。
- `GET /messages/reading/topics/{id}/sources`、`GET /messages/reading/insights/{id}/sources`：
  证据独立 keyset 分页（最大50）；每页重新检查 source/account，cursor 绑定筛选与版本。
- `POST /messages/reading/insights/{id}/attention`：
  `{expected_revision,viewed_revision?,dismissed_revision?,snoozed_until?}`；GET 不标已阅，不改变平台回执或候选。
- `GET/PUT /messages/reading/profile?scope=global|conversation_key`：显式配置。
  PUT 为 `{expected_revision,profile:{self_ids,aliases,keywords,critical_keywords,tracked_topics,important_contacts,exclusions}}`；
  修改语义配置会失效旧 checkpoint，不清除工作累计账本。
- `GET /messages/conversations/{key}/digest`：确定性低频简报、generated_at、stale 和 coverage；
  不额外调用模型，不用旧摘要冒充有证据的新结果。
- `GET/POST /messages/matter-proposals`、`GET /messages/matter-proposals/{id}`：
  只生成／读取候选，不创建 matter。POST 使用有界 ProposalInput，必须引用真实内部 evidence_message_ids。
- `POST /messages/matter-proposals/{id}/revalidate`：`{expected_revision}`，重新核验冻结候选并更新预览。
- `POST /messages/matter-proposals/{id}/decision`：严格 action union，公共字段为
  `{action,expected_revision,preview_digest,evidence_digest,decision_key}`。
  create 只允许额外 `reviewed_fields:{title,summary,priority,due_at,tags}`；link_existing 只允许
  `target_matter_id,expected_target_revision,source_reason`；reject 只允许 `reason`。
  不接受 decided_by、approved、普通 safety review 或导入凭据代替人类身份。

决定的鉴权、证据／代次／目标 revision 校验、matter/FTS/source links、决定、回执及 accepted
在同一 SQLite 事务提交；同一幂等键异内容或旧预览为409，无权限来源 opaque404。
停用分析／候选会冻结未确认候选，record 撤销不能重新启用旧候选；pause 不阻止人工批准。
已批准的有界副本是独立 matter，不因停用消息来源删除。消息来源 matter 的通用写路径
必须有域层 receipt；消息来源事项继续使用专用人工决定流程，普通工具审查不能替代该 receipt。
消息来源标签对旧记录兼容且仅作说明，不会形成 session 级工具禁用，也不会阻止之后经逐调用
safety review 的文件、命令或其他工具操作。这不是对同一 OS 所有者的防篡改保证。

Agent 新增 `messages.overview/topics/insights/read_insight/topic_sources`，全部只读，
返回 source_policy、untrusted_data、coverage 与分页。聚合先应用 source/account 权限再计算。

#### 活跃人物与自动群侧重点

新接口沿用以上读取 API/CONTROL 分权、loopback/Host/Origin 校验和 no-store。
未知或无记录许可的来源均返回 opaque 404，旧 revision 为409，非法字段为422。

- `GET /messages/reading/participants`：可选 conversation_key、limit（默认30、最大100）、cursor；
  返回 participants/has_more/next_cursor/untrusted_data。先在 SQLite 按 source/account、
  当前记录许可与 capture_epoch 筛选，再对热池及置顶人物分页；冷却、隐藏及删除者不混入活跃列表。
- `GET /messages/reading/participants/{conversation_key}/{sender_id}`：会话内真实 sender_id，
  不以昵称归并；返回 status、score/score_version/missing_components、revision、
  activity_observed_seq、summary、claims。claim 含原作者证据、explicit/observed/uncertain、
  valid_until、stale/contested；不是对人格或长期事实的认证。
- `GET .../participants/{conversation_key}/{sender_id}/sources`：limit 默认30、最大50、cursor；
  原文证据独立分页并再次验权；隐藏或删除者不允许获取画像证据。
- `POST .../participants/{conversation_key}/{sender_id}/control`：CONTROL 专用，
  `{expected_revision,action,summary?}`；action 为 pin/unpin/hide/unhide/delete/correct。
  correct 的 summary 最多240字符；delete 删除画像候选并保留 suppression，不删除消息原文，
  新消息、重放和许可再授权都不会自动复活该画像；这些控制没有 Agent 写工具。
- `GET /messages/reading/focus/{conversation_key}`：独立于旧 ReadingProfile 的自动群用途候选，
  冷启动 unknown；来源窗口、confidence_label 和有限样本覆盖明确显示。
- `PUT /messages/reading/focus/{conversation_key}`：CONTROL 专用，
  `{expected_revision,mode:"auto"|"manual",labels?}`；labels 最多四个、不重复，
  取值 technical_support/project_collaboration/interest/social/general。
  manual 优先于模型；切回 auto 不保留人为用途认证。GET 均只读，不推进后台游标。

Agent 新增 `messages.participants/participant/participant_sources/focus`，全部 read_only，
读取范围受消息来源及账户授权约束；child 必须同时有对应 source/account scope，空 grant 不等于无限制。
人物信息不能转成执行授权、matter 批准或个人 memory。前端沿用原生 JS 与 Java 精确代理，
控制凭据不进入页面或 Java。

`[message_history]` 新增 reading_algorithm=legacy（compact/selected 显式 opt-in）、
participant_pool_capacity=30、participant_pinned_capacity=10、profile_cold_days=14、
profile_retention_days=30、selector_max_messages=40、selector_exploration_fraction=0.1。
旧配置默认 legacy；这些字段遵守 active/desired 待重启合同，纳入 processing fingerprint，
不自行开启任何会话的 analysis。codec v2 可解码旧 v1；旧 schema 1/2 保留，
schema 3 增加人物/focus 候选、有界补证据请求及完整版本冻结。

coverage 增加 generation_published_seq、screened_seq、coverage_mode、model_seen_count、
selected_out_count、published_range、selection_manifest、model_seen_spans、processing_versions。
mode 为 full_text/selected_text/unknown；model_seen_count 是最近发布范围的已输入消息数，
不是模型正确理解数；selected_out_count 排除后来已补读的消息。无历史 manifest 时扫描未知，
不得补盖“完整”。manifest 按当前 capture_epoch 验权，同事务发布阅读与人物/focus。
selected 只推进 generation，不冒用全文 covered_seq；回退从真实全文水位补处理。
codec payload 节约率不包含提示、旧上下文、输出与重试，不能解释成真实总费用节约率。

历史离线入口 `scripts/replay_message_reading.py audit/label-template/score` 不调用远程模型、
不回写生产库。score 必须绑定快照/窗口/输出 digest，区分 human 与 agent/model 标注；
无完成的人工 gold 不宣称召回/精度达标，评分产物永不直接授予生产分析许可。
独立历史 CLI 用量账本与运行中 Windows 全局账本分开，不并行启动两侧真实模型试验。
固定合成数据见 `tests/fixtures/message_reading_cases.json`；离线命令
`uv run python -m scripts.eval_message_reading`。仅显式加 `--remote` 才允许最多4次真实模型调用，
仍只读合成文件／临时库，不连接生产消息库；工程通过不等于语义质量通过。

本地 badcase 全部强制 CONTROL 身份：`POST /messages/reading/evaluation/badcases/preview`
接收 `{evidence_message_ids}` 返回准确原文与 evidence_digest；随后 POST 同一 base 接收
`{evidence_message_ids,expected_evidence_digest,label,note?,local_copy_consent:true}`。
label 为 missed_importance/false_positive/topic_split/topic_merge/deadline_correction/evidence_error。
`GET /messages/reading/evaluation/badcases[/{id}]` 仅本地读取，列表最大50；逐次检查权限与 capture_epoch。
保存不触发模型、上传、训练或个人记忆。撤销后原副本保留但访问 opaque404，不自动复活。

#### 会话 metadata、统一消息检索与持久档案（后端接口）

下面描述消息检索及人物档案的接口。前端应通过自己的安全代理转发，
不向页面或模型暴露管理及导入凭据。

会话身份是 `platform + account_id + conversation_type + conversation_id`，服务端生成
`conversation_key`。人物身份是 `conversation_key + sender_id`，不能按昵称跨会话合并。
名称按人工别名、人工显示名、导入缓存群名依次选择；后端不主动获取平台群信息。

- `GET /messages/conversations/resolve?query=项目组`：归一化匹配名称、别名或平台 ID，
  返回 `matches/ambiguous/has_more`。重名时由用户选择，不默认使用第一项。
- `GET /messages/conversations/{conversation_key}/metadata`：平台、账号、类型、真实 ID、
  显示名、各类名称来源、cache_provenance、revision 与更新时间。
- `PATCH` 同一 metadata 路径：`{expected_revision,user_alias?,display_name?}`。
  revision 是人工标签 CAS 版本；导入缓存不覆盖人工标签。
- `POST /integrations/messages/conversations/metadata`：最多100项、128KiB，
  `{metadata:[{platform,account_id,conversation_type,conversation_id,capture_epoch,
  platform_name?,display_name?,cache_provenance?}]}`，返回 accepted/rejected。
  不创建或扩大白名单。
- `GET /messages/search?query=...`：支持 conversation_key、sender_id、sender、
  since/until（Unix秒）；时间优先 sent_at，否则 received_at。结果是本地分页。
- `GET /messages/records/{message_id}`：内部稳定 ID，对应原文、回复引用和会话 metadata。
- `GET /messages/records/{message_id}/context?before=10&after=10`：前后各最多25条，
  按采集 seq 排列；返回 anchor_message_id、gap、has_more 与 capture coverage。
- `GET /messages/reading/dossiers?conversation_key=...&limit=30&offset=0`：持久档案目录。
- `GET /messages/reading/dossiers/{conversation_key}/{sender_id}?limit=30&offset=0`：
  有来源的 claims 与待核实 machine_notes；返回 source_count、observation_count、
  has_more/next_offset 及人工纠正状态。
- `GET .../dossiers/{conversation_key}/{sender_id}/sources?limit=30&offset=0`：
  证据分页，最大50条；不返回内部文件路径，不读取独立人工 notes.md。
GET 需 API／CONTROL 配对读取，人工 metadata PATCH 需 CONTROL + expected_revision；
IMPORT 上报必须绑定现有白名单和 capture_epoch，不能自授权。

消息作为 `source_type=chat_message` 接入 `knowledge.search/load_chunks/load_document`。
可使用 `GET /knowledge/search?q=...&source_type=chat_message&limit=10` 仅查消息，
或带读取凭据进行邮件、知识与消息混合检索；没有凭据的旧接口排除消息来源。
`POST /knowledge/chunks/load` 以 chunk_ids 与 offset 读取消息正文；
`GET /knowledge/documents/{document_id}?include_text=true` 返回会话最新消息页，
不是完整聊天导出，应检查 metadata.coverage。
保护读取要求现有本机 Host／Origin 与消息配对凭据；未配对通用搜索排除消息而保留原文档行为。
知识结果 metadata 保留原会话、作者和时间；消息检索是实时本地关键词，不承诺 embedding。
人物档案分页区分 claims／machine_notes，并从 SQLite 即时执行隐藏、删除和人工纠正控制；
只有当前 capture 下仍存在、作者一致、逐字引用可核对的证据可返回。

新增 Agent messages 工具全部只读，并按配置进行逐调用 safety review。消息来源标记不产生
session 级工具禁用；事项候选仍须通过专用人工决定流程。没有 NapCat 原始客户端或 QQ 写操作。
