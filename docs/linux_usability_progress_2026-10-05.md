# P1 / P2 可用性优化执行记录（2026-10-05）

实施队列：[详细 TODO](linux_usability_p1_p2_todolist_2026-10-05.md)。
仅记录本轮实际执行，不覆盖历史报告中的失败或把离线测试当真实模型闭环。
用户要求减少无关测试；不运行全量 suite、不新增高开销模型。

## PRE-00 / P0

- 基线提交：`1bf5f3c`。现有消息、Windows、workload 等 dirty 改动属于用户，未纳入本轮提交。
- 使用项目 `.venv/bin/pytest`，不是系统 Python；未重启 8765、未更改生产库。
- P0 单批八个代表实例：Bash 实际 READ 拒绝写与普通读、继承 shell hook、条件只读发现
  与实际调用、native 无控制动作、SSE 无终态恢复、执行前拒绝、执行后失败审计。
- 结果：**8 passed / 1.54s**。未扩展安全矩阵、未调用远程模型。
- 旧 workload 15/11 列测试不属于本轮切片；未重复运行或修改用户功能。

## UX-01：降级后的历史 partial 与说明

场景：一项子任务已有 partial 后接受 skip/degrade，另一项仍失败；旧最终回答过滤了
前者，错误称未取得结果，并丢失降级说明。

改进：从服务端接受的 patch history 和真实 parent 子任务事件恢复交付用历史。
核对 plan/session/parent/correlation/child/attempt，校验 patch snapshot hash；
历史只用于交付，不改变 canonical aggregate、验证状态或失败门槛。
历史摘要和当前摘要共享原 5500 字符公平预算，明确历史、未独立核验与缺失义务。
mixed/all-skipped recovery 也携带独立 historical_task_results，不作为当前完成结果。

验证：

- 原显式复现 + 相邻 partial 交付：**19 passed / 0.60s**，原三红例转绿。
- 新增默认回归 `tests/test_degraded_partial_delivery_quality.py`：**10 passed / 0.45s**。
- 当前切片 Ruff：通过。

状态：implemented / offline_verified；未进行真实远程模型重放，不标 live_verified。
持久原件、失败状态、用户改动均保留。后续独立审查发现问题在本记录继续追加。

## UX-02：可回退的统一入口

改进：legacy / LangGraph 可直接由首个正常 ReAct 动作选择展开包、回答或按需委派，
取消额外的模型 route 阶段。不预先展开全体 schema，不跳过 ToolView / executor。
首个实际展开包成为 initial_package；没有包但有 fork 观察时仍将观察交给回答阶段。
保留已校验 child 单授权包的确定性快捷路径；统一入口的进度事件和检查点保持可观察。

配置：`[agent] unified_entry_enabled = true`。**默认 false**，先作为可配对验证的
回退开关，不修改用户本地配置或已运行进程；取消入口 route 不等于冷启动少一次调用，
因为第一次动态展开包也需要选择。

本地真实 runtime + 脚本 provider：改名文件读取为 4 次调用（展开/读取/结束/回答），
上下文回答为 2 次；最终回答保持独立 writer。双执行器 + 现有 child 路由 / fork 修复
检查共 9 项通过，原批次 5.74s；独立低成本代码审查未发现新增具体缺陷。
这些只证明控制流程，不代表真实模型选择准确率或测得端到端加速。

## UX-03：协调输入瘦身与一次真实重放

有剩余委派深度的 coordinator 也使用紧凑控制视图，仍可合法 fork / plan_patch；
原冻结角色、权限、子任务 32768 累计预算和回答预留不变。child_budget 增加已知输入/输出、
未知计费调用、在途保守预留和分阶段调用数，未知 dispatch 不记成免费。
同执行能力的控制 system prompt：**6943 → 2771 UTF-8 bytes（-60.1%）**。
不是 provider token 计数；本地 7 个定向检查通过 / 0.53s，Ruff 通过。

原 `parallel_audit` 仅远程重放一次，DeepSeek Flash、共享账本，无搜索：

| 指标 | 本次测量 |
| --- | --- |
| 任务耗时 | 80.300s（历史原失败 131.354s；非统计 A/B） |
| 父+子 dispatch | 22（父 5，子 17；历史 26） |
| 输入 / 输出 / 缓存读取 | 116536 / 14499 / 33408 tokens |
| 实际已知费用 | $0.113433728，22 次均已结算，无新增 unknown |
| 并行情况 | 发布/备份启动相隔 0.147s，确实重叠；第三任务随后启动 |
| 合同状态 | 1 completed、2 partial 后显式 skip；**不算三合同全部完成** |

原始报告：本地 `data/quality_runs/linux_20261005/parallel-quality-20261005T055551009734-3561daeb/parallel_audit_20261005T055551011294/report.json`，
SHA-256 `55432e411332f26ed44f2c155b602cb9d759bc87841d70eedca72eacfdb805d8`。
运行中源码/fixture hash 未变化。人工核对：最终保留发布窗口、待审批、恢复失败分段、
端口冲突及只读覆盖限制，并披露两个 partial；没有把降级洗成独立验证通过。
但回答仍偏长，两个 child 在下一次控制输入+回答预留不再同时可容纳时提前停止，
说明**提示瘦身尚不足以闭合 UX-03**。不因机械 fact_coverage=true 改为 fixed_live。

下一局部 TODO：裁剪累计证据重复与阶段视图，保留合同义务和实际原件路径；
再评估需要委派/聚焦执行提示。不是简单提高 ceiling 或降低回答预留。

## 已有补丁的有限验收（UX-04 / 06 / 07 / 08 / 09 / 10）

- 邮件低优先级必需行动和 omitted 披露；摘要三次来源继承、跨会话反例、metadata 上限；
  记忆纠正；SQLite 等待用户重启：7 passed / 6.99s。
- SQLite journal 发表前失败、发表后恢复、context 绑定前 crash、取消后不派新 attempt：
  4 passed / 2.04s。只取 retry_step 代表参数，不重复恢复矩阵。
- 独立低成本 agent 用项目 venv 核验六个网页代表节点：6 passed / 0.17s。包含
  article/body fallback、长 Unicode 分页、hash 变化拒绝、find→续读、零命中恢复。

均未把这些离线结果当成真实语义闭环，不重写已经有效的补丁；60 分钟压测本轮未跑。

## UX-01 后续 review：两个有损交付边界

独立 review 找到缺失计数在历史结果恢复后仍使用旧列表；另查发现超长 fork 观察
再次摘要时仅保留当前 task_results，会丢掉独立 historical_task_results / 降级说明。
两项新增回归先 **2 failed / 0.36s**，未修改原断言来掩盖。

修复：先按已恢复身份过滤缺失列表，再分页/计数；当前与历史共用 20 项摘要限额，
保留历史标志、result/snapshot 引用、降级说明及省略计数。不增加总体上下文预算。
保留既有重复缺失项统计语义，不顺手清洗历史数据。
修复后新旧 partial 交付 **21 passed / 0.41s**，Ruff 通过。

继续核对 provider 容量门槛发现第二层 PromptBudgeter 也只保留当前结果。现同样保留
历史 partial、missing_requirements、result/snapshot、降级说明，当前与历史仍共用
20 项上限。真实 fit + 原有预算回归 **4 passed / 0.35s**。测试编写时修正了误用的
`context_window_tokens` 参数，正确使用既有 `input_limit`，未变更真实容量合同。

## 后台连续运行探针：未闭合，暂不消耗远程预算

尝试把现有 medium followup 探针扩展为三次发布时，离线 provider 路径在进入实际
dispatch 前超时；第一条 context_compact 处于 running，provider/账本派发均为 0。
进一步以原两次模式和原 fixture 作有界诊断，亦复现进入 workload admission 后的等待。
`_reserve` 实际返回约 0.008s；不能将其归因为模型慢、SQL 预留计算慢或摘要来源拒绝。

本轮未确认等待链路的根因，**不宣称是生产摘要缺陷**；已有来源继承同步检查仍通过。
没有启动真实后台付费复测，没有绕过 workload 控制或改用户在开发的消息/配额模块。
三次探针扩展未验收，撤回本轮未完成扩展（原测试/脚本完整保留），不加 xfail 或放宽断言。
下一局部 TODO：隔离复现 admission 的后台 event-loop/future 返回，再区分评测注入、
当前并行修改与生产路径；根因确认后恢复三发布测试。60 分钟负载继续待办。

## UX-11：用户自述和外部取证分开

route、普通/native decision、answer/context_answer 共用通用来源语义：用户对自己
安排/偏好的陈述和更正可以作为本轮工作前提，不为了简单确认额外核实；但不等于
外部事实已验证、文件已修改或行动已获授权。引用/检索文本不能冒充用户更正。
没有关键词 intent 表、没有案例答案进入 core；本轮没有自动永久发布新记忆。

隔离真实 DeepSeek Flash，用统一入口 enabled 的两个小场景（原型开关，非生产默认）：

| 场景 | 实际行为 | 耗时 / 模型调用 |
| --- | --- | --- |
| 用户把自己的评审安排 11-06 改成 11-09，说明尚不发布、复核未完成 | 无工具，按用户陈述确认，保留否定/未完成 | 7.543s / 2 |
| 用户说自己改期，并明确要求核对记录 | 实际读一次文件，发现记录仍是 11-06，区分计划与已登记 | 9.871s / 4 |

人工核对两答及工具事件；第二例未修改文件。第一例“已记录”仅能指已保存本轮会话，
不视为已更新长期记忆；“原因”表述有推断成分，后续答案因果校准仍需处理。
两个报告 SHA-256：`cda1ac7308612d1722bd1d856e63db2e05102eb097673825bca7a5bc50904dac`、
`5645d2575586d8b8edc6fb338be2c402ba3b9fa2946d6d1ec94a18cb8794e71c`。
本地 artifacts 分别为 `user_plan_correction_20261005T062053679697` /
`user_record_verification_20261005T062102701992`（在原私有 quality_runs 目录内）。
两例累计输入 12446、输出 1931、cache read 5760 tokens，实际费用 $0.011620160。
所有 6 dispatch 已结算；最初预算 adapter 缺 snapshot 的准备错误未派发模型，已改用既有兼容 adapter。

本地来源/格式 provider 边界 + 统一入口 12 passed / 5.24s；新增 goal 不预指定注册工具
及 compact coordinator 2 passed / 0.55s；Ruff 通过。不是来源语义任意情形的准确率，
长会话摘要后的更正/项目记忆组合仍未闭合。

## 本轮预算与交付范围

新增远程：28 次 DeepSeek Flash，已知费用 $0.125053888；没有额外搜索或高开销 Agent。
共享累计账本：$6.277132704（含历史未知预留），1106 模型 dispatch、8 搜索；未超过原 50 额度。
未重启用户后端、未迁移生产数据。所有源码切片单独 Git 提交，用户并行工作保留。
尚未完成：三独立原合同全通过、统一入口稳定 A/B、网页三开放语义案例、真实低优先级邮件/
记忆组合、后台连续发布与长期负载。详细 TODO 未全部打勾，不能声称 P1/P2 全完成。

## 续轮 UX-07：区分隔离环境等待与真实后台问题

最小实验：后台线程中的 `asyncio.shield(asyncio.to_thread(lambda: None))` 在隔离命令环境
可能不返回，已完成 Task 的 callback 要等额外 timer 唤醒；实验不导入 LKA、不读取数据。
同一实验在宿主环境约 0.0036s 完成，线程正常退出。原两发布离线回归在宿主环境
**1 passed / 2.98s**。此前派发前超时不能归咎于模型、SQLite 或业务 admission，
不为此修改业务代码、不用轮询 timer 掩盖环境问题。涉及线程事件循环的验证在宿主执行。

扩展真实 outbox/worker 探针：先自然发布原两个 prefix，再追加第二批交流，等待至少
三次不同水位的发布；最后同会话追问旧事实、纠正日期与保留未审批/未完成。
不手工发布摘要、不增加 32-call 额度、不更改生产 65536 历史预算。
首版追加三条大消息留下过大的 raw tail，前台安全投影会追加本地摘录，导致发布版本
identity 检查 **2 failed / 8.19s**；这不是 model 发表失败。第二批也保留小 raw tail，
明确将“后台连续发表”和“应急前台投影”分开测；保留原精确版本断言。
最终 **2 passed / 6.44s**，继承首批 trace ID、同会话版本交付和合法改写均通过。
这是离线 provider 的实际后台链路证据；真实语义保真与长期压力仍不据此打勾。

一次真实 DeepSeek Flash 重放（隔离合成记录，无搜索）：水位 **4→6→12→14**，
4 次 model publication；旧事实来源在最终摘要已不在 raw tail。实际回答 prompt
精确加载已发表版本；两次回答分别使用 11-06 与用户本轮更正 11-09，均保留未批准
上线与安全复核未完成。人工核对摘要和回答，不只靠 literal 指标。前台两轮
**4.542s / 4.933s**，总探针 81.422s；10 dispatch，全结算，输入 21588、输出
19814、cache read 256 tokens，$0.080474496。源码指纹一致、无前台写工具，
结束时全部 job succeeded、workload 活跃槽归零。长会话更正分支因此有现场证据，
但不是 65536 生产阈值、前后台重叠或 60 分钟负载验收。
原件 `runtime_background_followup_20261005T071651015234/report.json`，SHA-256
`1b36ab4600781c8f7faef871f8fa1002d4c15012068315ac0657b0d01aaeb36c`。

新发现：虽未丢事实，模型摘要逐轮罗列记录序号、transport trace 和时间戳，多个
结构化段又重复相同结论，最终 2351 字符，降低后台效率并浪费前台上下文。
最小改进：只对完全重复的连续文本生成可逆 provider 视图（线性算法，无语义猜测），
保留一次完整措辞、重复次数、尾部残片及原件 hash；最多 8 个重复行且单行有工作量
上限。原会话、来源和序列不修改；不同结论/否定/更正不合并。来源元信息与叙述事实
在摘要规则中区分，日期只在实际事实/相对日期解析需要时保留；不追加另一轮 reviewer。
5 个投影/来源定向检查 **5 passed / 2.57s**，实际 worker 回归
**1 passed / 3.14s**，Ruff 通过。真实前后效果单独追加，不先宣称加速。

评测目录更新为 **41 项**：4 基础基线、16 bounded live 修复、13 离线修复、8 open。
默认 focus 为 21 项，仍跳过基础与已确认 live 修复；UX-01 从 open 改为 fixed_offline，
新增 UX-11 仍为 open（两个新分支通过不替代原长会话组合验收）。统计由目录计算，
引用/统计两个定向检查 2 passed / 0.11s；历史档案保留优化前快照并增加醒目说明。

相同 seed/水位/32-call 额度复测一次：4 次 model publication 与两次纠正/追问继续通过，
10 次调用全结算、源码不变、原始来源仍继承，活跃槽归零。末版摘要
**2351→1235 字符（-47.5%）**；输入 **21588→13394 tokens（-38.0%）**，输出
19814→18127、cache read 256→768；费用 **$0.080474496→$0.068119488（-15.4%）**。
前台两轮 4.507s / 4.589s；整轮 **81.422→81.958s**，无端到端加速，不能把
输入缩短宣传成响应速度提升。仍罗列部分重复的无决策记录，摘要质量不是最优；
真实推理耗时仍主导此场景。单对样本非统计 A/B，不代表生产 SLA。
原件 `runtime_background_followup_20261005T072431067367/report.json`，SHA-256
`4088846d7fb20e5fc37a38c9c37434e633369946ccbf381fa5dabfa20645232f`。

## 续轮 UX-03：native 请求中的重复执行合同

检查发现 native decision 同时在函数定义与 JSON `expanded_tools` 中重复描述/schema。
新投影只引用同一请求实际发送的唯一函数映射：相同 description 不再重复，只有完整
原 schema 与 native parameters 的类型敏感 JSON 相同才去重；legacy/custom/有额外
语义的 schema 继续完整保留。明确模型函数名称映射，read_only、确认要求、output
schema、origin/effects 元数据继续显式可见。ToolView、executor、fork 能力及 32768
child ceiling 不变，JSON fallback 仍有完整 schema，不在运行时加载案例/gold。

真实注册 Bash 六工具只读投影测量：expanded metadata **3999→3566 UTF-8 bytes**
（-10.8%），函数定义 2330 bytes 原样保留；六份 schema 均非精确等价，诚实保留，
没有为了节省而移除约束。当前三独立审计使用 JSON 控制，此改动**不算它的闭环修复**。
实际 native 控制、fork 可见与原生调用定向 **5 passed / 6.56s**；review 补充
boolean/number 对比反例（Python `True == 1` 不足以证明合同一致），修复后模块
**4 passed / 0.53s**，Ruff 通过。未再消耗付费模型复测这个局部协议投影。

本续轮共两次真实 DeepSeek Flash、20 dispatch、$0.148593984、零搜索。
不启动高开销 Agent，不改用户已有消息/配额/Windows 代码，未重启用户后端。
共享账本累计 $6.425726688（含 7 条历史未知预留）、1126 模型 dispatch、8 搜索。
评测仍为 41 项；连续 prefix 案例有真实证据后改为 fixed_live：4 baseline、17 live、
12 offline、8 open，默认 focus **21→20**。用户自述保留 open 并记录长会话新证据，
不把尚未完成的引用/跨项目组合一起算通过。目录引用/自动统计
**2 passed / 0.10s**；一次错误测试节点选择未运行任何测试，改用实际节点后完成验证。

## 续轮 UX-03：子任务 control 冷证据视图

原三任务 trace 在预算未耗尽时提前结束：下一次重复携带已读文件的 decision 输入，
加上必须保留的完整 answer 输入/输出额度，超过剩余额度。不能靠扩大 ceiling、削减
answer 预留或给原评测增加能力修复。本轮在 child control 构造可回读工作集：最新观察
原样保留；仅较早 completed/accepted/protocol-valid 的结果尝试摘录长字符串，字典、
数组、标量状态和缺口字段完整保留。root、失败、action/replan、历史缓存不投影。

投影须有当前 child ToolView 允许的实际只读 reader、同 run 原始 artifact，且原件必须
与此前完整观察类型敏感地精确一致；缺失、改写、越权或遍历超限时保留原观察。
复用现有 gate 的分页 ID/路径和 reader 提示，不在 core 写领域工具名、关键词或 gold。
原观察与 artifact 不修改。JSON/native 的控制输入均可瘦身，但两条链路的 answer
预算预测和最终回答继续使用原观察；取消不被可选投影吞掉。事件只记录 projection
数量/错误和 UTF-8 bytes，不把 bytes 当成 provider tokens。

8 个新边界实例、4 个 native 合同投影、3 个预算/重规划实例及一个真实注册 reader
隔离测试合计 **16 passed / 2.12s**；Ruff 通过。低成本 Luna 独立只读审查未发现阻塞
缺陷，没有额外模型 reviewer 调用。这里仅记录离线验证，原 UX-03 保持 open，待原
三独立任务、相同 32768 ceiling、32-call 总上限的单次真实重放核对。

原任务真实重放 **53.147s / 21 dispatch**（parent 4、child 17）；原三个 child 各有
独立 bash 取证、明确来源/范围/未确认项，canonical aggregate complete、三份 TaskResult
completed、没有 skip/replan/预算提前结束，parent 未补读或替换 child。消费累计 tokens
分别 release **29373**、backup **30467**、startup **22274**，均低于 32768。
release/backup 开始相差 **0.156s**，有实际执行重叠；startup 随第二槽释放启动，保持
max-2 并发。输出六事实、路径、否定状态与“未确认”人工核对通过；不从机械字符串
通过推断合同通过。保留独立形式校验未裁决说明，不洗成 verification pass。

分阶段 provider tokens（输入/输出，输入含 cache read；不是上下文窗口占用）：

| child | route | control | answer | 累计 |
| --- | ---: | ---: | ---: | ---: |
| release | 1697/117 | 21508/714 | 4474/863 | 29373 |
| backup | 1704/134 | 21746/803 | 4720/1360 | 30467 |
| startup | 1697/178 | 14382/866 | 4149/1002 | 22274 |

费用 **$0.074443808**，输入 96725、输出 9149、cache read 41088，21 条全部结算；
原件/代码/fixture 未改变、零搜索、文件未修改。较上轮 80.300s 的样本耗时 -33.8%、
输入 -17.0%、费用 -34.4%，但**新投影只实际生效一次，9009→8867 bytes（142 bytes）**。
本次取证动作/修复次数与上次不同，单样本不能把差值归因新投影，不能据此宣布稳定
性能或整体闭环。最终回答仍 1796 字符、重复列出缺口；部分结论先以确定口吻推断应用加载
配置、随后才承认未验证，表达校准仍有改善空间。UX-03 保持 open 和 focus 中，待
换名/变体及更稳定预算验收；不为漂亮统计降级验收要求。
原件 `parallel-quality-20261005T083859340749-cc6195b1/parallel_audit_20261005T083859344084/report.json`，
SHA-256 `fefbaf35fabbb33e3c25627c7f67ce7180c5237e9363574653d0c005ff2afa35`。

静态复查暴露进一步原因：同一个结果里完整文本被兼容字段重复承载，现有摘录的
JSON 元数据抵消很多收益。追加只对**完全相同**长字符串的一次摘录+JSON Pointer
局部引用，保留每个原路径/长度/partial 标识，不合并相似内容、不改原结果、不得
跨观察引用。这是通用类型投影，不以 stdout/output 等具体字段名分支。为 root 不投影、
artifact 读取后取消传播和转义路径/相似文本不合并追加三例。
离线回放同一实际 ToolResult，在保持最新观察完整的控制视图中：release 8253→8253、
backup **9009→8414**、startup **5727→5537 UTF-8 bytes**。仅计 bytes，不据此推断
tokens、调用数或延迟；这次追加未再付费重放。原三任务现场通过来自前一提交
`b15c16f`，不得冒充追加精确引用的 live 证据。

补齐后的同组定向回归 **19 passed / 1.94s**，Ruff 通过；没有跑全量 suite。

最终主审再收紧原件身份校验：两个截断 preview 相等不能证明被省略的中间内容未被
同-ID 改写。因此已经 gated/compacted 的观察不作这次冷证据二次投影，只比较完整
ToolResult 的类型敏感 JSON；其已有 gate/续读行为不变。追加已 gate 与结构化数组
已截断两例，最终 **21 passed / 2.19s**，Ruff 通过。此收紧是离线验收，不扩张上面的
旧提交 live 证据边界。目录引用与统计同步 **2 passed / 0.13s**。

本轮使用 DeepSeek Flash 单次、21 dispatch、$0.074443808，零搜索；不使用高开销
子 Agent、不更改用户并行开发的消息/Windows 模块、不重启后端。共享账本累计
$6.500170496、1147 dispatch（7 条历史未知预留），搜索仍 8。评测状态计数维持
41 项：4 baseline / 17 bounded live / 12 offline / 8 open，focus 仍 20。

## 能力续轮 UX-05：限定条件、网络事实与正文交付

用户要求暂停开销优化，改进能力表现。本轮不改变窗口、预算、模型、角色或 route，
优先修网页“命中声明但漏掉条件”和“猜测搜索摘要为什么不同”。不把 case/gold、
Python/SQLite 的事实写入 core，也不新增默认 LLM reviewer/自动改写回答。

工具根因：原 `web.find` 只保留命中前约100、合计400字符，同段末尾或下一段条件会
遗漏。新逻辑按可读提取中的空行分块，优先完整保留命中块及前后各一块，最多1200
字符。放不下时退到命中完整块，再退到有界片段；始终完整包含实际命中，并返回
`context_start/end`、`context_complete`、`snippet_scope`，让模型按同一提取 hash 续读。
这些是局部范围，不是 HTML 语义结构或 claim 的支持证明；`complete` 仍只表示匹配
分页结束。抓取 URL、hash、时间沿用原协议，SSRF/单跳公网校验/字节/时限不放宽。

open/find 新增这次请求实际的 `network_observations`：原 URL、真正经过的 redirect
跳数/状态/目标、最终状态、选定响应头前512字符。无跳转就是0，不能把 URL 名字不同
当发生过跳转；即使有 Age/Cache-Control，`cache_origin` 仍 not_determined，不能用
这个页面的响应头诊断搜索服务的摘要来源。同步工具描述/包 metadata/输出 schema，
旧输入参数和输出字段不移除，能力 API 的 flat schema 形式保留。

兼容检查发现初版将 flat 输出 schema 改为标准 object，两个原 API/spec 检查失败；
改回 flat 表达，不更改旧断言。三个相关模块 **63 passed / 1.88s**。独立 Luna 审查
另发现旧 casefold 匹配可能将 `s` 对上半个 `ß→ss`，再把整个原字符当成 literal；
增加折叠字符左右边界校验，保留完整 STRASSE/ß 匹配及准确 Unicode 原文区间。

真实 harness 缺口：普通 delivery binding 把“最多24条元数据”同时作为整树24节点
遍历预算，嵌套 metadata/匹配数组在到正文前就耗尽，返回空 binding。拆为累计256
节点遍历和最多24 binding/receipt，并继续每个 dict/list 最多枚举24；权限、原件
身份/精确片段核验与最终重新拟合不变。初版扩大宽容器枚举触发原≤24断言，保留
宽度限制后修复，不放宽旧测试。五个完整抓取的局部片段经普通 gate 后确实丢掉中间
条件，新的 receipt 正确报告 partial/upstream unknown，而非“模型已完整读过”。

新增12个边界实例：条件在下一块/上一块、邻居过长续读及版本变化拒绝、200字符
query 折叠后400字符仍完整命中、真实mock HTTP两次跳转/直连/未知缓存来源、四个
字段伪造反例、普通gate中间条件丢失、Unicode半字符误命中、实际SQLite artifact→
answer拟合→假provider的最终覆盖状态。新模块与既有delivery模块合计
**35 passed / 3.12s**，provider链路没有额外调用。只证明确定性范围与交付，不把
假provider的回答文案当模型语义成功。三个网页语义旧案例仍 open，等单次原任务重放。

最终五个相关模块 **98 passed / 4.89s**，Ruff 与 diff check 通过。独立审查确认总树
遍历、单容器宽度与绑定条数都仍有界，认证不扩权。工作区另有用户的两处 web 工具
权限属性改动，单独构造本轮diff暂存，原属性改动保持未暂存，未纳入本轮提交。

原 SQLite 任务第一次真实重放 **54.209s / 9 calls / 1 search / $0.045987392**，
全部模型usage结算：输入63506、输出7244、cache read35712；源码/原目标未变。
两官方正文确实取回，机械检查通过；人工仍判 bad：读快照时点仅说“之前某时刻”，
没有明确读事务开始；WAL无界增长解释仍漏持续写入条件，回答1097字符且大量审计说明。
同时拿到页面正文不等于模型看了全文：两段wal的cached text均只交付500字符；
isolation.find的830/1016字符完整块又被规则gate截首尾，部分snapshot条件在中间，
对应receipt是partial。不能靠“已经两来源/全文取回”冒充限定条件被理解。
原件 `runtime_web_sqlite_20261005T093115175754/heldout_web_sqlite_20261005T093115183533/report.json`，
SHA-256 `298c825041774087fa24764726a21037fe86fbb00e4b5ed750280e85878a62a3`。

针对真实缺口追加**注册生产者的声明式短证据保留规则**：ToolSpec可声明严格整数
`output_preview_max_string_chars`，700..1200，默认700。只有实际registry spec可影响
预览，ToolResult.output的同名字段无效；未声明工具/MCP沿用默认。web.find声明1200，
其已确定有界的短块不再二次截掉中间限定句。更长字符串仍首尾摘录，整体序列化
7000字符gate cap、深度/列表边界、授权和回答窗口预算不变；core不识别领域名字。
普通精确原文binding会确认保留下来的cached value complete，而upstream仍unknown。
不保证一次命中等于语义支持、不自动替模型完成进一步网页读取。

追加默认/未知工具、输出伪造、登记字段非法/强转值、整体cap及实际provider完整短块
验证；既有“通用默认gate会丢中间内容”负例仍保留，未改成伪完整。新的provider验收
明确是该registered片段完整交付，不是全文或事实验证；仍无额外审查模型调用。

独立审查补齐两处边界：child冷证据force分支也遵循相同叶子保留上限；只有
observation工具名与ToolResult工具名一致才解析注册spec，否则默认700。新增错配、
force小结果断言，不让其它工具借用扩大预览；未经注册的同名输出字段仍无效。
最终相关默认/gate/provider/child/native/评测探针定向 **63 passed / 7.25s**，
Ruff通过；独立审查未发现上述两处残留。评测源码指纹加入影响行为的ToolSpec文件，
不能因为只改metadata就漏记版本。用户现有tools约束和web权限修改仍分hunk保留。

修复后原任务确认重放 **84.427s / 11 calls / 0 search / $0.076505088**，输入85036、
输出13813、cache read45568；全部模型调用结算，代码/原任务未改。实际取证了wal与
transaction两份官方正文，并主动用既有observation.search回查writer/WAL/remembers。
完整1122字符concurrency窗口进入最终模型prompt，receipt complete/upstream unknown；
大页仍partial，不暗中宣称上游全文被核验。

人工核心语义检查：单写者、**读事务开始时的固定快照**、checkpoint不能越过活跃读者
end mark的条件均有实际来源；不再额外声称“长读必然导致WAL增长”，避免首次重放的
不精确快照时点和未经限定外推。这是原任务的一个有界语义改善样本，并非统计准确率。
仍bad：回答1129字符、冗长引文/审计说明未满足简短目标；WAL日期来自HTTP
Last-Modified，却被叙述为“页面标注”，与正文时间的来源类型未区分好。没有额外
出现WAL增长结论也不证明模型已主动补齐其持续写入条件。日期和必要条件应由任务
核查项/来源表控制，后续不能继续靠领域事实补prompt。SQLite逻辑案例保留open，
Python/版本搜索两个旧语义案例本轮未重放，不顺带关闭。

比首轮更慢、调用/token更多：本轮目的为正确性，没有优化或宣称提速，不改输出预算
来换漂亮指标；不再复跑无变化样本。原件
`runtime_web_sqlite_20261005T094439708579/heldout_web_sqlite_20261005T094439715866/report.json`，
SHA-256 `e452732eb90d18861c94a12dd47b7fcb2d818c850ce862d721d31d8d358e35db`。
本轮付费合计20个DeepSeek Flash调用、1搜索、**$0.12249248**；未使用高开销模型。
共享账本累计模型$6.622662976 / 1167 dispatch（7条历史未知预留）、9搜索。

评测目录增加两个可独立验收的离线回归问题：相邻条件摘录丢失、嵌套元数据导致正文
mapping饥饿；不将这两项工具/harness修复洗成网页语义三题成功。现43逻辑项：
4 baseline / 17 bounded live / 14 offline / 8 open，focus22；基础四项与已确认live项
仍默认跳过，两个新项均已见合成测试，不伪装heldout准确率。

目录引用和自动统计最终 **2 passed / 0.11s**；源码Ruff与diff check通过。
本轮所有提交都排除并保留用户既有工具约束/权限和其它并行开发修改；未重启后端、
未读取或修改真实邮件、未把本地原始prompt/网页快照纳入Git。

## 能力续轮 UX-05：来源角色与任务核查交接

继续暂停开销优化。本轮针对上一现场“HTTP Last-Modified被说成正文标注日期”和
“长篇重复审计说明”接入通用回答工作集，不写SQLite/Python/版本事实或gold到core。

注册生产者声明output-relative路径的来源角色；正文、搜索候选、网络元数据、采集
时间、来源定位符和表示版本分别呈现，来源文本仍不可信。工具输出不能自声明角色，
工具名错配与未知MCP默认unknown。final_answer/原native finish可选附最多8个任务
核查项，每项最多3个真实observation ID/路径/精确可见引用及gap。只在已有decision
中交接，不新增planner/reviewer调用，不为填表增加检索。旧reason-only操作仍有效。

provider拟合后本地核对quote是否仍在实际保留的字符串中；丢失、截断、失败、重复
或错配ID不能得到可见确认。工作集不复制正文，不把可见性/协议成功/角色标签说成
语义蕴含、完整核查或真实原文认证。引用经过cache-search等未声明生产者时仍unknown，
不会靠字段名字猜它源自哪个领域。保留warning-only和原JSON/CSV输出合同，不自动
重写答案；context_answer只增加同一来源/前提校准，不重认证普通会话历史。

可选工作集最多12观察/每项24路径/6000字符；仅检查最近64观察，有界通配只展开数组。
若拟合会挤掉正文、任何会话内容或child回答预留，放弃工作集、继续原安全prompt，
不增大窗口、权限或工具查询预算。native finish改为非strict可选schema，避免启用了
provider严格全属性required协议时拒绝旧reason-only调用；本地仍校验可选notes。

新增19个定向实例（含参数化），正文/HTTP/搜索时间类型、未知工具/伪造角色、重复
与错配ID、消失/截断quote、通配边界/转义、无效声明、最终假provider交付与JSON格式、
取消、可选metadata不能挤掉证据/历史/child预留、旧native调用。
最终七个相关目标 **84 passed / 6.21s**，Ruff/diff check通过；独立Luna只读审查
未发现具体残留问题。假provider证明机制和交付，不证明真实语义准确率。

首轮新fixture遗漏ToolSpec必填type导致12个实例失败，补齐fixture后通过。另一个
原fork_schema测试替身仍只收一个参数，而此前已提交的answer观察预算调用会传
max_chars；该失败不在本轮改动，定向原生schema组运行时明确排除该一项（56 passed /
1 deselected），没有改断言、生产逻辑或假装全套通过。实际语义验收待下面现场记录。

原SQLite任务一次真实重放：**89.177s / 11 calls / 0 search / $0.07960096**；输入
87294、输出14655、cache read47360，全部11调用usage结算。原目标/运行中源码指纹
不变，机械四项通过，两官方正文取回。人工核对单写者、读事务开始时的固定快照、
checkpoint受活跃读者end mark限制仍正确；没有HTTP日期当正文标注，也没有额外
WAL无条件增长结论。本次避免未请求日期，是有界单样本改善，不是日期归因通用准确率。

答案**1104字符**，仍有长引文、机制铺陈和范围说明，不满足本题简短目标，逻辑案例
保留open。不要用比1129略少25字符来宣称简洁性完成，也不宣称本轮提速。
工作集实达最终provider，标记3个来源观察；模型本次没有生成可选answer_checks，
因此只证实来源角色路径，不证明任务核查机制已被真实模型稳定采用。

覆盖人工复核中的一次纠正：初看三个receipt均partial，曾怀疑“Concurrency小节完整
读到”的措辞。但实际observation.read将首缓存文本的**7900–11900**完整送入prompt，
该提取中的小节从**8043到10956**，全部在可见范围内。这句话有局部依据；整页依旧
partial，上游unknown。不能把整页partial等同所有小节partial，也不因核心事实正确
把未读的后半页宣称读过。说明范围不是必要输出时仍应省去冗余审计。

现场还暴露真正的交接缺口：LangGraph的_answer只重建action/reason，会丢弃final
operation中的可选核查项。补齐**仅转交answer_checks**，不增加reason重复或决策
最终prose；旧reason-only路径完全不变。新增实际Graph→最终假provider集成，精确
引用/角色/可见性全部验证。该合成例不等同真实模型主动填表，修复后不再付费复跑
同任务；下一步是有界、可选择的语义/简洁合同检查，而非继续叠加领域或gold提示。

真实原件（Git外、本地私有）
`runtime_web_sqlite_20261005T101455332375/heldout_web_sqlite_20261005T101455341226/report.json`，
SHA-256 `2fd1af35615130a79c89a0e8b1ce66884e23042e206dcf47238d38a28e80428e`。
报告未回写成“语义通过”；最终Graph交接补丁由离线链路另行验收，不把前一版本
付费报告冒充新补丁的真实复测。

本轮新增付费仅上述一次，账本累计 **$6.702263936 / 1178 dispatch / 1171 known**，
7条为历史未知预留；搜索累计9。没有高开销子模型、邮箱操作或backend重启。
新模块从19增至21个实例（另加Graph回归、索引外重复ID）；目录只给原open案例补定向测试入口，
不膨胀逻辑题数、不将offline机制换算模型语义准确率。基础与已确认live题继续跳过。

Graph修复和旧web协议回归的最终组合 **87 passed / 7.88s**，Ruff通过。随后补一项
索引外重复ID负例：观察超过64时不能确认索引内ID唯一，引用保守不确认，仍只检查
角色元数据，不自动继续读源。该模块 **21 passed / 2.70s**；这些测试有重叠，不能
相加报告108个唯一用例。历史裸章节/整页范围的区别按实际原文区间核对，不按partial
标签机械下结论。目录维持43逻辑项（4 baseline / 17 live / 14 offline / 8 open），
focus22，独立回归入口88、独立付费重放任务20，未触发全量重放。
