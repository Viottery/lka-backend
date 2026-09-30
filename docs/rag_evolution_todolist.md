# RAG 演化 Todo：从工具化 Hybrid RAG 到安全 Agentic RAG

> 本文档是 RAG 后续演化的执行清单和设计约束。
>
> 当前推荐方向：以 Workflow RAG 作为确定性、安全的检索执行底座，以 Agentic RAG
> 作为检索策略决策和动态编排能力。模型决定“是否 rewrite、使用哪种检索、搜哪些
> 已授权来源、是否继续”，系统决定“是否允许、如何执行、何时强制停止”。

---

## 0. 文档定位与执行规则

### 0.1 文档定位

- [x] 确认当前 RAG 基线：本地知识库、邮件知识镜像、SQLite FTS5、可选本地 embedding、sqlite-vec、RRF、隐私过滤和 Agent 工具调用。
- [x] 确认目标架构：安全的 Workflow RAG 执行底座 + 受预算约束的 Agentic RAG 策略层。
- [x] 确认 RAG 不只是问答能力，还要服务于 Agent 的规划、工具选择、执行和验证。
- [ ] 将本文档加入文档索引或项目 README 的开发文档入口。

### 0.2 执行规则

- [ ] 一次只推进一个阶段中的一个小闭环。
- [ ] 每个条目开始前明确目标、修改文件、风险和验证方式。
- [ ] 每个条目完成后运行针对性测试，再更新本清单。
- [ ] 允许 Agent 决定 rewrite、检索模式、来源候选、检索轮数和停止意图，但不将这些决策直接等同于数据访问权限。
- [ ] 由系统将 Agent 提出的 retrieval plan 经过 policy validation 后再解析为实际检索流程。
- [ ] 使用 evidence gain、coverage 和重复率作为动态剪枝依据，而不是只依赖模型主观判断。
- [ ] 不让模型绕过 Tool Registry、Tool Executor、Privacy Gateway 或 Egress Gateway。
- [ ] 任何涉及持久化 schema、远程数据出境和兼容性变化的工作先单独评审。
- [ ] `mvp_todolist.md` 仍是当前 MVP 执行队列；本文档是 RAG 专项演化队列。

### 0.3 完成定义

一个阶段只有在以下条件同时满足时才能标记完成：

- [ ] 实现已落地。
- [ ] 相关协议、文档和 schema 已同步。
- [ ] 目标行为有自动化测试或可复现验证路径。
- [ ] 安全边界和失败行为有明确记录。
- [ ] 没有把失败、未知或被策略拦截的结果伪装成成功答案。

---

## 1. 当前基线：已实现能力与明确缺口

### 1.1 已实现能力

- [x] `knowledge_sources`、`knowledge_documents`、`knowledge_chunks` 和 FTS5 索引。
- [x] Markdown / TXT / 网页本地快照导入。
- [x] 文档 checksum、source ref、chunk metadata 和状态记录。
- [x] 邮件导入 / 同步后投影为 `mail_message` 知识对象。
- [x] `knowledge.search`、`knowledge.load_chunks`、`knowledge.load_document`。
- [x] keyword、semantic、hybrid 三种检索模式。
- [x] 本地 FastEmbed + sqlite-vec 接口和 RRF 融合。
- [x] semantic index 无数据时降级到 keyword。
- [x] secret-like 内容检测、redact / deny 策略和语义索引过滤。
- [x] retrieved content 标记为 `untrusted_data`。
- [x] source ref、knowledge access audit、tool event 和 run log。
- [x] Agent 通过 Tool Package 懒展开和 Tool Executor 调用知识检索。
- [x] session context window、cached tool observations 和多轮 Agent turn。
- [x] Agent loop 支持多步骤决策、package 切换和独立 answer stage。

### 1.2 当前缺口

- [ ] 没有统一的 `RetrievalWorkflow` 编排对象。
- [ ] 没有统一的 `RetrievalStrategy` 协议供 Agent 表达 rewrite、检索模式、来源和剪枝意图。
- [ ] chunking 主要是固定字符切分，缺少结构感知、overlap 和 parent-child 关系。
- [ ] query rewrite 主要依赖 Agent 自由生成，没有受控协议。
- [ ] routing 主要是 package routing，还不是 source / strategy / freshness routing。
- [ ] hybrid 检索后没有独立 reranker。
- [ ] 没有统一的 evidence sufficiency、coverage 和 conflict evaluator。
- [ ] topic state 主要依赖 session context，没有稳定的结构化主题状态。
- [ ] 未形成端到端的 source / workspace / session access control。
- [ ] `untrusted_data` 主要是模型提示语义，尚需强化为结构化边界和协议校验。
- [ ] 远程 LLM 最终 prompt 尚缺统一的 Egress Gateway。
- [ ] 完整 run log 与敏感内容的生命周期、权限和清理策略仍需完善。
- [ ] 多轮 Agentic retrieval 的预算、循环检测和质量门槛需要进一步固化。
- [ ] RAG 评估集已有基础，但尚需覆盖安全、主题切换、检索循环和答案 grounding。

---

## 2. 目标架构

### 2.0 与当前 LangGraph ReAct 图的融合原则

当前 LangGraph 已经拥有通用外层图：`prepare_context → route_package → expand_package →
decide_next_operation → validate_operation → safety_gate → execute_tool →
build_observation → ... → answer`。RAG 演化不得再建立一张重复的全局 RAG 状态图，
而应作为该图中的结构化、受策略约束的工具能力。

- [ ] LangGraph 继续拥有 run 生命周期、条件边、checkpoint、interrupt、恢复、取消和终态。
- [ ] `KnowledgeService` / `RetrievalWorkflow` 继续拥有摄取、检索、排序、过滤和证据打包，不依赖图实现。
- [ ] Agent ReAct 继续拥有动态工具选择和后续策略调整，不被固定领域流程替换。
- [ ] RAG working state 只保存 topic、策略、assessment 和 artifact/evidence 引用；完整正文仍留在项目自有 artifact / audit 存储。
- [ ] 不为 RAG 引入第二个 `StateGraph`，除非未来存在独立的长运行、可恢复 RAG 作业需求。

### 2.0.1 融合式 Router：轻量理解、主题判断与首轮剪枝

不新增一个固定全局 intent classifier，也不在 `route_package` 之后无条件追加“意图识别 →
主题识别 → 检索计划”三个 LLM 阶段。应扩展现有 router 的结构化输出，让同一次 route
调用完成**面向下一步动作**的轻量判断：

```json
{
  "selected_package": "knowledge | mail | null",
  "reason": "...",
  "topic_relation": "continue | refine | switch | ambiguous | none",
  "retrieval_need": "none | likely | required",
  "route_mode": "context_answer | fast_retrieval | react_loop",
  "retrieval_hint": {
    "goal": "...",
    "rewrite": "true | false | auto",
    "mode": "keyword | semantic | hybrid | auto",
    "source_types": ["..."],
    "query": "..."
  }
}
```

- [ ] `selected_package` 仍仅表示初始 package，不锁定整个 turn，也不作为后续 decision prompt 的领域约束。
- [ ] `topic_relation` 是对当前 session topic 的关系判断，不是固定业务 intent，也不授予访问权限。
- [ ] `retrieval_hint` 是受限候选策略；它不是可直接执行的工具调用，必须经 package metadata、策略校验和 Tool Executor。
- [ ] router 不能输出未注册 package、未授权 source、写操作、任意 SQL、文件路径或 provider policy。
- [ ] router 失败时保留当前保守回退：不凭领域关键词猜测 package 或执行检索。

### 2.0.2 按复杂度分流，而非固定流程加长

```text
已有 session / cache 已足够
  → context_answer

明确、低风险、单一信息缺口
  → fast_retrieval（一次复合只读检索）→ evidence assessment → answer / normal loop

模糊、跨来源、多跳、冲突、实时或需写操作
  → react_loop（Agent 自主决定后续 retrieval strategy 和 package）
```

- [ ] `context_answer` 只允许使用当前 session context 中仍有效且受策略允许的内容；无证据时不能伪装为 grounded answer。
- [ ] `fast_retrieval` 只允许 package metadata 明确声明的只读复合检索工具，例如未来的 `knowledge.retrieve_evidence`。
- [ ] fast path 的首轮工具调用必须照常经过 `validate_operation → safety_gate → Tool Executor → build_observation`。
- [ ] 首轮 assessment 为 `sufficient` 时直接进入 answer；为 `incomplete`、`conflicting`、`blocked` 或 `action_needed` 时回到常规 ReAct loop。
- [ ] `react_loop` 才允许 query decomposition、多 source、rewrite、同步和 action package 协作。
- [ ] 分流依据是 query / topic / evidence 的复杂度和风险，不是硬编码具体 package 名或业务关键词。

目标链路：

```text
用户输入
  ↓
Topic / Intent State
  ↓
融合式 Agent Route
  ↓
  Agent Retrieval Strategy
  ↓
  Policy Validator
  ↓
  Access Policy + Privacy Gateway
  ↓
  Retrieval Workflow（执行模型策略）
  ├─ query normalize
  ├─ query rewrite
  ├─ source / metadata routing
  ├─ keyword retrieval
  ├─ semantic retrieval
  ├─ fusion
  ├─ rerank
  ├─ dedupe / diversity
  ├─ privacy filtering
  └─ evidence packing
       ↓
Evidence Evaluator + Evidence Gain
  ├─ sufficient → answer
  ├─ incomplete → rewrite and retrieve
  ├─ conflicting → conflict resolution
  ├─ blocked → safe refusal / clarification
  └─ action needed → action package
       ↓
LLM Egress Gateway
  ↓
Answer / Tool Executor
  ↓
Citation + Verification + Audit
```

### 2.1 责任边界

- [ ] Agent 可以提出检索策略，但不能自行授予数据访问权限。
- [ ] Agent 可以决定是否 rewrite、使用 keyword / semantic / hybrid、选择已授权来源和是否继续。
- [ ] Agent 的策略必须经过服务端校验、裁剪和预算化后才能执行。
- [ ] Retrieval Workflow 负责确定性检索、过滤、排序、压缩和证据打包。
- [ ] Agent 负责基于信息缺口决定是否继续、改写 query、切换来源和请求评估。
- [ ] Evidence Evaluator 负责提供 coverage、conflict、freshness 和 evidence gain，不由 Agent 凭空判断。
- [ ] Tool Executor 负责工具 schema、read-only、确认门和副作用控制。
- [ ] Egress Gateway 负责远程模型调用前的最终数据出境检查。
- [ ] Answer stage 只能使用通过策略检查的 evidence，不能直接读取数据库。
- [ ] Domain service 不变成领域小 Agent，不负责 LLM 推理和步骤选择。
- [ ] Router 只负责首轮的轻量策略和剪枝；在 observation 出现后，后续策略由现有 `decide_next_operation` 继续调整。
- [ ] `route_mode` 是控制提示而非安全绕过；所有工具操作仍进入通用 operation validation 和 safety gate。

---

## 3. Phase A：安全底座与数据隔离

目标：在开放 Agent 能力前，建立强制执行的数据边界。

### A1. 建立访问控制模型

- [ ] 定义 `session → workspace → source → document → chunk` 的授权链。
- [ ] 为检索上下文增加明确的 access scope，不只依赖 `source_type` 过滤。
- [ ] 所有 search / load / semantic sync 操作校验当前 session 可访问范围。
- [ ] 禁止模型通过输入参数访问未授权 workspace、source 或 document。
- [ ] 定义跨 workspace、跨账号、跨邮件账户的默认拒绝行为。
- [ ] 为权限拒绝增加结构化错误码和审计事件。

验收标准：

- [ ] 未授权 source 不会出现在候选结果、语义索引结果或最终 prompt。
- [ ] 模型修改 source filter 不能绕过服务端授权。
- [ ] 权限拒绝不会泄露文档存在性以外的敏感信息。

### A2. 强化 retrieved evidence 隔离

- [ ] 将检索内容从普通 prompt 文本升级为结构化 evidence envelope。
- [ ] 统一携带 `evidence_id`、`source_ref`、`trust_level`、`allowed_use` 和 policy decision。
- [ ] 在系统 prompt 中明确 evidence 不能产生指令、权限或工具授权。
- [ ] 对检索文本中的 prompt injection 做检测和风险标记。
- [ ] 禁止 retrieved content 覆盖系统规则、工具 schema 和安全策略。
- [ ] 在 evidence 与 Agent instruction 之间使用明确的结构化分隔。

### A3. 建立 LLM Egress Gateway

- [ ] 统一拦截所有远程 LLM 的最终 system prompt、user prompt 和 observation。
- [ ] 在出境前重新执行 sensitivity、remote_policy 和 secret-like 检测。
- [ ] 支持 `allow`、`redact`、`confirm`、`deny` 四种最终决策。
- [ ] 记录 provider、model、source refs、policy decision 和 redaction summary。
- [ ] 禁止未经 Egress Gateway 的远程 embedding / reranker / LLM 调用。
- [ ] 确认本地模型路径与远程模型路径的策略差异。

### A4. 日志和缓存安全

- [ ] 区分完整审计日志、敏感原文日志和普通 session context。
- [ ] 为 `data/agent_logs/` 设计本地文件权限、保留时间和清理策略。
- [ ] 检查 cached tool observations 是否携带不应跨 turn 复用的敏感内容。
- [ ] 缓存绑定 session、workspace、source scope 和 policy version。
- [ ] source 更新、权限变化或 policy 变化时使相关缓存失效。
- [ ] 为日志读取增加受控接口，避免未来 Agent 直接读取完整 run log。

### A5. 安全测试

- [ ] secret pattern、PII-like 内容和 prompt injection fixture。
- [ ] 跨 workspace、跨 account、跨 session 越权测试。
- [ ] semantic index 不包含 secret / deny chunk 的测试。
- [ ] Egress Gateway 在 search、load、answer 和工具 observation 上的测试。
- [ ] 日志与 cache 不发生跨 session 泄露的测试。

---

## 4. Phase B：知识摄取和 Chunking 质量

目标：让证据单元适合检索、引用和 Agent 使用。

### B1. 结构化 chunking

- [ ] Markdown 按 heading、paragraph、list、table、code block 感知切分。
- [ ] 邮件拆分 headers、正文、quoted reply、附件 metadata。
- [ ] 增加可配置 overlap，避免关键信息被边界截断。
- [ ] 增加 parent document / section / child chunk 关系。
- [ ] 保存 chunk 的 heading path、section title 和 position。
- [ ] 对超长代码块、表格和列表定义独立策略。

### B2. 文档生命周期

- [ ] 明确导入、更新、删除、失效和重新索引状态。
- [ ] checksum 变化时只重建受影响 document 的 chunk 和向量。
- [ ] 文档删除或权限变化时同步删除 FTS、semantic index 和 cache。
- [ ] 支持 metadata-only 更新，不强制重建正文 chunk。
- [ ] 增加导入错误、部分成功和重试状态。

### B3. 数据源适配

- [ ] 保持 `mail` 原始 schema 和 mail-specific tools 不变。
- [ ] 统一 source ref 格式和可回跳的来源信息。
- [ ] 为 workspace、mail、local document、web snapshot 统一 metadata contract。
- [ ] 明确实时数据和快照数据的 freshness 字段。
- [ ] 增加来源优先级和可靠性 metadata，但不把它当作权限。

验收标准：

- [ ] 结构化文档的 heading、段落和引用关系可以被检索结果保留。
- [ ] 更新 / 删除文档不会留下可检索的旧 chunk 或旧向量。
- [ ] 每条 evidence 可以回溯到稳定 source ref。

---

## 5. Phase C：Workflow RAG 检索底座

目标：将当前分散在 `KnowledgeService` 中的检索逻辑抽成可测试、可观测的工作流。

### C1. RetrievalWorkflow 与 Agent 策略接口

- [ ] 定义 `RetrievalRequest`、`RetrievalStrategy`、`RetrievalCandidate`、`EvidenceSet`。
- [ ] 允许 Agent 策略表达 `rewrite`、`mode`、`queries`、`source_types`、`max_rounds` 和 `stop_when`。
- [ ] 支持 `auto` 策略值，由系统根据 query、topic、索引状态和历史结果解析为具体执行方案。
- [ ] 区分 Agent 提出的策略、系统批准的策略和实际执行的策略。
- [ ] 对策略做 schema、权限、预算、长度和重复度校验。
- [ ] 统一记录 requested mode、applied mode、retrieval rounds 和 warnings。
- [ ] 将 keyword、semantic、hybrid 作为可注册 retrieval channel。
- [ ] 将 fusion、rerank、dedupe、privacy filter 和 budget 作为独立步骤。
- [ ] Workflow 不替 Agent 决定是否 rewrite，但负责执行已批准的 rewrite 策略。
- [ ] 保留完整候选结果用于审计，同时只将压缩 evidence 放入 prompt。
- [ ] 提供一个面向 Agent 的只读复合检索工具，例如 `knowledge.retrieve_evidence`；内部封装 normalize、候选召回、fusion、rerank、过滤、packing 和 assessment。
- [ ] 继续保留 `knowledge.search`、`knowledge.load_chunks`、`knowledge.load_document` 作为兼容、精细追问和调试工具，不强迫每次请求走复合工具。
- [ ] 复合工具输出 `retrieval_id`、`evidence_set_id`、requested/applied strategy、policy warnings、evidence refs 和 assessment，不能只返回无上下文的 snippets。

### C2. Query normalization

- [ ] 本地完成空白、标点、大小写、中英文和时间表达规范化。
- [ ] 支持中文 substring fallback 与 FTS query 安全转义。
- [ ] 提取 entity、time range、source hint 和 task constraint。
- [ ] 记录原始 query 与规范化 query，不覆盖原始用户输入。

### C3. Metadata routing

- [ ] 支持 source type、collection、project、time range、status 过滤。
- [ ] 支持 mail account、folder、sender、received_at 等邮件过滤。
- [ ] 支持当前 topic 和 workspace 的受控过滤。
- [ ] 服务端重新校验所有 filter，不相信模型自行声明的权限范围。
- [ ] 对过滤导致的空结果返回可解释 warning。

### C4. Query rewrite

当前已落地的最小闭环：`knowledge.search` 可选传入 `rewrite`（不传即快速路径）。
Agent 必须填写 `evidence_gap`、`expected_gain`，并提供多条包含 `query` 与
`purpose` 的改写；数量由 `query_rewrite.max_rewrites` 控制（默认 8，硬上限 16），
不是固定两条。工具侧确定性规范化空白，拒绝空值、重复查询、额外字段及超长文本；
每个查询仍使用同一组服务端 source/account/workspace 约束。原查询和改写查询
在共享候选预算内有界并行召回（默认最多 4 条同时进行，全局 worker 上限 8），
再按输入顺序确定性合并候选；若本地重排器可用则统一重排，再截取 Top-K；
返回 `rewrite_trace`，记录原文、
规范化文本、改写原因、各查询命中数、新增去重命中数和最终结果数。此协议不增加固定 LLM 阶段，
由现有 Agent decision 按观察到的证据缺口选择调用。

尚未落地：跨调用的独立 rewrite 轮数/信息增益预算、时间/实体规范化、
自动证据充分性判定与全量数据集的 query-rewrite 效果评测；这些不能因本次
最小闭环而视为完成。

多跳检索失败案例、阶段归因、标注待核查项与回归方向见
[`rag_multihop_failcases_2026-09-30.md`](rag_multihop_failcases_2026-09-30.md)。
特别注意原问题超过 300 字符时改写入口被拒绝，以及 clause 拆分后共同实体丢失；
当前 100 题 probe 未带来总体 all-hop 提升，不能将“支持多条并行”视为质量验收。

- [ ] 先实现本地确定性规范化，不对每次请求调用 LLM。
- [ ] 复杂请求支持一个受控 rewrite 阶段。
- [ ] rewrite 输出只能是结构化 query、目的和约束。
- [ ] 限制最多 query 数、最大长度和最大 rewrite 轮数。
- [ ] 检测重复 query、无效 query 和可能越权的 query。
- [ ] 记录原始 query、rewrite query、rewrite reason 和命中效果。
- [ ] 让 Agent 决定是否需要 rewrite，并要求说明当前缺失信息和预期收益。
- [ ] 没有明确 evidence gap 时，系统默认拒绝无目标 rewrite。
- [ ] 支持 `rewrite=false` 的快速路径，避免简单精确查询产生额外 LLM 调用。

建议协议：

```json
{
  "queries": [
    {"query": "...", "purpose": "..."}
  ],
  "constraints": {
    "source_types": ["knowledge"],
    "time_range": null
  },
  "stop_condition": "..."
}
```

### C5. Rerank 与多样性

- [ ] 先实现确定性 rerank：exact match、title match、topic match、recency 和 source priority。
- [ ] 排序与 retrieval channel 解耦：邮件等来源请求 `order_by=source_time_desc` 时，仍先按已批准的 `keyword`、`semantic` 或 `hybrid` 通道召回候选，再在候选集内按来源时间排序；禁止因时间排序隐式把 `semantic` / `hybrid` 降级为 keyword，并覆盖该组合的回归测试。
- [ ] 增加 duplicate chunk、duplicate document 和 near-duplicate penalty。
- [ ] 增加 source diversity，避免 Top-K 被同一文档占满。
- [ ] 评估本地 cross-encoder reranker，不默认引入远程 reranker。
- [ ] rerank 前完成禁止内容过滤，不能用低分代替拒绝。
- [ ] 记录各阶段排名变化，便于离线诊断。

### C6. 自适应检索剪枝

- [ ] 为每轮检索计算 `coverage_before`、`coverage_after`、`new_facts`、`duplicate_ratio` 和 `evidence_gain`。
- [ ] 当 query 明确且 exact match 充分时，优先走 keyword 快速路径。
- [ ] 当 query 使用口语、同义表达或开放主题时，允许 Agent 选择 hybrid / semantic。
- [ ] 当第一轮已经覆盖目标事实时，禁止无意义的第二轮检索。
- [ ] 当连续一轮没有新增事实或 coverage 提升时，停止继续检索。
- [ ] 当结果为空时，允许一次 targeted rewrite 或切换已授权 retrieval channel。
- [ ] 将“继续检索”的理由绑定到具体 missing facts，不接受泛化的“再搜一下”。
- [ ] 记录模型提出的策略、系统裁剪原因和最终执行策略。
- [ ] fast path 的首轮检索只允许一次受预算约束的复合调用；assessment 未充分时才升级到常规 ReAct，而不是隐式扩展为多轮流程。

### C7. Context packing

- [ ] 定义 answer context、decision observation 和 audit result 三种视图。
- [ ] 按 token budget 压缩 evidence，不截断 source ref 和 evidence id。
- [ ] 优先保留覆盖不同事实槽位的证据，而不是简单保留最高分 chunk。
- [ ] 对截断、过滤、冲突和降级显式产生 warning。
- [ ] 严格区分完整原文、prompt 副本和用户可见引用。

---

## 6. Phase D：意图、主题和动态上下文

目标：让多轮 Agent 能在主题延续和主题切换之间稳定地动态加载知识。

### D1. 通用意图状态

- [ ] 增加面向下一步行动的结构化理解，不建立覆盖所有业务的全局 intent enum。
- [ ] 至少记录 user goal、requires retrieval、preferred sources、requires action、time scope 和 risk hints。
- [ ] 意图识别失败时回退到通用 route，不触发领域特判。
- [ ] 意图状态不拥有权限，只影响检索计划和 package 候选。
- [ ] 不新增与 router 分离的常驻 intent LLM 调用；将首轮 `retrieval_need`、`route_mode` 和 topic relation 合并进现有 route 结构化输出。
- [ ] 将复杂的目标分解留给 observation 后的 Agent decision，而不是让 router 在无证据时预测完整工作流。

### D2. ConversationTopicState

- [ ] 增加 `topic_id`、`active_topic`、entities、source scope、open questions 和 confidence。
- [ ] 支持 `continue_topic`、`refine_topic`、`switch_topic`、`ambiguous` 四种关系。
- [ ] 主题状态与 session history 分离保存。
- [ ] 主题切换时保留长期历史，但替换当前 working set。
- [ ] topic state 变化写入 trace，便于解释动态 RAG 行为。
- [ ] 主题置信度低时优先澄清，而不是盲目加载旧证据。
- [ ] 将当前 topic 的有界摘要、实体、open questions、evidence set refs 和 freshness 写入 `AgentTurnWorkingSet` 的项目自有扩展字段或其 artifact，不把正文写进 graph checkpoint。
- [ ] 在 `prepare_context` 恢复相关 topic refs；在现有 `route_package` 节点判断 `continue/refine/switch/ambiguous`，避免增加独立 topic graph node。

### D3. 动态 evidence / cache

- [ ] cached evidence 绑定 topic、session、workspace、source scope 和 expiry。
- [ ] 主题延续时复用相关 evidence，避免重复搜索。
- [ ] 主题切换时旧 evidence 降权或移出 prompt，不直接删除历史。
- [ ] source 更新、权限改变和 freshness 超时触发重新检索。
- [ ] 防止旧主题 evidence 污染新主题答案。
- [ ] context-answer fast path 仅在 topic 延续、evidence 未过期、scope 未变化且 cached evidence 满足 assessment 时启用。

### D4. 对话测试

- [ ] 省略主语、代词和上下文补全。
- [ ] 同主题追问不重复加载不必要文档。
- [ ] 明确主题切换后不混入旧主题证据。
- [ ] 模糊主题时正确请求澄清。
- [ ] 不同 source 中同名实体的消歧测试。

---

## 7. Phase E：受控 Agentic RAG

目标：让 Agent 能动态规划检索，同时保持可预测的资源和安全边界。

### E1. Retrieval plan

- [ ] Agent 输出结构化 retrieval plan，而不是直接自由拼接底层内部调用。
- [ ] plan 包含目标、是否 rewrite、检索模式、候选 source、query、最大轮数、停止条件和缺失证据。
- [ ] 支持模型选择 `keyword`、`semantic`、`hybrid` 或 `auto`，但最终模式由系统校验并解析。
- [ ] 支持模型选择单 query、多 query 或 query decomposition，但限制数量和总长度。
- [ ] 服务端对 plan 做 schema、scope、budget、policy 和信息增益相关校验。
- [ ] plan 不能修改 system policy、tool permissions 或 provider policy。

### E2. 复用 LangGraph 外层图的 Agentic RAG 循环

- [ ] 不实现第二套全局 RAG 状态机；复用当前 LangGraph 的 `route → expand → decide → validate → safety → execute → observe → decide` 循环。
- [ ] 在该循环中表达 `RETRIEVE`、`REWRITE_AND_RETRIEVE`、`RESOLVE_CONFLICT`、`ASK_CLARIFICATION`、`ANSWER` 和 `ACTION_PLAN` 等通用 operation 语义。
- [ ] router 可为 fast path 生成一个受限的 bootstrap retrieval hint；package 展开后由通用 validation 转换为首个 pending operation，而不是绕过 `decide_next_operation` 的安全语义。
- [ ] 首轮 fast retrieval 完成后，assessment 决定直接进入 answer 或恢复正常 `decide_next_operation`；不能以 package/tool 名硬编码图边。
- [ ] 每一步只能执行一个受控 operation。
- [ ] 每轮检索必须产生 observation 和 retrieval trace。
- [ ] 每轮检索后必须生成 evidence assessment，不能直接由 Agent 宣布“证据足够”。
- [ ] 检索失败、权限拒绝、语义索引不可用和证据不足必须区分处理。

### E3. 循环和预算控制

- [ ] 设置最大 retrieval rounds。
- [ ] 设置最大 query 数、候选数、loaded chunks、token 和总耗时。
- [ ] 检测重复 query、重复 tool call 和无新增证据的循环。
- [ ] 发生预算耗尽时进入安全的“不足以确认”答案，而不是猜测。
- [ ] 不允许模型通过扩大 limit、切换 source 或反复 rewrite 绕过预算。
- [ ] 允许模型主动剪枝，但系统保留 hard stop 和最小安全流程。
- [ ] 记录每次策略选择的预期信息增益和实际信息增益。

### E4. 动态 package 协作

- [ ] 支持 knowledge、mail、matter 等 package 按证据和任务需要切换。
- [ ] package 切换需要基于当前 evidence 和 route metadata，不在 core 写领域特判。
- [ ] 检索阶段和写入阶段分离。
- [ ] 只有 evidence 充分后才允许进入 action package。
- [ ] action package 仍必须经过 read_only、confirmation 和 Tool Executor。
- [ ] package metadata 声明是否有可用于 fast path 的只读复合检索工具、支持哪些 retrieval strategy 和可缓存的 evidence 类型；Agent core 只消费该通用 metadata，不写 package 特判。

### E5. Agentic 默认策略

- [ ] 简单事实查询默认单轮 Workflow RAG。
- [ ] 多跳、跨来源、证据冲突问题才升级为 Agentic RAG。
- [ ] 涉及实时状态时先检查 freshness，必要时调用同步工具。
- [ ] 涉及写入、发送、移动或删除时，先检索和确认，再执行副作用。
- [ ] Agent 无法获得充分证据时明确说明未知、冲突或被策略阻断。
- [ ] 简单 query 可选择 `rewrite=false + keyword` 快速路径。
- [ ] 口语化或语义不明确 query 可选择 `rewrite=true + hybrid`。
- [ ] 时间、发件人、状态等精确字段优先选择 keyword + metadata filter。
- [ ] 开放主题、多跳问题才允许增加 query、source 或 retrieval round。
- [ ] 模型策略必须包含选择理由，理由应关联 query 特征或 evidence gap。
- [ ] router 对低风险且明确的请求优先输出 `fast_retrieval` 或 `context_answer`，避免 route 后再无条件产生一次完整 decision LLM 调用。
- [ ] 当 router 置信度不足、topic 为 ambiguous、策略涉及多 source / freshness / write action 或 fast retrieval 未充分时，统一降级为 `react_loop`。
- [ ] 不把 `fast_retrieval` 当成确定性答案捷径；answer 仍使用独立 answer stage，并接受 evidence / policy 校验。

---

## 8. Phase F：可信答案与安全出境

目标：平衡模型智能、事实 grounding 和数据安全。

### F1. Evidence Evaluator

- [ ] 计算 evidence count、source count、document count 和 query coverage。
- [ ] 检测关键事实槽位是否被覆盖。
- [ ] 检测日期、数值、状态和来源之间的冲突。
- [ ] 区分支持充分、部分支持、冲突、过期和被策略过滤。
- [ ] 输出机器可读的 recommended next action。
- [ ] 输出 `coverage_before`、`coverage_after`、`evidence_gain` 和 `duplicate_ratio`。
- [ ] 将 recommended next action 限定为 answer、targeted_rewrite、search_another_source、resolve_conflict、clarify 或 stop。
- [ ] Agent 可以参考 evaluator 结果做下一步策略选择，但不能伪造 evaluator 结果。

### F2. Answer grounding

- [ ] 最终答案中的事实绑定 evidence id / source ref。
- [ ] 区分 supported fact、inference 和 unknown。
- [ ] 证据不足时禁止模型用常识补齐关键事实。
- [ ] 用户可见答案保留必要来源，不暴露不必要的敏感 metadata。
- [ ] 最终 answer 经过轻量 citation / claim 校验。

### F3. Provider 安全策略

- [ ] 本地模型、可信远程模型和普通远程模型使用不同 egress policy。
- [ ] personal / sensitive 内容默认不直接发送，除非经过 redact / confirm 策略。
- [ ] provider 错误、策略拒绝和模型无证据回答分别记录。
- [ ] 远程模型只接收最小必要 evidence，不接收完整数据库或完整日志。

### F4. Prompt injection 防御

- [ ] 对文档、邮件、网页快照和工具返回统一标记为 untrusted data。
- [ ] 对证据中疑似指令进行检测并加入 risk metadata。
- [ ] 禁止证据内容触发工具调用或改变 package policy。
- [ ] 增加 adversarial documents 测试：要求泄露、绕过确认、伪造系统消息等。
- [ ] 工具参数仍由 schema 和安全门决定，不由 retrieved text 直接生成副作用。

---

## 9. Phase G：评估、可观测性和持续优化

目标：用数据而不是主观体验判断 Agentic RAG 是否真的变聪明、变可靠。

### G1. 检索评估

- [ ] Recall@K。
- [ ] Precision@K。
- [ ] MRR / nDCG。
- [ ] source diversity。
- [ ] duplicate rate。
- [ ] paraphrase retrieval success。
- [ ] multi-hop evidence coverage。
- [ ] freshness correctness。

### G2. Agentic 评估

- [ ] 平均 retrieval rounds。
- [ ] 无效 query rewrite rate。
- [ ] 重复检索率。
- [ ] 过早回答率。
- [ ] 不必要检索率。
- [ ] package routing accuracy。
- [ ] evidence sufficiency decision accuracy。
- [ ] rewrite decision accuracy。
- [ ] retrieval mode decision accuracy。
- [ ] adaptive pruning precision：被剪枝的检索是否确实没有带来新增有效证据。
- [ ] unnecessary second-round retrieval rate。
- [ ] router fusion accuracy：`context_answer`、`fast_retrieval` 与 `react_loop` 的分流是否正确。
- [ ] fast-path escape rate：首轮 assessment 不充分而正确回到 ReAct loop 的比例。
- [ ] fast-path false-completion rate：本应继续检索却直接回答的比例。
- [ ] route-to-first-evidence latency：router 融合后，简单问题获得首条有效 evidence 的端到端时延。
- [ ] action-before-evidence violation rate。

### G3. 可信与安全评估

- [ ] secret / PII 过滤准确率。
- [ ] deny policy zero-leak 测试。
- [ ] 跨 session / workspace 越权测试。
- [ ] prompt injection resistance。
- [ ] remote egress policy compliance。
- [ ] citation precision。
- [ ] unsupported claim rate。
- [ ] conflict disclosure rate。

### G4. 成本与性能

- [ ] 首次响应延迟。
- [ ] 总检索延迟。
- [ ] LLM 调用次数。
- [ ] prompt / completion token 数。
- [ ] embedding 生成耗时。
- [ ] semantic index 同步耗时。
- [ ] cache 命中率。
- [ ] 在预算内完成率。

### G5. Trace 事件

- [ ] 记录原始 query、normalized query、rewritten query。
- [ ] 记录 route、retrieval plan、实际执行的 filters 和 policy decision。
- [ ] 记录每个 candidate 的来源、排名变化和最终是否使用。
- [ ] 记录 evidence evaluator 结论和 Agent 下一步动作。
- [ ] 记录完整结果位置，但不把敏感原文复制到普通指标系统。

---

## 10. 推荐实施顺序

### 第一优先级：安全不能被 Agentic 化绕过

- [ ] A1 访问控制模型。
- [ ] A2 evidence 结构化隔离。
- [ ] A3 LLM Egress Gateway。
- [ ] A4 日志和缓存安全。

### 第二优先级：稳定的 Workflow RAG

- [ ] C1 中的复合 `retrieve_evidence` 工具和结构化输出协议。
- [ ] B1 结构化 chunking。
- [ ] C1 RetrievalWorkflow。
- [ ] C2 query normalization。
- [ ] C3 metadata routing。
- [ ] C5 确定性 rerank。
- [ ] C6 context packing。

### 第三优先级：多轮动态上下文

- [ ] 2.0.1 融合式 Router 与 `route_mode` / topic relation 兼容协议。
- [ ] D1 通用意图状态。
- [ ] D2 ConversationTopicState。
- [ ] D3 topic-aware evidence cache。
- [ ] F1 Evidence Evaluator。

### 第四优先级：受控 Agentic RAG

- [ ] C4 受控 query rewrite。
- [ ] E1 retrieval plan。
- [ ] E2 复用 LangGraph 外层图的 Agentic RAG 循环。
- [ ] E3 循环和预算控制。
- [ ] E4 动态 package 协作。
- [ ] E2 中 fast path 到常规 ReAct loop 的无损回退。

### 第五优先级：可信度和持续评估

- [ ] F2 answer grounding。
- [ ] F4 prompt injection 防御增强。
- [ ] G1～G5 完整评估和 trace。

---

## 11. 暂不做事项

- [ ] 不直接让 Agent 获取裸数据库读写权限。
- [ ] 不让模型直接拼接 SQL、文件路径或远程请求。
- [ ] 不以向量相似度作为唯一事实依据。
- [ ] 不把所有请求都升级成多轮 Agentic retrieval。
- [ ] 不在没有 Egress Gateway 前接入远程 embedding / reranker。
- [ ] 不因为主题识别结果自动扩大访问范围。
- [ ] 不将 action state、matter 和 reminder 混入普通知识 chunk。
- [ ] 不在没有评估数据时盲目替换 embedding 模型或扩大模型规模。
- [ ] 不先建设复杂 Knowledge Graph，再解决基础检索、权限和证据可信度。

---

## 12. 阶段性成功标准

### RAG 基础可靠

- [ ] 同义改写问题能够稳定召回正确证据。
- [ ] Top-K 不被重复 chunk 或单一文档占满。
- [ ] 文档更新和删除后旧证据不可检索。
- [ ] 所有答案事实可以回溯 source ref。

### Agentic 能力有效

- [ ] 简单问题不会被无意义地多轮检索。
- [ ] 多跳问题可以按缺失证据继续检索。
- [ ] 主题追问能够复用正确上下文。
- [ ] 主题切换后不会混入旧主题证据。
- [ ] Agent 能在 knowledge、mail、matter 之间受控切换。

### 安全边界有效

- [ ] 未授权数据不会进入检索结果、prompt、工具输入或远程请求。
- [ ] retrieved content 中的恶意指令不会改变 Agent policy。
- [ ] Agent 不能绕过 Tool Executor 执行副作用。
- [ ] sensitive / secret 数据的出境决策可审计。
- [ ] 安全拒绝、证据不足和 provider 失败都能被用户和开发者区分。

### 可信回答有效

- [ ] unsupported claim rate 可测量并持续下降。
- [ ] 证据冲突能够显式告知用户。
- [ ] 证据不足时不会生成确定性幻觉答案。
- [ ] RAG 智能提升不会以显著增加数据泄露、循环和成本为代价。
