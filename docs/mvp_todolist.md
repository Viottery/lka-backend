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

### 0.1 MVP 最终应具备的能力

- 能在本地启动 Linux Backend。
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
3. 构造上下文包。
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

- [ ] 确认 `app/`、`docs/`、`Dockerfile`、`docker-compose.yml`、`pyproject.toml` 的职责划分清晰。
- [ ] 确认 `docs/` 内文档分层正确：
  - [ ] `project_overview.md` 作为项目总纲
  - [ ] `backend_engineering_guide.md` 作为后端架构说明
  - [ ] `backend_implementation_plan.md` 作为长期实施路线图
  - [ ] `api_contract.md` 作为接口契约
  - [ ] `ai_coding_standard.md` 作为 coding 规范
  - [ ] `mvp_todolist.md` 作为当前执行清单
- [ ] 确认 `temp.md` 作为源草稿保留，不参与对外说明。

### 1.2 术语统一

- [ ] 统一 `Main Agent Brain` 的定义和中文解释。
- [ ] 统一 `Knowledge Context Engine` 的职责边界。
- [ ] 统一 `Capability Registry`、`Native Skill`、`Expert Tool`、`Sub Agent` 的命名。
- [ ] 统一 `Trace`、`Context Package`、`Verifier` 的文档描述。
- [ ] 统一“计划 / 任务 / 轨迹 / 确认 / 能力 / 技能”这些核心词汇的用法。

### 1.3 执行规范

- [ ] 明确 coding agent 必须先读哪些文档。
- [ ] 明确 coding agent 每一步都要告知预期目标。
- [ ] 明确 coding agent 每一步都要说明修改的文件。
- [ ] 明确 coding agent 每一步都要说明测试与验证结果。
- [ ] 明确 coding agent 遇到架构、API、数据模型变更时需要先确认。

### 1.4 运行目标确认

- [ ] 明确后端默认监听地址为 `127.0.0.1:8765`。
- [ ] 明确 SQLite 作为本地持久化方案。
- [ ] 明确 Qdrant 先作为能力预留或占位，不把它当作 MVP 成败关键。
- [ ] 明确前端与后端的职责边界。

### 完成标准

- 文档、命名、执行规范没有明显冲突。
- 读者能在 5 分钟内理解这个项目做什么、怎么跑、先做什么。

---

## 2. 先做上下文管理

这一阶段是项目亮点的第一根支柱。没有 Context Engine，后面的规划和执行都只是规则分支。

### 2.1 定义上下文对象

- [ ] 定义 `Context Package` 的结构。
- [ ] 至少支持 `related_files`、`related_snippets`、`project_constraints`、`risk_notes`、`suggested_tools`、`verification_plan`。
- [ ] 让 Context Package 能表达“这次任务为什么这么做”。
- [ ] 明确 Context Package 是任务规划和专家工具输入的共同基础。

### 2.2 本地知识检索

- [ ] 实现 workspace 内容扫描。
- [ ] 能识别常见文件类型。
- [ ] 能提取文本内容。
- [ ] 能生成可用于规划的上下文摘要。
- [ ] 能根据任务关键词召回相关文件或片段。
- [ ] 能识别 README、测试命令、配置文件等高价值上下文。

### 2.3 Context Assembly

- [ ] 根据用户任务组装上下文包。
- [ ] 根据任务类型选择不同的上下文来源。
- [ ] 给复杂任务提供更完整的上下文。
- [ ] 给简单任务提供轻量上下文。
- [ ] 明确上下文包里哪些信息是“建议”，哪些是“约束”。

### 2.4 约束注入

- [ ] 注入项目约束。
- [ ] 注入风险提示。
- [ ] 注入用户偏好或历史行为。
- [ ] 注入建议执行工具和验证计划。
- [ ] 注入专家工具调用前必须携带的必要上下文。

### 完成标准

- 系统不只是“拿到文件列表”，而是能形成可用于 agent 决策的上下文包。
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
- [ ] 结合 Context Package 生成计划。
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

- [ ] 根据 Context Package 组织给 Codex / Claude Code 的输入。
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
- [ ] 识别适合沉淀为 Native Skill 的模式。

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

### 11.2 Docker 运行

- [ ] `docker compose up --build` 可启动服务。
- [ ] API 容器可访问。
- [ ] Qdrant 容器可启动。

### 11.3 运行说明

- [ ] README 写清本地运行方式。
- [ ] README 写清 Docker 运行方式。
- [ ] README 写清默认端口和默认监听地址。

### 完成标准

- 项目能被别人拉下来后快速启动。

---

## 12. 当前阶段交付顺序建议

如果你要真正按顺序做，我建议这样排：

### 第一阶段：先理解

- [ ] Context Package
- [ ] workspace 扫描与上下文摘要
- [ ] 任务意图识别
- [ ] 基础计划生成

### 第二阶段：再执行

- [ ] `/tasks/plan`
- [ ] `/tasks/run`
- [ ] capability 列表
- [ ] 基础 skill / sub agent 入口
- [ ] Expert Tool 调用入口

### 第三阶段：再解释

- [ ] `/traces`
- [ ] `/traces/{trace_id}`
- [ ] verifier 基础记录
- [ ] confirmation 流程
- [ ] skill proposal 生成

### 第四阶段：再演示

- [ ] 文档统一
- [ ] 示例请求齐全
- [ ] demo 路径可讲清楚
- [ ] smoke test 可复现

---

## 13. MVP 完成验收

当以下条件全部满足时，可以认为 MVP 完成：

- [ ] 能启动服务。
- [ ] 能索引 workspace。
- [ ] 能生成 Context Package。
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

