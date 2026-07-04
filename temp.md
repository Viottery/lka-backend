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

然后再决定是由 Native Skill 处理，还是调用 Claude Code / Codex。

当用户要求整理文件夹时，系统应先检索：

* 文件内容
* 文件类型
* 历史分类规则
* 语义相似文件
* 已提取的任务

然后再生成整理计划。

因此，本项目中的 RAG 服务于整个 Agent 系统，而不是只服务于问答。

---

### 3.4 构建 Agent Harness

本项目不是单纯调用一个大模型，而是构建一个完整的 Agent Harness。

Agent Harness 包括：

* Main Agent Brain
* Knowledge Context Engine
* Capability Registry
* Native Skill Library
* Sub Agent Execution
* Local Tools
* Expert Tools
* Verifier
* Trace Recorder
* Skill Evolution Layer

其核心作用是：

> 把一个无状态的大语言模型包装成一个可以长期执行任务、调用工具、验证结果、记录经验的智能系统。

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

---

### 3.6 支持 Skill Evolution

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

当某类任务多次成功出现时，系统可以尝试将其沉淀为 Native Skill。

例如：

如果用户多次要求“整理入学材料并提取待办”，系统可以逐渐形成一个专门的 skill：

```text
summarize_admission_documents
```

未来再次遇到类似任务时，系统可以优先使用自己的 Native Skill，而不是每次都重新规划或调用外部专家工具。

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
WSL / Docker Local Backend Server
          │
          ├─ Main Agent Brain
          ├─ Knowledge Context Engine
          ├─ Capability Registry
          ├─ Native Skills
          ├─ Local Tools
          ├─ Expert Tools
          ├─ Verifier
          ├─ Trace Recorder
          ├─ Skill Evolution
          ├─ SQLite
          └─ Qdrant
```

其中：

* Linux 后端运行在 WSL / Docker 中，负责所有核心智能逻辑。
* Windows 前端是主要用户入口，负责路径映射、用户交互和 Windows 桌面动作。
* Linux 前端主要用于调试、开发和后端测试。

---

## 5. 核心模块说明

### 5.1 Main Agent Brain

Main Agent Brain 是整个系统的核心控制层。

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

---

### 5.2 Knowledge Context Engine

Knowledge Context Engine 负责从本地知识中构造任务上下文。

它的输出不是简单答案，而是 Context Package。

Context Package 可能包含：

* 相关知识片段
* 相关文件
* 相关代码
* 历史任务
* 用户偏好
* 项目约束
* 风险提示
* 建议工具
* 验证计划

这个模块是本项目区别于普通 RAG 系统的关键。

---

### 5.3 Capability Registry

Capability Registry 统一管理系统中的所有能力。

能力包括：

* Native Skills
* Local Tools
* Expert Tools
* Future MCP Tools

每个能力都有自己的：

* 名称
* 描述
* 输入格式
* 输出格式
* 成本
* 延迟
* 风险等级
* 是否需要用户确认
* 适用场景

Main Agent 通过 Capability Registry 判断当前任务应该调用哪些能力。

---

### 5.4 Native Skill Library

Native Skills 是系统内部可复用的能力。

MVP 阶段可实现：

* search_local_knowledge
* summarize_folder
* extract_tasks
* organize_files
* analyze_repo
* delegate_to_coding_agent

Native Skill 是系统自身能力的体现，也是未来 Skill Evolution 的目标载体。

---

### 5.5 Sub Agents

Sub Agent 是短生命周期的执行单元。

Main Agent 可以把复杂任务拆解成多个子任务，再由 Sub Agent 执行。

Sub Agent 的特点是：

* 不持久化全局状态
* 只处理单一子任务
* 使用受限上下文
* 调用被授权的能力
* 返回结构化结果

MVP 阶段可以先串行执行 Sub Agent，后续再支持并行。

---

### 5.6 Expert Tools

Expert Tools 包括：

* Claude Code
* Codex

它们适用于：

* 大规模代码修改
* 跨文件重构
* 复杂 bug 修复
* 复杂文件处理
* 高难度自动化任务

调用 Expert Tool 前，系统必须构造完整 Context Package。

---

### 5.7 Verifier

Verifier 负责检查任务结果是否可靠。

它可以执行：

* Git diff 检查
* 测试命令运行
* 输出格式检查
* 文件越权检查
* 风险操作确认
* 执行报告生成

Verifier 是系统从 Demo 走向真实可用工具的关键。

---

### 5.8 Trace Recorder

Trace Recorder 负责记录完整执行轨迹。

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

这些轨迹既用于调试，也用于未来 Skill Evolution。

---

### 5.9 Skill Evolution Layer

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

---

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

---

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

---

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

---

### 6.5 复杂代码任务辅助

用户可以提出复杂代码修改需求。

示例：

```text
帮我给这个 repo 增加一个 health check API endpoint，并运行测试。
```

系统应执行：

1. 分析 repo。
2. 检索相关上下文。
3. 构造 Context Package。
4. 判断是否需要 Claude Code / Codex。
5. 生成 expert tool prompt 或调用 CLI。
6. 检查 Git diff。
7. 运行测试。
8. 生成验证报告。

---

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

---

### 6.7 Skill Proposal

当系统发现重复成功模式时，应能生成 Skill Proposal。

示例：

```text
过去 3 次任务都涉及“总结文件夹 + 提取 TODO + 生成 checklist”，建议沉淀为 summarize_and_extract_tasks skill。
```

MVP 阶段不要求自动生成可执行 skill，但要能展示 Skill Evolution 的方向。

---

## 7. MVP 范围

一个月内的 MVP 应聚焦以下功能。

### 必须完成

* WSL / Docker 后端环境
* FastAPI 本地 HTTP API
* Qdrant / SQLite 基础存储
* 文件夹索引
* Knowledge Object
* Context Package
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

---

### 10.2 对工程能力展示的价值

项目可以展示以下能力：

* Agent 系统设计
* RAG 工程化应用
* Context Engineering
* Tool Calling
* 多端架构设计
* 后端 API 设计
* Docker / WSL 部署
* 安全边界设计
* 执行验证机制
* Trace / Memory 设计
* Skill Evolution 思路

这比单纯做一个聊天机器人、RAG 问答系统或 LangChain Demo 更有辨识度。

---

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

