# Local Knowledge Agent OS：项目总体说明

## 1. 项目名称

**Local Knowledge Agent OS**

副标题：

> 一个以本地知识为核心、面向真实桌面任务的智能 Agent 系统。

英文定位：

> A local knowledge-augmented desktop agent system for real-world task execution and continuous skill evolution.

---

## 2. 项目背景

随着大语言模型和 Agent 技术的发展，越来越多的 AI 工具开始具备调用工具、处理文件、编写代码和执行自动化任务的能力。然而，现有许多 Agent 项目仍然存在明显问题：

1. **过度依赖聊天交互**
   许多系统本质上仍然是聊天机器人，只是额外接入了一些工具，无法真正处理用户桌面环境中的复杂任务。

2. **RAG 只服务于问答**
   传统 RAG 系统通常只用于“基于文档回答问题”，但在真实桌面任务中，本地知识应该服务于规划、决策、工具调用、执行验证和长期记忆。

3. **复杂任务直接外包给专家工具**
   Claude Code、Codex 等工具具备强大的代码修改能力，但如果系统只是简单把任务交给它们，本身就缺少 Agent 的独立决策能力和工程价值。

4. **缺少执行验证与安全边界**
   桌面任务通常涉及文件修改、代码修改、命令执行等风险操作，系统需要具备权限控制、结果验证、用户确认和执行日志。

5. **缺少长期演化能力**
   多数 Agent 每次执行任务都像第一次执行，无法从历史任务中沉淀可复用的能力，也无法逐渐形成自己的技能库。

本项目希望构建一个真正可用的本地桌面 Agent 系统，让 AI 不只是“回答问题”，而是能够理解用户本地知识、规划任务、选择能力、协调工具、验证结果，并从长期使用中逐步演化。

---

## 3. 项目核心目标

Local Knowledge Agent OS 的核心目标是：

> 构建一个能够理解本地知识、协调多种工具、完成真实桌面任务，并从执行经验中持续沉淀能力的 Agent 系统。

具体来说，项目希望实现以下目标：

### 3.1 以本地知识为核心

系统需要能够读取、索引和理解用户本地环境中的知识，包括：

* 本地文件
* PDF / Markdown / TXT 文档
* 代码仓库
* 任务记录
* 历史执行轨迹
* 后续可扩展的数据库、邮件、日历、浏览器记录等数据源

这些信息不只是用于问答，而是作为 Agent 做任务规划、能力选择和结果验证的上下文基础。

本项目默认把本地知识组织为两层：

* 结构化元数据与文件构成索引
* 可选的语义检索层

其中，文件构成索引指的是对 workspace 的目录结构、文件名、扩展名、路径层级、README、测试命令、配置文件和其他高价值元数据进行显式建模，用于提升非向量检索场景下的效率和准确率。

语义检索层则是备用能力，不是默认前提。

---

### 3.2 构建真正有用的桌面助手

系统的目标不是做一个简单 Agent Demo，而是完成真实的桌面任务，例如：

* 总结某个文件夹中的资料
* 根据本地文档提取待办事项
* 整理下载目录或研究资料目录
* 分析代码仓库结构
* 生成项目上下文
* 协助复杂代码修改
* 调用 Claude Code / Codex 等专家工具
* 检查 Git diff
* 运行测试
* 生成执行报告

用户最终感受到的应该是：

> 这是一个可以帮我处理电脑里真实事务的本地 AI 助手。

---

### 3.3 让 RAG 成为 Context Provider

在本项目中，RAG 不应只是一个问答模块。

RAG 的定位是：

> Knowledge Context Provider

也就是说，每次 Agent 执行任务之前，都可以先通过本地知识检索获取相关上下文。

例如：

当用户要求修改代码时，系统应先检索：

* 相关代码文件
* README
* 测试命令
* 历史任务
* 项目约束
* 用户偏好

然后再决定是由 Native Skills 处理，还是调用 Claude Code / Codex。

当用户要求整理文件夹时，系统应先检索：

* 文件内容
* 文件类型
* 历史分类规则
* 语义相似文件
* 已提取的任务

然后再生成整理计划。

因此，本项目中的 RAG 服务于整个 Agent 系统，而不是只服务于问答。

在实现上，本项目不把检索能力简单等同于向量检索。默认检索层应优先使用 workspace 的结构化元数据、文件构成索引和本地文件系统检索，再按需启用语义检索作为补充。

---

### 3.4 构建 Agent Harness

本项目不是单纯调用一个大模型，而是构建一个完整的 Agent Harness。

Agent Harness 包括：

* Main Agent Brain（主代理大脑）
* Knowledge Context Engine
* Capability Registry
* Native Skills
* Sub Agent Execution
* Local Tools
* Expert Tools
* Verifier
* Trace Recorder
* Skill Evolution Layer
* Workspace File Structure Index

其核心作用是：

> 把一个无状态的大语言模型包装成一个可以长期执行任务、调用工具、验证结果、记录经验的智能系统。

Workspace File Structure Index 是知识上下文构造的基础输入之一，负责为非向量检索提供稳定的文件结构和元数据支撑。

### 3.4.1 Tool Package 与懒展开

Capability Registry 对 Main Agent Brain 的第一层暴露不应是很长的工具列表，而应优先暴露粗粒度 Tool Package。

例如第一层只暴露：

```text
mail
workspace
filesystem
code
calendar（后续）
browser（后续）
```

当 Agent 判断当前目标和某个 package 相关时，才展开该 package 中的具体工具。

例如 `mail` package 展开后才暴露：

```text
mail.search
mail.load_messages
mail.sync
mail.match_related（后续）
mail.summarize（后续）
```

例如 `matter` package 展开后才暴露：

```text
matter.create
matter.create_many
matter.search
matter.list
matter.update
matter.link_source
```

当前邮件处理 MVP 暂不注册 `runtime` package。这样可以避免一次性把大量工具 schema
塞进上下文，也让后续 skill / plugin / MCP 能力更容易按领域组织。

### 3.4.2 不维护全局 intent 枚举

本项目不应尝试为所有可能任务维护一个固定的全局 intent 标签表。

Agent 需要输出的是当前步骤可执行的结构化判断，而不是把用户目标强行归入某个预设分类。例如：

```text
用户目标复述
候选 Tool Package
是否需要展开 package
是否需要读取本地数据
是否需要外部 LLM
风险提示
下一步动作
```

也就是说，系统可以有 `RoutingDecision`、`ExecutionDecision`、`Observation`，但不要把 `mail_matter_extraction`、`repo_analysis` 等标签设计成必须覆盖所有任务的核心枚举。

### 3.4.3 Domain Service 与 Agent Loop 边界

邮件、文件、workspace、代码仓库等领域服务不应该自己变成小 agent。

以邮件为例，`MailService` 的职责是：

```text
导入和同步邮件
本地持久化
关键词检索
加载完整邮件正文
同步远程邮件
```

它不负责：

```text
理解用户目标
选择下一步动作
调用 LLM 做推理
决定是否继续执行
组织多轮会话反馈
```

这些职责属于 Agent 的会话与决策-执行-反馈 loop。邮件能力应通过工具包装给 Agent 调用，LLM 推理应发生在 Agent Loop 中。

事务管理也遵循同样边界。`MatterService` 是独立于邮件的数据服务，负责本地事务、任务、
事件和提醒候选项的持久化、检索、状态更新和来源链接。邮件只作为 `source_link` 之一
关联到 matter；matter 不应被建模为邮件内部状态。Agent 如果需要从邮件归纳事务，应先用
mail tools 读取证据，再由通用 Agent Loop 决定是否调用 matter tools 写入或更新事务。
旧的 `mail.persist_matters` / `mail_matters` 只作为历史兼容和迁移对象保留，不再暴露给
Main Agent Brain 的 Tool Registry。

文件访问也分为两层：workspace context 负责文件结构索引、摘要、检索和长期上下文管理；
`filesystem` Tool Package 只处理模型主动要求读写某个明确文本文件的场景。读取工具默认返回
受限片段并携带 full-file sha256；编辑工具使用 `expected_sha256` 和唯一
`old_text -> new_text` 替换，避免基于过期上下文覆盖用户修改。目录 listing、文本搜索和
命令执行不放在 filesystem package 中，后续由 shell/command 工具承担。

实时时间属于 runtime context。后端在每次 Agent turn 的 context window 中注入当前
UTC / 本地时间 / 时区 / 日期，帮助 LLM 处理相对时间。`runtime.now` 暂不注册为
Agent 可见工具。

### 3.4.4 Agent Run Log

每次 Agent Loop 运行都应生成本地、自然语言友好的运行过程 log。

该 log 不由 LLM 生成，而是由后端代码根据执行过程模板化写出。它用于调试、复盘、后续
Verifier 和 Skill Evolution。

run log 至少记录：

```text
运行时间
session_id / run_id
用户输入
可见 Tool Package 列表
展开的 Tool Package
每个 tool 的输入、输出、状态和错误
给 LLM 的完整 system prompt
给 LLM 的完整 user prompt
LLM 的完整输出
最终结构化结果
```

因为 log 可能包含完整邮件正文、本地文件内容和其他个人信息，它默认只写入本地数据目录，不进入 git，不默认上传外部服务。

---

### 3.5 将 Claude Code / Codex 作为普通工具

Claude Code 和 Codex 在本系统中的定位是：

> External Expert Tools

它们只是在复杂代码修改、跨文件重构、复杂文件处理等任务中被调用的专家工具。

它们不是系统的核心，也不是所有任务的默认出口。

系统应该自己判断：

* 是否需要调用 Claude Code / Codex
* 调用前需要提供哪些上下文
* 哪些文件允许修改
* 哪些文件禁止修改
* 需要执行哪些测试
* 如何验证结果

也就是说：

> Agent 负责想清楚问题，专家工具负责高质量执行复杂子任务。

### 3.6 默认运行与存储约定

MVP 默认绑定地址是 `127.0.0.1:8765`，但必须通过配置项覆盖，不应写死。

Backend Core 应支持 Windows 和 Linux 原生 Python 运行。平台差异应集中在
`app/platform/`，包括路径解析、文件系统扫描、后续命令执行和本地工具调用。
WSL 可以作为可选部署或开发方式，但不应成为 Windows 支持的前提。当前项目不再维护
Docker 运行路径。

本地持久化默认使用 SQLite，作为任务、轨迹、确认和 workspace 索引元数据的主存储。

Qdrant 保留为可选的语义检索扩展，不作为 MVP 的必要前提。

### 3.7 Workspace 文件构成索引

Workspace 文件构成索引负责为每个 workspace 建立结构化索引，重点记录：

* 目录层级
* 文件名与扩展名
* 路径位置
* README 与配置文件
* 测试命令和启动信息
* 其他高价值元数据

它的目标不是替代语义检索，而是在不依赖向量检索时仍然提高召回效率、精确度和上下文组织能力。

### 3.8 支持 Skill Evolution

系统需要具备长期演化能力。

每次任务执行后，系统都会记录：

* 用户目标
* 检索到的上下文
* 任务计划
* 调用的能力
* 子任务执行结果
* 工具输出
* 验证结果
* 成功或失败原因

当某类任务多次成功出现时，系统可以尝试将其沉淀为 Native Skills。

例如：

如果用户多次要求“整理入学材料并提取待办”，系统可以逐渐形成一个专门的 skill：

```text
summarize_admission_documents
```

未来再次遇到类似任务时，系统可以优先使用自己的 Native Skills，而不是每次都重新规划或调用外部专家工具。

Skill Evolution 的目标不是让 Agent 直接修改核心代码，而是形成：

* 可审计
* 可测试
* 可确认
* 可回滚

的技能沉淀机制。

---

## 4. 项目整体架构

项目整体采用：

> 后端核心 + 多前端适配

的架构。

推荐运行方式：

```text
Windows Frontend / Linux Frontend
          │
          │ HTTP
          ▼
Native Python Backend Server
          │
          ├─ Main Agent Brain（主代理大脑）
          ├─ Knowledge Context Engine
          ├─ Capability Registry
          ├─ Native Skills
          ├─ Local Tools
          ├─ Expert Tools
          ├─ Verifier
          ├─ Trace Recorder
          ├─ Skill Evolution
          ├─ SQLite
          └─ Optional Semantic Store
```

其中：

* 后端以 Windows / Linux 原生 Python 运行为当前支持路径，WSL 可以作为可选开发环境。
* Windows 前端是主要用户入口，负责路径映射、用户交互和 Windows 桌面动作。
* Linux 前端主要用于调试、开发和后端测试。

### 架构目标

这套架构的目标不是让前端变复杂，而是让后端成为统一的智能中枢：

* 前端负责交互与本地动作。
* 后端负责理解、规划、调度、验证与记录。
* 所有复杂任务都先进入后端，再由后端决定如何分配给能力、工具或子代理。

---

## 5. 核心模块说明

### 5.1 Main Agent Brain（主代理大脑）

Main Agent Brain 是整个系统的核心控制层，负责理解任务、规划执行、调度能力、控制风险和组织验证。

它负责：

* 理解用户任务
* 判断任务类型
* 检索相关上下文
* 制定执行计划
* 拆解复杂任务
* 分配子任务
* 选择能力
* 控制风险
* 验证结果
* 记录执行轨迹
* 触发 Skill Evolution

Main Agent 不应该亲自完成所有事情，而是负责决定：

> 这个任务应该如何完成。

### 5.2 Knowledge Context Engine（知识上下文引擎）

Knowledge Context Engine 负责把本地知识转成任务可用的上下文，不直接给出答案，也不直接替代 Main Agent Brain 的规划职责。

本项目中的上下文不是单一对象，而是一组从粗到细逐步派生的结构。核心原则是：

* 对话层上下文负责持续承接信息。
* 规划层上下文负责解释“为什么这样做”。
* 执行层上下文负责给技能、工具和子代理提供最小必要输入。
* 验证层上下文负责说明结果应该如何被检查。

这一设计参考了现有 agent 系统中的通用做法：把短期会话状态、长期记忆、运行时依赖和 LLM 可见输入分开管理；让子代理或复杂工具调用使用隔离后的上下文视图；把执行过程和上下文选择写入 trace，避免上下文成为不可解释的 prompt 拼接。

它分为两个主要部分：

#### 5.2.1 Background Knowledge Layer（背景知识层）

Background Knowledge Layer 负责整理和维护长期可复用的默认上下文，作为系统在没有明确任务时也可以调用的静态知识底座。

它可以覆盖：

* 个人习惯
* 重要信息
* 常用偏好
* 特化知识场景中的核心内容
* 长期项目约束

#### 5.2.2 Dynamic Context Assembly（动态上下文组装层）

Dynamic Context Assembly 负责根据用户当前输入现场扫描、摘要、筛选和组装任务上下文。

它的输出不是简单答案，而是可以被规划、执行和验证消费的 TaskContext。这里的 TaskContext 承接原 todolist 中 `Context Package` 的目的，但不再为旧名称单独保留具体子类。

#### 5.2.3 Context 层级

MVP 阶段先定义以下上下文层级：

* `BaseContext`：所有上下文对象的公共抽象，统一记录来源、范围、约束、事实、风险、建议能力、验证线索和可追踪元数据。
* `SessionContext`：与对话窗口绑定的会话级上下文，随用户交互和执行反馈持续演化。它可以包含系统提示词摘要、用户偏好、长期记忆引用、当前 workspace、已确认事实、当前目标草稿和最近执行状态。
* `TaskContext`：从 `SessionContext`、workspace 索引、本地知识检索结果和当前目标中派生出的任务级上下文。它承载计划内容与结构，解释当前目标为什么应该这样规划、选择哪些能力、注意哪些风险以及如何验证。它不是 task 的私有字段，也不默认携带 `task_id`。
* `ExecutionContext`：面向 Native Skills、Local Tools、Sub Agents、Expert Tools 和 MCP Tools 的执行上下文，是从 `TaskContext` 裁剪出的最小必要输入。
* `VerificationContext`：面向 Verifier 的验证上下文，重点包含预期结果、改动范围、验证命令、风险点和验收标准。

这些对象可以用父类或协议统一表达，但转换逻辑不应全部塞进父类。推荐使用 `ContextAssembler` 或 `ContextDeriver` 负责从会话态上下文派生出规划、执行和验证所需的具体上下文对象。

#### 5.2.4 TaskContext 结构

TaskContext 至少包含以下字段：

* `context_id`：上下文自身的稳定标识。
* `context_type`：例如 `task`、`execution`、`verification`。
* `lifecycle_status`：例如 `bootstrap`、`gathering`、`ready_for_planning`、`ready_for_execution`、`consumed`、`stale`。
* `session_id`：所属对话窗口或交互会话。
* `workspace_id`：当前关联的 workspace，可为空。
* `goal_summary`：当前目标的结构化摘要，不等同于 task。
* `source_refs`：上下文来源引用，例如 workspace index、用户消息、系统提示词、记忆、trace、文件片段。
* `related_files`：与当前目标相关的文件路径、角色、相关原因和置信度。
* `related_snippets`：与当前目标相关的文本或代码片段、来源位置和引用原因。
* `project_constraints`：项目规则、架构边界、运行约束和用户已确认限制。
* `risk_notes`：风险信号、潜在副作用和是否需要 confirmation 的理由。
* `suggested_tools`：建议使用的能力、工具、子代理或专家工具，以及推荐理由。
* `verification_plan`：建议的检查方式、测试命令、人工验收点和失败处理建议。
* `reasoning_summary`：简短说明“为什么这些上下文足以支持当前规划或执行”。
* `visibility`：说明该上下文是否可给 LLM、工具、子代理或 trace 使用。
* `token_budget`：可选字段，用于控制派生给 LLM 或专家工具的上下文大小。

其中，`related_files`、`related_snippets`、`project_constraints`、`risk_notes`、`suggested_tools` 和 `verification_plan` 是 MVP 必需字段。

#### 5.2.5 生命周期与派生规则

TaskContext 可以伴随对话创建一个空白或默认版本，但初始版本只是任务上下文的草稿容器，不代表已经完成上下文组装。

推荐生命周期：

1. `bootstrap`：对话开始时创建，记录 `session_id`、默认约束、系统提示词摘要、用户偏好引用和空的上下文槽位。
2. `gathering`：用户补充目标、workspace 或限制条件后，系统更新 `SessionContext` 并收集候选上下文来源。
3. `ready_for_planning`：目标已经足够明确，系统从 `SessionContext` 和 workspace 索引派生出 TaskContext。
4. `ready_for_execution`：计划已经形成，系统按具体 step、skill、subagent 或 expert tool 从 TaskContext 裁剪出 ExecutionContext。
5. `consumed`：规划、执行或验证完成后，Trace Recorder 记录使用过的上下文摘要、来源引用和决策理由。
6. `stale`：当用户目标、workspace、风险状态或关键文件发生变化时，旧上下文标记为过期，需要重新派生。

派生 TaskContext 或其子视图的决策依据包括：

* 当前目标是否已经明确到可以规划。
* 消费方是 planner、skill、local tool、subagent、expert tool 还是 verifier。
* workspace、文件范围、风险等级或用户约束是否发生变化。
* 当前上下文是否包含过多无关历史、冗余输出或过期事实。
* 是否需要隔离高噪声操作，例如日志分析、测试输出、全仓搜索或专家工具调用。

TaskContext 的价值在于表达“这次任务为什么这么做”：它不仅列出相关材料，还要说明这些材料如何支持目标理解、计划生成、能力选择、风险控制和验证方案。Main Agent Brain、Expert Tools 和 Verifier 都应消费 TaskContext 或其派生视图，而不是各自重新拼接不透明的 prompt。

这个模块是本项目区别于普通 RAG 系统的关键：它把本地知识组织成可追踪、可裁剪、可验证的 agent 决策输入。

### 5.3 Capability Registry

Capability Registry is the system’s authoritative capability catalog.

It defines what the system can call, how each capability should be described, and what risk and confirmation metadata must be attached before use.

It includes:

* Native Skills
* Local Tools
* Expert Tools
* Future MCP Tools

Each capability should expose:

* name
* description
* input schema
* output schema
* cost
* latency
* risk level
* confirmation requirement
* applicable scenarios

Capability Registry does not execute capabilities. It only registers, describes, and exposes them so Main Agent can choose the right option.

### 5.4 Native Skills

Native Skills are reusable capabilities built into the system itself.

MVP 阶段可实现：

* search_local_knowledge
* summarize_folder
* extract_tasks
* organize_files
* analyze_repo
* delegate_to_coding_agent

Native Skills are the system’s own capabilities and the main target for future Skill Evolution.

### 5.5 Local Tools

Local Tools are execution-oriented helpers that operate directly on the local environment, such as reading files, inspecting directories, running tests, and checking diffs.

They are not the system’s planning layer. They are the hands the system uses to interact with the local machine.

### 5.6 Sub Agents

Sub Agent 是短生命周期的执行单元。

Main Agent 可以把复杂任务拆解成多个子任务，再由 Sub Agent 执行。

Sub Agent 的特点是：

* 不持久化全局状态
* 只处理单一子任务
* 使用受限上下文
* 调用被授权的能力
* 返回结构化结果

MVP 阶段可以先串行执行 Sub Agent，后续再支持并行。

### 5.7 Expert Tools

Expert Tools 包括：

* Claude Code
* Codex

它们适用于：

* 大规模代码修改
* 跨文件重构
* 复杂 bug 修复
* 复杂文件处理
* 高难度自动化任务

调用 Expert Tool 前，系统必须构造完整 TaskContext，并按工具需求裁剪出 ExecutionContext。

### 5.8 MCP Tools

MCP Tools are standardized external tools exposed through the MCP ecosystem.

They are reserved for future expansion and are not required to complete the current MVP baseline.

### 5.9 Verifier

Verifier 负责检查任务结果是否可靠。

它可以执行：

* Git diff 检查
* 测试命令运行
* 输出格式检查
* 文件越权检查
* 风险操作确认
* 执行报告生成

Verifier 是系统从 Demo 走向真实可用工具的关键。

### 5.10 Trace Recorder

Trace Recorder 负责记录完整执行轨迹，并保留支持回放与回退所需的上下文和操作信息。

记录内容包括：

* 用户原始任务
* 识别出的 intent
* 执行计划
* 检索到的上下文
* 调用的 capability
* 子任务结果
* 工具输出
* 验证结果
* 成功或失败状态
* 回退所需的前置状态、操作顺序和结果引用

这些轨迹既用于调试，也用于回放、回退和未来 Skill Evolution。

### 5.11 Skill Evolution Layer

Skill Evolution Layer 负责从历史任务中发现可复用模式。

MVP 阶段可以只实现 Skill Proposal，而不自动生成可执行 skill。

例如：

当系统发现某类任务多次成功执行后，可以生成：

```text
建议将该流程沉淀为新 Native Skill。
```

并输出：

* Skill 名称
* 适用场景
* 输入参数
* 执行步骤
* 验证方式
* 来源 trace

### 5.12 Execution Concepts

#### 5.12.1 Task

Task is the execution object created around a user goal. It stores the original input, state, related workspace, related plan, related trace, and final result.

Task answers the question: what is being done?

#### 5.12.2 Plan

Plan is the structured decision output generated before execution. It describes the intended steps, candidate capabilities, risk level, confirmation points, and verification approach.

Plan answers the question: how should this task be done?

#### 5.12.3 Confirmation

Confirmation is the explicit approval step for risky or user-controlled actions. It records whether execution may continue and captures the decision outcome.

Confirmation answers the question: may this step proceed?

#### 5.12.4 Skill

Skill is a reusable task pattern that can be promoted from repeated successful execution. A skill is more stable than a single task run and is usually surfaced through the capability registry.

Skill answers the question: what reusable method has the system learned?

### 模块之间的关系

这几个模块不是并列堆砌的功能列表，而是一条连续的任务处理链：

1. Main Agent Brain（主代理大脑）先理解任务。
2. Knowledge Context Engine 再构造上下文。
3. Capability Registry 决定可用能力。
4. Plan 描述执行方式，Task 承载任务对象，Confirmation 控制高风险步骤。
5. Native Skills、Local Tools、Expert Tools、Sub Agents 负责执行。
6. Verifier 检查结果。
7. Trace Recorder 留下轨迹并保留回放与回退所需信息。
8. Skill Evolution Layer 从历史中发现模式。

---

## 6. 预期实现效果

MVP 完成后，系统应能达到以下效果。

### 6.1 本地知识索引

用户可以指定一个本地文件夹，系统能够读取并索引其中的文件。

示例：

```text
索引 C:\Users\chuan\Documents\NTU
```

系统应能够识别：

* 文档数量
* 文件类型
* 内容片段
* 可检索知识
* 文件路径

### 6.2 本地知识总结

用户可以要求系统总结某个文件夹或某批资料。

示例：

```text
总结这个文件夹中关于 RAG evaluation 的内容，并引用来源。
```

系统应输出：

* 总结内容
* 相关来源
* 相关文件路径
* 关键信息点

### 6.3 文件夹整理建议

用户可以要求系统整理某个目录。

示例：

```text
帮我整理这个 NTU 入学准备文件夹，提取待办事项，并告诉我哪些材料还缺。
```

系统应输出：

* 文件夹内容摘要
* 文件分类建议
* 待办事项列表
* 缺失材料推测
* 后续行动建议

MVP 阶段只生成建议，不直接移动或删除文件。

### 6.4 代码仓库分析

用户可以要求系统分析一个 repo。

示例：

```text
分析这个 repo 的结构，告诉我主要模块、启动方式和测试命令。
```

系统应输出：

* 项目类型
* 主要目录
* 关键文件
* 依赖信息
* 测试命令
* 可能的入口文件
* 代码结构总结

### 6.5 复杂代码任务辅助

用户可以提出复杂代码修改需求。

示例：

```text
帮我给这个 repo 增加一个 health check API endpoint，并运行测试。
```

系统应执行：

1. 分析 repo。
2. 检索相关上下文。
3. 构造 TaskContext。
4. 判断是否需要 Claude Code / Codex。
5. 生成 expert tool prompt 或调用 CLI。
6. 检查 Git diff。
7. 运行测试。
8. 生成验证报告。

### 6.6 执行轨迹记录

每次任务完成后，系统都应生成 trace。

Trace 应回答：

* 用户要求是什么？
* 系统如何理解任务？
* 检索了哪些上下文？
* 选择了哪些能力？
* 执行了哪些步骤？
* 结果是否通过验证？
* 有哪些文件被影响？
* 后续是否可以沉淀为 skill？

### 6.7 Skill Proposal

当系统发现重复成功模式时，应能生成 Skill Proposal。

示例：

```text
过去 3 次任务都涉及“总结文件夹 + 提取 TODO + 生成 checklist”，建议沉淀为 summarize_and_extract_tasks skill。
```

MVP 阶段不要求自动生成可执行 skill，但要能展示 Skill Evolution 的方向。

### 效果导向说明

这一节描述的不是“代码里有哪些函数”，而是“用户最终会得到什么能力”。
如果后续新增的功能不能服务这些输出，就不应该优先进入 MVP。

---

## 7. MVP 范围

一个月内的 MVP 应聚焦以下功能。

### 必须完成

* Windows / Linux 原生后端环境
* FastAPI 本地 HTTP API
* SQLite 基础存储
* 文件夹索引
* Knowledge Object
* TaskContext
* Basic Retrieval
* Main Agent Runtime
* Capability Registry
* search_local_knowledge
* summarize_folder
* extract_tasks
* analyze_repo
* delegate_to_coding_agent manual mode
* Verifier 基础功能
* Trace Recorder
* Windows / Linux CLI 调用后端

### 尽量完成

* organize_files 建议模式
* Claude Code / Codex CLI 自动调用
* Git diff 检查
* 测试命令执行
* Skill Evolution Proposal
* Streamlit Demo UI

### 暂不实现

* 完全自动 skill 生成
* 复杂 GUI
* 多用户系统
* 企业权限系统
* 全量 MCP 生态
* 本地模型训练
* 自动删除文件
* 自动 git push
* 自动上传本地文件到外部服务

---

## 8. 典型使用场景

### 场景一：本地知识助手

用户：

```text
总结我这个 research 文件夹里关于 Agent Harness 的内容。
```

系统效果：

* 检索相关文档。
* 汇总核心观点。
* 引用来源文件。
* 给出后续阅读建议。

---

### 场景二：入学材料整理助手

用户：

```text
帮我整理 NTU 入学准备材料，提取还没完成的事项。
```

系统效果：

* 扫描文件夹。
* 总结已有材料。
* 提取待办事项。
* 生成 checklist。
* 标出可能缺失的材料。

---

### 场景三：代码仓库助手

用户：

```text
分析这个项目，告诉我怎么启动、怎么测试、主要模块是什么。
```

系统效果：

* 分析目录结构。
* 读取 README。
* 查找配置文件。
* 推断项目框架。
* 总结启动和测试方式。

---

### 场景四：复杂代码修改助手

用户：

```text
帮我增加一个 health check endpoint，并确保测试通过。
```

系统效果：

* 分析 repo。
* 构造上下文。
* 调用 Claude Code / Codex 或生成 prompt。
* 检查修改结果。
* 运行测试。
* 输出报告。

---

## 9. 项目成功标准

MVP 成功的标准不是功能数量，而是是否形成完整闭环。

成功闭环如下：

```text
用户任务
  ↓
本地知识检索
  ↓
任务理解
  ↓
执行计划
  ↓
能力选择
  ↓
工具 / Skill / Sub Agent 执行
  ↓
结果验证
  ↓
执行轨迹记录
  ↓
Skill Evolution Proposal
```

当系统能够稳定完成以上闭环时，即使功能数量不多，也已经具备较高的工程价值和展示价值。

### 判定原则

- 能跑，不等于能用。
- 能用，不等于可解释。
- 可解释，不等于可扩展。
- 只有能稳定完成“用户任务 -> 本地知识检索 -> 任务理解 -> 执行计划 -> 能力选择 -> 执行 -> 验证 -> 轨迹 -> Skill Proposal”的链路，才算达到你要的效果。

---

## 10. 项目价值

### 10.1 对用户的价值

用户得到的是一个能处理真实本地事务的 AI 助手，而不是只能聊天的机器人。

它可以帮助用户：

* 管理本地知识
* 整理文件
* 提取任务
* 分析代码
* 协助开发
* 记录工作过程
* 提升日常生产力

### 10.2 对工程能力展示的价值

项目可以展示以下能力：

* Agent 系统设计
* RAG 工程化应用
* Context Engineering
* Tool Calling
* 多端架构设计
* 后端 API 设计
* Windows / Linux / WSL 部署
* 安全边界设计
* 执行验证机制
* Trace / Memory 设计
* Skill Evolution 思路

这比单纯做一个聊天机器人、RAG 问答系统或 LangChain Demo 更有辨识度。

### 10.3 对未来扩展的价值

该项目后续可以扩展为：

* Windows 桌面助手
* Obsidian 知识助手
* VS Code 项目助手
* 个人研发助手
* 本地 MCP Host
* 企业知识工作台
* Personal Knowledge Operating System

---

## 11. 最终愿景

Local Knowledge Agent OS 的最终愿景是：

> 成为用户本地数字世界的智能入口。

它应该能够理解用户的本地知识、历史任务、工作习惯和项目上下文，并在此基础上帮助用户完成真实任务。

长期来看，它不只是一个工具集合，而是一个能够逐渐积累经验、沉淀技能、适应用户工作方式的个人智能系统。

最终目标：

```text
从 Local Knowledge Assistant
演化为
Personal Knowledge Operating System
```

---

# Project Mission

**Build a desktop agent system that understands local knowledge, safely orchestrates capabilities, and continuously evolves through experience.**
