# Local Knowledge Agent OS：MVP Todolist

> 目标：按下面的顺序逐项实现，最终得到一个真正以 Agent 核心能力为中心的 MVP。
>
> 这份清单的目标不是“把后端堆起来”，而是打通一条最小但完整的闭环：
>
> 用户任务 -> 本地知识检索 -> 上下文构造 -> 任务理解 -> 计划生成 -> 能力选择 -> 子任务 / 专家工具执行 -> 结果验证 -> 轨迹记录 -> Skill Proposal
>
> 执行规则：
>
> - 一次只推进一个阶段中的少量任务。
> - 每个条目开始前先明确预期目标。
> - 每个条目都要说明修改了哪些文件。
> - 每个条目都要说明做了哪些测试、结果如何、如何验证。
> - 优先完成“能形成闭环”的能力，而不是孤立基础设施。

---

## 0. MVP 成功定义

### 0.0 Urgent 方向调整：邮件处理助手优先

第一版 MVP 的主场景调整为邮件处理助手。本地 workspace 能力继续保留，但在第一版中降级为辅助 context provider。

邮件优先主线不推翻原有 Agent Harness 设计，而是把主数据源从 workspace 暂时切换为邮件：

```text
邮件导入 / 同步
  -> 本地邮件持久化
  -> 关键词 / 语义检索
  -> TaskContext / MatterContext
  -> Main Agent Brain
  -> LLM 处理循环
  -> 事务整理
  -> Trace Recorder
  -> 后续 Skill Evolution
```

Urgent track 的阶段目标：

- [x] 建立本地邮件存储模型，支持邮件、附件 metadata、邮件分块、事务和处理记录。
- [x] 提供 `POST /mail/import`，先支持本地 JSON 导入，便于无 OAuth 场景下测试。
- [x] 提供 `GET /mail/search`，先支持 SQLite FTS 关键词检索。
- [x] 提供 `GET /mail/matters`，查看事务列表。
- [x] 新增本地 provider 配置模板，覆盖 LLM、Outlook、IMAP 和 local BGE embedding 配置。
- [x] 接入 Outlook 只读同步，采用 Device Code Flow，权限优先限制为 `User.Read Mail.Read offline_access`。
- [x] Outlook 第一版先同步邮件正文和附件 metadata，附件内容后续按需下载。
- [x] Outlook 同步支持 Graph delta state、服务启动自检、后台轮询和 Agent 按需 `mail.sync` 工具触发。
- [x] 部署 / 接入真实第三方 LLM API provider；未配置 API key 时保留 mock provider，保证测试稳定。
- [x] 建立第一版邮件 Tool Package / Tool Executor，避免 `MailService` 直接调用 LLM。
- [x] 建立通用 Agent turn 入口，由会话层选择是否展开 `mail` package 并记录工具调用 / LLM prompt / output。
- [x] 对 LLM HTTP `429` 限流做显式识别、等待重试、run log 记录和本地 heuristic 降级。
- [x] 对 LLM 认证、网络、超时、非 429 HTTP 和 provider 响应解析错误做分类记录和针对性降级。
- [x] 建立第一版平行 / 多轮会话基础设施，支持创建会话、追加消息、列出会话和读取历史；邮件工具调用只接收 `session_id` 作为访问上下文，不自动写会话历史。
- [x] 建立独立事务管理 MVP，提供 `MatterService`、`/matters` API 和 `matter` Tool Package。
- [x] 提供 `runtime.now` 工具，并在每次 Agent turn 的 context window 中注入当前时间。
- [ ] 将邮件纳入本地持久化存储管理，补齐邮件整理、检索和概括工具。
- [ ] 将 TaskContext / MatterContext / trace 查询进一步接入 session，让前端会话切换能恢复完整运行上下文。
- [ ] 接入本地 `BAAI/bge-m3` embedding provider，并兼容 Windows / Linux 模型缓存路径。
- [ ] 设计 harness skill evolution 路径：允许 Agent 在受控权限下从数据库 schema、历史 run log
  和工具结果中归纳可复用遍历 / 批处理策略，沉淀为可审计 skill proposal，而不是直接绕过
  Tool Registry 任意访问数据库。
- [ ] 后续再把 Codex / Claude Code Expert Tools 放回邮件任务后的优化路径。

决策记录：

- 邮件是第一版 MVP 的主数据源，本地 workspace 是辅助上下文来源。
- Outlook 真实数据接入优先使用 Microsoft Graph Device Code Flow，避免本地测试依赖公网 callback。
- 初期只申请只读权限，不发送邮件、不修改邮箱状态、不删除邮件。
- 附件初期只保存 metadata，避免首次同步被大附件、空间和下载失败拖慢。
- 语义检索可以先预留接口和数据结构，关键词检索先用 SQLite FTS5 落地。
- 本地配置文件使用 `config/local.toml`，该文件不进入 git，也不会通过 WSL -> Windows 同步脚本复制。
- Outlook Graph Device Code Flow 不保存邮箱密码；IMAP 路径如需密码，优先使用 `password_env` 引用环境变量。
- Outlook 同步提供 `POST /mail/outlook/auth/start`、`POST /mail/outlook/auth/complete`
  和 `POST /mail/outlook/sync`；同步采用 Microsoft Graph delta query 记录 `next_link` /
  `delta_link`，并支持 startup、background、API 和 Agent tool 四种触发来源。Webhook /
  push notification 后续再实现。
- Capability Registry 第一层优先暴露 Tool Package，不一次性暴露全部工具 schema；Agent 确认目标相关后再展开具体工具。
- 不维护覆盖所有任务类型的全局 intent 枚举；Agent 产出面向下一步动作的 routing / execution decision。
- `MailService` 只负责确定性的本地存储、检索、完整正文加载和持久化；LLM 推理和工具编排必须发生在 Agent Loop。
- Agent run log 由本地代码生成，不调用 LLM；日志可能包含完整邮件正文和个人信息，默认仅写入本地 `data/agent_logs/`。
- LLM 限流属于 Agent Loop 的外部 provider 失败，必须被记录、等待重试并在重试耗尽后降级处理，不应交给 `MailService`。
- 其他 LLM 调用错误必须在 Agent Loop 中分类处理，避免把所有 provider 问题折叠成不可诊断的通用失败。
- 会话由显式 `session_id` 区分，后端不维护隐式全局当前会话；前端切换会话时必须把目标 `session_id` 传入运行入口。
- 邮件数据源是全局本地知识源，不存在独立邮件会话引擎；mail tools 只提供一次性观察结果，各 session 是否保留邮件引用、摘要和上下文由通用 Agent turn 决定。
- 不提供 `/mail/process` 这类邮件专属 agent endpoint；邮件整理必须通过通用 Agent turn 调用 `mail` tools 完成。
- 通用 Agent turn 已扩展为单次查询内的 step-limited 决策循环：先选择 Tool Package，
  再按观察结果多次决定是否调用工具或最终回答；当前可执行 package 仍以 `mail` 为主。
- Agent decision 输出已区分 `assistant_message` 和 `operation`，并收紧为
  operation-first envelope。`operation` 是唯一控制通道，工具调用和最终回答必须进入
  结构化 `operation`；`assistant_message` 只作为展示文本，不能触发工具、不能补成
  final，也不能被裸文本恢复为最终回答。
- ReAct loop 已支持 `expand_package`，route 只选择起始 package，不再把整个 turn 锁死在
  一个工具包里。邮件事务整理应先展开 `mail` 读取证据，再展开 `matter` 写入独立事务。
- 旧 `mail.persist_matters` 已从 Agent 可见 Tool Registry 中隐藏；它只作为
  `mail_matters` 历史兼容 / 迁移路径保留。
- 每个真实工具调用后都会生成 `feedback`，至少记录成功 / 失败状态和可读 message；LLM
  可用时还会通过独立 `tool_result_check` 阶段检查工具结果是否符合上一条调用决策，并把
  检查结果写回 tool event 和后续 observation。
- decision 阶段的非 JSON 自然语言会记录为 `invalid_plain_text_decision`，不能恢复为
  `answer`。疑似工具调用的损坏 JSON 必须先尝试 `decision_repair`，修复失败则记录
  `malformed_tool_call` 并停止，避免出现“看起来调用了工具、实际没有执行”的假成功。
- Agent Loop 对 route、decision、decision repair、tool result check 和 answer 阶段不再
  主动设置 `max_tokens`，避免本地 harness 侧截断模型输出；真实 provider 自身限制仍需
  通过错误处理和日志观察。
- 多轮追问中，如果当前 session context window 已足够回答，Agent 可以不展开工具包，
  直接进入 `context_answer` LLM 阶段；如果 route LLM 返回不完整 JSON 但明确选择
  `mail`，Agent 会保守恢复该 package 选择并继续执行工具链。
- Tool Executor 已在执行前统一校验 `input_schema`，包括 required fields、基础类型、
  数组元素、嵌套 object、枚举值和 minimum。校验失败会返回 `status=rejected` 和
  `validation_errors`，不执行工具副作用；Agent Loop 会把 rejection 作为 observation
  反馈给 LLM，让模型修正参数后继续。
- 同一 session 中已经通过 `mail.load_messages` 读取过的完整邮件会作为
  `cached_mail_messages` 注入后续 turn；追问应优先复用缓存或把缓存作为已有
  observation，而不是重复加载同一邮件原文。
- 每个 session 已新增本地 context window，默认预算 `65536` token；窗口未满时保留近期
  user / agent 问答，满后由独立 `context_summarize` LLM 调用重写前文摘要，并只保留最近
  两条消息原文。完整运行过程仍保存在本地 run log，后续再接入可检索历史 trace。
- 远程邮件同步归 runtime 管理，不建立邮件专属 Agent。启动自检和后台轮询只更新本地邮件
  知识源；当用户要求最新邮箱状态或本地缓存可能过期时，Main Agent Brain 可以选择
  `mail.sync`，同步后仍通过 `mail.search` / `mail.load_messages` 获取证据并回答。
- 事务管理独立于邮件。`MatterService` 保存任务、事件、待办和提醒候选项；邮件、trace、
  文件和后续日历对象都只能作为 `source_link` 关联到 matter。Agent 从邮件归纳事务时，
  必须通过通用 loop 先读取证据，再选择 `matter` tools 写入或更新。
- Agent turn 会输出本地生成的 `progress_events`，用于展示 package 选择、模型过程文本、
  工具开始 / 完成、工具反馈、最终回答和校验 warning；这些运行过程不进入上下文窗口。
- 工具反馈支持按工具适配的可选 `domain_summary`，不要求每个工具都实现。当前仅
  `matter.*` 工具提供 matter 数量、状态计数、优先级计数和 due date 数量，用于区分
  工具执行状态和业务对象状态。
- Agent turn 已有轻量最终回答校验，当前只记录 `verification_warnings`，不自动改写答案。
  Matter tools 的 schema 已补充 required fields、allowed values 和 examples，decision
  prompt 会要求模型遵循这些合同，并在有重复风险时先检索已有 matters。
- 每次 Agent turn 都会注入 `current_time`，并可按需调用 `runtime.now`；相对时间解析
  应优先基于这个确定性上下文，而不是让 LLM 猜当前日期。
- Agent 不应默认拥有裸数据库写权限。未来可以通过受控 DB inspection / query tools 让它读取
  schema、统计量和历史轨迹，再由 Skill Evolution Layer 生成遍历、批处理和去重策略的
  skill proposal；真正落库仍应走明确注册的 domain tools 和风险控制。

### 0.1 MVP 最终应具备的能力

- 能在本地启动 Backend Core。
- 能通过 HTTP API 接收任务。
- 能索引一个本地 workspace。
- 能从本地知识中构造上下文。
- 能根据任务生成可读、可解释的执行计划。
- 能识别任务意图、风险等级和候选能力。
- 能执行基础技能或子任务编排。
- 能按上下文管理策略调用 Codex / Claude Code 等专家工具。
- 能自动生成 Skill Proposal，必要时可生成 Skill Draft / Scaffold。
- 能记录 task、trace、confirmation 等核心状态。
- 能提供基础的 capability 列表。
- 能在高风险操作时进入确认流程。
- 能给出可追踪、可回放、可验证的结果。

### 0.2 MVP 不是必须完成的内容

- 不要求真正的复杂 agent 自主推理闭环。
- 不要求生产级 embedding / rerank / vector search。
- 不要求完整 GUI。
- 不要求多用户系统。
- 不要求企业权限系统。
- 不要求自动删除文件或自动 git push。
- 不要求一次性完成全部 skill 自动生成体系。

### 0.3 MVP 的核心判断标准

如果用户输入一个真实任务，系统能够：

1. 理解任务类型。
2. 检索相关本地知识。
3. 构造 TaskContext。
4. 生成可读计划。
5. 选择合适能力。
6. 执行一个或多个子步骤。
7. 记录执行轨迹。
8. 在高风险时要求确认。
9. 返回稳定、结构化的结果。

那么 MVP 就算成立。

---

## 1. 先固化规则与文档

这一阶段的目标不是写业务逻辑，而是把项目“怎么做”先定义清楚。

### 1.1 仓库结构确认

- [x] 确认 `app/`、`docs/`、`pyproject.toml` 的职责划分清晰。
- [x] 确认 `docs/` 内文档分层正确：
  - [x] `project_overview.md` 作为项目总纲
  - [x] `backend_engineering_guide.md` 作为后端架构说明
  - [x] `backend_implementation_plan.md` 作为长期实施路线图
  - [x] `platform_support.md` 作为跨平台运行与适配说明
  - [x] `api_contract.md` 作为接口契约
  - [x] `ai_coding_standard.md` 作为 coding 规范
  - [x] `mvp_todolist.md` 作为当前执行清单
- [x] 确认 `temp.md` 作为源草稿保留，不参与对外说明。

### 1.2 术语统一

- [x] 统一 `Main Agent Brain` 的定义和中文解释。
- [x] 统一 `Knowledge Context Engine` 的职责边界。
- [x] 统一 `Capability Registry`、`Native Skills`、`Local Tools`、`Expert Tools`、`MCP Tools`、`Sub Agents` 的命名。
- [x] 统一 `Trace`、`Context Package`、`Verifier` 的文档描述。
- [x] 统一“计划 / 任务 / 轨迹 / 确认 / 能力 / 技能”这些核心词汇的用法。

### 1.3 运行目标确认

- [x] 明确后端默认监听地址为 `127.0.0.1:8765`。
- [x] 明确 SQLite 作为本地持久化方案。
- [x] 明确 Qdrant 先作为能力预留或占位，不把它当作 MVP 成败关键。
- [x] 明确前端与后端的职责边界。

### 1.4 跨平台基础适配

- [x] 明确后端目标从 Linux-only 调整为 Windows/Linux 原生 Backend Core。
- [x] 增加 `app/platform/` 作为平台差异边界。
- [x] 增加平台识别能力，支持 `auto`、`windows`、`linux`、`macos`。
- [x] 增加 workspace 路径解析入口，避免业务层直接处理路径差异。
- [x] 增加只读文件系统扫描器，支持递归、隐藏文件、symlink 和扫描上限配置。
- [x] 让 `/workspaces/index` 使用平台扫描配置。
- [x] 让 `/runtime/debug` 的 local retrieval 复用平台扫描器。
- [x] 更新 README、API 合同、工程指南、长期路线图和跨平台说明文档。

决策记录：

- 不拆分 Windows Backend / Linux Backend 两套代码。
- 一套 Backend Core 保持 FastAPI、SQLite、Context、Trace 等核心逻辑共享。
- Windows/Linux 差异集中进入 `app/platform/`。
- WSL 作为可选运行方式，不作为 Windows 原生支持的前提；当前项目不再维护 Docker 运行路径。
- 后续命令执行、专家工具和本地工具调用应新增统一 `CommandRunner`，不要在业务层写死 shell。

### 完成标准

- 文档、命名、执行规范没有明显冲突。
- 读者能在 5 分钟内理解这个项目做什么、怎么跑、先做什么。
- 读者能理解 Windows/Linux 原生运行的路径、配置和测试差异。

---

## 2. 先做上下文管理

这一阶段是项目亮点的第一根支柱。没有 Context Engine，后面的规划和执行都只是规则分支。

### 2.1 定义上下文对象

- [x] 定义 `Context Package` 的结构。
- [x] 至少支持 `related_files`、`related_snippets`、`project_constraints`、`risk_notes`、`suggested_tools`、`verification_plan`。
- [x] 让 Context Package 能表达“这次任务为什么这么做”。
- [x] 明确 Context Package 是任务规划和专家工具输入的共同基础。

决策记录：

- 上下文对象采用从粗到细的层级：`BaseContext`、`SessionContext`、`TaskContext`、`ExecutionContext`、`VerificationContext`。
- `SessionContext` 与对话窗口绑定，随用户交互、用户设置、记忆、workspace 和执行反馈持续演化。
- `TaskContext` 承接原 checklist 中 `Context Package` 的目的，用于表达计划内容与结构，不再为旧名称单独保留具体子类。
- `TaskContext` 从 `SessionContext`、workspace 索引、本地知识检索结果和当前目标中派生，不默认绑定 task，也不默认携带 `task_id`。
- `TaskContext` 至少包含 `related_files`、`related_snippets`、`project_constraints`、`risk_notes`、`suggested_tools`、`verification_plan`，并通过 `reasoning_summary` 表达“为什么这些上下文支撑当前规划或执行”。
- Main Agent Brain、Expert Tools、Sub Agents 和 Verifier 应消费 `TaskContext` 或其派生视图，避免各模块自行拼接不透明上下文。
- 具体结构、生命周期和派生规则以 `docs/project_overview.md` 的 Knowledge Context Engine 章节为准。

### 2.2 Runtime Debug Infrastructure

这一阶段先搭建调试型运行骨架，目的不是实现复杂 agent 自动执行，而是让后续加入的 Context、Retrieval、Tool、LLM 和 Trace 模块都能被独立验证。

- [x] 定义 `EventRecord`，统一记录 `event_id`、`event_type`、`session_id`、`context_id`、`payload`、`status`、`error`、`created_at`。
- [x] 定义显式 `RuntimeLoop` 骨架，按固定阶段串联 context 更新、检索调用、工具调用、LLM 调用和 trace 记录。
- [x] 定义 `LLMClient` 接口和 mock provider，先支持稳定假响应，避免早期调试依赖真实模型。
- [x] 定义 `RetrievalProvider` 接口和 mock/local provider，用于后续验证 workspace 扫描、本地知识检索和上下文召回。
- [x] 定义 `ToolSpec`、`ToolInvocation`、`ToolResult`，统一 native skill、local tool、expert tool 的调用形状。
- [x] 定义最小 `TraceRecorder`，记录每个阶段的输入、输出、context id、event id、错误信息和验证线索。
- [x] 提供一个调试入口，用于验证 `SessionContext -> TaskContext -> Retrieval -> Tool/LLM -> Trace` 的结构化链路。
- [x] 明确该阶段不做复杂自主规划、不做后台任务队列、不做生产级 embedding、不做真实文件修改工具。

决策记录：

- 新增 `POST /runtime/debug` 作为 2.2 阶段的调试入口，不替代后续 `/tasks/plan` 或 `/tasks/run`。
- Runtime Debug 链路采用固定事件顺序：`session_context.updated`、`task_context.derived`、`retrieval.completed`、`tool.completed`、`llm.completed`、`trace.recorded`。
- LLM 调用使用 `MockLLMClient`，返回稳定假响应，不依赖外部模型或网络。
- Retrieval 使用 `LocalDebugRetrievalProvider`，只做只读 workspace 文件元数据采样，不做文本抽取、embedding 或 rerank。
- Tool 调用使用 `runtime_debug_echo` mock local tool，只回显结构化上下文信息，不修改文件。
- Trace 和事件写入 SQLite 的 `traces` 与 `runtime_events` 表，供后续调试、回放和 verifier 扩展使用。

完成标准：

- 给定 `session_id`、workspace 和用户输入，系统能生成结构化事件序列。
- 系统能创建或更新 `SessionContext`，并派生一个可检查的 `TaskContext`。
- 系统能触发一次 mock/local retrieval 调用，并返回结构化结果。
- 系统能触发一次 mock LLM 或 mock tool 调用，并返回结构化结果。
- 系统能记录一条 trace，说明每一步使用了什么 context、调用了什么接口、得到什么结果。

### 2.3 本地知识检索

- [ ] 实现 workspace 内容扫描。
- [ ] 能识别常见文件类型。
- [ ] 能提取文本内容。
- [ ] 能生成可用于规划的上下文摘要。
- [ ] 能根据任务关键词召回相关文件或片段。
- [ ] 能识别 README、测试命令、配置文件等高价值上下文。

### 2.4 Context Assembly

- [ ] 根据用户任务组装 TaskContext。
- [ ] 根据任务类型选择不同的上下文来源。
- [ ] 给复杂任务提供更完整的上下文。
- [ ] 给简单任务提供轻量上下文。
- [ ] 明确 TaskContext 里哪些信息是“建议”，哪些是“约束”。

### 2.5 约束注入

- [ ] 注入项目约束。
- [ ] 注入风险提示。
- [ ] 注入用户偏好或历史行为。
- [ ] 注入建议执行工具和验证计划。
- [ ] 注入专家工具调用前必须携带的必要上下文。

### 完成标准

- 系统不只是“拿到文件列表”，而是能形成可用于 agent 决策的 TaskContext。
- 上下文管理可以独立支撑任务规划、专家工具输入和 trace 解释。

---

## 3. 再做 Main Agent Brain

这一阶段目标是把“知道任务”变成“知道怎么做”。

### 3.1 任务理解

- [ ] 实现任务分类。
- [ ] 能区分知识总结、文件整理、代码分析、复杂修改、一般问答。
- [ ] 能识别任务目标中的动作意图。
- [ ] 能识别任务中隐含的风险信号。

### 3.2 任务规划

- [ ] 为每类任务输出明确步骤。
- [ ] 步骤按执行顺序排列。
- [ ] 计划中能体现检索、执行、验证三个阶段。
- [ ] 计划中能体现高风险点。
- [ ] 计划中能体现是否适合直接执行、是否需要确认、是否需要专家工具。

### 3.3 风险判断

- [ ] 识别修改、删除、重命名、批量操作等高风险任务。
- [ ] 将高风险任务标记为 `high`。
- [ ] 将可直接执行的任务标记为 `low` 或 `medium`。
- [ ] 让风险等级直接影响后续执行路径。

### 3.4 能力选择

- [ ] 从能力目录中选择候选能力。
- [ ] 将任务类型映射到 native skills。
- [ ] 保留 expert tools 的入口。
- [ ] 保留未来 MCP tools 的扩展位置。
- [ ] 让能力选择结果能进入 trace 和 verifier。

### 完成标准

- 系统能对用户输入给出“怎么做”的答案，而不只是“是什么”的回答。
- Main Agent Brain 已经可以输出结构化计划与能力建议。

---

## 4. 打通任务计划与执行

这一阶段是 MVP 的核心闭环，重点是“从输入到输出”。

### 4.1 Task Plan

- [ ] 实现 `/tasks/plan`。
- [ ] 输入 task 后先做 intent 识别。
- [ ] 结合 TaskContext 生成计划。
- [ ] 返回建议能力列表。
- [ ] 返回风险等级。
- [ ] 让返回值足以给前端做下一步决策。

### 4.2 Task Run

- [ ] 实现 `/tasks/run`。
- [ ] 根据 task、workspace、frontend、mode 生成稳定 task id。
- [ ] 根据 task 和 plan 生成稳定 trace id。
- [ ] 将 task 记录写入数据库。
- [ ] 将 trace 记录写入数据库。
- [ ] 返回执行结果对象。
- [ ] 支持把复杂任务拆解为多个可追踪步骤。

### 4.3 Execution Mode

- [ ] 支持 `interactive` 模式。
- [ ] 支持 `manual` 或 `dry_run` 思路的预留。
- [ ] 高风险任务优先进入确认流。
- [ ] 低风险任务可以直接走完整执行闭环。

### 4.4 子任务编排

- [ ] 把复杂任务拆成多个子步骤。
- [ ] 每个子步骤有明确目标。
- [ ] 每个子步骤都有可追踪的中间结果。
- [ ] 允许先串行，后并行。
- [ ] 让子任务结果可以回流到 trace 和 skill proposal。

### 完成标准

- 用户任务能从“输入”变成“计划 + 执行记录 + trace”。
- 这条链路是 MVP 的核心，而不是附属功能。

---

## 5. 补齐 Capability Registry 和 Skills

这一阶段让 Agent 的“能做什么”变得显式可管理。

### 5.1 能力清单

- [ ] 设计显式能力列表。
- [ ] 包含至少以下能力：
  - [ ] `search_local_knowledge`
  - [ ] `summarize_folder`
  - [ ] `extract_tasks`
  - [ ] `organize_files`
  - [ ] `analyze_repo`
  - [ ] `delegate_to_coding_agent`
  - [ ] `claude_code`
  - [ ] `codex`
- [ ] 每个能力包含类型、风险、是否需要确认。

### 5.2 能力分类

- [ ] 区分 `native_skill`。
- [ ] 区分 `local_tool`。
- [ ] 区分 `expert_tool`。
- [ ] 为未来 `mcp_tool` 留出扩展空间。

### 5.3 任务与能力映射

- [ ] 知识总结优先匹配 `summarize_folder`。
- [ ] TODO 提取优先匹配 `extract_tasks`。
- [ ] 文件整理优先匹配 `organize_files`。
- [ ] repo 分析优先匹配 `analyze_repo`。
- [ ] 复杂代码修改保留 `delegate_to_coding_agent` / `claude_code` / `codex` 路径。

### 5.4 专家工具接入

- [ ] 根据 TaskContext 和裁剪后的 ExecutionContext 组织给 Codex / Claude Code 的输入。
- [ ] 专家工具调用前先检查风险等级和确认条件。
- [ ] 专家工具调用后收集输出、diff 或结果摘要。
- [ ] 将专家工具调用结果写入 trace。

### 完成标准

- 用户和系统都能看到“当前可用能力是什么”。
- 任务计划能与能力清单建立对应关系。
- 专家工具不再是“旁门”，而是受控的能力入口。

---

## 6. 做好 Trace、Verifier、Confirmation

这一阶段是让系统从“能跑”走向“可信、可解释、可回放”。

### 6.1 Trace Recorder

- [ ] 记录用户原始目标。
- [ ] 记录识别出的 intent。
- [ ] 记录执行计划。
- [ ] 记录上下文摘要。
- [ ] 记录使用的能力。
- [ ] 记录子任务结果。
- [ ] 记录验证结果。
- [ ] 记录成功或失败状态。

### 6.2 Verifier

- [ ] 设计基础 verifier 接口。
- [ ] 支持 diff 检查的预留。
- [ ] 支持测试命令执行的预留。
- [ ] 支持文件越权检查的预留。
- [ ] 支持风险操作确认的预留。
- [ ] 支持把 verifier 结果写入 trace。

### 6.3 Confirmation Flow

- [ ] 高风险任务进入确认流程。
- [ ] `POST /confirmations/{confirmation_id}` 接收 decision。
- [ ] 支持 approved。
- [ ] 支持 rejected。
- [ ] 决策写入数据库。
- [ ] 状态更新为 resolved。

### 6.4 Trace 查询

- [ ] 支持 trace 列表。
- [ ] 支持 trace 详情。
- [ ] trace 列表返回精简字段，方便浏览。
- [ ] trace 详情返回完整信息，方便调试。

### 完成标准

- 任意一次任务执行都不是黑箱。
- 能通过 trace 回答“系统做了什么、为什么这么做”。

---

## 7. 引入 Skill Evolution

这一阶段把“经验沉淀”变成能力，而不是只停留在日志。

### 7.1 模式发现

- [ ] 从 trace 中识别重复成功模式。
- [ ] 识别“文件总结 + TODO 提取 + checklist 生成”等高频模式。
- [ ] 识别需要专家工具介入的重复任务模式。
- [ ] 识别适合沉淀为 Native Skills 的模式。

### 7.2 Skill Proposal

- [ ] 生成 Skill Proposal。
- [ ] 生成 Skill Draft 或 Scaffold。
- [ ] 为 skill 定义名称、输入、步骤、验证方式、适用场景。
- [ ] 让 proposal 可以被人工审查和回滚。

### 7.3 Skill 注册

- [ ] 能把 skill 作为候选能力注册到 capability registry。
- [ ] 能让 skill 被计划器优先考虑。
- [ ] 能让 skill 的来源 trace 可追溯。

### 7.4 Skill 演化边界

- [ ] MVP 阶段允许生成 proposal / draft。
- [ ] MVP 阶段不要求完全自动把 proposal 写成生产代码。
- [ ] MVP 阶段必须保证 skill 演化可审计、可回退。

### 完成标准

- 系统不只是会执行任务，还能从任务中学习。
- 轨迹和 skill proposal 之间形成闭环。

---

## 8. 路由层与代码组织

这一阶段目的是让实现和文档一致，代码结构清晰。

### 8.1 FastAPI App 装配

- [ ] 在 `main.py` 中创建 FastAPI app。
- [ ] 注入 settings。
- [ ] 挂载 runtime 到 `app.state`。
- [ ] 挂载所有路由模块。

### 8.2 路由拆分

- [ ] `health.py`
- [ ] `workspaces.py`
- [ ] `tasks.py`
- [ ] `traces.py`
- [ ] `capabilities.py`
- [ ] `confirmations.py`

### 8.3 路由行为

- [ ] 路由层只做参数接收、错误转换和结果返回。
- [ ] 业务逻辑不写在路由层。
- [ ] 路由命名与文档保持一致。
- [ ] 路由返回类型与 schemas 保持一致。

### 8.4 代码结构要求

- [ ] runtime 保持为协调层，不堆过多细节逻辑。
- [ ] 存储层只处理持久化。
- [ ] schema 层只处理数据结构。
- [ ] 文档和代码术语保持一致。

### 完成标准

- 代码层分层清楚。
- 路由层不会变成难维护的“大函数堆”。

---

## 9. 文档与说明

这一阶段保证别人能看懂、能跑起来、能继续开发。

### 9.1 总览文档

- [ ] `project_overview.md` 保留项目背景、目标、愿景、场景和成功标准。
- [ ] `backend_engineering_guide.md` 保留后端架构、模块说明和技术边界。
- [ ] `backend_implementation_plan.md` 保留长期路线图。
- [ ] `api_contract.md` 保留接口样例与约束。
- [ ] `ai_coding_standard.md` 保留 coding 规范。
- [ ] `mvp_todolist.md` 作为当前执行入口。

### 9.2 README

- [ ] README 能说明项目是什么。
- [ ] README 能说明怎么启动。
- [ ] README 能说明文档结构。
- [ ] README 能让新读者快速找到正确文档。

### 9.3 文档一致性

- [ ] 文档里的接口示例和代码保持一致。
- [ ] 文档里的术语和代码保持一致。
- [ ] 文档里的目标和 MVP 范围保持一致。

### 完成标准

- 新读者能靠文档理解项目。
- 开发者能靠文档知道下一步做什么。

---

## 10. 测试与验证

这一阶段的目标是让每个模块都有可重复的验证方法。

### 10.1 基础校验

- [ ] 运行 Python 语法检查。
- [ ] 运行最小启动验证。
- [ ] 确认 `/health` 可访问。

### 10.2 接口验证

- [ ] 验证 `/workspaces/index`。
- [ ] 验证 `/tasks/plan`。
- [ ] 验证 `/tasks/run`。
- [ ] 验证 `/tasks/{task_id}`。
- [ ] 验证 `/capabilities`。
- [ ] 验证 `/traces`。
- [ ] 验证 `/traces/{trace_id}`。
- [ ] 验证 `/confirmations/{confirmation_id}`。

### 10.3 数据验证

- [ ] 确认数据库文件可生成。
- [ ] 确认数据表可创建。
- [ ] 确认 workspace 索引可持久化。
- [ ] 确认 task / trace / confirmation 可持久化。
- [ ] 确认重启后数据仍可读。

### 10.4 端到端验证

- [ ] 选择一个真实文件夹作为测试 workspace。
- [ ] 跑通索引 -> 规划 -> 执行 -> trace 查询 的最小闭环。
- [ ] 跑通高风险任务 -> confirmation 的分支闭环。
- [ ] 跑通至少一次 expert tool 或 skill proposal 的流程。

### 完成标准

- 至少有一条端到端 smoke test 能跑通。
- API、存储、运行时、规划、执行、轨迹、演化三层都能被基础验证覆盖。

---

## 11. 部署与运行

这一阶段让项目真正具备“别人拉下来就能看”的能力。

### 11.1 本地运行

- [ ] 能用 `uv sync` 安装依赖。
- [ ] 能用 `uv run uvicorn app.api.main:app` 启动服务。
- [ ] `.env.example` 中的配置可直接参考。
- [ ] Windows PowerShell 原生启动说明可复现。
- [ ] Linux shell 原生启动说明可复现。

### 11.2 运行说明

- [ ] README 写清本地运行方式。
- [ ] README 写清默认端口和默认监听地址。
- [x] README 写清 Windows/Linux 原生启动基础命令。
- [x] `docs/platform_support.md` 写清平台配置、路径、API 调用和测试矩阵。

### 完成标准

- 项目能被别人拉下来后快速启动。

---

## 12. 当前阶段交付顺序建议

如果你要真正按顺序做，我建议这样排：

### 第一阶段：邮件优先闭环

- [x] 部署 / 接入真实 LLM API provider，并保留 mock fallback。
- [x] 建立第一版 `mail` Tool Package，包含 `mail.search`、`mail.load_messages` 和 `mail.sync`；旧 `mail.persist_matters` 不再暴露给 Agent。
- [x] 建立通用 Agent turn，并在其中生成本地自然语言 run log。
- [x] 为 LLM 调用加入限流等待重试和错误分类降级。
- [x] 建立第一版平行 / 多轮会话基础设施；mail tools 只使用 `session_id` 作为工具访问上下文，不写 session history。
- [ ] 将邮件作为本地持久化知识源管理，明确 account、message、attachment、chunk、matter、processing run 的生命周期。
- [ ] 补齐邮件整理、检索和概括工具，让邮件候选集可以进入 TaskContext / MatterContext。
- [ ] 将 session 与 TaskContext / MatterContext / trace 查询接口进一步整合。
- [ ] 将 agent run log 与 session / trace 查询接口关联起来。
- [x] 基于邮件上下文完成一次 mock/real LLM 处理链路验证。

### 第二阶段：再理解

- [ ] TaskContext / MatterContext
- [ ] workspace 扫描与上下文摘要
- [ ] 邮件与 workspace 混合上下文组装
- [ ] 任务意图识别
- [ ] 基础计划生成

### 第三阶段：再执行

- [ ] `/tasks/plan`
- [ ] `/tasks/run`
- [ ] capability 列表
- [ ] 基础 skill / sub agent 入口
- [ ] Expert Tool 调用入口

### 第四阶段：再解释

- [ ] `/traces`
- [ ] `/traces/{trace_id}`
- [ ] verifier 基础记录
- [ ] confirmation 流程
- [ ] skill proposal 生成

### 第五阶段：再演示

- [ ] 文档统一
- [ ] 示例请求齐全
- [ ] demo 路径可讲清楚
- [ ] smoke test 可复现

---

## 13. MVP 完成验收

当以下条件全部满足时，可以认为 MVP 完成：

- [ ] 能启动服务。
- [ ] 能索引 workspace。
- [ ] 能生成 TaskContext。
- [ ] 能规划任务。
- [ ] 能运行任务。
- [ ] 能列出 capabilities。
- [ ] 能处理 confirmation。
- [ ] 能查询 task 和 trace。
- [ ] 能做基础验证。
- [ ] 能调用 Codex / Claude Code 作为专家工具。
- [ ] 能基于 trace 生成 Skill Proposal 或 Skill Draft。
- [ ] 文档能完整解释项目目标和运行方式。
- [ ] 至少一条任务闭环可演示。

---

## 14. 后续演进预留

当前阶段完成后，下一阶段可以继续做：

- [ ] 真正的 retrieval / embedding / vector store 接入。
- [ ] 更强的 intent parser。
- [ ] 更细粒度的 capability registry。
- [ ] Native skill 拆分与扩展。
- [ ] Verifier 增强。
- [ ] 复杂代码任务自动化。
- [ ] Skill evolution 机制增强。
- [ ] Windows frontend 适配。
