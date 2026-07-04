# Linux Backend：Long-Term Roadmap

这份文档描述的是长期路线图，不是当前执行清单。
它回答的是“这个后端最终要长成什么样”，而不是“下一步先做哪一项”。

当前执行请看 `docs/mvp_todolist.md`。

---

## 1. 目标分层

### 1.1 短期目标

- 保持一个稳定、可启动、可验证的 Linux Backend 骨架。
- 保留最小的 HTTP 服务、workspace 索引和静态能力目录。
- 让文档、接口和代码保持同一套术语。

### 1.2 中期目标

- 让系统从“索引工具”升级为“上下文驱动的 agent runtime”。
- 把本地知识转成 Context Package，而不只是文件列表。
- 让任务理解、计划、执行和验证成为显式阶段。

### 1.3 长期目标

- 让 system 可以选择技能、工具和子代理来完成真实任务。
- 让执行结果可追踪、可验证、可回放。
- 让历史经验能够沉淀为 Skill Proposal，并逐步演化成可复用技能。

---

## 2. 总体演进路径

```text
基础服务
  -> Context Package
  -> Main Agent Brain
  -> Capability Registry
  -> Native Skills / Sub Agents / Expert Tools
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
- 基础文档对齐

验收思路：

```bash
curl http://127.0.0.1:8765/health
```

```bash
curl -X POST http://127.0.0.1:8765/workspaces/index \
  -H "Content-Type: application/json" \
  -d '{"workspace": "/mnt/c/Users/chuan/Documents/NTU"}'
```

---

## 4. 阶段二：知识上下文层

目标：

```text
workspace -> Context Package
```

主要内容：

- `Knowledge Context Engine`
- 文件扫描与类型识别
- 文本提取
- 片段切分
- 本地知识摘要
- 任务相关上下文组装
- 项目约束与风险提示注入

这一层的核心不是“回答问题”，而是把本地知识转换成可以驱动 agent 决策的上下文。

---

## 5. 阶段三：任务理解与规划层

目标：

```text
任务输入 -> intent -> plan -> capability candidates
```

主要内容：

- `Main Agent Brain`
- 任务分类
- 意图识别
- 执行计划生成
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
- `Sub Agents`
- `Expert Tools`
- `local tools`
- `delegate_to_coding_agent`
- `analyze_repo`
- `summarize_folder`
- `extract_tasks`
- `organize_files`
- `Codex / Claude Code` 接入路径

这一层强调受控执行：

- 先选择能力，再执行
- 先构造上下文，再调用专家工具
- 先判断风险，再决定是否需要确认

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
- 任务过程回放
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
- skill 注册与版本管理
- 可回滚、可审计的技能沉淀机制

这一层让系统不仅能“完成任务”，还能够“从任务中长出技能”。

---

## 9. 阶段七：多前端与更多数据源

目标：

```text
Linux Backend 变成统一智能中枢
```

主要内容：

- Windows frontend 适配
- Linux frontend 适配
- 更多本地数据源
- 更完善的路径映射
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

当前仓库已经完成的是阶段一的最小骨架：

- FastAPI app bootstrap
- `/health`
- `/workspaces/index`
- SQLite init
- capability list

后续能力会按照上面的阶段顺序逐步补齐，不再回到之前那条基于规则的 task 路径。
