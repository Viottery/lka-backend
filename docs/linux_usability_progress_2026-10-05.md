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
