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
