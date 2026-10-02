# Backend Core：Long-Term Roadmap

这份文档描述的是长期路线图，不是当前执行清单。
它回答的是“这个后端最终要长成什么样”，而不是“下一步先做哪一项”。

当前执行请看 `docs/mvp_todolist.md`。

---

## 1. 目标分层

### 1.1 短期目标

- 保持一个稳定、可启动、可验证的 Backend Core 骨架。
- 保留最小的 HTTP 服务、workspace 索引和静态能力目录。
- 让文档、接口和代码保持同一套术语。
- 支持 Windows/Linux 原生 Python 运行，WSL 仅作为可选路径；当前项目不再维护 Docker 运行路径。

### 1.2 中期目标

- 让系统从“索引工具”升级为“上下文驱动的 agent runtime”。
- 把本地知识转成 TaskContext，而不只是文件列表。
- 让任务理解、计划、执行和验证成为显式阶段。

### 1.3 长期目标

- 让 system 可以选择技能、工具和子代理来完成真实任务。
- 让执行结果可追踪、可验证、可回放。
- 让历史经验能够沉淀为 Skill Proposal，并逐步演化成可复用技能。

---

## 2. 总体演进路径

```text
基础服务
  -> TaskContext
  -> Main Agent Brain（主代理大脑）
  -> Capability Registry
  -> Native Skills / Local Tools / Sub Agents / Expert Tools / MCP Tools
  -> Verifier / Trace Recorder
  -> Skill Evolution
  -> 多前端与更多数据源
```

这条路径强调的是“先基础，后智能；先显式，后自动；先可验证，后扩展”。

---

## 3. 阶段一：基础服务层

目标：

```text
服务可启动，workspace 可索引，能力目录可查看。
```

主要内容：

- FastAPI app bootstrap
- `/health`
- `/workspaces/index`
- SQLite 初始化
- 静态 capability catalog
- 平台识别
- workspace 路径解析
- 跨平台只读文件扫描
- 基础文档对齐

验收思路：

```bash
curl http://127.0.0.1:8765/health
```

```bash
curl -X POST http://127.0.0.1:8765/workspaces/index \
  -H "Content-Type: application/json" \
  -d '{"workspace": "/home/chuan/Documents/NTU", "source_frontend": "linux-native"}'
```

Windows PowerShell 验收思路：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/workspaces/index" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"workspace":"C:/Users/chuan/Documents/NTU","source_frontend":"windows-native"}'
```

---

## 4. 阶段二：知识上下文层

目标：

```text
SessionContext + workspace -> TaskContext -> ExecutionContext / VerificationContext
```

主要内容：

- `Knowledge Context Engine（知识上下文引擎）`
- `Background Knowledge Layer（背景知识层）`
- `Dynamic Context Assembly（动态上下文组装层）`
- `BaseContext`
- `SessionContext`
- `TaskContext`
- `ExecutionContext`
- `VerificationContext`
- `ContextAssembler / ContextDeriver`
- 文件扫描与类型识别
- 文本提取
- 片段切分
- 本地知识摘要
- 默认上下文整理
- 任务相关上下文组装
- 项目约束与风险提示注入

这一层的核心不是“回答问题”，而是把对话状态、本地知识、项目约束、用户偏好和执行反馈转换成可以驱动 agent 决策的上下文。

TaskContext 承接原 todolist 中 `Context Package` 的目的，不再为旧名称单独保留具体子类。它不应被建模为 task 的私有对象，而应从会话级 `SessionContext` 中按目标、阶段和消费方派生，并能继续裁剪为执行和验证所需的上下文视图。

MVP 阶段的 TaskContext 至少需要表达：

- `related_files`
- `related_snippets`
- `project_constraints`
- `risk_notes`
- `suggested_tools`
- `verification_plan`
- 当前目标为什么需要这些上下文
- 这些上下文如何支撑计划、专家工具输入和 trace 解释

---

## 5. 阶段三：任务理解与规划层

目标：

```text
任务输入 -> intent -> plan -> capability candidates
```

主要内容：

- `Main Agent Brain（主代理大脑）`
- `Task`
- 任务分类
- 意图识别
- 执行计划生成
- `Plan`
- 风险识别
- 候选能力选择

这一层需要把“用户想做什么”拆成系统可以执行和验证的步骤。

---

## 6. 阶段四：能力与执行层

目标：

```text
plan -> skills / sub agents / expert tools
```

主要内容：

- `Capability Registry`
- `Native Skills`
- `Local Tools`
- `Sub Agents`
- `Expert Tools`
- `MCP Tools`
- `delegate_to_coding_agent`
- `analyze_repo`
- `summarize_folder`
- `extract_tasks`
- `organize_files`
- `Codex / Claude Code` 接入路径

Capability Registry is the authoritative catalog for available capabilities. It records what can be called, what metadata is attached to each capability, and whether confirmation is required before use. It does not execute capabilities itself.

Capability Registry 的第一层暴露单位应优先是 Tool Package，而不是所有具体工具。
Agent 先根据用户目标和上下文决定是否展开某个 package，再看到其中的工具 schema。
这能避免工具列表过长，也为后续 skill、plugin 和 MCP 能力扩展保留清晰边界。

本项目不维护覆盖所有任务类型的全局 intent 枚举。Agent 应输出面向下一步动作的
`RoutingDecision`、`ExecutionDecision` 和 `Observation`，而不是强制把每个用户目标映射到固定标签。

这一层强调受控执行：

- 先选择能力，再执行
- 先构造上下文，再调用专家工具
- 先判断风险，再决定是否需要确认
- 领域服务只提供确定性数据能力；LLM 推理和决策-执行-反馈 loop 属于 Agent Harness

---

## 7. 阶段五：验证与可解释层

目标：

```text
执行结果 -> verifier -> trace
```

主要内容：

- `Verifier`
- diff 检查
- test runner
- safety checker
- `Trace Recorder`
- `Confirmation`
- 任务过程回放
- 任务回退支持
- 执行结果摘要
- 失败原因记录

这一层保证系统不是黑箱，后续每一次执行都可以复查。

---

## 8. 阶段六：Skill Evolution

目标：

```text
trace -> pattern -> skill proposal
```

主要内容：

- 重复成功模式识别
- Skill Proposal
- Skill Draft / Scaffold
- `Skill`
- skill 注册与版本管理
- 可回滚、可审计的技能沉淀机制

这一层让系统不仅能“完成任务”，还能够“从任务中长出技能”。

---

## 9. 阶段七：多前端与更多数据源

目标：

```text
Backend Core 变成统一智能中枢
```

主要内容：

- Windows frontend 适配
- Linux frontend 适配
- Windows native backend 适配
- Linux native backend 适配
- 更多本地数据源
- 更完善的路径映射
- 更完善的命令执行抽象
- 未来的 MCP / local tool 扩展

---

## 10. 演进原则

- 先基础，后智能。
- 先显式，后自动。
- 先可验证，后扩展。
- 先单点可用，后系统协作。
- 每个阶段都要能独立验收。
- 长期路线图可以理想化，但当前实现必须与现实代码一致。

---

## 11. 与当前实现的关系

以下记录最初阶段一的交付基线，并非当前实现总量：

- FastAPI app bootstrap
- `/health`
- `/workspaces/index`
- `/capabilities`
- `/runtime/debug`
- SQLite 初始化
- 平台识别、路径解析和只读 workspace 文件扫描

当前仓库已继续实现 Agent、受控工具/子任务、记忆与后台任务等后续能力，具体状态见
[当前模块与运行流程](current_module_flows.md)。未来取舍仍以 `mvp_todolist.md` 的当前队列为准，
不因本页的阶段编号自动推进功能。
