# Linux Backend：Implementation Plan

这份文档保留项目推进节奏，同时尽量把“愿景”和“当前阶段”连起来。
它既是开发路线图，也是模块演化的约束说明。

---

## 总体目标

把一个任务从输入一路推进到：

```text
识别意图 -> 生成计划 -> 选择能力 -> 执行技能 -> 验证结果 -> 记录轨迹
```

最终形成一个可展示、可调试、可迭代的后端闭环。

---

## Week 1：后端基础与 Knowledge Layer

目标：

```text
可以启动后端，可以索引 workspace，可以构造 ContextPackage。
```

任务：

- FastAPI skeleton
- `/health`
- `/workspaces/index`
- SQLite init
- Qdrant init
- KnowledgeObject
- File loaders
- Chunker
- Embedder
- Retriever
- ContextPackage
- KnowledgeContextEngine v0

验收：

```bash
curl http://127.0.0.1:8765/health
```

```bash
curl -X POST http://127.0.0.1:8765/workspaces/index \
  -H "Content-Type: application/json" \
  -d '{"workspace": "/mnt/c/Users/chuan/Documents/NTU"}'
```

### 这一周的重点

- 先把服务跑起来。
- 先把 workspace 当成最小知识单元。
- 先建立上下文构造链路，哪怕是简化版。

---

## Week 2：Main Agent + Capability + Skills

目标：

```text
用户任务 → intent → plan → capability → skill execution。
```

任务：

- Intent Parser
- Planner
- Capability Registry
- Capability Selector
- search_local_knowledge
- summarize_folder
- extract_tasks
- `/tasks/plan`
- `/tasks/run`

### 这一周的重点

- 把“看懂用户在做什么”变成明确步骤。
- 把能力注册和能力选择分开。
- 把技能执行从核心编排里拆出去。

---

## Week 3：SubAgent + Expert Tools

目标：

```text
支持 repo 分析和 expert tool prompt 生成。
```

任务：

- SubAgent
- organize_files
- analyze_repo
- delegate_to_coding_agent
- Claude Code manual mode
- Codex manual mode
- git diff helper

### 这一周的重点

- 把需要更强推理或更高风险的动作单独隔离。
- 让人工介入成为系统设计的一等公民，而不是补丁。
- 让专家工具调用进入受控流程。

---

## Week 4：Verifier + Trace + Skill Evolution Stub

目标：

```text
完成可展示闭环。
```

任务：

- Verifier
- diff_checker
- test_runner
- safety_checker
- Trace Recorder
- `/traces`
- Confirmation API
- Skill Evolution Proposal Stub
- README / Demo polishing

### 这一周的重点

- 所有动作都要能回放、能解释、能追踪。
- 风险动作必须有确认机制。
- 做出“能展示”的同时，也保留“能继续长大”的空间。

---

## 演进原则

- 不要一开始把所有 agent 能力都做成自动化。
- 每个阶段都要能独立验证。
- 先把数据结构、轨迹、确认机制做稳，再扩大执行面。
- 计划文档允许理想化，但实现文档必须与当前代码保持一致。

---

## 和当前实现的对应关系

当前仓库已经具备的内容，主要落在 Week 1 的一部分和 Week 2 的最小闭环：

- FastAPI app bootstrap
- `/health`
- `/workspaces/index`
- SQLite init
- task planning stub
- task run stub
- trace persistence
- confirmation persistence

后续可以继续沿着 Week 1 到 Week 4 的顺序补齐，不需要推倒重来。

