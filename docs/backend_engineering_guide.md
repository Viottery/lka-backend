# Backend Core：Engineering Guide

## 1. 项目定位

Backend Core 是整个 Local Knowledge Agent OS 的核心执行层。它承载 HTTP API、
运行时调度、知识上下文构建、能力选择、技能执行、验证与轨迹记录等核心职责。

Backend Core 目标是支持 Windows 和 Linux 原生 Python 运行。Docker/WSL 可以作为
可选运行方式，但不应成为 Windows 支持的前提。

这个文档既描述当前已实现的后端骨架，也保留项目的中长期愿景，方便后续分阶段落地。

---

## 2. 核心职责

Backend Core 目前只负责五个基础动作：

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

当前阶段先把服务、索引和能力目录做稳，不再保留基于规则的任务规划或执行闭环。

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

`MailService` 只提供确定性的存储、查询、加载和持久化方法，不调用 LLM，不选择执行步骤。
LLM 推理、工具选择、观察工具结果和反馈循环属于 Agent Loop。

每次 Agent Loop 运行都必须生成本地自然语言友好的 run log。run log 由代码模板生成，
不调用 LLM，至少记录：

- 运行时间、`run_id`、`session_id` 和用户输入。
- 第一层 Tool Package catalog 和实际展开的 package。
- 每个 tool 的选择时间、输入、输出、状态和错误。
- 给 LLM 的完整 system prompt、user prompt 和 LLM 完整输出。
- 最终结构化结果。

这些日志默认写入 `data/agent_logs/`。由于日志可能包含完整邮件正文和个人信息，
它们必须保持本地持久化，不应进入 git，也不应默认同步到外部服务。

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
        capabilities.py

    core/
      runtime.py
      context.py
      retrieval.py
      runtime_loop.py
      tools.py
      tracing.py

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
- `app/platform/` 是跨平台边界，用于集中处理路径、文件系统和后续命令执行差异。

### 存储与检索约定

- SQLite 是当前 MVP 的主存储方案，用于 workspace 索引元数据、任务记录、trace、confirmation 和其他结构化状态。
- Qdrant 只作为可选语义检索扩展，不作为 MVP 必需项。
- 非向量检索应优先依赖 workspace 的结构化索引、文件构成索引和本地文件系统检索。
- Workspace 路径必须先经过 `PathResolver`，文件扫描必须优先经过 `FilesystemScanner`。
- 后续本地命令、专家工具、测试命令应经过统一 `CommandRunner`，避免业务代码写死 shell。
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

当前代码实现的是一个轻量骨架，主要覆盖：

- Health Check
- Workspace index 的基础统计
- Capability 列表返回
- Runtime Debug 结构化链路
- SQLite trace / runtime event 持久化
- 平台识别、workspace 路径解析和只读文件扫描

这意味着：

- 你在愿景里定义的模块没有丢，它们是后续的目标结构。
- 当前实现只是把最基础的 HTTP 服务和索引先跑起来，避免一开始就陷入复杂度。
- 未来可按 P0 / P1 / P2 逐步增加真正的知识层和能力层。

---

## 7. 设计原则

- 先有最小闭环，再补智能能力。
- 先把服务、索引、能力目录打牢，再做更复杂的 agent 协作。
- 先保留显式能力注册，再考虑自动选择和自动调度。
