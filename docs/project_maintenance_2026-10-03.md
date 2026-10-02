# 项目维护记录：2026-10-03

本轮目标：保留近期模块实现，整理 Git 工作区与模块流程，检查可复现 bug、安全边界和
跨模块回归。没有推进新路线图功能、升级依赖、修改个人配置或推送远端。

## 工作区与提交边界

起点为 `5e4b639`（Checkpoint multi-agent and RAG backend work）。初始工作区有
43 个已跟踪文件修改，以及大量新增实现、测试、设计文档和评估脚本；原有内容均作为
用户实现保留。近期功能共同依赖 `runtime.py`、`agent_turn.py`、`local_config.py`、
SQLite 和 API 组装，未强行按文件夹拆成互不完整的功能提交。

近期模块快照为 `f1fad33`（152 个文件）；独立维护提交保存本轮修复/文档收敛。
维护补丁从快照提交中剥离，工作区代码
不回滚。无需把新功能阶段描述和本轮修复混成同一条记录；最终提交 ID 通过 `git log -2`
查看。

本地产物 `:memory:.ses` 和 `app/core/tools.py.orig` 移至被忽略的
`scratch/maintenance-2026-10-03/`，保留内容，不进入提交。
`.env`、`config/local.toml`、`data/`、运行日志和数据库继续排除。
本轮修改/新增文件的凭据模式扫描未发现候选；这是有限模式检查，不等于完整秘密审计。

## 当前实现梳理

统一入口为 [当前模块与运行流程](current_module_flows.md)，包括模块地图、前台对话、
邮件处理、记忆/压缩与每日关注流程。README 和文档导航已对齐；工程指南和路线图中的
“仅最小骨架”标记为早期基线；旧详细架构页保留历史内容并指向当前入口，纠正安全门、
分页与多 Agent 的明显过时状态。API Contract 继续作为接口字段的来源。

最近实现主要集中于：

- 长期记忆、来源版本/冲突/撤回、MEMORY.md 和显式 child memory 引用。
- 抽取/压缩持久队列、租约恢复、取消与重试、后台配置与资源预算、健康/SSE。
- 整体 prompt 预算、指导文件索引/分页、原始工具结果 artifact 与 run-scoped 回读。
- 邮件元数据枚举与可选只读专家，覆盖账本与候选事项对齐。
- 公开网页搜索/文本抓取、每日关注的 occurrence/会话/简报。
- 会话分页/回收站、项目稳定身份/名称 CAS、文件预览和前端默认设置。
- 子任务决策修复、provider-native required tool choice、推理/thinking 控制和实际工具审计。

## 已确认并修复的问题

| 问题 | 触发与影响 | 修复与证据 |
| --- | --- | --- |
| P1 后台远程记忆抽取静默失败 | async 批量适配器内部再次 `asyncio.run()`；异常被抽取层吞掉，实际模型调用为零，任务却完成 | `background_llm.py` 在 `asyncio.to_thread()` 中执行恢复桥，保留 async 接口与 workload context；测试检查真实 async 假客户端调用、证据分区缓存和任务上下文；未自动重放历史已完成空任务 |
| P1 工作区预览/列表符号链接竞态 | 路径校验后父目录被替换；最终文件 `O_NOFOLLOW` 不保护祖先；工作区根先被替换后 resolve 还会将外部目录当新 root | 已绑定 canonical 路径保持一致；POSIX 从 anchor 逐层 `dir_fd/O_DIRECTORY/O_NOFOLLOW` 打开，列表用目录 fd；文本/PDF、根/子目录替换及列表回归 |
| P2 特殊文件阻塞 | FIFO 可在 `os.open(O_RDONLY)` 等待 writer，永远到不了文件类型检查 | `O_NONBLOCK` 后 fstat 只接受普通文件；两个预览入口用带 timeout 的子进程验证拒绝 |
| P2 关注日期更新失败 | 已存 starts_at/ends_at 的关注仅改标题，持久化日期字符串进入 datetime 校验后异常 | `WatchService.update` 统一日期类型；覆盖标题、单侧日期更新及非法范围 |
| P2 短关注指导无法回读 | 抽取摘要遗漏规则，文件不足 4 KiB 没有 next_offset，原逻辑不给读取工具 | 有指导文件即可授权既有只读 instructions 工具；保持摘要策略，覆盖短/长文件 |
| P2 网页慢速响应超时失效及协议处理 | `HTTPResponse.read(size)` 可多次 recv 填满缓冲，逐片重置 socket timeout，绕过循环的总时长检查；HTTP 协议错误也未转换为领域失败 | 改 `read1()` 在片段间复查 deadline；HTTPException 转 WebSearchError；IPv6 Host 保留方括号；测试不联网验证慢速片段、协议错误及 Host 格式 |
| 测试接口适配 | frozen inference profile 测试调用内部 LLM 方法时缺少新增必需 response_mode，后续错误处理又被精简 mock 遮蔽 | 明确传入并断言 response_mode，继续检验冻结 client/model/profile 进入真实请求参数 |
| P2 专家聚合与终态不一致 | 新聚合只接受 ReAct 工具审计，无法识别 Codex 隔离执行；审计未知时还可能产生阻塞回答却将父运行/计划置 completed | 增加有完整性、冻结作用域、snapshot/hash/count、同 run 连续唯一事件核对的隔离工作区审计；仅零变更且外部效果已排除时证明无副作用；缺审计与 staged 写入仍阻塞；pending replan 的 plan/run 均失败 |
| P2 SSE 终态读取竞态 | 读取事件与查询运行状态之间写入末尾事件；当前 stream 提前结束，重连读到额外事件 | terminal 状态后再次读取剩余事件并脱敏发送；确定性时序替身复现，无需概率性延长 sleep |
| 记忆场景测试的即时领取假设 | 完整回归保留的队列任务仍为 queued；宿主 UTC 回拨约 1.25 秒，任务 available_at 晚于当前时间，单次 run_one 合法返回 False | 测试驱动作业采用有界 monotonic 等待；生产 UTC 定时、租约与 deadline 语义保持原样 |
| P2 关注默认预算不足与测试误取结果 | 两封短邮件加前次观察即可在 search/load 后触及 30k token 预算；occurrence 已失败，测试却读取上一份简报，表现为漏新邮件 | 默认有界预算调为 40k，仍保留 6 次 LLM / 8 次工具 / 120 秒上限；测试按 occurrence 精确关联简报；1 token 负例检查执行前拒绝、失败状态且无新简报 |

Codex 审计仅在隔离工作区成功收集完整变更后生成。无 lease、审计缺失/重复/畸形/错关联、
外部效果不能排除时继续 unknown；有 staged 修改仍为 partial 且需审批/应用。
普通事件只新增 snapshot 标识、哈希和计数，不新增原始正文或工作区路径。

## 验证记录

首轮完整主机回归：`988 passed, 2 failed, 2 skipped, 3 xfailed`，415.92 秒。
失败为 Codex 专家只读聚合回归及内部接口测试适配；没有将失败隐藏或改为跳过。
沙箱中的完整测试停滞于异步/线程调度，已停止，以上结果来自经自动审批的主机执行。

后续集成回归暴露 SSE 尾事件竞态，单项偶尔通过；补充四种终态的确定性测试后修复。
收尾时执行会话重建，临时日志与进程句柄丢失，不能据此宣称最后一轮已完成。
完整验证改用仓库忽略目录 `scratch/maintenance-2026-10-03/pytest-final.log`
保留日志，配置/数据同样隔离到测试目录。

该轮完整回归为 `1021 passed, 2 failed, 2 skipped, 3 xfailed`，376.75 秒。
失败分别来自上述 UTC 回拨下的测试假设，以及关注 child 触及默认 token 预算；
后者检查保留数据库后确认 occurrence 已失败，测试错误地读取上一份简报。

目标验证已覆盖：记忆/后台 196 项、邮件/关注/指导/项目/UI/文件十组 89 项、最终文件
权限与关注三组 41 项，以及网页 25 项、inference profile 1 项。各组存在重叠，不能累加
当作独立测试数。时钟/预算修正后，记忆三组 65 项、关注两组 16 项通过。

最终固定代码的完整主机回归：**1024 passed, 2 skipped, 3 xfailed**，370.23 秒，退出码 0。
日志保存在被忽略的 `scratch/maintenance-2026-10-03/pytest-verified.log`。
`compileall app scripts tests`、维护修改的 24 个 Python 文件定向 Ruff、暂存 diff 空白检查
和本轮文档相对链接检查均通过；凭据有限模式扫描无候选，提交不含本地配置/运行数据。

两项 skip 是需显式 `LKA_CODEX_LIVE_BINARY` 的真实 Codex smoke；三项 strict xfail
是已有公共检索改写评测：长问题的 300 字符门及共享实体上下文切分问题，未在本轮改写。

复现完整回归：

```bash
LKA_DATA_DIR="$PWD/scratch/maintenance-2026-10-03/test-runtime-verified" \
LKA_LOCAL_CONFIG="$PWD/scratch/maintenance-2026-10-03/missing.toml" \
  .venv/bin/python -m pytest -q --disable-warnings --tb=short
.venv/bin/python -m compileall -q app scripts tests
git diff --check
```

Ruff 的起点 `5e4b639` 基线为 58 条，整理后当前为 54 条，均属于既有规则问题；本次新增测试
及维护实现的定向 Ruff 通过。修正了近期新增抽取测试的 import 排序，没有批量自动修复
旧代码。全仓 Ruff 尚不通过，不作为已通过检查报告。

## 收敛期的下一步与限制

| 优先级 | 已知边界/后续验收 | 本轮处置 |
| --- | --- | --- |
| P1 | 历史 HTTP API 尚未统一鉴权；CORS 不是认证 | 保留默认 loopback 部署；记录风险，未擅自改变现有客户端合同 |
| P1 | Windows 文件浏览尚无 native reparse handle 的等价竞态保护 | 保留 Windows 现有可用路径逻辑；本轮强化与测试限于 POSIX，Windows 原生验收仍缺失 |
| P1 | SQLite WAL 与 job lease 不等于多进程 graph 执行互斥 | 支持边界仍为单后端进程；未加入多 worker 承诺 |
| P2 | 真实 provider、邮件账户、模型质量与成本 | 本轮仅本地确定性回归；未调用真实 LLM、Brave、Outlook 或 live Codex 验收 |
| P2 | 指导摘要不是全部规则；网页响应头/解析也没有完整端到端硬截止保证 | 保留原文回读；本轮修复 body 片段截止，不声称解决全部网络 slowloris 场景 |
| P2 | 全仓 54 条既有 Ruff 告警、旧详细架构章节仍需逐节核对 | 提供当前统一入口和基线，后续可分批清理，未格式化整个仓库 |
| P2 | watch 领域编排位于 core，与通用 core 领域无关原则有偏差 | 记录分层债，避免本轮变成跨层重构 |

本轮没有执行在线依赖 CVE 审计、渗透测试或全平台验证，不能据此声称项目不存在漏洞。
建议下一轮先完成明确的剩余边界验收，再选择少量新功能；MVP 队列不因本次整理自动
推进 Skill Evolution、日历或其他长期路线图。
