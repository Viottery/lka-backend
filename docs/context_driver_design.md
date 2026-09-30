# ContextDriver 设计说明

## 1. 定义

ContextDriver（Context Deriver / Context Assembly Service）是位于 Planner、Scheduler 和 Child Agent 之间的确定性系统组件。它把系统拥有的全部上下文转换为当前 Agent 被允许、需要且能够理解的最小上下文。

```text
Parent Context + PlanStep + Dependencies + Policy + Budget
                              ↓
                        ContextDriver
                              ↓
                   immutable ContextSnapshot
                              ↓
                         Child Agent
```

它不是业务 Agent，不负责拆解任务，也不是简单的 prompt 拼接器。

## 2. 职责边界

```text
Planner：决定任务需要什么
Policy：决定哪些内容和能力被允许
ContextDriver：生成当前 Agent 实际看到的上下文
Scheduler：决定何时生成和使用上下文
Child Agent：在 Snapshot 范围内执行
Tool Executor：对每次工具调用再次校验
Verifier：检查执行结果是否可信
```

RAG 负责检索候选 evidence；ContextDriver 负责从授权候选中选择、压缩和绑定适合当前任务的 evidence。
实现中 `EvidenceRef.source_ref` 是供引用的显示标识，`source_id` 才是权限求交标识，
不能混用。父 run 的 knowledge 工具结果只提供候选引用；fork 时重新按当前
source/account scope 加载 chunk、执行隐私过滤，再按子 Agent scope 与 token 预算分配。
旧工具结果中的正文不会直接作为子 Agent 的权限依据或长期缓存。

## 3. 确定性核心与可选 LLM

ContextDriver 的核心必须由确定性代码实现，负责：

- 解析 PlanStep 和依赖。
- 计算 parent scope 与 child scope 的交集。
- 校验 workspace、source、account 和路径范围。
- 执行敏感内容、远程出境和 `untrusted_data` 策略。
- 应用 token、时间、调用次数和检索轮数预算。
- 选择已授权的 artifact / evidence。
- 生成 AgentView、ToolView 和 AuditView。
- 冻结 Snapshot 并写入审计记录。

可以选择性使用 LLM 做候选集合内的相关性排序、长 artifact 摘要、重复结果压缩、evidence coverage 判断和追加上下文建议。

LLM 输出只能作为 recommendation：

```text
LLM 推荐候选
    ∩ parent scope
    ∩ session scope
    ∩ policy scope
    ∩ budget
    ↓
最终 ContextSnapshot
```

LLM 不能决定工具权限、文件路径、source scope、预算、并发、用户确认或数据出境。LLM 不可用时，系统回退到引用加载、规则过滤、摘要缓存和预算裁剪。

## 4. 输入与派生流水线

输入至少包括：

```text
ParentContext
PlanStep
DependencyResults
Policy / AccessScope
RuntimeBudget
Freshness / Version Information
```

```text
接收 ContextRequest
  ↓
读取 ParentContext 和 PlanStep
  ↓
检查依赖是否完成、有效且未过期
  ↓
计算 effective scope
  ↓
收集已授权的 context candidates
  ↓
选择相关 artifact / evidence
  ↓
处理敏感内容和 untrusted data
  ↓
按 budget 进行 context packing
  ↓
生成 AgentView / ToolView / AuditView
  ↓
冻结 immutable ContextSnapshot
```

权限使用交集计算：

```text
effective_scope =
    planner_requested_scope
    ∩ parent_scope
    ∩ session_scope
    ∩ workspace_scope
    ∩ policy_scope
```

典型 `PlanStep`：

```json
{
  "step_id": "step_code_analysis",
  "objective": "分析 app/core 的职责边界",
  "allowed_packages": ["filesystem", "knowledge"],
  "input_refs": ["workspace_snapshot_001", "doc_backend_guide"],
  "output_contract": "architecture_report",
  "verification_criteria": ["列出核心模块", "每个结论包含文件引用"]
}
```

## 5. ContextSnapshot

Snapshot 至少绑定以下信息：

```text
snapshot_id / parent_snapshot_id
run_id / plan_id / step_id
goal / objective / output_contract
input_refs / dependency_result_refs / evidence_refs
allowed_packages / allowed_tools / allowed_scopes
side_effect_level / verification_criteria
budget
policy_version / workspace_version / permission_version
created_at / expires_at
```

Snapshot 必须不可变。权限、workspace、PlanStep 或 evidence 变化时，创建新 Snapshot 和新 Child Run，不覆盖旧 Snapshot。

大段正文优先通过引用传递：

```text
Reference View：引用 + 摘要 + source refs
Working View：引用 + 相关片段 + 结构化事实
Full View：完整正文或工具结果
```

只有任务确实需要全文、内容已授权且预算允许时，才生成 Full View。

## 6. 依赖结果、生命周期与缓存

下游 Agent 不应默认接收上游 Agent 的完整自然语言回答，而应接收结构化 `TaskResult`、`Artifact` 和 `EvidenceRef`。ContextDriver 根据当前 PlanStep 决定传递引用、摘要、相关片段还是完整原文，不自动继承所有上游结果。

调用时机：

```text
Plan 创建：保存引用和候选输入
Child Run ready：使用最新依赖和权限生成 Snapshot
Child Agent 执行：只使用冻结 Snapshot
依赖完成 / re-plan / 权限变化 / evidence 过期：创建新 Snapshot
```

Context cache 至少绑定：

```text
session_id / workspace_scope / source_scope / topic_id
policy_version / permission_version / evidence_version
plan_step_objective
```

workspace、权限、policy、source、topic、PlanStep 或 evidence 变化时必须失效。

## 7. 安全、预算与失败状态

- retrieved content 必须标记为 `untrusted_data`，不能覆盖 system policy。
- evidence 不能产生工具授权、改变 package policy 或扩大 source scope。
- Tool Executor 必须根据 ToolView 再次校验真实路径、参数和副作用。
- 敏感内容执行 `allow`、`redact`、`summarize`、`confirm` 或 `deny`。
- 远程 LLM 的最终 prompt 仍经过 Egress Gateway。
- Snapshot 元数据、过滤决策和版本写入 trace。

ContextDriver 应返回结构化状态：

```text
ready
blocked_dependency
scope_denied
policy_denied
budget_exceeded
evidence_stale
context_insufficient
generation_failed
```

ContextDriver 不私自创建业务任务，而是把缺口报告给 Scheduler / Planner。Planner 决定等待、重试、缩小范围、创建替代任务、降级完成或请求用户确认。

## 8. 建议接口与第一版范围

当前第一版确定性核心位于 `app/core/context_driver.py`。它接收已由
Scheduler/Runtime 装载的 `PlanStep`、依赖结果、权限 scope、预算和版本信息，
只负责裁剪与冻结，不自行查询业务存储或执行工具。结果显式区分
`ready`、`blocked_dependency`、`scope_denied` 和 `budget_exceeded`；真正的
Plan/ParentContext loader、ToolView 校验和审计持久化仍由后续阶段接入。

```python
class ContextDriver(Protocol):
    async def derive(
        self,
        request: ContextRequest,
    ) -> ContextDerivationResult:
        ...
```

内部边界：

```python
async def derive(request):
    task = await load_plan_step(request.step_id)
    dependencies = await load_dependency_results(request)
    policy = await resolve_effective_policy(task, request)
    candidates = await collect_authorized_candidates(task, dependencies, policy)
    selected = await select_relevant_context(candidates, task, policy)
    packed = await pack_with_budget(selected, task.budget)
    views = build_context_views(task, policy, packed)
    return await freeze_snapshot(task, policy, views)
```

第一版优先实现：

1. 根据 PlanStep 生成 Child Context。
2. 绑定 allowed packages、tools 和 workspace paths。
3. 注入任务目标、依赖 artifact 引用和验收标准。
4. 执行基本 token budget 和敏感内容过滤。
5. 生成不可变 ContextSnapshot。
6. 让 Tool Executor 根据 Snapshot 做权限检查。
7. 将 Snapshot、过滤结果和版本写入 trace。
8. 支持 `ready`、`blocked_dependency`、`scope_denied` 和 `budget_exceeded`。

复杂的 LLM context selection、自动压缩、Context Delta、跨进程 Context Store 和智能缓存可以后续增加，但不能破坏确定性权限和 Snapshot 语义。
