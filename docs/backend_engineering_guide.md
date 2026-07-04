# Linux Backend：Engineering Guide

## 1. 项目定位

Linux Backend 是整个 Local Knowledge Agent OS 的核心执行层。它承载 HTTP API、运行时调度、知识上下文构建、能力选择、技能执行、验证与轨迹记录等核心职责。

这个文档既描述当前已实现的后端骨架，也保留项目的中长期愿景，方便后续分阶段落地。

---

## 2. 核心职责

Linux Backend 负责把用户任务从“请求”推进到“可执行计划”，再推进到“受控执行”和“可追踪结果”。

```text
HTTP API
  ↓
Runtime
  ↓
Main Agent Brain
  ↓
Knowledge Context Engine
  ↓
Capability Registry
  ↓
Native Skills / Local Tools / Expert Tools
  ↓
Verifier
  ↓
Trace Recorder
```

当前实现只覆盖其中最小可运行闭环，未来会逐步补齐完整链路。

---

## 3. 推荐目录结构

```text
backend/
  Dockerfile
  docker-compose.yml
  pyproject.toml
  .env.example

  app/
    api/
      main.py
      routes/
        health.py
        workspaces.py
        tasks.py
        traces.py
        capabilities.py
        confirmations.py

    core/
      runtime.py

      brain/
        main_agent.py
        intent.py
        planner.py
        decomposer.py
        orchestrator.py
        risk.py

      agents/
        sub_agent.py
        worker_pool.py

      knowledge/
        object.py
        context_package.py
        context_engine.py
        loaders/
          file_loader.py
          pdf_loader.py
          markdown_loader.py
          code_loader.py
        chunker.py
        embedder.py
        indexer.py
        retriever.py

      capabilities/
        schema.py
        registry.py
        selector.py

      skills/
        base.py
        search_local_knowledge.py
        summarize_folder.py
        extract_tasks.py
        organize_files.py
        analyze_repo.py
        delegate_to_coding_agent.py

      tools/
        filesystem.py
        git.py
        shell.py
        sqlite.py
        claude_code.py
        codex.py

      verifier/
        verifier.py
        diff_checker.py
        test_runner.py
        safety_checker.py

      memory/
        task_history.py
        trace_recorder.py
        skill_memory.py
        skill_evolution.py

      storage/
        db.py
        models.py
        vector_store.py
```

### 说明

- 上面这份结构是“目标结构”，不是当前实现的全部内容。
- 当前仓库只实现了其中很小一部分，但目录规划保留了后续演进路径。
- 这份结构的价值在于：它让后续扩展不会每次都重新发明分层方式。

---

## 4. 核心接口

```python
class LocalKnowledgeAgentRuntime:
    def index_workspace(self, workspace: str, options: dict | None = None) -> dict:
        ...

    def plan_task(self, task: str, workspace: str | None = None) -> dict:
        ...

    def run_task(self, task: str, workspace: str | None = None, frontend: str = "unknown") -> dict:
        ...

    def get_trace(self, trace_id: str) -> dict:
        ...
```

### 设计意图

- `index_workspace`：把 workspace 变成可检索、可统计、可追踪的知识入口。
- `plan_task`：把自然语言任务转成 intent、plan 和 capability 候选。
- `run_task`：执行或编排任务，并产出 task / trace / verification 记录。
- `get_trace`：把一次任务的完整执行过程暴露给前端和调试工具。

---

## 5. 后端模块优先级

### P0

- FastAPI skeleton
- SQLite
- Qdrant
- KnowledgeObject
- ContextPackage
- workspace index
- retrieval
- Capability Registry
- Intent Parser
- 3 个 Native Skills
- Trace Recorder

### P1

- SubAgent abstraction
- organize_files
- analyze_repo
- delegate_to_coding_agent manual mode
- Verifier diff/test
- Confirmation API

### P2

- Claude/Codex CLI 自动调用
- SSE/WebSocket 任务流
- Skill Evolution proposal
- 并行 SubAgent

---

## 6. 当前实现与愿景的关系

当前代码实现的是一个轻量骨架，主要覆盖：

- Health Check
- Workspace index 的基础统计
- Task planning 的规则化版本
- Task / Trace / Confirmation 的持久化
- Capability 列表返回

这意味着：

- 你在愿景里定义的模块没有丢，它们是后续的目标结构。
- 当前实现只是把整体系统先跑起来，避免一开始就陷入复杂度。
- 未来可按 P0 / P1 / P2 逐步替换现有 stub。

---

## 7. 设计原则

- 先有最小闭环，再补智能能力。
- 先把轨迹、状态、确认机制打牢，再做自动化执行。
- 先保留显式能力注册，再考虑自动选择和自动调度。
- 先支持人工可理解的计划，再逐步引入更强的 agent 协作。

