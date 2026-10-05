# Backend Core：Engineering Guide

## 1. 项目定位

Backend Core 是整个 Local Knowledge Agent OS 的核心执行层。它承载 HTTP API、
运行时调度、知识上下文构建、能力选择、技能执行、验证与轨迹记录等核心职责。

Backend Core 目标是支持 Windows 和 Linux 原生 Python 运行。当前项目不再维护 Docker
运行路径；WSL 可以作为可选运行环境，但不应成为 Windows 支持的前提。

这个文档既描述当前已实现的后端骨架，也保留项目的中长期愿景，方便后续分阶段落地。

---

## 2. 核心职责

以下为早期基础服务链路；当前已实现模块与完整前台/后台流程见
[当前模块与运行流程](current_module_flows.md)。

```text
HTTP API
  ↓
Workspace Index
  ↓
Platform Path / Filesystem Adapter
  ↓
Workspace File Structure Index
  ↓
SQLite 持久化
  ↓
Static Capability Catalog
```

该基础链路继续保留；实际 Agent 执行已由结构化决策、LangGraph、工具注册表和安全门承载。

Workspace Index 同时需要记录 workspace 的结构化元数据和文件构成索引。文件构成索引包括目录层级、文件名、扩展名、路径位置、README、配置文件、测试命令和其他高价值元数据，用于在不依赖向量检索时提升效率和准确率。

邮件优先 MVP 中，Agent Harness 的执行边界必须保持清晰：

```text
HTTP API
  ↓
Agent Loop / Session
  ↓
Tool Package Registry
  ↓
Tool Executor
  ↓
Mail Tools
  ↓
MailService / SQLite
```

只读邮件专家 `mail_expert@1` 是可选的 ChildExecutor；它在专家内部使用固定的元数据快照、批量正文加载与有界分片分析流程，不把邮件领域逻辑写入通用 Agent core。具体启用方式、覆盖合同和限制见 [邮件专家 Agent 设计](mail_expert_design.md)，实施核对见 [开发 TODO](mail_expert_todolist.md)。

`MailService` 只提供确定性的存储、查询、加载和持久化方法，不调用 LLM，不选择执行步骤。
LLM 推理、工具选择、观察工具结果和反馈循环属于 Agent Loop。
第一版通用 Agent turn 使用单次查询内的 step-limited harness：先选择 Tool Package，
再展开具体工具，然后反复执行 `decision -> tool call -> observation`，直到 Agent
给出最终回答或达到步数上限。每一步只能调用一个工具或回答，避免在领域服务里隐藏
多步骤自动化。
ReAct 过程中可以通过 `expand_package` decision 继续展开其他 Tool Package；第一次 route
只决定起始 package，不应把整个 turn 锁死在单一领域工具里。典型邮件事务流程应是先展开
`mail` 检索 / 加载证据，再展开 `matter` 调用 `matter.create` 或 `matter.create_many`
写入独立事务。
`selected_package` 只作为兼容输出字段保留，语义等同于 `initial_package`，用于日志、
旧客户端和 route 质量评估；它不能作为后续 decision、decision_repair、tool_result_check
或 answer prompt 的上下文。跨 package 状态应通过 `expanded_packages`、`used_packages`
和 `active_package` 记录。工具结果检查必须使用真实 tool 所属 package，而不是初始
route package。
Tool Executor 在真正执行工具前会基于 Tool Registry 中的 `input_schema` 做统一校验，当前
支持必填字段、基础类型、数组元素类型、嵌套 object、枚举值和最小数值。校验失败时工具不会
执行，而是返回 `status=rejected`、`validation_errors` 和本地失败反馈；Agent Loop 会把这类
结果作为 observation 反馈给 LLM，让其修正参数或改选其他工具。
Agent Harness core 必须保持 package / domain agnostic。`app/core/` 的 prompt 和 fallback
逻辑不能写死具体 package 名、工具名、领域流程、路由关键词或工具调用示例；具体领域策略必须
放在 `app/tool_packages/` 的 package metadata、tool description、input/output schema、
routing hints、decision hints 和 cache policy 中。Agent Loop 可以把 Tool Registry metadata
注入 prompt，但不能在 core 里重新编码某个 package 的调用顺序、读写边界或跨 package 工作流。
decision 输出必须区分自然语言和内部操作，并采用 operation-first envelope：`operation`
是唯一控制通道，保存 `tool_call` / `final_answer` / `request_confirmation` 等结构化动作；
`assistant_message` 只保存用户可见的过程说明，不能选择工具，也不能补成最终回答。
`decision.operation.type == "final_answer"` 只表示证据已足够、可以进入独立 `answer`
stage；`operation.final_answer` 即使存在也不能作为最终用户答案返回。真正展示给用户的
最终自然语言回答只能来自独立 `answer` 或 `context_answer` LLM stage。这样工具调用中途的
模型文本可以被展示和记录，但不会和真实执行动作或最终回答混在同一个字段里。
这里的“最终回答”可以是用户明确要求的 JSON、CSV 或其他交付文本，不等于内部 operation
控制信封；不得用全局 JSON 禁令或无条件语言指令覆盖用户的交付格式。显式 TaskContract
的 schema 在原结构化 answer 路径保持优先并验证；普通用户格式要求尚不是强制 schema
校验，context_answer 也尚未补齐该合同验证，不能宣传所有结构化输出都已保证合法。
如果 LLM 判断当前 turn 不需要展开任何 Tool Package，Agent Loop 仍应允许 LLM 基于当前
session context window 直接回答；“不需要工具”和“系统无法处理”不能混为一谈。
当 provider 返回不完整 JSON 但明确选择了某个 package 时，Agent Loop 可以做保守恢复，
并在 run log 中保留原始输出，避免模型格式问题直接破坏工具链路。
如果 decision 阶段返回非 JSON 的自然语言文本，Agent Loop 必须记录为
`invalid_plain_text_decision`，不能将其恢复为最终回答。Agent Loop 应先做一次格式重试，
在重试 prompt 中带上上一条 plain-text 输出并强调必须返回 operation-first JSON。若重试仍
失败但本轮已经通过工具读取到足够证据，Agent Loop 可以进入独立 `answer` 阶段让 LLM 基于
观察结果重新生成自然语言回答；否则应停止本轮工具执行，并返回可追踪的结构化决策失败提示。
如果 decision 阶段返回的非 JSON 内容疑似工具调用，例如包含 `tool_name`、`tool_input`
或 `tool_call`，Agent Loop 必须 fail closed：先尝试 `decision_repair` 修复为合法
operation，修复失败则停止本轮执行，不能把工具调用残片当作最终 answer。
每次真实工具调用后都必须生成反馈 observation。反馈至少包含执行成功 / 失败状态和可读
message。Agent Loop 必须先执行本地协议校验：工具声明 `output_schema` 时按该 schema
校验 `ToolResult.output`；未声明时只要求 `ToolResult` 是完整 JSON 对象形状。只有工具
执行失败、被拒绝、输出协议不匹配或本地无法确认时，才追加独立 `tool_result_check` LLM
调用。底层 `ToolResult.status` 已失败时，反馈不能被升级为成功。工具包可以按自身
metadata 或工具输出提供额外 summary，但这不是 Agent core 的领域特判。
进入后续 LLM prompt 的 observation 可以是压缩副本。超过通用大小阈值的工具结果进入
规则 gate：完整 `ToolResult` 保存在当前 run 的本地 artifact 中，模型只看到有结构路径、
数量/长度和省略标记的预览，以及 `_result_cache.artifact_id`。需要更多信息时展开
`observation` package，通过只读 `observation.read` 以 JSON Pointer 路径和 offset/limit
分页查看；读取只接受当前 run 的 `tool_result` artifact，跨 run、跨子 Agent 均拒绝。
子 Agent 还必须在其不可变 ToolView 中获准使用 `observation` package；未获授权时
仍以普通工具范围检查拒绝，不能凭 artifact ID 扩权。
最终 `answer` 的 provider 发送边界还会生成有界 `context_delivery`：它描述最终拟合
prompt 中保留的缓存字符串区间，单位为 Unicode codepoints，按 artifact/hash/path
合并；不是工具扫描量或上游来源完整性的证明。`coverage=complete` 仅表示这个版本
的 cached value 完整进入本次 prompt，`upstream_coverage` 保持 unknown。元数据加入
后重新拟合并核对，不能稳定或挤占 child 回答预留时放弃可选元数据，保留原安全 prompt。
原始工具 JSON 中的 `_delivery_view` 等字段只作描述，不具备认证权限。需要把分页文本
映回原缓存的 registered tool 可提供 `context_delivery_bindings(result_payload=...,
view_payload=..., context=..., check_cancel=...)` 后端 callback；它必须重验当前 run、
ToolView、artifact/hash、路径与原文，逐次 I/O 前后检查取消。Core 不识别具体包名，
未注册 callback 时使用有界通用精确文本/规则预览映射。未知投影与重复 observation ID
不能得到完整性认证；此机制不增加 LLM 调用，也不保证自由回答的语义正确。
回读页亦有大小上限；工具返回文本视为不可信数据，不能覆盖上层指令。小结果沿用原有
压缩逻辑：单个字符串超过 4000 字符时保留首尾；列表最多展示前 20 项，
并在 `_prompt_compaction.truncated_lists` 标明路径、原始/可见/省略条数；多条观察合计
约 16000 字符预算，超限时优先保留较新的观察并报告较早观察被省略的数量。
这些压缩后的可见条目不能用于推断完整结果集为空或只有这么多条；清单类工具必须另行
返回总数、当前页范围和续页状态。完整工具输入输出必须继续保留在 `tool_events`、session payload
和本地 run log，避免为了节省 token 牺牲审计与可回放性。
Agent core 只能把工具返回的原始结果、通用反馈和 package metadata 交给 LLM；如果某个领域
需要状态计数、去重提示、读写边界或业务摘要，应由对应 Tool Package 或 domain service 生成。
Agent Loop 还会由本地 harness 生成 `progress_events`，用于前端展示用户友好的运行过程：
package 选择、LLM 的 `assistant_message`、package 展开、工具开始 / 完成、工具反馈、最终回答
和校验 warning 都会进入该事件流。`progress_events` 不参与后续上下文窗口压缩，也不由 LLM
生成，避免把运行流水混入对话记忆。
HTTP stream endpoint 使用 SSE 输出统一的 Agent Run event，不维护另一套事件模型。stream
输出会用 `stream_part` 标注 lifecycle、progress、tool_result、llm_audit、llm_delta 和
final_answer 等结构。`POST /agent/turn/stream` 默认使用 stream response mode，Agent
runtime 中所有 LLM stage 都通过 `LLMService.stream()` 接收 provider token delta，并作为
`llm_delta` 事件发送。route、decision、decision_repair、tool_result_check 等 JSON stage
只流式接收和审计 token，必须累计完整输出后再解析 JSON 和执行工具。自然语言输出 stage
会标记为 `assistant_answer`，其他 LLM stage 会标记为 `agent_process`。前端应只把
`llm_delta.payload.display_target == "assistant_answer"` 的 delta 当作最终回答实时输出；
`final_answer` event 只作为最终校准 / 补全事件。
最终回答可以经过本地轻量校验，结果写入 `verification_warnings`。当前 Agent core 不硬编码
具体 package / domain 的 verifier；如果某个领域需要事实或副作用校验，应通过 package
metadata 或独立 verifier 注册，校验只记录 warning，不自动改写答案。

会话基础设施当前由本地 SQLite 管理，使用显式 `session_id` 支撑平行会话和多轮会话。
后端不维护隐式全局当前会话；前端切换会话时必须把目标 `session_id` 传给运行入口。
Session Service 只负责创建会话、追加消息、读取历史和更新时间，不调用 LLM，也不选择工具。
每个 session 还维护一个本地 context window，默认预算为 `65536` token。窗口只保存
前文摘要和近期 user / agent 问答，用于下一轮 prompt 注入；完整工具结果、LLM prompt /
output、错误和运行过程保留在 `data/agent_logs/`，必要时再通过日志或历史检索恢复。
窗口未满时不触发摘要；超过预算时由独立 `context_summarize` LLM 调用重新生成 summary，
并只保留最近两条 user / agent 消息原文。
Agent Loop 会把同一 session 中已加载过、且被 Tool Package metadata 标记为可缓存的本地
资源作为 session-scoped resource cache 重新注入后续 turn。缓存以通用
`cached_tool_observations` 形式暴露给 route / decision prompt；后续追问如果缓存已经足够，
Agent 应直接基于缓存回答，或把缓存作为已有 observation 使用，而不是重复调用等价工具。
缓存归 harness 管理，工具仍保持一次性、无会话状态；哪些工具结果可缓存由 Tool Package
metadata 决定，不能在 Agent core 中写死。
邮件数据源是全局本地知识源，不存在独立的“邮件会话引擎”。Mail tools 只在某个 Agent turn
中按当前 `session_id` 读取一次性信息并返回观察结果；是否把用户输入、工具观察、`run_id`
或 `log_path` 写入会话历史，必须由通用 Agent turn / Session 层显式决定，邮件工具和
`MailService` 不应自动写会话消息。
`knowledge` domain 是 source-agnostic 的本地证据库，负责导入 Markdown/TXT/网页文本快照、
生成 chunk、写入 SQLite FTS、保留 source refs，并在检索输出进入 Agent prompt 前执行最小
Privacy Gateway 过滤。`knowledge.search` 只能返回 Top-K 最小必要片段；`knowledge.load_chunks`
返回选中 chunk 的有界文本；`knowledge.load_document` 默认只返回 metadata 和 chunk ids。
所有 retrieved content 都必须标记为 untrusted data，不能被当作 Agent 指令。Minecraft Wiki、
PRTS 等大型 wiki 的页面抓取应作为外部 crawler 或前端流程，把页面文本以 `web_page` source
导入 knowledge；Agent 可见工具不负责自动爬站。后续可以将 `mail_chunks` 映射或同步进
`knowledge_chunks`，但 `mail` schema 和 mail-specific tools 必须继续保留。
远程邮箱同步由 runtime 管理，不属于独立邮件 Agent。服务启动时 runtime 可以执行一次
Outlook 自检同步，把最新邮件写入本地 SQLite；服务运行期间可以通过后台轮询继续同步。
按需同步则暴露为 `mail.sync` 工具，由 Main Agent Brain 在用户要求“最新/同步/当前邮箱”
或本地邮件可能过期时选择调用。无论触发来源是 startup、background、API 还是 tool，
同步过程都只负责更新本地邮件知识源，后续检索、加载、概括和事务整理仍通过 Agent Loop
中的 mail tools 完成。
事务管理是独立 domain service，不从属于邮件。`MatterService` 负责 `matter` 的本地
持久化、关键词检索、列表、状态更新和来源链接；`mail`、`agent_trace`、`file`、后续
`calendar_event` 等都只是 `matter_source_links` 中的 source。Agent 从邮件中提取待办
时，应先通过 mail tools 获取证据，再通过 matter tools 创建或更新事务，不能让
`MailService` 直接替用户做事务决策。
Agent 可见的 matter tool schema 必须明确必填字段、枚举和示例。写入类工具只能使用
`open`、`in_progress`、`waiting`、`done`、`cancelled` 作为 status，只能使用 `low`、
`normal`、`high`、`urgent` 作为 priority。创建事项前如果存在重复风险，Agent 应先调用
`matter.search` 检查已有事项；发现相似事项时，应选择 update、skip/no_op，或明确说明为何
它是独立新事项后再 create。
旧的 `mail.persist_matters` / `mail_matters` 是邮件优先 MVP 早期遗留能力，只作为历史
数据和迁移路径保留，不再注册进 Agent 可见的 Tool Registry。
runtime context 目前通过上下文提供：每次 Agent turn 都会把 `current_time` 注入 session
context window，包含 UTC、本地时间、时区和日期，用于处理“今天”“明天”“8月5号之后”
这类相对时间。`runtime.now` 暂不注册为 Agent 可见工具，避免在当前 MVP 中干扰工具路由。

每次 Agent Loop 运行都必须生成本地自然语言友好的 run log。run log 由代码模板生成，
不调用 LLM，至少记录：

- 运行时间、`run_id`、`session_id` 和用户输入。
- 第一层 Tool Package catalog 和实际展开的 package。
- 本轮使用的 session context window 快照。
- Agent 每一步 decision，包括 action、assistant_message、operation、reason、选中的
  tool、tool input 或最终 answer。
- 每个 tool 的选择时间、输入、输出、状态、错误和反馈。
- 本地生成的 progress events 和最终回答 verification warnings。
- 给 LLM 的完整 system prompt、user prompt 和 LLM 完整输出。
- 最终结构化结果。

这些日志默认写入 `data/agent_logs/`。由于日志可能包含完整邮件正文和个人信息，
它们必须保持本地持久化，不应进入 git，也不应默认同步到外部服务。

LLM provider 的失败必须进入 run log。HTTP `429` 限流应被单独识别为
`rate_limited`，记录 `status_code`、`retry_after`、完整 prompt 和错误输出，并由
Agent Loop 按 `Retry-After` 或本地默认等待时间重试；重试耗尽后再降级到本地 heuristic
或其他后备策略，而不是让领域服务自行处理；对于没有明确后备策略的工具执行阶段，应停止
继续调用工具并返回可追踪失败提示。认证失败、网络失败、超时、非 429 HTTP 错误和 provider
响应解析失败也必须拆分记录，便于后续调试和策略调整。
每次 LLM 调用还会生成结构化审计记录，当前落点是 `AgentTurnResult.llm_events`、
Agent Run event payload 和 markdown run log。审计字段包括 `llm_call_id`、`run_id`、
`trace_id`、`session_id`、stage、client、provider、model、response mode、状态、耗时、
HTTP status、provider request id、`Retry-After`、OpenAI / OpenAI-compatible error body
字段、canonical `error_category`、`is_retriable`、finish reason、token usage、
content length、prompt summary 和隐私受控 metadata。完整 prompt / output 保留在本地
run log 中，SQLite `llm_calls` 表留到第二版再落地。

---

## 3. 推荐目录结构

```text
backend/
  pyproject.toml
  .env.example

  app/
    api/
      main.py
      routes/
        health.py
        workspaces.py
        capabilities.py

    core/
      agent_turn.py
      llm.py
      runtime.py
      context.py
      retrieval.py
      runtime_loop.py
      tools.py
      sessions.py
      runtime_context.py
      tracing.py

    domains/
      mail.py
      matters.py

    integrations/
      outlook.py

    tool_packages/
      mail.py
      matter.py
      runtime.py

    platform/
      base.py
      detect.py
      paths.py
      filesystem.py

    storage/
      db.py
      models.py
      vector_store.py
```

### 说明

- 上面这份结构是“目标结构”，不是当前实现的全部内容。
- 当前仓库只实现了其中很小一部分，但目录规划保留了后续演进路径。
- 这份结构的价值在于：它让后续扩展不会每次都重新发明分层方式。
- `app/core/` 只放 Agent Harness 核心结构、LLM 调用、会话 / 上下文、安全校验 / trace
  等通用运行时能力；具体领域服务和工具包实现不放在 `core`。
- `app/domains/` 放确定性领域服务与领域数据模型，例如邮件和事务。领域服务不调用 LLM，
  不选择工具，不隐藏多步骤 agent 行为。
- `app/tool_packages/` 放 Agent 可见的工具包适配层，例如 `mail`、`matter`、`filesystem`、`bash`。
  这些模块可以依赖领域服务和核心 tool 协议，但不应把具体工具实现放回 `core`。
- `app/integrations/` 放 Outlook / Graph 等外部 provider 接入，避免 provider 代码污染
  Agent Harness 核心层。
- `app/platform/` 是跨平台边界，用于集中处理路径、文件系统和后续命令执行差异。

### 存储与检索约定

- SQLite 是当前 MVP 的主存储方案，用于 workspace 索引元数据、任务记录、trace、confirmation 和其他结构化状态。
- Qdrant 只作为可选语义检索扩展，不作为 MVP 必需项。
- 非向量检索应优先依赖 workspace 的结构化索引、文件构成索引和本地文件系统检索。
- Workspace 路径必须先经过 `PathResolver`，文件扫描必须优先经过 `FilesystemScanner`。
- Agent 主动读取或修改明确文件路径时使用 `filesystem` Tool Package，不走 workspace
  index/context 摘要路径。`filesystem.read_file` 返回受限片段、行号和 full-file sha256；
  `filesystem.edit_file` 只做已存在 UTF-8 文本文件的 targeted `old_text -> new_text`
  替换，必须带 `expected_sha256`，默认要求 `old_text` 唯一匹配。filesystem 路径字段中的
  相对路径按当前 session workspace（未设置时为第一个 configured workspace root）解析，并支持 `workspace_root`、
  `WORKSPACE_ROOT` 和 `LKA_WORKSPACE_ROOT` 变量展开。
- 目录 listing、全文搜索、测试命令和交互式命令执行使用 `bash` Tool Package。
  `bash.run` 支持同步和后台终端；后台终端通过 `bash.read_session`、`bash.write_session`、
  `bash.interrupt_session` 和 `bash.terminate_session` 继续交互。具体命令按白名单动态判定
  `read_only`，同时检查参数和 shell 展开，不按命令名直接放行。原地写入、输出文件、
  子进程 hook、可编程脚本、可隐藏参数的动态展开与可执行文件路径不视为已证明只读；
  `sed` 仅保留数字/末行 print 选择，`uniq` 不允许第二个输出文件参数。未知/未证明只读的
  调用仍可在批准后执行，但不能绕过冻结 READ scope。这是保守分类，不是 OS sandbox，
  不能证明第三方可执行程序及其隐式本地配置绝无副作用。POSIX 分类保留引号识别真实
  分隔符/注释，换行后命令分别检查；READ 同步/后台环境不继承 RIPGREP_CONFIG_PATH、
  BASH_ENV、ENV 或 BASH_FUNC_ 导出函数，审批后的非只读调用保留相关语义。这不隔离
  宿主 login profile、PATH 或所有程序配置。`bash.run` 默认工作目录是当前 session
  workspace（未设置时为第一个 configured workspace root），相对 `cwd` 在该 root 内解析；命令环境注入 `workspace_root`、
  `WORKSPACE_ROOT`、`LKA_WORKSPACE_ROOT` 和以分号分隔的 `LKA_WORKSPACE_ROOTS`，模型应优先
  使用相对路径或这些变量定位 workspace 文件。
- Capability Registry 第一层应优先暴露 Tool Package，而不是一次性暴露所有具体工具。
- 具体工具应在 Agent 决定展开某个 package 后再进入上下文。
- 不维护全局 intent 枚举；Agent 应输出面向下一步执行的 routing / execution decision。

---

## 4. 核心接口

```python
class LocalKnowledgeAgentRuntime:
    def index_workspace(self, workspace: str, options: dict | None = None) -> dict:
        ...

    def list_capabilities(self) -> list[dict]:
        ...
```

### 设计意图

- `index_workspace`：把 workspace 变成可检索、可统计、可追踪的知识入口。
- `list_capabilities`：返回当前对外公开的静态能力目录。

---

## 5. 后端模块优先级

### P0

- FastAPI skeleton
- SQLite
- workspace index
- static capability catalog
- local-only binding
- native Windows/Linux path resolution
- cross-platform read-only filesystem scanning

### P1

- KnowledgeObject
- TaskContext
- retrieval
- Capabilities registry redesign
- Native Skills 接口抽象
- CommandRunner 平台抽象

### P2

- SubAgent abstraction
- organize_files
- analyze_repo
- delegate_to_coding_agent manual mode
- Verifier diff/test
- Claude/Codex CLI 自动调用
- SSE/WebSocket 任务流
- Skill Evolution proposal

---

## 6. 当前实现与愿景的关系

早期轻量骨架覆盖：

- Health Check
- Workspace index 的基础统计
- Capability 列表返回
- Runtime Debug 结构化链路
- SQLite trace / runtime event 持久化
- 平台识别、workspace 路径解析和只读文件扫描

现已在该基础上实现通用 Agent、子任务调度、邮件/知识工具、会话与项目、长期记忆、
异步压缩与关注简报。当前实现边界见 [模块流程](current_module_flows.md)；
下面的演进说明保留作历史背景：

- 你在愿景里定义的模块没有丢，它们是后续的目标结构。
- 当前实现只是把最基础的 HTTP 服务和索引先跑起来，避免一开始就陷入复杂度。
- 未来可按 P0 / P1 / P2 逐步增加真正的知识层和能力层。

---

## 7. 设计原则

- 先有最小闭环，再补智能能力。
- 先把服务、索引、能力目录打牢，再做更复杂的 agent 协作。
- 先保留显式能力注册，再考虑自动选择和自动调度。
