# Linux 后端日常可用性优化：P1 / P2 设计与开发 TODO

日期：2026-10-05。状态：实施中；实际结果见
[执行记录](linux_usability_progress_2026-10-05.md)，不以本页设计目标冒充测量结果。

用户要求：P0 只作简单检查；逐项设计 P1 / P2，优先改善实际体验，不把 RAG
准确率架构优化当本轮主线。本文件是 `mvp_todolist.md` 的专项实施细目，
不替换现有项目队列，不自动启动其他未完成路线图。

## 1. 范围、依据与不可破坏的约束

依据：

- [项目总纲](project_overview.md)、[工程指南](backend_engineering_guide.md)、
  [长期路线图](backend_implementation_plan.md)、[API 合同](api_contract.md)、
  [当前队列](mvp_todolist.md)。
- [历史案例档案](linux_quality_badcases_2026-10-05.md)、
  [逐轮记录](quality_iteration_2026-10-05.md)、
  [最终报告](linux_quality_final_report_2026-10-05.md)。
- `evals/fixtures/linux_quality_cases_2026-10-05.json` 是历史案例与证据索引，
  不是运行时策略、模型提示词或正确答案来源。

本页初始版本为设计文档；后续实施按执行记录逐项更新，未标完成的条目保持待办。
旧记录中的 `open` 表示明确未闭合；`fixed_offline` 表示已有补丁，首先核验真实
行为，不默认重新实现。新能力、字段和开关均为提案，不代表当前 API 已经支持。

实施必须遵守：

- Core 只编码通用执行、预算、上下文、状态和验证机制；不得加入邮件、网页、
  README 等关键词路由、固定工具序列、站点事实或测试答案。
- 具体能力说明与工作流放在 package metadata、注册工具和专家内部；domain
  service 保持确定性、无 LLM 调用。不引入全局业务 intent 枚举。
- 保留用户要求的灵活 fork；不能以全局禁用规划/子 Agent 获得性能提升。
- 每步最多一个工具或进入回答；未展开包不能直接调用其工具。最终文本仍来自
  独立 `answer` / `context_answer`；完整控制响应才可驱动动作。
- 不删原始结果、完整历史或日志；有损视图必须标省略并提供已有受控读取路径。
  Artifact ID 不可跨 run / child 直接扩权，handoff 使用授权引用或受控副本。
- 新输入投影不改变冻结 ToolView、写操作审查、来源作用域、租约和取消边界。
- 后台整理不加入每次前台响应的同步关键路径。自动记忆不是 AGENTS.md，
  不授予权限；沿用功能开启即授权已持久化会话处理的个人使用策略，不新增逐条审批。
- 不默认增加 planner、答案 reviewer 或语义压缩 LLM；额外调用必须有具体缺口、
  有限次数及实际计费。模型通过现有配置选择，不写死业务模型；不滥用高开销子 agent。
- 保留现有 HTTP / SSE 行为；需要新增公开字段时，采用可选加法字段并同步合同。
  不新增 `/mail/process`，不做前端推送、不实现消息阅读模块的旁支路线图。

## 2. 问题映射、状态与依赖

| 工作项 | 优先级 | 历史问题 / 当前证据 | 本轮工作性质 |
| --- | --- | --- | --- |
| UX-01 降级后的历史结果交付 | P1-1 | `degraded_history_partial_missing`，open | 修复明确缺陷 |
| UX-02 简单任务路径与错误路由 | P1-2 | `catalog_routing_efficiency`，open | 设计轻量入口，配对验证 |
| UX-03 复杂任务协调开销与预算 | P1-3 | `parallel_three_independent_contracts`，open | 分阶段改进执行效率 |
| UX-04 重规划及重启恢复 | P1-4 | `replan_atomic_attempt_recovery`，fixed_offline | 先验收已有原子补丁 |
| UX-05 网页回答可信性与简洁性 | P1-5 | Python / SQLite / 版本查询三项 open | 共用机制，三项分别验收 |
| UX-06 邮件低优先级必需行动 | P1-6 | `mail_low_priority_action_handoff`，fixed_offline | 真实模型分支验收 |
| UX-07 连续摘要与长期后台运行 | P1-7 | `summary_prefix_provenance_inheritance`，fixed_offline；长期缺口 | 先闭环，再压力验证 |
| UX-08 记忆否定、期限、作用域 | P1-8 | `memory_extractive_grounding`，fixed_offline | 正负例真实闭环 |
| UX-09 HTML 正文抽取 | P2-1 | `web_html_readability`，fixed_offline | 代表页面验证，按失败修复 |
| UX-10 长网页续读与 find 恢复 | P2-2 | `web_bounded_paging_find_recovery`，fixed_offline | 分支实际使用验证 |
| UX-11 用户自述被过度核实 | P2-3 | 最终报告的额外体验问题；尚无独立 catalog ID | 新增可追踪案例与来源语义 |

本表覆盖此前 P1 的 10 个 catalog 条目、P2 的 2 个条目及额外 UX-11，
不是把多个历史案例伪装成多个独立实现模块。

依赖与实施顺序：

```text
PRE-00 工作区/基线 + P0 简查
  ├─ UX-01 → UX-04 → UX-03
  ├─ UX-02 ─────────→ UX-03 的轻量执行视图
  ├─ UX-09 → UX-10 → UX-05 的最终真实闭环
  ├─ UX-06（独立邮件工作流验证）
  └─ UX-07 + UX-08 + UX-11 → 长会话个人助理组合验收
```

UX-05 的证据设计可提前开展，不要求先跑完所有 P1 才处理网页工具依赖。
UX-02 / UX-03 / UX-05 都涉及 Agent turn，落地时串行集成，避免多个 agent
同时改同一核心文件。确有独立工作时最多少量低成本子 agent；本轮文档未委派。

## 3. PRE-00：基线与 P0 简查

### 基线

- [ ] 保存 Git HEAD、相关 dirty 文件、实际配置指纹与进程启动信息；不记录密钥。
  区分“工作区源码”和“8765 已加载代码”。最后报告未重启，不推断现在进程状态。
- [ ] 创建隔离 SQLite / workspace，用当前代码运行目标任务；不迁移、清空生产库，
  不擅自重启用户服务。需要实际发布时走已有本地应用生命周期流程。
- [ ] 核对用户并行 workload schema 的 15 / 11 列测试失配。若仍存在，记录为
  独立测试兼容问题，不改用户功能、不放宽验收、不声称全量绿。
- [ ] 将本表映射加入评测选择计划；UX-11 新建 catalog 条目时同步统计快照及测试。
  新增案例只放评测层，运行时不能加载本 TODO 或 gold 作为专项提示。

### P0：仅一个短检查批次

时间盒目标 10–15 分钟；通常不超过 8 个已存在的测试节点 / 定向参数实例。
不遍历安全矩阵，不全量测试，不付费模型复测、不新造 fuzz / sandbox 系统。

历史映射：`bash_readonly_arguments`、`bash_shell_lexing_hooks`、
`tool_discovery_read_truth_table`、`incomplete_control_not_completion`、
`preexecution_rejection_audit`。这五项已离线修复，不标记为仍未修的漏洞。

- [ ] Bash：现有真实 READ executor 的写参数反例和普通读命令各一个；再取
  一个继承 shell hook 同步/后台代表反例，不重跑全参数矩阵。
- [ ] 工具发现：条件只读工具可见，实际写参数仍拒绝；取既有定向入口。
- [ ] 不完整控制：native 无合法控制动作、SSE EOF 无终态各取一个反例。
- [ ] 审计：执行前拒绝无副作用记录、执行后失败保持 unknown 的代表路径。
- [ ] 记录节点、结果和耗时。若发现实际越权写入或假完成，停止相关危险路径，
  先作最小修复；“简单检查”不表示忽略明确破坏性缺陷。其余项继续不受影响的工作。

入口：`tests/test_bash_readonly_argument_quality.py`、
`tests/test_bash_shell_lexing_quality.py`、`tests/test_conditional_readonly_discovery_quality.py`、
`tests/test_decision_protocol_quality.py`、`tests/test_preexecution_tool_audit_quality.py`。
SSE 节点实施前从真实现有测试定位，不在本文件杜撰节点名。

## 4. UX-01：失败 / 降级不能丢失已经得到的结果

问题：release 已有 883 字符 partial，接受 skip 后最终却报“未取得结果”，降级说明消失。
明确是交付遗漏，不是数据删除；当前显式复现 3 RED / 7 GREEN。

设计决策：拆开“当前合同是否完成”和“已经产生什么历史结果”两个视图。
Canonical aggregate 继续判断完成与失败；单独生成只读、经过身份校验的 delivery
view，包含当前结果、历史 partial、接受过的降级说明、缺失义务和受控原件引用。
不能为显示历史而将 SKIPPED 纳入当前成功集，也不能简单信任模型自报旧结果。

- [ ] 保留 `aggregate_task_results` 的状态语义，梳理所有失败回答与 mixed/all-skipped
  recovery 路径；让这些路径共用历史结果收集和显示规则，避免只修一条分支。
- [ ] 从服务端 plan patch history、合法 subtask result events / 已验证历史恢复结果。
  校验 session / parent / plan / correlation / child / step / attempt 归属；同 attempt
  冲突不任选一份。旧 attempt 必须标历史，不覆盖最新合法结果。
- [ ] 缺失判定区分“未执行”“有历史局部发现但本合同未完成”“执行失败无可用输出”。
  明确降级放弃了哪些义务，历史结论仍可能过期或未经核验。
- [ ] 沿用公平分配的 5500 字符交付上限；先保留状态、降级说明、缺口和引用，
  再分配摘要空间。省略历史明确计数，不重新无限注入完整 child 输出。
- [x] 将 `evals/reproductions/skip_degrade_delivery.py` 中已修复的回归纳入默认 tests，
  更新 catalog 引用；不靠 xfail、改断言或永久移出默认收集宣称完成。
- [ ] 检查 HTTP / SSE 与本地最终文本的一致性；新增公开摘要字段才更新 API 合同。

验证：现有 3 红例转绿；保留 foreign / duplicate / forged skip 负例；再验证
混合 current+historical 以及 all-skipped。至多一次定向模型重放验证实际交付。
验收：原 partial 和 note 可见，原业务失败不洗成成功；不再把它说成从未执行。

入口：`app/core/agent_turn.py` 的 `_unresolved_multi_agent_answer`、
`_render_partial_child_results`、`_terminal_plan_recovery`；
`app/core/multi_agent_aggregation.py`、`app/core/multi_agent_scheduler.py`。
依赖：PRE-00。回退：只回退新 delivery view，原结果与 patch history 不删除。

## 5. UX-02：减少简单任务的重复路由与错误探索

问题：读取 README 的简单问答需 7–9 次调用，还误查事项包；目录精简不等于实际提速。

设计决策：用“一次入口动作选择”替代 route 后再做一次等价选择。入口只看到有界
package catalog、当前目标、必要会话上下文与通用动作：展开包、进入独立回答等。
包展开后才出现可执行 tool schema；不把所有 schema 塞进首轮，也不绕过执行器。
默认无需先构建 DAG，复杂任务仍可在普通执行中按需 fork / 进入规划。

- [ ] 先在实际 trace 中按 route / decision / repair / tool / answer 拆分调用与输入
  token，定位重复选择；禁止把总延迟变化全归因目录大小。
- [ ] 在现有 Agent turn / LangGraph 入口设计可回退的 unified-entry 开关。
  复用完整动作协议、ToolView 与懒展开；不建立第二套 Agent Loop。
- [ ] 复用当前 fast-path 的通用上下文充分性 / capability 约束，不启用关键词分类器，
  不新增 README→filesystem 等核心映射。Metadata 改进放在真实包的描述里。
- [ ] 进入 `context_answer` 仍经过独立 answer stage；保留 route 兼容审计，但明确
  `initial_package` 的实际展开来源，不能伪造一次并未发出的 route LLM 调用。
- [ ] 目录按角色 / 阶段投影，保留能力区分说明，剔除尚未使用的执行细则与重复字段。
  目录缓存按 registry / scope / workspace / 配置版本失效，缓存不替用户选工具。
- [ ] 空结果或不相关观察作通用反馈，只描述本次范围和证据缺口；不禁止所有空结果
  重试，也不把跨域升级写成固定流程。相同成功调用仍沿用已有去重与刷新边界。
- [ ] 检查 sync / async / stream 共用入口、取消、格式恢复、已有缓存追问不退化。

验证任务：显式文件问题、无工具追问、需要刷新旧缓存、跨包任务、陌生注册包。
旧 README 案例 + 改名文件 / 改写问法作主要重放，不给模型案例标签。

验收与目标：

- 正确回答、无无关事项查询；同一简单读文件任务目标通常 4–5 次模型调用，
  不是新的硬 step cap。遇到真实缺口允许继续。
- 同配置、同输入、同预算交替配对；先各一次，确需判断波动时最多三对。
  输入 token 中位数下降约 25%、端到端中位数下降约 20%作为暂定目标，不是既有结果。
  小样本只报告样本，不宣称生产 p95 / SLA；未实现目标如实保留。
- 减少一次入口 LLM 不能牺牲答案、越权检查或用户要求的独立执行。

入口：`app/core/agent_turn.py` 的 route、decision 和 catalog projection；
实际 graph 装配、`app/core/multi_agent_fast_path.py`、registry metadata。
依赖：PRE-00。回退：统一入口开关关闭，走原入口，不修改持久化消息语义。

## 6. UX-03：复杂任务把预算用在实际取证上

问题：三独立审计 131.354 秒 / 26 次请求仍失败；多次重复输入耗掉 32768 累计
child token 预算。原预算不是上下文窗口；child run completed 不等于合同完成。

设计决策分两步：先优化已有 coordinator 的控制视图，不同时变更角色和预算；
再按测量结果引入“聚焦执行 / 需要委派”的执行提示，由服务端解析为合法执行策略。
现有 `PlanStep.agent_kind` 是服务端字段，不能让模型通过同名参数直接扩权。

- [ ] 为每个 child 报告实际预算分解：控制提示、任务与证据输入、输出、最终回答
  预留、调用 / 排队耗时；说明下次 dispatch 为什么被预算拒绝，区分非预算故障。
- [ ] 第一阶段保持原三任务、独立性和 32768 ceiling，裁剪 coordinator 重复 schema
  / fork 示例 / 不相关目录；保留当前合法动作，不省略必须执行的 schema。
  有条件地加载完整委派说明时，要有明确可用的发现动作，不能让模型猜隐藏能力。
- [ ] ContextDriver 按任务构造执行工作集：原合同、输入来源、待取证项、授权范围、
  当前少量有效证据。根完整历史、兄弟无关输出与原始大工具结果不全量复制。
  待解决项不能随着普通 observation 淘汰而无提示消失。
- [ ] 用已有 task contract / missing_requirements 汇总取证进展；包可以返回确定性
  覆盖统计，模型理解自然语言义务，core 不判断发布 / 备份等业务关键词。
- [ ] 第二阶段若控制开销仍显著，再提出可选 execution hint 和 schema 版本：
  聚焦节点不重复携带全部规划指引；需要继续委派时反馈父 planner。
  角色变化由服务端策略、深度、预算校验，并重建合法快照 / attempt；不原地修改冻结
  ToolView，不重置已消费费用，不全局禁止 coordinator。旧请求保持原默认。
- [ ] 先用相同有效预算比较轻量视图；若角色预算不同，单列实验，不伪装同配置 A/B。
  保留可执行的回答预留，实际拟合下一次 prompt，不单纯缩小预留或提高 ceiling。
- [ ] 验证独立节点真正重叠：记录 queued/start/end 和 provider 并发；查明是
  scheduler 串行、共享 workload 限流还是模型服务慢，再针对实际瓶颈调整。
  不用无上限 fan-out，保持前台优先和后台让出。
- [ ] 失败时以 UX-01 交付已有发现，以 UX-04 处理有限重规划；空检索须报告来源范围，
  不能靠父级补读、第四个万能 child 或放宽合同把三独立审计算通过。

验证：原三任务完整链路一次 + 同结构换文件内容 / 名称的一次；本地调度探针
确认独立工作可重叠。模型侧服务限流造成的串行单列，不宣称并行提速。
验收：三个原始合同各有自己的实际取证与缺口检查，最后综合结论有支持；
保持原总预算和只读约束。先达到正确完成，再比较耗时 / token，不先设虚假的速度 SLA。

入口：`agent_turn.py`、`context_driver.py`、`multi_agent.py`、scheduler、
`multi_agent_aggregation.py`、现有 workload admission / inference profile 配置。
依赖：UX-01、UX-02、UX-04。回退：关闭新执行投影 / hint，保留原角色与预算。

## 7. UX-04：失败后的重规划与重启恢复可靠闭环

已有补丁：一次有字段反馈的 PlanPatch 修复；history / attempt reservation / journal
原子发表与 CAS。下一步先验证它，不重写持久化编排架构。

- [ ] 使用隔离真实 SQLite 和现有 scheduler，注入一个可恢复的 child 读取失败；
  给 planner 本次身份、失败原因、已得证据和仍缺的合同，不给标准 patch 答案。
- [ ] 单次 schema 修复后仍不合法则明确退出并交付 partial，不能连续八次格式重试。
- [ ] 在提交前、提交后派发前、child 运行中分别用有限故障点验证重启恢复；
  每个 patch 只有一个新 attempt，重复恢复不重复派发 / 发布。
- [ ] 校验重试、缩小范围、替代与 skip 的区分。放弃原合同需显式降级说明；
  旧失败与费用保留，替代成功不能自动证明原合同完成。
- [ ] 验证取消 / 等待审批 / 用户继续与租约并存；取消后不派发替代 child。
- [ ] 若真实验证失败，定位 transaction / checkpoint / scheduler 中的最小缺口；
  不自动重写旧孤立 history，不修改生产旧 journal。

验收：一次模型驱动重规划完成正确新 attempt，定向本地 crash 点原子且幂等，
最终结果与重规划历史一致。SQLite 模拟闭环与真实模型闭环分别记录。
入口：`multi_agent_replan.py`、scheduler、run / checkpoint 存储和现有恢复测试。
依赖：UX-01。回退：已有 journal 格式保持兼容，回退新增反馈不清理历史。

## 8. UX-05：网页答案有据、保留条件、尽量简洁

三个 open 案例独立验收，不能“一套提示词测试绿”就把三个状态全改为通过。

### 8.1 共用设计

把“内容抓取成功”“实际给模型看了多少”“结论被证据支持”分开。
复用 EvidenceRef 与 context_delivery receipts，用有界 evidence working set
表达用户要求的核查项、来源片段、版本/时间、条件和缺口；不是复制全部网页。
模型提取的 claim / condition 仍需核对原文，来源存在不代表语义蕴含成立。

- [ ] 证据工作集附 source_ref / fetch hash / 原文区间 / 抓取时间 / partial，
  保留至少包含限定句的有界相邻段落，而不是只取词语命中行。
- [ ] 以任务提出的核查点维护 coverage；源码只能检查 ID、区间、版本和数量，
  不能靠词法匹配宣布任意结论已经验证。多来源要求按实际独立来源去重。
- [ ] 已取证片段在最终回答拟合后再检查是否仍被交付；丢失则重新选取必要片段或
  明示未知，不因为工具抓到了整页就宣称模型已经阅读全文。
- [ ] 回答路径直接利用证据表的支持 / 未知状态。普通任务不加默认 reviewer。
  只有用户要求多源核查 / 条件对比或存在明确缺口时，才在共享预算内启用一次有界
  语义核对 / 修复；仍不确定就降级表达，不能无限反思或强行补齐事实。
- [ ] 格式与长度使用通用回答合同 / 用户要求，默认优先结论、关键条件、必要来源。
  真正超长时一次有预算的精简或诚实保留完整结果；不能按字符硬截掉条件。
  是否自动修复答案属于新行为，先通过内部开关验证，不能悄悄改变现有 warning-only API。
- [ ] package metadata 表达网页读取 / find 的覆盖边界；core 只消费通用来源和
  完整性，不写 Python、SQLite、uv 事实，不用测试正确答案作提示。

### 8.2 UX-05A：Python 多来源条件

历史案例：`web_python_multisource_conditions`，open。

- [ ] 原任务的版本、默认构建 / opt-in、支持级别、扩展条件各自有官方正文依据。
  同站搜索片段不是第二个独立正文来源，页面数量不代替逐项支持。
- [ ] 一项缺证据时定向找相邻段落，必要时续读；找不到就明确缺口，不借其他版本推断。
- [ ] 原案例 + 新的公开技术条件对比，评审器核对实际运行时材料，不把随时间变化的
  当前版本知识写成永久业务事实。

### 8.3 UX-05B：SQLite 限定条件与简洁性

历史案例：`web_sqlite_condition_brevity`，open。

- [ ] 核对写者、读快照、checkpoint 各自条件；说明 WAL 持续增长时保留持续写入条件。
- [ ] 原案例使用其原始简洁要求单独验收；若旧任务没有数字长度合同，则另加一个
  明确“500 字符以内”的变体，不把新阈值偷偷套到历史测试上。
- [ ] 条件完整、两来源有支持和简洁性三项分别报告；任何一项失败不算整题通过。

### 8.4 UX-05C：版本查询中的未证实因果

历史案例：`web_release_unproved_causes`，open。

- [ ] 版本 / 日期 / 两项变更来自本次正文，并记录 as-of；旧搜索片段只标不一致。
- [ ] 无实际缓存证据不解释成缓存；未请求 / 未观察跳转不声称发生跳转。
  若确有网络行为取证需求，由网页 adapter 返回实际观察 metadata，而非模型猜测。
- [ ] 真实新鲜搜索一次，核对核心事实和额外解释；缓存命中或跳转 metadata 不能
  自动证明搜索服务为何给出旧摘要。保留现有请求与配额限制。

验收：三旧案例各自正确，至少一个换领域的任务不依赖历史答案；引用可定位、
条件不省略、未知不编因果、没有无必要重复查证。只报告真实调用和观察证据。
入口：Agent answer / context delivery、注册 verifier / package metadata、
`app/tool_packages/web.py`、`app/integrations/web_search.py`、网页评测脚本。
依赖：UX-09 / UX-10 是最终闭环的工具基础。回退：保留证据 metadata，关闭额外修复调用。

## 9. UX-06：邮件必需行动不能因优先级低而消失

已有补丁：`MailExpertExecutor._review_summary` 先保留 action_required，
超预算披露 missing_requirements。中优先级现场成功不能证明低优先级分支。

- [ ] 构造通知、高优先级但无行动、低优先级需行动、明确已取消行动的邮件快照。
  需要模型从正文判断，不直接把 action_required 标准标签作为模型输入。
- [ ] 同时检查专家原始提取、bounded handoff、父最终答复，定位是识别错误还是交付遗漏。
  优先级与是否必需行动分别判断；纯通知不能凭截止日期被强行当待办。
- [ ] 先合成真实模型重放；再从已授权真实隔离快照挑有适配内容的一项验证。
  真实数据不含低优先级行动时如实说明，不能给无关样本贴 gold 标签。
- [ ] 人为缩小交付预算，确保 total / shown / omitted 一致，父答案不能说“全部已整理”。
  超出 handoff 的原件通过已有 child 受控结果路径查看，不直接跨 run 读 artifact。
- [ ] 仅失败时改专家摘要 / 识别 schema / 工作流；不在 core 增加邮件特判，
  不自动创建事项、不发邮件、不修改生产邮箱。

验收：明确低优先级必需行动出现在父结果，未完成项可见；通知不变待办，
取消项不复活。记录低优先级分支实际模型输出，而不只记录本地函数测试。
入口：`app/experts/mail.py`、专家工具与真实任务评测。依赖：PRE-00。
回退：保留已有先行动后通知的排序，回退新增行为不删提取产物。

## 10. UX-07：连续摘要和后台处理长期可靠、前台不等待

问题：旧 summary 的合法 trace1 在第二次压缩被误拒，只接纳当前 chunk 的 trace2，
导致 model→local_fallback。来源继承补丁已离线通过，尚无新现场闭环。

设计：继续使用 durable outbox、水位、summary+raw tail 同快照、CAS / lease。
已有前台主动压缩入口保留；正常整理仍由后台完成，硬溢出使用有界紧急视图并标降级，
不临时在前台等待后台 LLM。Fallback 是可用性降级，不伪装 model publication 成功。

- [ ] 重放原 medium probe，至少连续三次有效 model publication：旧摘要+新消息，
  验证继承来源只来自服务端同会话、已覆盖序列，不信正文自报 ID。
- [ ] 在每次摘要后追问旧事实、新事实，再更正日期 / 否定旧决定，检查实际 prompt
  交付；来源 membership 合法不等于摘要语义正确，人工单独核对保真。
- [ ] 超出 64 IDs / 4096 bytes 的 metadata 上限明确截断 / incomplete，保留原始
  历史与合法续作策略；不能为了 model 连续通过而无限增大 metadata 或关闭校验。
- [ ] 先本地 replay 长历史到实际 65536 历史预算附近及超过预算，保留生产输入目标
  与模型容量区分；测试中文长文本、工具噪声、连续纠正，不仅缩小预算触发压缩。
- [ ] 用真实 SQLite 做队列满、运行中追加、worker 重启、租约过期、取消的少量定向
  场景；pending watermark 能自动续作，不依赖用户再发一条消息来唤醒。
- [ ] 做一次本地 60 分钟持续负载，记录 pending 水位、队列积压、SQLite 等待、
  event-loop lag、进程内存与前台 p50/p95；后台关闭 / 开启相同输入比较。
  假 provider 的结果只证明调度，不代表真实模型长期稳定。
- [ ] 真实模型验证至少三次发布，同时有前台文件任务 / 问答。延迟目标：相对同配置
  前台基线中位数不增加超过约 15%，无同步摘要等待；初步目标不承诺跨机器 SLA。
- [ ] 背景摘要失败有 reason、重试耗尽与 fallback 方式；不把 model 格式失败、
  provenance 拒绝、provider timeout 都归为一个“摘要失败”。

验收：原 model→fallback 问题闭合；新事实 / 更正保真；取消后旧 worker 不发表；
长历史和满队列最终追上合法水位，前台不被语义压缩阻塞。未知或仍降级保持可见。
入口：`sessions.py`、`memory_background.py`、`background_jobs.py`、
`background_llm.py`、workload admission；context-status / background health。
依赖：PRE-00；组合体验验收结合 UX-08 / UX-11。
回退：保留旧合法摘要和 raw tail，只停止新发布，不删除原文、水位或 pending 工作。

## 11. UX-08：记忆准确保留否定、临时要求和项目范围

设计：模型产生原文支撑的候选；服务端验证真实用户来源、子句和作用域，
保留 claim / evidence / scope / 时间限制 / 显式纠正。低置信只作候选，不为提升
recall 放宽原文支撑；也不能把所有有条件要求都拒绝。使用已有发布与撤回机制。

- [ ] 从现有正负例选择少量真实模型场景：长期偏好、当前任务例外、项目专属、
  否定要求、重复来源、更正撤回、引用别人说的话；不同 scope 用独立项目验证。
- [ ] 同时报告候选识别率、原文支撑、scope / 极性 / 时间正确性、错误晋升率，
  不把“全部弃答”当高准确率，不把文本改写造成的 label mismatch 当全语义失败。
- [ ] 原始完整子句回查保留否定与条件；无明确日期不捏造永久期限，无法确定长期性
  时留当前会话上下文或 candidate。已有普通候选重复来源政策保持，不强制逐条审批。
- [ ] 下一轮在相同 / 不同项目提问，检查实际注入与回答；候选存在不等于可用记忆，
  相同来源重试不晋升，项目 A 不影响项目 B，临时要求不永久覆盖长期偏好。
- [ ] 否定 / 纠正 / 撤回与晚到 worker 一起验证。先验收现有补丁，只按具体漏判
  补最小规则，不发展成全语言 entailment 系统、不启动模型训练或 Laya 实验。
- [ ] 对确实无法通过词法判断的复杂候选，仅在后台、预算允许时语义核验；
  未确定保留候选，不能阻塞当前回答。新验证方式须与普通发布审计一致。

验收：明确负例零错误长期晋升，长期正例实际可用，临时 / 项目条件完整；
用户纠正生效且原记忆不被晚结果复活。原文字面得分和人工语义结果分别保留。
入口：`memory_extraction.py`、`memory_background.py`、memory domain / context、
已有 `tests/test_memory_candidate_grounding_quality.py` 与模型记忆评测。
依赖：PRE-00。回退：暂停新学习，保留已确认条目和审计，不删除历史候选。

## 12. UX-09：真实 HTML 正文抽取足够可读

已有 main/article 优先、隐藏状态、链接保持补丁，先验收实际网页，不扩建浏览器。

- [ ] 用 4–6 类小型固定 HTML 检查结构：main/article、文档正文、普通 body、
  导航/表单密集、隐藏 void 标签、无可读正文。复用原 fixture，不重造大抓取评测。
- [ ] 抽查两篇代表性真实技术网页，保存本地响应与抽取文本；输出带 title、URL、
  fetch 时间、hash、大小与截断标记。私有页面 / 登录态不在本轮 scope。
- [ ] 对比原正文关键限定段落、标题和链接是否保留；不能为去噪删除重要例外，
  也不能优先 main 后静默丢失多个主内容块。
- [ ] 仅对实际失败修复结构抽取 / fallback；没有正文或需要 JavaScript 时明确限制，
  不假装已渲染，也不因为“可用性”加入 headless 浏览器及新服务依赖。
- [ ] 输出仍遵守现有网络地址、超时、字节和配额边界；不借抽取优化放宽安全限制。

验收：固定关键段落完整、导航与隐藏内容不污染、链接大小写不损坏；两真实页面
可用于后续条件核查。动态网页仍单列能力限制，不视为自动支持。
入口：`app/integrations/web_search.py`、`tests/test_web_readability.py`、web tools。
依赖：PRE-00。回退：恢复可见 body fallback，原响应本地保留，不伪造完整性。

## 13. UX-10：模型实际使用长网页续读与零匹配恢复

已有 offset/hash 分页、去重窗口和零匹配恢复补丁，关注“模型用没用、取证是否足够”。

- [ ] 构造固定 >20k 字符页面，关键条件在中后段；通过测试 HTTP adapter 注入
  标准网页工具，而不是仅调用分页函数。测试中允许固定本地传输 fixture，不能为了
  localhost 测试关闭生产 SSRF 边界。
- [ ] 工具层精确核对 Unicode offset / returned_chars / next_offset、hash 变更拒绝，
  引用真实范围；find 的 match offset 与正文字符 offset 不混用。
- [ ] 真实模型在标准工具目录下完成“定位并解释尾部条件”，trace 必须实际调用续读；
  完整正文不预先赠送给模型，最终 answer receipts 也包含必要条件片段。
- [ ] 零字面匹配场景要求模型利用有界 recovery preview 定位相关表述或明确未知；
  零命中不能回答“主题不存在”。偏移耗尽与根本零命中分别验证。
- [ ] 核对去重后多窗口合计覆盖，不因第一页面太短又丢后面窗口；页间 hash 改变
  不拼成一份所谓完整文档，重新取样或明确来源已变化。
- [ ] 不追求无条件全文读完；按具体证据缺口续读，目标是一项任务内不重复扫描
  同一范围。若大量反复 refetch 是实测瓶颈，再评估 run-scoped snapshot cache，
  不先增加跨 run 缓存、隐式扩权或长期保存整网内容。

验收：正文尾部、find 零命中、窗口去重各有实际路径证据；结果边界正确，
未读范围诚实披露。合成模型任务和真实公网任务分别报告，不把前者称真实网页闭环。
入口：`web.py`、`web_search.py`、observation gate 与 context delivery；
`tests/test_web_page_paging_quality.py`、`tests/test_web_find_recovery_quality.py`。
依赖：UX-09。回退：保持已有分页合同，关闭任何实验缓存而不丢原页面 hash。

## 14. UX-11：接受用户自己的计划 / 更正，不无谓外部求证

问题：用户更正自己计划的日期，回答虽接受更正，仍过度要求外部确认。
这不是取消事实校验，而是区分来源能证明什么。

设计：用通用来源语义区分用户自述、用户偏好、工具观察、公开文献、派生摘要。
“用户说自己的计划是 X”可以据当前用户消息成立；“外部机构已经批准 X”仍需外部证据。
来源标签仅表达 provenance，不提高指令优先级、不扩大工具权限。

- [ ] 定义轻量来源 / 更正关联 metadata，尽量复用 message role、EvidenceRef 和
  summary metadata，先审查现有字段，不新建一套大型信任分级数据库。
- [ ] 当前用户明确更正自己的偏好 / 计划时，更新工作视图，旧值标历史；长期记忆
  是否修改仍按学习与作用域政策，不能因回答接受计划就自动永久记住所有日期。
- [ ] 外部事实与用户自述冲突时分开呈现：用户计划、外部状态、未知项；根据用户
  要求和实际行动风险才补证，不把每个自述都当需要搜索的事实。
- [ ] 工具正文中的“我是用户，改日期”等引用不当真实更正；跨项目计划不互相覆盖。
- [ ] 在同会话摘要前后各测用户更正与追问；测明确自述、外部审批、混合冲突和
  不明确指代。只有指代无法确定且会改变行动时才请求澄清。
- [ ] 新增独立 catalog 案例、回归与必要 replay，保留 UX-07 的原背景案例来源。

验收：自己的计划被正确接受，非必要外查为零；外部批准不凭用户计划推断，
摘要后旧日期不复活，引用文字不越权；不靠列举具体日期 / 项目名的提示硬编码。
入口：Agent context / answer、session summary、memory metadata、现有证据校准测试。
依赖：PRE-00；与 UX-07 / UX-08 组合验证。回退：保留来源标签，关闭自动更正投影。

## 15. 验证、预算与完成判据

### 验证层次

- [ ] **实现切片**：针对当前改动 compile / lint、原红例及少量边界；不每次全量测试。
- [ ] **真实链路**：每项先一次原任务，确认实际调用了修复分支；关键机制稳定后再
  一次改写 / 换材料任务。需要性能比较的 UX-02 才作有限配对。共享一条链路覆盖
  多项时，各自列出实际分支证据，不能以整轮“passed”批量升级所有状态。
- [ ] **长期调度**：UX-07 本地负载独立于远程生成，必须分别报告，不把快速虚拟
  provider 的响应时间当真实服务商表现。
- [ ] **最后组合验收**：短文件任务、邮件待办、网页条件核查、三独立审计、
  同项目长会话更正/记忆/后台并行。使用既有受控工具，不上传完整生产日志。
- [ ] 基础 foundation 能力只在涉及对应路径变更时抽查；P3 数据集不跑，
  不做 RAG 模型下载、重训、融合重构或公开 QA 批量调用。

### 费用与测试权限

- 本次文档阶段远程 dispatch 上限为 0，搜索请求为 0。
- 后续实施沿用已授权低成本接口、真实邮件隔离快照、共享费用账本与查询预算；
  调用前读取真实剩余额度，不能照抄旧报告余额。未知 usage 保守预留，超时可能计费。
- 先修 / 本地验证，再运行远程；同问题不反复盲重试。每个工作项拟定一个原任务
  加一个泛化任务的调用 / token / wall cap；达 cap 后记录 partial，不自动扩大额度。
- 不给新模型临时升级授权。高开销模型需要单独授权；绝不让子 agent 绕过共享账本。
- 真实邮件只读已批准快照与当前获准接口；不 sync/send，不修改真实邮件。
  邮件、正文、完整 prompt、SQLite 与原始 trace 留在 gitignored 私有运行目录。

### 必须记录的指标与完成证据

每项最小报告：场景 / 原缺陷 → 原因 → 改动 → 原任务与泛化验证 → 效果 → 仍有缺口。

- 正确性：合同满足情况、错误 / 漏项、证据支持、partial、实际副作用。
- 效率：端到端耗时、答案首 token、各 stage 耗时、实际 provider 调用、修复次数、
  输入 / 输出 / 缓存 token、工具调用、费用及未知预留；工具开始事件不算答案首 token。
- 并行 / 后台：实际执行区间重叠、排队等待、积压水位、event-loop lag 与取消发表情况。
- 来源：HEAD + dirty 源码指纹、配置 / fixture hash、时间、实际模型、原始本地报告路径。

完成状态分开：`designed` → `implemented` → `offline_verified` → `live_verified`。
只运行离线检查不能标 live；非触发分支、字面 facts 命中、child completed、合法引用
均不能替代业务验收。历史全部 seen，不把改写前的旧题重新称为盲测。

每个实现切片独立 Git 管理，精确暂存本任务文件；保留当前用户 dirty 工作，
不整库 add / 提交、不改历史、不自动 push。公开文档只写脱敏结果与可复核指标。

## 16. 本轮不实施的低优先级事项

- `public_retrieval_all_hop_regression`：多跳融合 / embedding / 候选池优化。
- `public_qa_relation_reasoning`：公开 QA 关系推理、弃答与评分分解的进一步迭代。
- `benchmark_query_prefix_profile`：已有 prefix 接线的同题数据集重测。

上述保留原状态；不把本轮上下文 / 执行改善宣传成 RAG 数据集准确率提升。
若遇到正文静默丢失、权限错误、缓存无法读取等影响日常任务的实际缺陷，则按
交付可靠性修复，不因它涉及 knowledge 工具就自动归入 P3。
