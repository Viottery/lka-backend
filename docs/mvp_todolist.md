# Local Knowledge Agent OS：MVP Todolist

> 目标：通过逐项完成本清单，最终实现一个可运行、可演示、可验证、可继续扩展的 Local Knowledge Agent OS MVP。
>
> 这份清单以“先闭环、再增强”为原则，优先保证最小可用路径完整，再逐步增加上下文、能力、验证与演化能力。

---

## 0. MVP 成功定义

在进入具体任务之前，先明确 MVP 必须达到什么程度。

### MVP 最终应具备的能力

- 能在本地启动 Linux Backend。
- 能通过 HTTP API 接收任务。
- 能索引一个本地 workspace。
- 能根据任务生成可读的计划。
- 能记录 task、trace、confirmation 等核心状态。
- 能提供基础的 capability 列表。
- 能在高风险操作时进入确认流程。
- 能给出可解释、可追踪、可回放的结果。
- 能支持未来扩展到更强的 RAG、技能和专家工具调用。

### MVP 不是必须完成的内容

- 不要求真正的复杂 agent 自主推理闭环。
- 不要求完整向量检索和 embedding 生产级实现。
- 不要求自动修改大量文件。
- 不要求多用户系统。
- 不要求完整 GUI。
- 不要求复杂权限系统。
- 不要求自动 skill 生成。

### MVP 的核心判断标准

如果用户输入一个真实任务，系统能够：

1. 理解任务类型。
2. 生成可读计划。
3. 选择合适能力。
4. 记录执行轨迹。
5. 在高风险时要求确认。
6. 返回稳定、结构化的结果。

那么 MVP 就算成立。

---

## 1. 项目基础与工程约定

### 1.1 仓库结构确认

- [ ] 确认 `app/`、`docs/`、`Dockerfile`、`docker-compose.yml`、`pyproject.toml` 的职责划分清晰。
- [ ] 确认 `docs/` 内文档分层正确：
  - [ ] `project_overview.md` 作为项目总纲
  - [ ] `backend_engineering_guide.md` 作为后端架构说明
  - [ ] `backend_implementation_plan.md` 作为实施路线图
  - [ ] `api_contract.md` 作为接口契约
  - [ ] `mvp_todolist.md` 作为执行清单
- [ ] 确认 `temp.md` 作为源草稿保留，不参与对外说明。

### 1.2 术语统一

- [ ] 统一 `Main Agent Brain` 的定义和中文解释。
- [ ] 统一 `Knowledge Context Engine` 的职责边界。
- [ ] 统一 `Capability Registry`、`Native Skill`、`Expert Tool` 的命名。
- [ ] 统一 `Trace`、`Context Package`、`Verifier` 的文档描述。
- [ ] 统一“计划 / 任务 / 轨迹 / 确认 / 能力”这些核心词汇的用法。

### 1.3 运行目标确认

- [ ] 明确后端默认监听地址为 `127.0.0.1:8765`。
- [ ] 明确 SQLite 作为本地持久化方案。
- [ ] 明确 Qdrant 目前可以作为服务占位，但 MVP 不强依赖其真实检索能力。
- [ ] 明确前端与后端的职责边界。

### 完成标准

- 文档中的术语、结构、职责没有明显冲突。
- 读者能在 5 分钟内理解这个项目做什么、怎么跑、先做什么。

---

## 2. API 与数据模型骨架

### 2.1 请求与响应模型

- [ ] 定义 Health 响应模型。
- [ ] 定义 Workspace Index 请求模型。
- [ ] 定义 Workspace Index 响应模型。
- [ ] 定义 Task Plan 请求模型。
- [ ] 定义 Task Plan 响应模型。
- [ ] 定义 Task Run 请求模型。
- [ ] 定义 Task Run 响应模型。
- [ ] 定义 Task Record 响应模型。
- [ ] 定义 Capability Item 与 Capability List 响应模型。
- [ ] 定义 Trace Record 响应模型。
- [ ] 定义 Confirmation Decision 请求模型。
- [ ] 定义 Confirmation Response 模型。

### 2.2 API 契约对齐

- [ ] 确认 `/health` 返回内容稳定。
- [ ] 确认 `/workspaces/index` 支持 `workspace`、`source_frontend`、`options`。
- [ ] 确认 `/tasks/plan` 返回 `intent`、`plan`、`suggested_capabilities`、`risk`。
- [ ] 确认 `/tasks/run` 返回 `task_id`、`status`、`summary`、`trace_id`、`requires_user_action`、`artifacts`。
- [ ] 确认 `/tasks/{task_id}` 能返回单个任务。
- [ ] 确认 `/capabilities` 返回能力清单。
- [ ] 确认 `/traces` 返回轨迹列表。
- [ ] 确认 `/traces/{trace_id}` 返回完整轨迹。
- [ ] 确认 `/confirmations/{confirmation_id}` 能记录用户决策。

### 2.3 错误约定

- [ ] 未找到 task 时返回 `404 task not found`。
- [ ] 未找到 trace 时返回 `404 trace not found`。
- [ ] 无效请求返回 FastAPI/Pydantic 默认校验错误或统一格式错误。
- [ ] 明确哪些接口未来可能扩展错误码。

### 完成标准

- 所有 MVP API 都有明确的请求/响应结构。
- 前后端可以不看实现直接按契约集成。

---

## 3. 存储层 MVP

### 3.1 SQLite 初始化

- [ ] 在配置的数据目录下创建 SQLite 文件。
- [ ] 在启动时自动初始化数据库表。
- [ ] 确保数据库目录不存在时可自动创建。
- [ ] 确保重复启动不会破坏已有数据。

### 3.2 数据表设计

- [ ] 创建 `workspaces` 表。
- [ ] 创建 `tasks` 表。
- [ ] 创建 `traces` 表。
- [ ] 创建 `confirmations` 表。
- [ ] 确认每张表的主键、必填字段和默认值合理。

### 3.3 持久化行为

- [ ] workspace 索引结果可写入数据库。
- [ ] task 记录可写入数据库。
- [ ] trace 记录可写入数据库。
- [ ] confirmation 决策可写入数据库。
- [ ] 同 ID 冲突时可更新旧记录而非报错。

### 3.4 连接与读取

- [ ] SQLite 连接使用 row factory，便于按字段名读取。
- [ ] 读取 task 时可返回结构化对象。
- [ ] 读取 trace 时可返回结构化对象。
- [ ] 列表接口返回稳定顺序。

### 完成标准

- 后端重启后，历史记录仍然可读。
- 核心对象有稳定的持久化基础。

---

## 4. Runtime 核心闭环

### 4.1 Health 能力

- [ ] 实现 `health()`。
- [ ] 返回 `status`、`version`、`service`。
- [ ] `/health` 路由直接透传 runtime 结果。

### 4.2 Workspace Index

- [ ] 实现 `index_workspace()`。
- [ ] 输入 workspace 路径后可检查目录是否存在。
- [ ] 对存在目录进行文件统计。
- [ ] 生成稳定的 `workspace_id`。
- [ ] 将索引结果写入 SQLite。
- [ ] 返回 `indexed_files` 和 `indexed_chunks`。

### 4.3 Task Planning

- [ ] 实现 `plan_task()`。
- [ ] 至少支持基础意图分类。
- [ ] 至少支持文件整理类任务识别。
- [ ] 至少支持 TODO / 待办提取类任务识别。
- [ ] 至少支持一般任务兜底分类。
- [ ] 输出可读的计划步骤。
- [ ] 输出建议能力列表。
- [ ] 输出风险等级。

### 4.4 Task Run

- [ ] 实现 `run_task()`。
- [ ] 根据任务生成稳定的 `task_id`。
- [ ] 根据任务和计划生成稳定的 `trace_id`。
- [ ] 写入 task 记录。
- [ ] 写入 trace 记录。
- [ ] 返回任务执行结果对象。
- [ ] 在高风险任务时切换到 `waiting_for_confirmation`。

### 4.5 Task / Trace 查询

- [ ] 实现 `get_task()`。
- [ ] 实现 `list_traces()`。
- [ ] 实现 `get_trace()`。
- [ ] 查询不到时返回 `None`，由路由层转为 404。

### 4.6 Confirmation 记录

- [ ] 实现 `confirm()`。
- [ ] 保存 decision。
- [ ] 将状态标记为 resolved。
- [ ] 返回确认结果对象。

### 完成标准

- 单次任务从输入到 task/trace/confirmation 的最小闭环完整。
- 不依赖真实 agent，也能稳定输出结构化结果。

---

## 5. 路由层 MVP

### 5.1 FastAPI App 装配

- [ ] 在 `main.py` 中创建 FastAPI app。
- [ ] 注入 settings。
- [ ] 挂载 runtime 到 `app.state`。
- [ ] 挂载所有路由模块。

### 5.2 路由拆分

- [ ] `health.py`
- [ ] `workspaces.py`
- [ ] `tasks.py`
- [ ] `traces.py`
- [ ] `capabilities.py`
- [ ] `confirmations.py`

### 5.3 路由行为

- [ ] 路由层只做参数接收、错误转换和结果返回。
- [ ] 业务逻辑不写在路由层。
- [ ] 路由命名与文档保持一致。
- [ ] 路由返回类型与 schemas 保持一致。

### 完成标准

- 代码层分层清楚。
- 路由层不会变成难维护的“大函数堆”。

---

## 6. Capability Registry MVP

### 6.1 能力清单

- [ ] 设计一个显式能力列表。
- [ ] 包含至少以下能力：
  - [ ] `summarize_folder`
  - [ ] `extract_tasks`
  - [ ] `organize_files`
  - [ ] `claude_code`
  - [ ] `codex`
- [ ] 每个能力包含类型、风险、是否需要确认。

### 6.2 能力分类

- [ ] 区分 `native_skill`。
- [ ] 区分 `expert_tool`。
- [ ] 为未来 `local_tool`、`mcp_tool` 留出扩展空间。

### 6.3 能力选择规则

- [ ] 文件总结类任务优先匹配 `summarize_folder`。
- [ ] TODO 提取类任务优先匹配 `extract_tasks`。
- [ ] 文件整理类任务优先匹配 `organize_files`。
- [ ] 复杂代码修改类任务保留对 `claude_code` / `codex` 的入口。

### 完成标准

- 用户和系统都能看到“当前可用能力是什么”。
- 任务计划能与能力清单建立对应关系。

---

## 7. 任务规划 MVP

### 7.1 意图识别

- [ ] 识别“文件整理”类任务。
- [ ] 识别“待办提取”类任务。
- [ ] 识别“代码分析”类任务。
- [ ] 识别“复杂修改”类任务。
- [ ] 提供“通用助手”兜底类型。

### 7.2 计划生成

- [ ] 每类任务输出一组清晰步骤。
- [ ] 计划步骤数量保持在可读范围内。
- [ ] 步骤命名保持动词开头、短句表达。
- [ ] 高风险任务计划里要显式体现确认点。

### 7.3 风险识别

- [ ] 识别包含删除、修改、remove、delete 等高风险词汇的任务。
- [ ] 识别复杂文件操作类高风险任务。
- [ ] 风险等级至少分为 `low`、`medium`、`high`。

### 7.4 计划与能力联动

- [ ] plan 返回建议能力。
- [ ] 任务类型与能力选择有明确映射。
- [ ] 计划可以作为后续执行和 trace 的基础。

### 完成标准

- 用户输入任务后，系统能给出“为什么这么做”的计划感。

---

## 8. Trace 与可解释性 MVP

### 8.1 Trace 记录

- [ ] 记录用户原始目标。
- [ ] 记录识别出的 intent。
- [ ] 记录执行计划。
- [ ] 记录上下文摘要。
- [ ] 记录使用的能力。
- [ ] 记录验证结果。
- [ ] 记录成功或失败状态。

### 8.2 Trace 查询

- [ ] 支持 trace 列表。
- [ ] 支持 trace 详情。
- [ ] trace 列表返回精简字段，方便浏览。
- [ ] trace 详情返回完整信息，方便调试。

### 8.3 可解释性要求

- [ ] 每次 task run 后都能找到对应 trace。
- [ ] 每个 trace 都能看出任务是怎么被处理的。
- [ ] 轨迹数据可以用于未来 skill evolution。

### 完成标准

- 任意一次任务执行都不是黑箱。
- 能通过 trace 回答“系统做了什么、为什么这么做”。

---

## 9. 确认机制 MVP

### 9.1 触发条件

- [ ] 高风险任务进入确认流程。
- [ ] 确认流程与 task 状态联动。
- [ ] 确认流程不依赖前端私有逻辑。

### 9.2 API 行为

- [ ] `POST /confirmations/{confirmation_id}` 接收 decision。
- [ ] 支持 approved。
- [ ] 支持 rejected。
- [ ] 决策写入数据库。
- [ ] 状态更新为 resolved。

### 9.3 风险边界

- [ ] 高风险动作不应默认直接执行。
- [ ] 删除或大规模修改类动作必须可拦截。
- [ ] 后续可扩展为真正的 action gating。

### 完成标准

- 系统具备最基础的安全闸门。
- 用户可以明确控制高风险动作。

---

## 10. Workspace Index MVP

### 10.1 输入处理

- [ ] 接收本地 workspace 路径。
- [ ] 允许前端传入来源标识。
- [ ] 接收但不强依赖 options。

### 10.2 索引行为

- [ ] 扫描目录中的文件。
- [ ] 统计文件总数。
- [ ] 估算 chunk 数量。
- [ ] 生成可复用 workspace id。

### 10.3 索引结果

- [ ] 返回索引状态。
- [ ] 返回统计信息。
- [ ] 将结果持久化。

### 完成标准

- 用户能把一个目录交给后端，后端能给出可记录、可查询的索引结果。

---

## 11. 文档与说明 MVP

### 11.1 总览文档

- [ ] `project_overview.md` 保留项目背景、目标、愿景、场景和成功标准。
- [ ] `backend_engineering_guide.md` 保留后端架构、模块说明和技术边界。
- [ ] `backend_implementation_plan.md` 保留阶段推进节奏。
- [ ] `api_contract.md` 保留接口样例与约束。
- [ ] `mvp_todolist.md` 作为执行入口。

### 11.2 README

- [ ] README 能说明项目是什么。
- [ ] README 能说明怎么启动。
- [ ] README 能说明文档结构。
- [ ] README 能让新读者快速找到正确文档。

### 11.3 文档一致性

- [ ] 文档里的接口示例和代码保持一致。
- [ ] 文档里的术语和代码保持一致。
- [ ] 文档里的目标和 MVP 范围保持一致。

### 完成标准

- 新读者能靠文档理解项目。
- 开发者能靠文档知道下一步做什么。

---

## 12. 测试与验证 MVP

### 12.1 基础校验

- [ ] 运行 Python 语法检查。
- [ ] 运行最小启动验证。
- [ ] 确认 `/health` 可访问。

### 12.2 接口验证

- [ ] 验证 `/workspaces/index`。
- [ ] 验证 `/tasks/plan`。
- [ ] 验证 `/tasks/run`。
- [ ] 验证 `/tasks/{task_id}`。
- [ ] 验证 `/capabilities`。
- [ ] 验证 `/traces`。
- [ ] 验证 `/traces/{trace_id}`。
- [ ] 验证 `/confirmations/{confirmation_id}`。

### 12.3 数据验证

- [ ] 确认数据库文件可生成。
- [ ] 确认数据表可创建。
- [ ] 确认任务和轨迹可持久化。
- [ ] 确认重启后数据仍可读。

### 完成标准

- 至少有一条端到端 smoke test 能跑通。
- API、存储、运行时三层都能被基础验证覆盖。

---

## 13. 部署与运行 MVP

### 13.1 本地运行

- [ ] 能用 `uv sync` 安装依赖。
- [ ] 能用 `uv run uvicorn app.api.main:app` 启动服务。
- [ ] `.env.example` 中的配置可直接参考。

### 13.2 Docker 运行

- [ ] `docker compose up --build` 可启动服务。
- [ ] API 容器可访问。
- [ ] Qdrant 容器可启动。

### 13.3 运行说明

- [ ] README 写清本地运行方式。
- [ ] README 写清 Docker 运行方式。
- [ ] README 写清默认端口和默认监听地址。

### 完成标准

- 项目能被别人拉下来后快速启动。

---

## 14. MVP 交付顺序建议

### 第一阶段：能启动

- [ ] 配置
- [ ] FastAPI app
- [ ] `/health`
- [ ] SQLite 初始化
- [ ] README 基础说明

### 第二阶段：能记录

- [ ] `/workspaces/index`
- [ ] `/tasks/plan`
- [ ] `/tasks/run`
- [ ] task 持久化
- [ ] trace 持久化

### 第三阶段：能解释

- [ ] `/tasks/{task_id}`
- [ ] `/traces`
- [ ] `/traces/{trace_id}`
- [ ] capability 列表
- [ ] 更清晰的计划输出

### 第四阶段：能控制风险

- [ ] confirmation API
- [ ] 高风险任务拦截
- [ ] 风险标注

### 第五阶段：能演示

- [ ] 文档统一
- [ ] 示例请求齐全
- [ ] demo 路径可讲清楚
- [ ] smoke test 可复现

---

## 15. MVP 完成验收

当以下条件全部满足时，可以认为 MVP 已完成：

- [ ] 能启动服务。
- [ ] 能索引 workspace。
- [ ] 能规划任务。
- [ ] 能运行任务。
- [ ] 能记录 task。
- [ ] 能记录 trace。
- [ ] 能列出 capabilities。
- [ ] 能处理 confirmation。
- [ ] 能查询 task 和 trace。
- [ ] 文档能完整解释项目目标和运行方式。
- [ ] 至少一条真实任务链路可演示。

---

## 16. 后续演进预留

MVP 完成后，下一阶段可以继续做：

- [ ] 真正的 retrieval / embedding / vector store 接入。
- [ ] 更强的 intent parser。
- [ ] 更细粒度的 capability registry。
- [ ] Native skill 拆分与扩展。
- [ ] Verifier 增强。
- [ ] 复杂代码任务自动化。
- [ ] Skill evolution proposal。
- [ ] Windows frontend 适配。

