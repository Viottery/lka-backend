# Personal Knowledge And Agent Memory Plan

本文记录个人桌面 Agent 的本地知识库与长期记忆方向。它是后续实现
`knowledge` / memory / retrieval 相关能力的设计依据，不是一次性必须完成的
完整产品规格。

## 1. Positioning

个人知识库首先服务于 Agent 的行动和判断，其次才服务于知识整理本身。

本系统不追求第一阶段就建设传统知识管理软件、完整知识图谱或企业级
ontology。目标是为个人桌面 Agent 提供一套本地优先、可追溯、可检索、可逐步
结构化的外部记忆系统。

它应帮助 Agent 回答五类问题：

- Who: 这个人是谁，与用户有什么关系。
- What: 这件事情、资料或任务是什么。
- Why: 当时为什么这么决定。
- When: 事情发生在什么时候，现在是否仍然有效。
- What Next: 接下来应该做什么。

其中 `What Next` 对桌面 Agent 最重要，因为知识库最终要支撑行动连续性。

## 2. Core Principles

### 2.1 Local First

个人知识默认保存在本地，支持离线工作、本地搜索、明确的数据目录、导出和备份。
外部服务只作为数据来源或可选增强，不作为本地知识系统的前提。

### 2.2 Raw Data First

原始内容是最终事实来源。邮件、文档、笔记、对话、Agent run log 和用户反馈都
应尽量保留原始 source。

LLM 生成的 summary、tag、entity、relation、preference 和 experience 都是派生
信息，必须能追溯到 source，不能替代 source。

### 2.3 Temporal By Default

个人知识天然会过期。项目状态、人物职位、用户偏好、任务状态和计划安排都可能
变化。

长期知识项应尽量保留：

- `created_at`
- `updated_at`
- `valid_from`
- `valid_until`
- `status`
- `source`
- `confidence`

目标不是只回答“过去记录过什么”，还要回答“现在什么仍然成立”。

### 2.4 Confidence Aware

模型推断的信息不能直接升级成绝对事实。偏好、关系、项目归属、人物识别和经验
总结都应带 confidence，并保留 evidence。

推荐形态：

```text
Observation
Inference
Confidence
Evidence
```

只有经过重复证据或用户确认后，推断信息才逐步升级为 Preference、Entity 或
长期规则。

### 2.5 Progressive Structuring

采用“先存，再理解，再结构化”的策略：

```text
Raw Information
  -> Store Original Content
  -> Metadata
  -> Full Text Search
  -> Optional Embedding
  -> Entity Hint
  -> Repeated / Important
  -> Promote to Entity
  -> Create Relations
```

不要在信息进入系统时立即做完整实体化。结构化成本只投入到持续出现、长期有用、
会影响未来行动的信息。

### 2.6 Lazy KG

图结构应该从个人记忆和实际使用中逐渐形成，而不是作为第一阶段主存储模型。

初期可以使用轻量关系模型：

```text
Entity
Relation
Document
Memory
```

关系类型限制在少量高价值类型：

- `belongs_to`
- `works_on`
- `related_to`
- `assigned_to`
- `mentioned_in`
- `depends_on`
- `created_by`
- `supports`

底层仍使用 SQLite。只有当复杂关系查询成为真实瓶颈时，再考虑 Graph Database
或 GraphRAG。

### 2.7 Privacy And Safety As Architecture

个人桌面 Agent 不依赖本地 LLM，因此安全目标不是“所有计算都在本地完成”，而是：

> 原始数据尽可能保留在本地，敏感信息默认不出机，远程模型只获得完成当前任务所必需的
> 最小上下文。

隐私、安全和权限控制应作为一级架构能力，而不是知识库完成后的补丁。系统需要两个独立
安全关口：

```text
Knowledge -> Cloud LLM
Privacy Gateway

Cloud LLM -> Computer
Action Policy
```

前者决定什么数据可以离开本机，后者决定模型可以对电脑执行什么操作。这两个边界必须由
本地确定性代码掌控，不能交给 LLM 自行决定。

## 3. Memory Layers

系统长期可以划分为四层记忆。

### 3.1 Working Memory

短期上下文，包括当前任务、当前项目、当前文件、当前对话、最近操作和当前窗口。
生命周期短，主要用于一次或数次 Agent turn。

当前项目已有 `SessionService` 和 session context window，可作为 Working Memory
基础。

### 3.2 Structured Memory

高价值结构化信息，包括：

- Person
- Project
- Task / Matter
- Event
- Decision
- Preference

这些信息应该有状态、时间属性、来源和置信度。

当前项目已有 `MatterService`，它应继续作为 open loop / task / reminder 的确定性
领域服务，不应被并入普通文档知识库。

### 3.3 Semantic Memory

大量非结构化知识，包括本地文档、邮件、网页、笔记、代码、会议纪要和 Agent
conversation。

访问方式按优先级逐步建设：

1. SQLite FTS5
2. Metadata search
3. Optional vector search
4. Hybrid retrieval

### 3.4 Episodic Memory

过去实际发生的事情，包括对话、操作、会议、项目变化、用户反馈和 Agent 行为。

当前项目已有 `data/agent_logs/` 和 session messages。后续应提供 trace/log 查询 API，
让 Agent 和前端可以恢复历史上下文，而不是直接依赖文件系统读取日志。

## 4. Core Information Types

第一阶段只需要保留下列类型的设计边界，不需要全部实现完整 CRUD。

### 4.1 Document

Document 表示可检索的原始知识单元，例如 Markdown、TXT、PDF、Office 文档、网页
快照、邮件、笔记、代码片段或 Agent conversation。

Document 必须保留 source metadata、checksum、更新时间和 chunks。摘要、标签、实体
提示和关系都属于派生信息。

### 4.2 Memory

Memory 表示可被 Agent 长期召回的事实、上下文、观察或经验。它可以来自文档、邮件、
对话、用户显式要求或 Agent action。

Memory 不一定需要强结构；初期可以是带 source、status、confidence、valid time 的
轻量记录。

### 4.3 Person

Person 只为频繁出现、具有持续价值的人建立正式实体。第一次出现的名称可以先作为
entity hint，不立即做 entity resolution。

### 4.4 Project

Project 是个人 Agent 的重要组织单元，用于回答“这个项目现在进行到哪里了”。

Project memory 应长期包含目标、状态、阶段、相关资料、当前任务、关键决策、风险、
未决问题、下一步和时间线。

### 4.5 Task / Matter

未闭环事务比普通文档优先级更高，包括 task、reminder、waiting for、follow-up、
commitment 和 deadline。

当前 `matter` domain 是这类信息的主入口。知识库负责提供 evidence，matter 负责状态
和行动对象。

### 4.6 Event

Event 用于时间线，包括 meeting、conversation、decision、file change、milestone 和
Agent action。

### 4.7 Decision

Decision 应作为独立高价值记忆，记录 decision、context、alternatives、reason、
participants、timestamp、source 和 result。

它的价值是让 Agent 未来能解释为什么当时这么做，以及哪些方案曾被否决。

### 4.8 Preference

Preference 不应被视为永久事实。它应来自用户显式说明或多次稳定反馈，并保留 source、
confidence 和更新时间。

## 5. Relationship To Current Backend

当前后端已经具备适合承接本方向的基础：

- `mail_accounts` / `mail_messages` / `mail_chunks` / `mail_messages_fts` 已管理邮件知识。
- `matters` / `matter_source_links` 已管理独立事务和证据链接。
- `AgentTurnLoop` 已支持 Tool Package 懒展开和跨 package 工具调用。
- `SessionService` 已提供多轮会话和 context window。
- `data/agent_logs/` 已保存 Agent action 的完整执行记录。

后续知识库工作不应重做邮件系统，也不应把 matter 并入普通文档表。

推荐关系：

```text
mail domain
  -> mail-specific sync/search/load
  -> optionally mirrors chunks into knowledge index

knowledge domain
  -> source-agnostic document/chunk/search/load
  -> provides evidence to Agent

matter domain
  -> action/open-loop state
  -> links to knowledge/mail/trace sources
```

## 6. Security And Privacy Baseline

本地知识库保存邮件、文档、偏好、项目状态和 Agent 行为记录，安全风险高于普通
workspace index。第一版知识库必须把数据出机控制和工具权限控制纳入设计。

### 6.1 Threat Model

至少考虑以下风险：

- Local data leakage: SQLite、知识目录、备份、调试日志和临时文件泄露。
- Cloud data leakage: 完整文档、敏感邮件、聊天记录、embedding API 原文或 API 请求日志出机。
- Agent overreach: 用户只要求处理某个项目，Agent 却读取整个磁盘或无关私人目录。
- Tool abuse: 删除、覆盖、发送、上传、执行 shell command 或调用网络 API。
- Prompt injection: 邮件、网页、PDF、文档和搜索结果中的恶意文本试图变成 Agent 指令。

所有检索内容都应被视为 untrusted data。检索内容中的指令不能覆盖 system instruction、
用户请求或本地 policy。

### 6.2 Sensitivity And Remote Policy

所有长期 Knowledge Object 和 Memory 都应带敏感等级和远程策略。

推荐等级：

```text
sensitivity:
  public
  personal
  sensitive
  secret
```

推荐远程策略：

```text
remote_policy:
  allow
  redact
  confirm
  deny
```

不要只使用 `allow_remote: true / false`。`remote_policy` 能表达脱敏、单次确认和默认拒绝。

建议默认：

- `public`: 可在任务需要时发送最小上下文。
- `personal`: 策略允许时可发送必要片段。
- `sensitive`: 默认 redact 或 confirm。
- `secret`: 默认 deny，原则上不得发送给远程模型。

Secret 包括 password、API key、token、private key、SSH key、recovery code 和 credential
string。Secret detection 应优先使用确定性规则，例如 regex、prefix pattern、entropy
detection、known secret format 和 file path policy。

### 6.3 Privacy Gateway

所有发送给远程模型的数据必须经过统一 Privacy Gateway。业务代码不应绕过它直接调用
远程 LLM 或云端 embedding API。

推荐职责：

- classification
- ACL checking
- secret detection
- PII detection
- redaction
- remote policy enforcement
- context minimization
- audit logging

推荐检索到远程模型的管线：

```text
retrieve
  -> rank
  -> permission filter
  -> sensitivity check
  -> remote policy
  -> secret filter
  -> redaction
  -> truncate
  -> cloud model
```

禁止：

```text
retrieve -> send
```

### 6.4 Data Minimization

云端模型不应看到整个知识库，只能看到完成当前任务所需的最小上下文。

本地应先完成：

- storage
- search
- metadata filtering
- policy check
- redaction
- secret detection
- deterministic aggregation

云端模型负责：

- reasoning
- summarization
- generation

对于敏感数据，优先采用：

> Raw Data stays local; derived facts may go remote.

例如财务、合同或私人邮件场景中，本地先计算或抽取必要事实，再把脱敏后的统计结果或片段
发送给模型。

### 6.5 Filesystem And Tool Permissions

知识库不应默认读取用户整个磁盘。应采用 Default Deny：只有显式授权的目录和资源可访问。

推荐策略字段：

```text
allowed_paths
denied_paths
read_only_paths
write_allowed_paths
```

Read 和 Write 必须分权。能读取某个目录，不代表能创建、修改、删除、执行或上传。

Cloud LLM 不能直接访问本地文件系统。所有文件访问都必须走：

```text
LLM request
  -> Action Policy
  -> Filesystem / Knowledge Tool
```

### 6.6 Context And Instruction Separation

模型上下文应显式分层：

```text
SYSTEM INSTRUCTION
USER REQUEST
RETRIEVED UNTRUSTED DATA
TOOL RESULTS
```

Retrieved content 是不可信数据。它可以作为证据，但不能成为 Agent 指令。

### 6.7 Logging And Audit

日志本身是高风险数据源。运行日志可以用于调试和审计，但不能无差别记录所有原文。

推荐审计字段：

```text
query_id
tool_name
document_id
result_count
latency
policy_decision
timestamp
error_code
remote_data_sent
user_confirmation
```

系统应能回答：

- Agent 什么时候访问过某个文件或邮件。
- 哪些数据发送过远程模型。
- 某次工具调用为什么被允许或拒绝。
- 用户是否确认过敏感操作。

长期 Memory 必须支持：

```text
view
edit
delete
expire
disable
```

Agent 不应形成不可见、不可管理的用户画像。

### 6.8 V1 Minimum Security Baseline

第一阶段最低安全基线：

1. Knowledge folder 和 SQLite 文件保持在本地受控数据目录。
2. Agent 文件访问采用目录白名单和敏感路径 deny list。
3. Knowledge Object 带 `sensitivity` 和 `remote_policy`。
4. Secret 默认不入库或不进入远程上下文；如需记录引用，只保存 metadata。
5. 远程 LLM / 云端 embedding 调用必须经过 Privacy Gateway。
6. 本地完成关键词、metadata 检索和 Top-K 选择。
7. 只发送最小必要上下文。
8. Retrieved content 明确标记为 untrusted data。
9. 所有 Tool 调用继续经过 Action Policy / safety review。
10. Memory 可查看、可删除、可过期。
11. 远程数据发送和重要工具操作可审计。
12. Windows 环境建议依赖 BitLocker 保护整盘数据；高敏感 token 后续迁移到 DPAPI 或
    Windows Credential Manager。

## 7. Phase Plan

### Phase 1: Unified Local Knowledge Search

目标：建立可靠的个人搜索与记忆基础能力。

优先实现：

- SQLite schema for source-agnostic knowledge objects.
- Markdown / TXT local document import.
- Document checksum and metadata tracking.
- Chunking for imported text.
- SQLite FTS5 cross-source search.
- Sensitivity and remote policy fields on knowledge objects.
- Local-only retrieval, permission filtering, and minimum context assembly.
- `knowledge` Tool Package with search/load tools.
- Basic source refs so answers can cite where evidence came from.

建议 schema：

```text
knowledge_sources
- source_id
- source_type
- display_name
- uri
- metadata
- status
- sensitivity
- remote_policy
- access_scope
- created_at
- updated_at

knowledge_documents
- document_id
- source_id
- source_type
- title
- uri
- checksum
- mime_type
- status
- sensitivity
- remote_policy
- source_ref
- created_at
- updated_at
- indexed_at
- metadata

knowledge_chunks
- chunk_id
- document_id
- chunk_index
- text
- char_count
- token_estimate
- source_ref
- sensitivity
- remote_policy
- created_at

knowledge_chunks_fts
- chunk_id
- title
- text
- source_type
```

第一版工具：

```text
knowledge.search
knowledge.load_chunks
knowledge.load_document
```

第一版 `knowledge.search` 返回给 Agent decision prompt 的结果应是最小必要片段和
source metadata，不应直接返回整篇文档。完整原文只能通过显式 load 工具按需读取，并继续
经过权限和远程策略过滤。

暂不做：

- PDF / Office 全格式解析
- 本地目录监听
- 自动 entity extraction
- 自动 current state
- vector search
- Graph Database
- 完整 PII detection
- 加密备份和安全 dashboard

### Phase 2: Mail And Document Unification

目标：让邮件、本地文档、个人笔记以统一 evidence 形态进入 Agent context。

工作内容：

- 将 `mail_chunks` 映射或同步进 `knowledge_chunks`。
- 保留邮件原始 schema，不破坏 `mail` package。
- 让 `knowledge.search` 可以搜索 mail 和 local document。
- 让 `mail` 继续负责 Outlook sync 和邮件特有字段。
- 让 Agent 能从 `knowledge` evidence 切到 `matter` 写入事务。
- 将私人邮件默认标记为 `sensitive`，远程策略默认 `redact` 或 `confirm`。

### Phase 3: Retrieval Quality

目标：让检索结果更适合 Agent action，而不是只追求文本相似。

工作内容：

- metadata filter: source type, project, time range, status.
- result ranking: recency, source priority, exact match, task relevance.
- result compaction: chunks for decision prompt, full content for run log.
- source citation: document path, mail message id, chunk id, trace id.
- retrieval feedback: record which evidence was useful.
- privacy-aware ranking and filtering before remote LLM context assembly.
- audit records for retrieved and remotely sent knowledge refs.

### Phase 4: Optional Embedding And Hybrid Search

目标：在 FTS 闭环稳定后加入语义检索。

工作内容：

- Local embedding provider interface.
- `BAAI/bge-m3` config and model cache path support.
- Background embedding job.
- Embedding status per chunk.
- Hybrid retrieval combining FTS, metadata, and vector search.

注意：embedding 不是第一阶段成功条件。不要让模型依赖、下载和缓存问题阻塞本地知识
闭环。

如果使用云端 embedding，它和远程 LLM 一样属于数据出机，必须经过 Privacy Gateway。
默认优先考虑 local embedding model。

### Phase 5: Lightweight Structured Memory

目标：只对高价值信息逐步结构化。

工作内容：

- Person / Project / Decision / Preference minimal tables.
- Entity hints from documents and mail.
- Manual or user-confirmed promotion from hint to entity.
- Lightweight `relations` table.
- Time validity and confidence tracking.
- Memory provenance, view/edit/delete/expire/disable operations.

### Phase 6: Current State And Memory Consolidation

目标：让 Agent 启动或开始任务时优先理解“现在”。

工作内容：

- Active projects.
- Waiting for.
- Upcoming events.
- Current tasks.
- Recent decisions.
- Current focus.
- Recently relevant documents and people.
- Memory consolidation jobs.
- Expire / supersede / merge flows.
- Current State minimization before cloud prompt injection.

## 8. First Implementation Slice

建议第一个可交付切片保持很小：

1. 新增 `knowledge_sources`、`knowledge_documents`、`knowledge_chunks`、
   `knowledge_chunks_fts`。
2. 新增 `KnowledgeService`，支持 import Markdown/TXT 文本文件。
3. 为 source / document / chunk 增加 `sensitivity`、`remote_policy` 和 source refs。
4. 支持固定大小 chunk，写入 FTS。
5. 新增本地 Privacy Gateway 最小接口：permission filter、secret deny、context
   minimization。
6. 新增 `knowledge.search`，只返回 Top-K 最小片段。
7. 新增 `knowledge.load_chunks`。
8. 在 Agent package catalog 注册 `knowledge`。
9. 增加测试覆盖：
   - 导入文档。
   - FTS 搜索命中。
   - chunk 加载。
   - secret-like content 不进入远程上下文。
   - `sensitivity=secret` / `remote_policy=deny` 被过滤。
   - Agent 能展开 `knowledge` 并基于 evidence 回答。

验收标准：

- 不配置 embedding 也能完成本地知识检索。
- 不破坏现有 mail / matter / filesystem / bash package。
- 原始文档路径、checksum、chunk id 和 source ref 可追溯。
- 搜索结果以最小必要片段进入 Agent decision prompt。
- Secret 默认不发送给远程模型。
- 检索内容在 prompt 中标记为 untrusted data。
- 远程模型上下文可以追溯到具体 knowledge refs 和 policy decision。
- 完整内容是否写入 run log 必须受日志安全策略控制，不能默认记录所有原文。

## 9. Design Traps To Avoid

- Everything to embedding: 向量检索不能承担当前状态、精确任务、时间查询和关系维护。
- Everything to Knowledge Graph: 过早实体化会带来重复实体、错误关系和维护成本。
- Everything to LLM summary: 摘要只能是 cache，不能替代 source。
- Treat memory as immutable: memory 必须支持 update、supersede、expire、merge 和 delete。
- Merge action state into knowledge: matter/task/reminder 是行动对象，不是普通知识 chunk。
- Retrieve then send: 检索结果不能未经权限、敏感等级、secret filter、redaction 和截断
  直接发送给云端模型。
- Let the model decide permissions: LLM 不能决定自己可以读取什么或执行什么。
- Treat logs as harmless: prompt、邮件正文、token、剪贴板和完整文档内容都可能通过日志泄露。

## 10. Target Architecture

长期闭环：

```text
Raw Data
  -> Semantic Index
  -> Structured Memory
  -> Current State
  -> Agent Context
  -> Action
  -> New Experience
  -> Memory Update
```

运行循环：

```text
Observe
  -> Remember
  -> Retrieve
  -> Filter
  -> Reason
  -> Act
  -> Learn
```

安全闭环：

```text
Local Data
  -> Local Retrieval
  -> Privacy Policy
  -> Minimal Context
  -> Cloud Reasoning
  -> Action Policy
  -> Local Tool
```

总体判断：

> 不建设一个追求知识完整性的个人知识图谱，而建设一个能够随着使用逐渐形成结构的
> Agent Memory System。

> 本地承担数据主权和权限控制，云端模型承担推理能力。
