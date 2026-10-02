# 记忆与后台系统开发报告

日期：2026-10-02。范围：完善既有记忆/后台 TODO，保持个人本地应用授权模型，不改写用户指导、不重构无关业务。工作区已有大量其他开发修改，本轮没有提交、清理或覆盖它们。

## 结论与使用边界

后台整理、确定性长期记忆、选择性召回、子 Agent 冻结引用、资源预算和本地管理已有可用实现。不是让模型自由改写一份永久系统提示：原始用户来源、作用域、当前有效性和用户纠正始终控制发布与读取。普通对话不新增一轮必须等待的记忆 LLM。

不能宣称整个路线的生产验收已完成：早期真实模型提取和压缩各两次请求均超时；后续已取得真实压缩与超长推理恢复结果（见本文件 reasoning-exhaustion follow-up），但真实 QA/任务完成率、整轮 p95 和费用比较仍没有生产级评分。TODO 保留这些门槛，不用本地规则准确率替代它们。大型多主题记忆侧写、跨进程 provider 并发协调及任意语义冲突识别也不是本轮已实现能力。

## 设计与实现

### 1. 前台短，后台有界且可恢复

- 完成回答的消息与标识型 outbox 同事务提交。开关首次启用有恢复水位；重启补回漏排任务，不默认追溯全部旧私密会话。
- 默认双 worker；同任务类型/会话单飞，不把一个慢会话变成全系统串行。五秒 debounce，最多八个输入一组，模型最多三条原始用户消息一批。
- 已完成输入有租约保护的持久 checkpoint。半批失败只重新处理未完成输入；同源重复发布不增加版本。发布与 checkpoint 之间仍可能再次推理，不承诺 provider 收费恰好一次。
- 连续压缩请求合并为每会话最新水位；running目标不改写，后继水位持久保存。容量不足或重启后仍可恢复，不按每个turn创建冗余压缩队列。
- 最大1024个已物化待处理任务；溢出原始 ID 持久保存，容量恢复后每次导入最多八个。保留身份避免重新发布，30天后压缩终态 payload，usage ledger 保留90天。原始会话不会因此清除。
- 领取、deadline、lease epoch 与结果发布均有 fence；限流/网络/DB锁可退避重试，鉴权/权限/容量缺失等永久错误不无意义重试。停机遇到不可中断 handler 不悄悄扩建新线程池。

### 2. 不让后台抢走交互资源

统一 LLMService 按 interactive/background_memory/background_io 准入，默认总并发4、前台保留2、两个后台池各1。后台等待时不会占用 FastAPI 事件循环；tokenizer CPU计量也移到线程。

默认单任务32768 token、小时20万、每日100万；预约和实际 usage 存 SQLite。缺 usage 或已发送失败请求保守计量。配置单价才计算费用估算，绝不把未知 usage 写成实际零成本。后台独立路由可配置，默认网络超时30秒，不改变前台原超时。并发限制是本 controller 单进程限制；SQLite共享预算不等于分布式 provider semaphore。

### 3. 记忆不是不加区别地“记住一切”

- 支持中英明确长期偏好、多条/条件性要求；便宜规则优先，含糊信息可选模型提取。模型生成的 explicit 标签不是用户确认。
- 模型候选须匹配原始 USER evidence；批量共享 evidence 不计作多个独立来源。工具、邮件、网页、Agent回答都不直接成为用户长期规则。
- 候选晋升要求独立有效用户证据；撤回/来源失效立即影响读取。秘密/PII不自动长期发布；它们不作为已授权会话后台处理的额外出境审批。
- 部分偏好轴用 slot/polarity/condition 表示冲突，冲突待审不注入。模型候选不能阻断已有确认偏好；明确纠正在前台先撤回，后台再整理新要求。
- 原文明确 `有效至/valid until` 才生成期限；日期按UTC当日结束解释，带时间须有时区。新确认来源可续期，重试或模型推断不能自动续期。
- 不相关普通事实不以“最近”理由塞满prompt；偏好与当前项目决策有界装载。检索目前仍是词法/元数据排序，不能声称已解决同词不同城市等任意语义消歧。
- 重复确认的来源不会无限装入prompt：最多八个代表来源，显式source_count/省略数/续读引用；child按完整序列化引用的保守字节上界计预算，最终请求再以实际tokenizer校验。原始来源仍完整保存。

### 4. 上下文压缩与长期指导

会话 seq/covered_seq、原始 revision 与独立 summary_revision 分工：后台固定旧前缀，连续新消息留在完整 tail，新摘要才使旧发布失效。分段整理目标、决策、约束、更正、未决事项；重要用户要求另留带 trace 的原文锚点。巨大输入用确定性兜底并标记可能有损，原始消息仍可恢复。软阈值后台预排，硬阈值保留原同步压缩。

全局/项目 MEMORY.md 是可解释侧写，不是 AGENTS.md 的替代或自动权限入口；手改hash与数据库CAS阻止覆盖用户改动。未知Markdown不悄悄解析丢弃，过大侧写指向分页API。长 AGENTS.md 前台先加载最新预览、标示index pending，后台重建索引；显式搜索必要时读取最新原文。

### 5. 子 Agent、历史缓存、关注项

Planner显式指定 memory ID/version，服务端查父作用域、来源与项目；ContextDriver把有界引用冻结进 snapshot/agent/tool三个视图。并行child不跟随后台更新；读取失权来源仍拒绝。没有显式引用就不自动继承长期记忆。

压缩水位有持久顺序：旧分段续作不能覆盖前台更新的更大target，即使新target已被物化后重启也不丢头水位；相等target允许继续整理尚未完成的旧前缀。过期pending任务清理后不会卡住全队列。

历史工具 cache 携带 as_of/age/TTL/来源及权限/工作区版本；权限变窄、工作区变化或未知scope不装载。历史结果不计作本轮已完成查询：新邮件到来后相同query会重新检索、读取。任意MCP来源版本无法凭TTL自动证明最新，所以历史cache始终不是current evidence。

Watch在持续授权scope内可读邮件等，更新/暂停使旧occurrence失效。简报区分变化、无变化、未确认、决策，匹配真实来源片段并保留失败源信息。来源片段匹配只证明出处，不证明命题为真。child输出合同由服务端快照约束，无法验证的JSON Schema关键字明确失败，不悄悄忽略。

保留Watch occurrence、邮件游标、Graph checkpoint各自事务；共用LLM准入，不为了“统一”弱化领域恢复合同。既有写工具durable claim：completed复用结果、uncertain拒绝自动重放，普通后台worker不推进审批。

## 真实场景发现的问题及修复

| 场景 | 暴露的问题 | 对应措施/验证 |
| --- | --- | --- |
| 连发多个偏好，中途模型超时 | 已成功前三条被重复推理 | 逐输入checkpoint，重试只发送后三条 |
| 同一邮件query，第二天新增邮件 | 旧观察被算作本轮工具成功 | historical-only，legacy/Graph实际再次search/load |
| 回答过程中后台发布摘要 | raw revision变动使压缩长期无法发布 | 独立summary_revision，新的完整消息留tail |
| 用户改详细/简洁要求，或仅模型猜测相反偏好 | 冲突可能静默覆盖，或候选压制确认要求 | 条件化冲突标记，前台明确纠正，候选不压制确认项 |
| 冲突peer被更正/撤回/过期 | needs_review可能永久卡住仍有效偏好 | 校验有效peer、清理旧内容冲突，候选不因此自动晋升 |
| 连续长对话，新旧分段压缩同时续作 | 每turn积压，或旧水位覆盖新请求 | 有界latest水位合并、running隔离和持久背压 |
| 票务期限过期后再次明确确认 | 内容去重导致新有效期仍过期 | 新确认来源续期事件；模型/同源重试不能续期 |
| 进程在发布后立即退出 | 接管者重复版本或迟到写入 | 原始来源幂等、租约fence、真实进程退出测试 |
| 跨项目搬目录再复用旧路径 | 路径复用误继承项目记忆 | 显式relocation；旧路径重新使用取得不同project ID |
| 超长指导变化、后台索引滞后 | 前台等索引或看到旧摘要 | 最新preview+pending，搜索最新版本 |
| 手改MEMORY.md时后台生成 | 手写内容被覆盖 | hash/CAS冲突，原文件保留，受控单条导入 |
| 很慢handler停机后再启动 | 旧线程未退出而启动第二池 | 保留活线程引用，禁止重叠扩池 |

## 测试与评测证据

集成回归：**460 passed / 174.65秒**，涵盖memory、background、token计量、LLM服务、sessions、instruction files、ContextDriver/child/scheduler、Watch、真实邮件缓存两轮、eval脚本、既有工具claim恢复和RAG工作区删除传播。最后的来源元数据预算修复另跑ContextDriver/child/scheduler/memory_tools/memory_context/live_scenarios，**53 passed / 9.48秒**；数量与前述集成有重叠，不相加作独立测试数。目标代码Ruff与`git diff --check`通过。

完整root turn场景在legacy/Graph各自验证：新会话answer prompt实际包含后台学习的偏好；暂停worker时用户忘记立即抑制；后台模型阻塞时前台另一会话仍完成。此处用确定性测试模型检查装配/并发，不评价真实模型答案智能程度。另有结果未知的非只读工具恢复测试：即使已有用户审批也不再次执行。

| 评测 | 本轮结果 | 解释 |
| --- | --- | --- |
| 68条双语合成提取/真实SQLite发布 | precision=1.00，recall=1.00，错误发布0，遗漏0 | development42/holdout26；并非独立生产语料 |
| 提取器本地耗时 | p50=0.130ms，p95=0.389ms | 仅提取器，不是持久化/召回/整轮耗时 |
| 新会话实际MemoryContextProvider | 6/6，precision/recall=1.00，误注入0 | 无记忆/仅会话摘要的新会话基线召回0；不是QA质量 |
| 本地压缩关键约束回归 | 2/2 | 关键字保留检查，不是语义审判模型 |
| 真实模型强制提取 | 2次均约30秒超时，可评分0 | accuracy/recall及实际费用未知 |
| 真实模型压缩 | 2次均约30秒超时，可评分0 | 没有response/usage，不算通过或零成本 |

可复跑：

```bash
uv run python scripts/eval_personal_assistant_memory.py
uv run python scripts/eval_memory_recall.py
uv run python scripts/eval_background_compaction.py
uv run python scripts/eval_personal_assistant_memory.py --force-model --max-cases 2 --max-calls 2 --timeout-seconds 30
uv run python scripts/eval_background_compaction.py --remote
uv run pytest -q tests/test_context_driver.py tests/test_child_agent.py tests/test_multi_agent_scheduler.py tests/test_memory_tools.py tests/test_memory_context.py tests/test_memory_live_scenarios.py
```

远程命令会使用本地配置并消耗调用；最后一个脚本仅含两条合成样本。宿主环境测试asyncio/thread回调，网络隔离沙箱无法完成同样验证。本轮未重启现有用户后端进程；加载修改需正常重启后端。

## 尚未验收与后续限制

1. 历史provider超时目前未复现，已完成合成压缩及推理耗尽恢复验证；真实回答质量/长期语义保留、无记忆基线、首token与整轮p95、真实计费仍未验收，需要固定路由和真实场景语料继续评测。
2. 多日大型积压/多进程Runtime共享provider并发、跨provider费用和公平性SLO未验收；个人单Runtime限制已实现。
3. 任意语义矛盾、同名实体与RAG新证据自动推翻旧记忆尚未通用实现；不可靠推断保留candidate/历史标记，不自动写永久规则。
   单条超过20,000字符的USER消息不进入自动模型提取，原始会话仍保留；不能声称任意超长用户文本里的偏好都已自动学到。
4. 大型多主题MEMORY.md、自由格式侧写导入、跨项目复制及多工作树共用身份的用户合同未完成；当前项目以明确路径身份和显式搬迁为准，不按目录名称自动合并。
5. 邮件游标提交间真实退出、完整Watch网络故障/DST长期联测及整图审批重启联合压力需要专门验收。已有领域恢复机制保留，不把本轮有限测试写成全面生产保证。

回滚只关闭memory/background开关或停止worker，不删除raw会话/来源/事件。自动记忆不得扩大领域读取权限；用户另行授权的后台关注项可按scope主动读取，无须对模型已见内容逐条再次审批。

## Reasoning-exhaustion recovery follow-up (2026-10-02)

The current endpoint was verified with synthetic data only. A forced 128-token
generation used all 128 completion tokens for reasoning and returned an empty
body with `finish_reason=length` after 1.513 seconds. The new bounded recovery
disabled thinking on the explicitly opted-in endpoint and returned complete JSON
after 1.366 seconds (59 input / 67 output tokens). Total across both calls:
338 tokens. The result preserved the corrected October 21 date, prohibition on
automatic payment, 600-yuan limit and outstanding refund-policy confirmation.
This verifies recovery behavior, not production latency or memory accuracy.

The actual background summarizer then passed both synthetic compaction scenarios
with normal reasoning left enabled: 6,125.73 ms and 5,901.95 ms, two provider calls,
611 input tokens and 2,552 completion tokens total. No recovery was needed with
the new 4,096-token generation allowance. These checks measure retention of named
critical terms (with the existing verbatim constraint anchors), not full semantic
fidelity. Recovery unit tests additionally reject valid JSON with a length finish
reason, cover exactly two calls through batching, and verify local fallback on
generation exhaustion and workload-budget exhaustion. Runtime restart is separate
from this code change; no production backend was restarted here.

Final targeted regression: **194 passed in 40.86 seconds**, covering LLM/config,
recovery, durable jobs, crash/safety recovery, memory integration and asynchronous
compaction. Targeted Ruff checks and `git diff --check` passed. Independent review
found no blockers in the stabilized changes. Existing unrelated lint findings in
the provider adapter were not modified.

## Frontend management follow-up (2026-10-02)

Added local-authenticated configuration GET/PATCH/reset/schema APIs with sparse
SQLite overrides, CAS revisions, active/desired values and explicit restart-only
application. Added exact job status, bounded manual retry, lease-fenced cancellation,
aggregate asynchronous SSE and body-free session compaction status. These are
backend contracts for frontend integration, not a completed frontend UI. Existing
global/project learning policy changes remain immediate. No HTTP self-restart or
external-action replay endpoint was added.

Validation: **142 passed in 37.45 seconds** across frontend configuration/control,
memory/compaction, durable jobs and recovery regressions; targeted Ruff and diff
checks passed. Independent review's cancelled-job CAS note was resolved by
documenting the deliberate no-write idempotent branch; requeued/running jobs still
reject stale control versions. No backend restart or Git commit was performed.
