# 记忆系统与后台任务系统开发 TODO

2026-10-06 更新：自然跨轮记忆、默认 LLM 提取和清晰长期信息直接发布已实现并加载到
Windows 运行实例。最新合同、测试与限制见 [本轮记录](contextual_memory_2026-10-06.md)；
下文早期“模型候选仅待审/默认关闭”的描述是历史阶段要求，不覆盖本轮用户确认的新策略。

本清单用于将现有会话上下文、全局与项目指导文件、RAG/领域数据、多 Agent 快照和后台调度连接成可长期使用的个人助理记忆系统。目标是：用户无需反复要求“记住”，系统也能在后续任务中准确使用稳定偏好和项目决策；耗时整理在后台运行，不增加常规对话的 LLM 等待；任何自动记忆均可解释、纠正和撤回。

本文件是**待实施路线**，不是已实现能力声明；当前执行队列仍以 [MVP TODO](mvp_todolist.md) 为准。执行时按依赖逐阶段推进，完成一个条目须同时提交实现、测试、文档和可复现的验收证据。不要把本清单当作自动授权去重构无关模块。

实施进度（2026-10-01）：持久队列、会话预压缩与硬阈值兜底、确定性记忆存储、保守后台提取、作用域召回、可编辑 `MEMORY.md` 侧写、读取工具和本地用户控制已形成最小纵向闭环，详见 [实现说明](memory_background_implementation.md)。本清单多数条目还包含未完成的生产验收子要求（真实 token 预算、完整隐私/来源策略、多 Agent 显式引用、真实模型评测与发布门槛），因此未把部分完成的条目勾选为已完成。

前轮续做：后台提取/预压缩可等待异步 LLM 服务；根 Agent 的记忆召回合并近期项与带作用域的全文搜索，不再只受最近 30 条限制；队列提供不暴露 payload 的健康聚合与本地只读接口。对应记忆测试 71 个通过。当时尚未实现真实 token 计量与 `131_072` 总 prompt 预算，所需模型容量随后在 2026-10-02 核验，见下文。

2026-10-02 最终续做：新增后台资源准入/持久预算、双 worker、最新有序压缩水位与背压、逐输入重试进度、冲突/期限生命周期、冻结 child 记忆、历史缓存失效和真实邮件两轮回归。集成回归460 passed；之后来源元数据预算的集中回归及细节见 [开发报告](memory_background_development_report.md)。纯合成记忆评测更新为68个场景（development42、holdout26），按真实会话组共享状态，其余相互隔离；离线precision/recall 1.00，错误发布/遗漏均为零。实际本地summarizer 2/2，跨会话召回6/6。holdout已用于开发复核，不应称为盲测。真实提取与压缩各两次有界请求均约30秒超时，无可评分答案及usage；真实模型质量、计费和整轮延迟不能由上述离线结果推断。

2026-10-02 续做：已用当前 PackyAPI 路由接受 148,738 个合成 input tokens 的请求；其 `/models` 未提供容量元数据，故当前本地配置采用 148,000 的已验证保守容量，而非声称中转支持官方全部 1M。官方 V4.1 tokenizer 已安装在被 Git 忽略的本地 `data/tokenizers/` 并在该模型配置中引用；8,000 字合成中文样本的本地计数 14,040，provider usage 为 14,062，额外保留 4,096 安全余量。AgentTurn 发送前以独立的 131,072 input-token 目标装配；未配置 tokenizer 时使用偏保守的 UTF-8 字节上界，未知 OpenAI 兼容模型在配置容量前拒绝发送。该变更不扩大 65,536 会话历史预算，也不代表本清单的真实模型、多 Agent 和发布验收全部完成。

2026-10-02 本轮续做：会话窗口的前台计数现按每轮实际选择的模型 tokenizer 隔离，不再只使用默认模型；本地自动提取支持低风险、直接陈述的长期偏好，并对引用、外部转述、秘密和高影响动作保持拒绝。已确认的重复候选可晋升，明确撤回的条目仍不可自动恢复。后台队列补充 deadline、租约保护和 provider 暂时/永久故障分类；远程提取遇到限流不发布半成品而交由队列重试。新增五例合成离线发布烟测，但明确不把它当作真实模型的生产验收。其余未勾选条目仍需逐项验收，尤其是资源预算、多 Agent 显式记忆引用、隐私边界和真实数据的质量/时延门槛。

个人本地应用的后台会话处理授权决定：启用记忆/后台功能即允许通用记忆提取与压缩处理已持久化的会话内容及派生摘要，无须对同一会话内容逐段再审批；`allow_remote_extraction` 只决定是否额外消耗模型调用。此项授权不自动赋予通用记忆任务读取知识库、工作区、邮件等领域源的能力；用户另行创建并授权的关注项或其他后台任务可在其持久化 scope 内主动调用相应只读工具。秘密/PII 检查用于阻止自动发布长期记忆，不再作为整条会话能否送往已配置模型的前置门槛。领域数据的访问权限与外部文本不可信原则仍独立有效。

相关约束：[个人知识与记忆规划](personal_knowledge_memory_plan.md)、[指导文件机制](agent_instruction_files.md)、[ContextDriver 设计](context_driver_design.md)、[后端工程指南](backend_engineering_guide.md)、[API 契约](api_contract.md)、[多 Agent 路线](multi_agent_todolist.md)。

## 1. 不可破坏的边界与完成定义

- [x] 区分五类数据：用户拥有的 `AGENTS.md` 指导、会话工作记忆、自动学习的长期记忆、邮件/知识库/事项等领域事实、不可变原始会话与运行记录。自动记忆不得直接改写 `AGENTS.md`，也不能授予工具、路径、账户或外部发送权限。
- [ ] 跨 workspace、完整 ContextDriver scope 交集、多工作树及子 Agent end-to-end 的全局/项目隔离仍待验收；显式 relocation API 的已测能力记录于阶段三。
- [x] 记忆提取对引用/转述外部指令 fail closed，source revoke 会撤回派生条目（`tests/test_memory_extraction.py`、`tests/test_memory_service.py`）。
- [ ] 邮件/RAG 各类原始来源校验及其派生索引清理仍待专项验收。
- [x] 未完成的后台任务不能使前台忘记最新对话；后台失败不阻断普通回答。保持现有主动压缩为硬上限兜底，不把所有整理改为必须等后台完成。
- [x] 本项目历史预算 `65_536` 是会话窗口预算，不是整个 prompt 的上限。此前提出的整体组装目标 `131_072` 真实输入 token 应单独建模；新增空间优先给系统功能信息、指导、当前任务证据和按需记忆，**不自动翻倍历史预算**。模型实际窗口、输出预留及安全余量必须优先于目标值。
- [x] 任何持久化 schema、公开 API、隐私出境策略或跨 workspace 权限变化，先评审合同和兼容性；关闭功能开关时现有会话、Agent run、Watch 和邮件接口仍可正常工作。
- [x] 每阶段完成前检查最终 diff、执行对应自动化测试、记录结果和剩余限制；未经测试或只写了设计的条目保持未勾选。

## 2. 阶段零：测量现状并冻结合同

依赖：无。产出：可复现基线、数据流图、配置与迁移合同。建议先改文档和测试，再动运行时。

### 2.1 前台耗时与上下文基线

- [ ] 分别记录 route、decision、answer、`context_summarize` 的 prompt/input/output token、耗时、是否命中缓存、压缩前后历史长度；统计 p50/p95，区分普通 turn、长对话、工具密集、多 Agent turn。不得把完整 prompt 放入普通 API 响应或 session metadata。
- [ ] 核实每个模型/客户端的真实 context window、最大输出、token 计数方式和请求超限行为；为未知兼容模型定义保守配置与失败提示。将现有“字符数÷4”仅视为粗估，特别测量中文、JSON、工具 schema 和 Unicode 的误差。
- [x] 建立逐阶段 prompt 预算合同：硬性安全/系统信息、用户与项目指导、当前任务、会话历史、工具定义与观察、长期记忆、输出预留。预算以真实 token 为单位；未使用的分区可借用，但不可挤掉权限与输出预留。记录裁剪原因、保留来源和可续读句柄。
- [ ] 确认会话消息、run terminal event、LangGraph checkpoint、Watch occurrence 和邮件同步游标的提交顺序；明确新后台事件应以哪个持久化提交点为触发，不以 SSE 断连或前端接收作为成功信号。

测试与验收：

- [ ] 用固定中英混合、长 `AGENTS.md`、多工具 schema 和长工具结果 fixture，对比本地估算、provider usage、实际超限；报告最大低估倍率，并验证 prompt 预算不会超出所选模型安全输入容量。
- [ ] 在同一批典型任务上记录未启用后台记忆时的答案质量、首 token、整轮耗时、压缩触发率和 token 成本，保存脚本与匿名化结果，作为后续回归基线。
- [x] 提交 schema/权限/保留策略设计评审记录：数据删除与“忘记”的关系、全局/项目作用域、用户指导和自动记忆冲突优先级、远程 LLM 隐私边界均有明确决定。

## 3. 阶段一：可恢复的后台任务底座

依赖：阶段零合同。首批只承接记忆和压缩任务；不同时重写 Watch、邮件同步或交互式 Agent 调度。建议在 `app/storage/db.py` 增 schema，在 `app/core/` 放任务调度与 worker；领域服务仍保持确定性、不直接调用 LLM。

### 3.1 持久化任务与领取

- [x] Durable `background_jobs` 与稳定 ID payload、领取/租约/deadline/fencing、重试/取消和 payload-free health 已实现并有单测（`tests/test_background_jobs.py`）；有界 claims/backpressure 配置为 queue max 1024、每轮 drain 8。
- [x] 两个真实进程争抢同一任务、提交前/入队后/工作中/发布后直接退出、接管后的旧租约拒写及 SQLite backup 恢复已验证（`tests/test_background_crash_recovery.py`）；80条输入分为10个任务、背压重启恢复及慢 handler 停机后不扩池已有测试。
- [ ] 更大规模、多日持续压力和生产环境异常停机仍需实测，不能由有限故障注入推断。
- [x] 提供事务内 enqueue/outbox：与产生它的会话消息或领域状态同事务提交；重复 terminal event、HTTP 重试和进程重启只产生一份逻辑任务。对现有分开的会话写入步骤制定过渡方案，不假设当前已经原子化。
- [x] 按网络限流、暂时数据库锁、模型暂时故障等分类重试并指数退避加抖动；鉴权失败、无效 schema、权限拒绝等永久错误不盲目重试。失败记录可检索但不得泄漏敏感 payload。
- [x] 在 Runtime 生命周期中有界启动和优雅停止 worker；设置 `busy_timeout`/WAL 或等效 SQLite 竞争策略。前台请求不在 FastAPI 事件循环上执行阻塞整理，worker 不持有长时数据库写事务等待 LLM。

### 3.2 隔离与可观测性

- [x] `interactive` / `background_memory` workload 隔离、前台保留 provider 槽、实际 usage 计入任务预算及取消等待者不占槽均有测试（`tests/test_llm_workloads.py`）。
- [x] Watch child 标记 `background_io`；后台模型统一网络超时默认30秒，前台超时不变；小时/日 token 与可配置费用估算持久记录，缺 usage 保守计量，预算不足延后而非消耗失败重试额度。
- [ ] 并发准入当前是单进程/controller，共享 SQLite 预算不是跨进程 provider semaphore；跨 provider/多 Runtime 精细协调及生产费用/积压验证未完成。
- [x] 近邻记忆提取任务可合批调用 provider，并按原始 source 独立校验/保存（`tests/test_background_scenarios.py::test_small_remote_burst_batches_and_preserves_each_original_source`）。
- [x] 同 kind/scope single-flight、跨会话双 worker 并行、等待时间 aging 已实现并有 targeted 测试；批量中途失败只重试未完成原始输入，已确认版本不重复更新。
- [ ] 多日公平性/饥饿率仍待压力测试；aging 加分有界，并非任意优先级下的严格无饥饿保证。
- [x] 暴露只读任务状态/健康指标：排队时长、执行时长、attempt、失败类别、租约恢复数、后台 token 消耗和当前积压；用户可看到“记忆待整理/已同步”，不把未发布候选伪装成已记住。

测试与验收：

- [x] 两个进程同时领取只有一个 owner，接管后旧 heartbeat/complete/input checkpoint 被拒绝；SQLite 事务回滚、锁故障分类、重复 enqueue/publication 有测试。
- [x] 提交前、入队后、工作中、发布后真实 `os._exit` 恢复测试通过；租约失效后迟到发布被拒绝。远程服务本身没有参与这些故障注入，不能声称 provider 收费恰好一次。
- [x] 验证取消、deadline、可重试与不可重试错误、退避上限和优雅停机；并发积压时普通 Agent 的 LLM 调用仍有可用槽位。
- [x] 队列关闭或故障时前台会话仍成功持久化；恢复后扫描 outbox 重新入队，无“回答成功但永久丢失整理任务”的窗口。

## 4. 阶段二：后台预压缩与同步兜底

依赖：阶段一。目标是减少长对话在最终回答后的同步 `context_summarize` 等待，同时保证连续性。

- [x] 工作窗口有单调 seq、revision、covered_seq、独立 summary_revision 及 summary_metadata（job/model/method/引用/lossy 标志）；采用追加迁移，保留原始消息，已有 token_estimate 不冒充 provider usage。
- [x] 前台结束时先可靠保存新 user/agent 消息和压缩任务；下一轮组装为“已发布摘要 + 水位之后的消息”。后台尚未完成时不得只读取旧摘要。对于未完成/失败 turn，定义是否进入记忆、是否进入工作窗口的明确规则。
- [ ] 按真实 token 用量在软阈值预排队，接近硬阈值提高任务优先级；达到硬安全线或 provider 即将拒绝时，仍调用现有同步压缩路径。阈值须按模型和预留输出计算，不用固定字符阈值冒充真实窗口。
- [x] 后台压缩固定旧消息区间，保留最近完整若干轮原文；输出结构化目标、已作决定、当前约束、未完成问题、关键实体、时间与原始引用。分段合并长历史，禁止无来源地把推断写成用户明确要求。
- [x] 固定压缩目标、并发新消息保留在 tail、旧 revision 发布 CAS 拒绝和硬阈值同步兜底已测试（`tests/test_async_context_compaction.py`）。
- [ ] 压缩来源引用/必留约束全面校验、多进程竞争及崩溃注入仍待验收。
- [x] 摘要生成异常、空输出、超长输出、输出不合 schema 或 provider 超时：保持旧快照和原始 tail，标记任务失败/重试；硬阈值兜底可用确定性压缩，但必须标记信息损失风险。

测试与验收：

- [ ] 压缩期间连续发送两轮、两个 worker 竞争、旧压缩结果晚到、同一轮重复 finalize、进程崩溃后恢复：没有消息丢失、重复覆盖或越序摘要。
- [ ] 长中文、代码块、邮件引用、较长单条消息、工具结果引用、时区/日期约束、用户否定与更正被正确保留；完整工具原文仍只在既有 run log/缓存，不错误复制到工作窗口。
- [x] legacy/Graph完整turn测试：后台模型阻塞时另一会话仍回答，新会话实际answer prompt加载偏好，worker未运行时明确forget立即抑制（`tests/test_memory_live_scenarios.py`）；硬阈值兜底/关闭兼容见sessions/integration测试。不据此承诺真实模型整轮SLO。
- [ ] 在长对话 fixture 上评测关键事实/目标/未决事项保留率和错误断言率；原始消息可恢复率必须为 100%，其余阈值以阶段零基线设定并记录未达标样本。
- [x] 实际本地 summarizer 有两个合成目标/日期/未决事项样本回归，当前 2/2（`tests/test_eval_background_compaction.py`）；remote 质量和生产保留率门槛未验收。

## 5. 阶段三：长期记忆存储、来源与撤回

依赖：阶段一；可与阶段二的独立存储工作并行，但必须在写入自动记忆前完成隐私与作用域合同。建议确定性 `MemoryService` 放在 `app/domains/`，LLM 提取编排放在 `app/core/`。

- [x] 建立 `memory_entries`、`memory_events`、`memory_sources`、`memory_snapshots`：记录类型、内容、`global/project` scope、状态 `candidate/active/superseded/retracted`、置信度、有效期、敏感级别、来源 ID/原文跨度或 checksum、版本、替代关系、创建/更新时间和提取模型版本。
- [x] 稳定 project ID/path map、显式 relocation API、旧路径可重新激活为 distinct project ID 及冲突保护已实现并测试（`tests/test_memory_service.py::test_explicit_project_move_keeps_identity_but_reused_old_path_does_not`）。
- [x] 会话软删除撤回来源、恢复不自动重新发布；项目显式搬迁与旧路径复用隔离已验证。撤回是停止使用，不删除不可变原始审计；重新确认要产生新来源/可追踪记录。
- [ ] 多工作树共享身份及跨项目复制仍需用户合同/实现，当前不同路径不自动合并。
- [x] 定义 `forget` 与原始记录保留的合同：撤回后立即停止注入并删除/更新派生索引；原始会话/审计数据是否仍保留必须明确展示，彻底清除走独立高风险操作。来源被删除、失权或过期时派生记忆失效。
- [x] 保留记忆事件历史与可逆更正；禁止静默改写旧结论。`MatterService` 继续是待办/事务状态的权威来源，记忆只保存引用和“为何重要”。文档、邮件和 RAG 数据仍由原领域服务管理。
- [x] 本地记忆列表/查看、纠正/撤回 API 与只读工具有实现及针对性测试（`tests/test_memory_integration.py`、`tests/test_memory_tools.py`）。
- [x] 列表/JSON 导出支持有界分页，来源接口只返元数据不返正文；后台发布记录 memory events 和租约 fence，Agent 修改工具仍经过 Safety Gate。用户本地控制 API 是用户确认入口，不伪装成 Agent 工具审批。

测试与验收：

- [x] 内容唯一身份/版本CAS、同源幂等/独立来源晋升、条件冲突待审、有效期与确认续期、撤回传播、会话软删除/恢复、重复迁移及SQLite备份恢复已有测试；冲突peer更正/失权/过期不会永久卡住有效偏好。
- [ ] 全局、项目、会话源之间不会误关联；被删除或失权的来源不能从 FTS/向量索引残留中再召回。检查 JSON/API/普通日志不回传凭证和原始私密正文。
- [x] 离线情况下可列出、纠正和撤回已有记忆；远程模型不可用不影响记忆数据库及会话读写。

## 6. 阶段四：自动提取、冲突处理与自进化指导

依赖：阶段三。这里实现“无需用户反复手动说存入文档”，但不允许模型直接把任意观察改成长期规则。

- [x] 后台只处理已完成且已持久化的对话区间；按 session/run 幂等提取。先用廉价确定性规则过滤寒暄、重复内容和可从源代码/领域库随时查到的事实，再对可能长期有用的片段调用可配置 LLM；允许批量合并与限额。
- [x] JSON 候选经类型/长度/秘密/权限/原文 evidence 验证；来源由服务端原始 USER 绑定，不接受模型虚构 ID。有效至仅接受原文明确日期或带时区时间；冲突采用条件/slot/polarity hints，不声称任意语义矛盾均可识别。
- [x] 低风险直接偏好自动 active；推断先 candidate，独立重复有效用户来源可晋升；相同作用域/条件下相反偏好 needs_review 并停止注入。模型候选不能屏蔽既有确认偏好；明确纠正前台撤回，版本/来源留存。只有新用户确认来源可续期旧事实。
- [x] 用户明确要求“忘记”或明确纠正一条可定位的旧记忆时，前台先同步撤回/抑制旧条目，再把新内容的提取与整理交给后台；下一轮不能因为后台排队而继续注入已否定的旧偏好。无法确定所指条目时只提出澄清，不批量猜测删除。
- [x] 区分“用户说了什么”与“Agent 做成了什么”：事实性用户偏好、项目决策、工作方式、已验证的有效策略可学习；一次失败、一次邮件描述或模型自我评价不可直接写成永久偏好。
- [x] 加入提示注入防线：网页、邮件、文档、RAG 结果和工具输出只可能产出低信任事实候选，不可成为自动行为指令、身份设定、权限策略或发送规则。来源内容中的“请记住/忽略以前指令”按数据处理。
- [x] 通用记忆提取与压缩只读取已完成、已持久化会话及派生摘要；启用记忆/后台功能即为此类会话处理授权，不再按内容敏感词逐段审批。另行授权的关注项/任务允许按各自持久化 scope 主动读取邮件、知识库、工作区等领域源，但不得借用通用记忆开关扩大 scope。保留作用域/来源校验、自动记忆发布过滤；配置不同后台模型、单任务/每日 token 预算和开关，不硬编码供应商。
- [x] 全局/项目 `MEMORY.md` 活跃条目视图记录 ID/版本/来源/时间，hash/CAS 与单条受控导入防止覆盖手改或新后台版本；非法格式不丢弃。超过1000条/8MiB明确拒绝侧写并指向分页API，原文件不覆盖。
- [ ] 多主题大型侧写与自由格式人工内容的辅助解析未实现；当前生成侧写有固定合同，自由格式用户指导仍使用 AGENTS.md，不自动导入不明内容。

测试与验收：

- [x] 显式/含糊偏好、引用/否定、更正、跨会话重复、项目作用域、失效来源/冲突等68个双语synthetic本地提取与create状态流转评测可复跑（development42、holdout26）；worker与完整turn链路另有场景测试。
- [ ] 反复失败建议等未覆盖场景及 production SLO/远程模型质量仍待评测；本地68/68不等于发布验收。
- [x] 恶意邮件/网页/检索文档要求“改变权限、发邮件、覆盖指导文件”时，自动规则晋升数为零；含秘密/PII 的会话仍可正常后台处理，但秘密/PII 不被自动发布为长期记忆。
- [x] 错误 JSON、空/过长/无证据 candidate 被拒绝；重复批量 evidence 不伪装成独立来源，模型矛盾候选不覆盖确认记忆，网络/限流按队列类型重试，已处理输入有 checkpoint。
- [x] 生成与手改hash冲突、导入与后台版本CAS冲突、非法UTF-8、preview后替换/消失、越界项目路径、过大文件/过多记录拒绝且不覆盖有测试；自由格式内容保持原文件并返回解析错误，不静默丢弃。
- [x] 用户在普通对话中说“刚才记错了”或“忘记这条”后，即使后台 worker 已暂停，下轮也不再看到被明确撤回的旧条目；不明确的纠正不会误删其他项目记忆。

## 7. 阶段五：选择性召回与上下文装配

依赖：阶段三与四的最小可用链路。目标是“记得对、拿得准”，而不是每轮灌入全部长期记忆。

- [x] 先按全局/项目、用户授权、敏感级别、有效期与状态做确定性过滤，再用 SQLite FTS5 + 元数据/时间/重要性排序；语义向量作为独立可选增强，检索失败可回退 FTS。
- [x] scope-filtered active recall、撤回即时生效和近期池以外相关记忆召回已有测试（`tests/test_memory_context.py`）。
- [x] 根 memory.read 精确查询，不受最近1000条限制；列表/导出分页；冲突 metadata 可查，prompt 告知 withheld_conflict_count，待审冲突不注入。
- [x] 统一 ContextAssembler 的实际输入 token 计量、分区裁剪、遗漏标识和来源续读。默认组装目标从 `131_072` 真实 token 起评测；保留会话历史原有最大预算；长 `AGENTS.md` 可索引、搜索、分页读取，不因为预览预算用尽而无法访问。指导文件修改后，前台先看到最新文件内容/版本，后台索引未就绪时不能伪装旧索引为最新。
- [x] >64K 指导文件变更可先展示最新 preview/index pending，不等待后台 index；长文件受控分页读取有测试（`tests/test_instruction_files.py::test_large_changed_guidance_preview_does_not_wait_for_index`、`::test_very_long_unstructured_instructions_reach_prompt_and_remain_fully_readable`）。
- [x] 大文件最新 preview 与 index pending 明确分开；search 校验当前文件签名并按需重建，不把旧索引当最新；已有修改中搜索/长文件全文读取场景测试。显式搜索重建仍可耗费该次工具时间。
- [x] 子 Agent ContextSnapshot/AgentView/ToolView 绑定显式 memory ID/version/content/source/project 及现有 permission/workspace 版本；scope 交集、缺失/错版本/预算不足拒绝派生；后台更新不修改已冻结内容。
- [x] 建立冲突优先级并可解释：确定性安全/权限策略 > 本轮用户明确要求 > 用户拥有的指导文件 > 已确认自动记忆 > 推断记忆 > 外部来源。作用域更具体只在同一信任层内增加相关性，不自动取得更高权限。
- [x] 历史工具 cache 记录 as_of/age/TTL/source/cache/permission/workspace 版本，未知 scope 或权限收紧/工作区变化不装载；历史结果永不满足本轮成功工具去重，legacy/Graph 两轮新增邮件同查询均实际重新检索/读取。
- [ ] 任意外部工具的 source version 自动重验证未通用实现；TTL 内也仅标记 historical_only，不据此承诺实时证据。

测试与验收：

- [ ] 同名人物跨项目、已撤回记忆、来源失权、过期票务信息、旧偏好与本轮纠正冲突、RAG 新证据推翻旧记忆等场景只注入允许且当前有效的内容。
- [x] prompt 大小接近、超过预算及模型窗口较小时安全裁剪；系统功能/工具合同和输出预留不被历史占满；被省略信息有明确“未读完”标记和读取路径。
- [x] Child 记忆工具只访问显式冻结 refs，未授权 ID 或撤回后的引用被拒绝（`tests/test_memory_tools.py::test_child_memory_tools_search_and_read_only_explicit_frozen_refs`、`::test_child_refs_without_assignment_or_after_retraction_are_rejected`）。
- [x] 双 child 并行派生后父记忆更新不改变旧快照，下一次派生可取得新版本（`tests/test_context_driver.py`）；读取撤回来源仍拒绝，不因冻结文本授予新权限。
- [x] 长指令文件修改后、后台索引滞后和工作区切换时，下一次 LLM 调用不会读取旧指导或越界路径；复用 `tests/test_instruction_files.py` 的长文件/更新 fixture。

## 8. 阶段六：接入现有后台业务与用户控制

依赖：阶段一至五稳定。此阶段按收益逐个迁移，不在同一 PR 中同时改 Watch、邮件和 Agent Graph。

- [x] 提供本地记忆列表、来源、事件/版本、更正/撤回及队列健康 API；候选、已发布和后台失败分开呈现，不弹出每轮确认。前端展示不在本次后端范围。
- [x] 全局/项目暂停、会话来源限制、分页导出、撤回和删除传播已实现并测试；Watch 每次成功推送独立创建会话，合批仅作用于后台提取 ID。
- [x] 评估 Watch、邮件同步和 Graph 接入统一底座：共享模型资源准入，但保留各自 occurrence/游标/checkpoint 的事务和恢复合同。整队列迁移当前不实施，以免弱化外部副作用语义；决定与回滚边界见开发报告。
- [x] 保留原 tool invocation durable claim 合同：completed 复用结果，uncertain 执行拒绝自动重放；审批/等待状态由 Graph 控制，不由通用后台 worker 推进。`tests/test_agent_runs.py` 验证已完成写工具恢复不再次执行；外部副作用不承诺恰好一次。

测试与验收：

- [ ] Watch 暂停/恢复、错过时段、时区/DST、手动 run-now、网络失败后重试均不重复推送；每次成功 occurrence 仅产生一个新会话。
- [ ] 邮件导入后但游标提交前崩溃可安全重放，不重复知识镜像；慢同步不占满交互池。Graph 重启恢复不会重复非幂等外部动作，审批等待状态不被后台 worker 绕过。
- [x] 旧客户端不使用记忆 API 时行为兼容；开关关闭后不再提取/注入新记忆，已有原始会话和业务数据仍能读取。用户撤回后的下一轮 prompt 不含该记忆。

## 9. 发布前评测与门槛

- [x] 记忆侧68-case双语 fixture（42 development/26 holdout）、6-case实际召回与2-case本地 summarizer 可复跑；分组隔离和 conflict_hints 与真实协调器一致。均是 synthetic 回归，不是生产质量门槛。
- [ ] 完整 Who/What/Why/When、后台崩溃恢复联合 fixture 及生产发布门槛仍未完成。
- [ ] 与“未启用记忆”及“仅会话摘要”两组基线比较：长期偏好/决定召回率、来源引用准确率、过期或冲突误召回率、无证据时拒答率、任务完成率、前台 p50/p95、后台积压、token 与费用。模型和测试样本固定，失败案例逐项归因。
- [ ] 安全发布门槛：跨项目/会话越权召回、撤回后再注入、外部提示注入晋升为规则、后台绕过 Safety Gate、后台擅自读取或外发未授权的独立领域源，测试集内均为 **0**；一旦出现即阻止发布。
- [ ] 可用性目标先作为待测 SLO：后台记忆开启后普通 turn 的前台额外本地耗时 p95 不超过基线 10%；明确长期偏好和用户更正的跨会话召回达到人工标注集 95% 以上；记忆错误发布率控制在 2% 以下。若样本不足或模型波动大，先扩充数据并报告置信范围，不通过改阈值掩盖失败。
- [ ] 故障发布门槛：原始会话保存率 100%；worker 崩溃、重复事件、租约接管或 DB 重试不造成丢消息/重复发布；后台不可用时前台仍可用，硬阈值同步压缩兜底可触发。
- [ ] 灰度顺序：观测模式（只产候选不注入）→ 只读召回 → 低风险自动发布 → 多 Agent 与 Watch 接入；每阶段记录实际评测、人工复核的 fail case 和回滚结果后再打开下一阶段。

## 10. 实施顺序与建议代码落点

1. 先完成阶段零和阶段一：这是阶段二到六的可靠性前提。复用 Watch 的租约经验，但不要把具体领域规则搬入通用调度器。
2. 完成阶段二并验证前台延迟改善；在尚未引入“智能记忆”前先消除同步压缩的主要痛点。
3. 完成阶段三、四、五的最小纵向闭环：一次明确偏好 → 后台提取 → 来源校验 → 发布 → 另一会话正确召回 → 用户撤回。闭环通过后再扩展类型和向量召回。
4. 最后做阶段六迁移和第九节的灰度验收；任何阶段回滚都不能删除原始消息、运行日志或已有领域数据。

建议模块边界：`app/core/` 承担后台 worker、LLM 提取编排和上下文装配；`app/domains/` 承担无 LLM 的记忆状态机；`app/tool_packages/` 暴露受 Tool Executor 和 Safety Gate 约束的工具；`app/storage/db.py` 管迁移；`app/api/routes/` 只放必要的状态与用户控制接口。测试按 `tests/test_background_jobs.py`、`tests/test_async_context_compaction.py`、`tests/test_memory_service.py`、`tests/test_memory_extraction.py`、`tests/test_memory_context.py`、`tests/test_memory_security.py` 和 `evals/` fixture 分层添加；这些路径是拟建目标，并非已存在文件。

## 前端管理接口补充（2026-10-02）

- [x] GET/PATCH/DELETE `/background/config`：SQLite 部分覆盖项、revision CAS、当前/待生效配置分离、重启应用与恢复 TOML/default。
- [x] GET `/background/config/schema`：可编辑字段类型、默认值和约束，不包含 provider 凭证。
- [x] GET `/background/jobs/{id}` 与 POST retry/cancel：任务状态 CAS、保留处理 checkpoint/用量、取消旧租约、限制重试类型与过期输入。
- [x] GET `/background/events`：本地鉴权、异步采样、初始快照与 keepalive，说明非持久化事件流与 fetch Authorization 接法。
- [x] GET `/sessions/{id}/context-status`：方法/水位/降级/计数/压缩任务状态，不物化消息正文；新会话与删除会话边界验证。
- [x] 前端接口与记忆/后台回归联测：142 passed / 37.45s；不替代上文未完成的生产级质量与长期压力验收。
