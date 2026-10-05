# 通用任务完成控制：TODO 与验证记录

目标：已知要求未完成时能继续执行；停止时保留缺口；简单任务不增加模型调用。
系统工具指引允许直接进入 core prompt，但不加入评测题关键词、答案、URL 或领域流程。

## 本轮执行项

- [x] 扩展现有可选 answer_checks：稳定 requirement_id、pending/supported/blocked。
- [x] 在 Graph checkpoint 保留要求与恢复次数；后续遗漏不能删除已知要求。
- [x] 结束前确定性检查已声明缺口；最多两次恢复，无新成功工具证据时停止。
- [x] Legacy loop 使用同一检查；step/child预算耗尽仍能产生部分回答，不循环。
- [x] 把要求状态交给独立 answer writer，保持用户JSON与现有SSE终态。
- [x] 收紧预览覆盖规则：未读全文不等于用户要求未完成，不增加默认 reviewer 调用。
- [x] 离线状态机、持久化/恢复、JSON、取消、流式和简单任务调用数回归。
- [x] 只做必要真实困难样本，保留失败、统计完整开销，不把机械通过当语义准确。
- [x] 独立复核、报告与范围明确的Git提交；保留并行用户修改。

## 设计边界

这是对**模型已声明**要求的状态机，不是需求完整性或事实蕴含验证器。
旧reason-only结束兼容；没有声明的要求不能凭代码自动发现。supported是模型声明，
不能升级为后端事实验证；blocked也不是权限授权。新协议不赋予新工具权限。
每次仍最多8条检查，总账本最多32项，超过容量显式部分停止，不能静默丢弃后当完整。
原文引用只留原决策/审计，checkpoint账本不复制引用正文。

本轮优先控制进入answer前的缺口。不扫描最终回答的“尚未”等关键词，不强迫用户答案
套私有JSON；回答写作时才发现的新缺口与通用语义审核，仍需后续版本化控制通道。
不改推理开关/模型配置，不重启生产或新增后台服务。

## 离线验证与过程问题

初轮定向 **123 passed / 1 deselected，30.87s**，最终验证包含后续补充案例。
新增文件当前包含14个纯状态机案例和22个参数化运行链路案例，
覆盖缺口保留、改名/重排不能重置次数、32项边界、步数停止、取消、真实文件工具、
SQLite checkpoint关闭后重新打开、实际provider流与一次终态、JSON和上下文压力。
账本还作为独立顶层task_completion进入JSON/native决策，避免反馈观察被预算裁掉后
模型失去要求；child最终回答输入预估也计入这份账本。无账本的简单任务原调用数不变。
这些scripted provider测试验证流程和交付，不代表真实模型准确率。

最终定向回归：**153 passed / 1 deselected，33.47s**；Ruff、compileall与diff检查通过。
未跑全量测试，未重启生产。复现命令（原旧预算夹具失败原因见下）：

```bash
.venv/bin/python -m pytest -q \
  tests/test_task_completion_quality.py tests/test_task_completion_runtime_quality.py \
  tests/test_answer_working_set_quality.py tests/test_unified_entry_quality.py \
  tests/test_native_contract_projection_quality.py tests/test_agent_graph_fork_repair.py \
  tests/test_requested_answer_format_quality.py tests/test_answer_generation_recovery_quality.py \
  tests/test_eval_runtime_web_quality.py tests/test_child_budget_quality.py \
  tests/test_child_completion_quality.py tests/test_child_answer_failure_quality.py \
  -k 'not test_byte_upper_bound_soft_finish_is_partial_with_exact_reads_and_unknowns'
```

测试过程的故障均保留说明，不修改旧用例以制造通过：

- 新stream夹具误用了不存在的add_listener，已改为真实离线LLMService provider stream
  与run event记录；验证assistant_answer delta、一次run_completed和一次final_answer。
- 新纯状态机测试曾把“保留缺口”误认为“每次必须拒绝finish”，以及误将16项当成32项
  上限；按明确的partial停止语义和32项容量纠正后通过，未放宽实现的循环边界。
- protocol说明最初增长573→918字符。收敛为665字符左右后保持通用字段说明；既有
  不为可选notes额外读工具的约束保留。协议提示只说明机制，不替代完成控制代码。
- 旧test_child_budget_quality字节上界场景要求固定4份读取，但当前树实际软停止在3份，
  导致scripted provider里的固定条数assert把partial答案变成failed。恢复HEAD旧policy，
  再在单独测试进程恢复本轮涉及的9个HEAD loop/graph方法，均复现相同失败。
  不回滚用户文件、不改测试或提高32768预算；该失败单独保留，最终相关集合排除这一项。
  这不是完整纯HEAD checkout验证，也不推断某项并行修改就是具体原因。

独立审查由低成本子agent执行，主agent负责集成与测试。审查同时指出legacy预算耗尽
且无观察时应能保留已知要求，已修复；主agent另查到child结果需同步携带新缺口，已补齐
服务端task_completion_handoff与终态恢复。child只取最后交付状态，不把已解决的旧pending
延续为缺口，不信任工具正文伪造的同名字段。容量耗尽即使32项全部supported仍留partial标记。
Graph生成失败与要求缺口同时存在时保留两类标记，不因metadata合并覆盖生成失败。
新增实际child夹具第一次忘记开启统一入口，模型脚本被旧route/context消费而没有进入decision；
修正测试配置后验证legacy/graph均执行预期链路，没有修改生产入口默认行为。
另新增writer失败测试最初错误地期望两次空provider回复会产生partial；实际既有合同是
一次恢复后抛出LLMClientError并让run失败。拆分测试：嵌入式writer返回None测试metadata，
真实离线provider两次空回复测试failed run且没有final_answer。该测试获取审计还误用了
request.metadata里的run_id与create_run的模型参数，已按实际run/API合同修正，不改生产合同。

## 唯一一次真实复测：仍是 bad case

原题heldout_web_multisource，DeepSeek Flash、当前配置的接口；检索Python两个版本的官方
free-threaded说明。不改原题、gold、判定器、模型配置；source/scenario指纹在实测过程中
保持一致。后续child交付修复只做离线验证，没有再刷付费样本。

| 指标 | 上轮有效样本 | 本轮单次样本 |
| --- | ---: | ---: |
| 任务耗时 | 37.357s | 103.431s |
| 模型／工具调用 | 7／4 | 13／7 |
| 已知输入／输出tokens | 54,117／5,796 | 112,933／11,940 |
| 缓存读取tokens | 29,696 | 44,416 |
| 最终答案字符 | 1,027 | 1,282 |
| 本地预算计账USD | 0.038559136 | 0.162689856 |

本轮12次模型调用有用量，计价0.093732256；另一次超时无provider用量，按评测预算策略
保守预扣0.0689576，所以表中**不是供应商实际账单**。已知tokens不包含该超时的未知用量。
搜索1次、搜索价格未配置，记账0不等于免费。仅两次不受控网络/模型单样本，不能推断
回归完全由协议改动导致，也不能宣称平均准确率或稳定提速。

机械检查四项均通过（包括加载必要页面），语义审阅不通过：decision的结束理由提到了
支持级别变化，但writer最终没有交付这一关键结论，反而声称读取片段中没有其原文；篇幅
也不够简短。按最终fitted prompt里的源内容无法定位相应关键支持条款，不能仅凭decision
reason推断该结论已被独立验证。这个样本同时暴露检索定位与answer证据交付的问题。

调用增多包括一次损坏DSML格式后的本地协议重试、一次30秒deadline超时及一次已分类的
控制生成恢复，另外展开observation包却未使用其工具。不能把全部额外耗时归因于新gate：
本次结束只有reason、没有answer_checks，**新完成控制没有触发**。没有新增默认review模型。

私有原始证据在（保留原始pending_root_review，不覆写artifact；本段是主agent语义复核，
不是用户人工gold）：
`data/quality_runs/linux_20261005/runtime_heldout_web_multisource_20261005T143501064843/heldout_web_multisource_20261005T143501071561/`
包含report.json、SHA256、web_analysis.json、provider请求/响应及完整计账。仅本地保存。

## 未闭合问题与下一步验收

- P1：复杂任务不稳定填写可选要求。后续设计可版本协商的需求／证据交付契约，而不是
  靠更长prompt或按Python/邮件题目关键词强行触发。保留reason-only兼容与简单任务快路径。
  验收：跨网页、文件、邮件的不同复杂任务，测要求覆盖率、调用数与首答时延。
- P1：answer才发现缺口时不能重开执行。需要独立结构化控制通道、一次性输出交付边界
  与有限resume，不能扫描用户最终答案关键词，更不能污染用户JSON或重复SSE最终回答。
  验收：writer有根据地报告缺口能补做；纯不确定/权限拒绝不无限重试。
- P1：检索阶段的关键原文在writer上下文中未必保留。后续把稳定source/offset/hash与
  用户所需结论关联，有界保留所需证据而非提高整页或全prompt预算。
  验收：多来源、否定限定、超长源文件在最终provider请求内可定位，而非仅工具成功。
- P2：原始模型协议波动、超时及冗长仍存在。当前失败分类与一次恢复有效，但本轮不能
  推断超时根因。后续对复杂度、成功/失败耗时、缓存命中分层统计，不再只看机械通过。

本轮可确认的是完成控制与状态交付的工程改善，**不是复杂任务语义问题已经解决**。
恢复进展目前只认成功工具输出变化，纯内部推理不视为新增证据；账本不是完整任务DAG。

## 2026-10-06：暂缓难点，转向日常任务收益

按用户最新要求，暂缓复杂问题要求自动声明、answer阶段缺口回执行、通用语义证据
交付优化；不关闭原bad case，不撤销现有完成控制，也不反复重跑同一付费题。
多跳RAG架构优化和多agent复杂独立合同的预算/角色大改同样不作为下一轮重点。
重新启动条件：出现新的可定位反例，或已有局部工程方案能在固定离线输入中验证收益；
先验证最小缺口，再决定是否值得一次真实模型验收，不因换一段prompt就重复付费。

下一步候选按用户实际使用收益排序。以下是**待验证的优化方向，不是已确认的新bug**；
复用已有工具、记忆、后台与评测入口，先复现再改，不开发另一套harness。

| 顺序 | 方向 | 具体任务与困难边界 | 验收与范围 |
| --- | --- | --- | --- |
| 1 | 文件与编码任务的可靠交付 | 多文件修改、长文件定位、缺失测试入口；并发编辑、同名文件、命令返回失败 | 在隔离工作区核对diff、原测试及最终文件；原测试失败不能凭一次命令completed宣称成功。小型代码基线已通过，不算从零修复 |
| 2 | 长会话中的更正与项目记忆 | 摘要前后更正日期/偏好、临时例外、切换项目；晚到worker | 检查下一轮实际prompt和回答；旧决定不复活、项目不串、临时要求不永久化。已有单点更正通过，补组合验收 |
| 3 | 后台任务不拖慢前台 | 摘要/记忆与文件任务同时运行；队列积压、取消、worker重启 | 少量离线provider+真实SQLite的并发/恢复探针，记录前台延迟、event-loop lag与积压；有具体失败才修，暂不先跑60分钟全矩阵 |
| 4 | 简单任务与追问少绕路 | 已读文件追问、纯上下文问答、源文件已变更的追问 | 复用现有统一入口/缓存机制，核对调用轨迹与回答正确性；新鲜度优先，不能为少一次调用使用过期证据。不自动改生产开关 |
| 5 | 邮件行动项的真实质量 | 低优先级但需操作、重要但纯通知、已取消行动、同事项更新 | 同时核对专家提取、handoff与父答案，不自动发邮件或创建事项；先小型合成场景，必要时才用已授权真实隔离快照 |

建议先选1，再选2：完成一个实际多文件任务、随后完成一组压缩后更正/跨项目任务，
比继续打磨同一道困难网页题更能检验“能否放心日常使用”。遇到语义瓶颈也保留问题并转向
可确定性修复的边界，不以新测试绿代替用户场景成功。本次只整理记录，没有实现这些候选、
运行新评测、调用远程模型或修改生产配置。
