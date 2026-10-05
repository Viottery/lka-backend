# Linux 后端真实任务质量测试：收尾报告

收尾时间：2026-10-05 11:57（Asia/Shanghai）。按用户要求停止继续迭代，不再启动新的
真实模型调用、搜索或优化轮次。本文是阶段交付，不是“全部质量 TODO 已完成”的声明。

完整逐轮记录见 [质量迭代记录](quality_iteration_2026-10-05.md)。每轮的失败情境、原因、
局部 TODO、实现、复测和审查均在那里保留；不覆盖原失败报告或将 partial 改判成通过。

## 结论

系统已有可复核的实用能力：小型文件整理和原生代码修复、邮件专家的确定性批量统计和
正文变更归纳、网页官方来源核对、后台记忆学习/撤回，以及后台压缩期间的前台继续运行。
本轮修复了多处真实链路缺陷，而不只是增加“工具是否可调用”的测试。

但复杂多 Agent 审计仍没有达到可靠完成标准；公开多跳检索/QA 没有证明稳定提升；网页
回答仍会丢失限定条件或过长。不能据本轮结果宣布整个系统已经普遍“聪明、快速、可靠”。

最后两个完成补丁只做了收尾离线验证，没有再次付费复测：后台摘要跨前缀来源继承，
以及公开检索评测的显式 query prefix。它们的真实端到端效果仍需将来另行验证。

## 预算与数据边界

权威账本：`data/quality_runs/linux_20261005/budget.sqlite3`，收尾只读核对：

| 项目 | 最终值 |
| --- | ---: |
| 模型派发记录 | 1,078 |
| 已知完整 usage | 1,071 |
| 未知 usage、仍保守计费 | 7 |
| 已知 input tokens（含缓存命中） | 5,845,172 |
| 已知 output tokens | 982,202 |
| 已知 cache-read tokens（input 的子集） | 2,667,776 |
| 已知 usage 按报价计算 | 5.727647616 |
| 未知 usage 保守预留 | 0.424431200 |
| 模型费用保守合计 | **6.152078816 / 50.00** |
| Brave queries | **8 / 3,000** |

全部金额与用户提供的定价、50 预算同单位；没有人民币换算。已用约 12.30%，预算剩余
约 43.8479。7 项未知中 5 项为失败/取消，另 2 项历史记录仍为 reserved，金额分别
0.0761752、0.0584464；并非当前仍有测试执行，不将这些预留清零或冒称实际供应商收费。
账本搜索栏仅记录次数，不记录 Brave 金额；0 不代表搜索免费。这里也不是 Codex 开发
会话或开发子 Agent 的平台账单。

真实模型调用共用持久账本，调用前预留、取消后关闭派发，未知 usage 不按免费处理。
`.env` 原文和识别到的密钥值在模型发送边界被拦截；这不是对任意编码变体的完整 DLP。
真实邮件仅在授权中转接口上测试隔离只读副本，不同步、发送或修改真实邮件。原始邮件、
prompt、trace、数据库和评测原文只留在 gitignored 本地目录；没有提交这些敏感原文。

## 真实场景结果

下表是不同任务的实际样本，不是统一成功率，也不是严格性能 A/B。耗时包括对应运行
链路；仅文本响应的样本没有可靠 TTFT，不能把首个进度事件当作首 token。

| 场景 | 结果与证据 | 关键指标 / 限制 |
| --- | --- | --- |
| 长日志诊断 | 修复协议和首尾预览后找到最后失败、版本与标识 | 19.130s / 5 calls；原 13.001s 是提前放弃，不是更快成功 |
| 原生代码修复 | Agent 修改代码，独立原测试通过，未改测试/安装依赖 | 留出题 25.479s / 10 calls / 4 项原 unittest 通过 |
| 本地文件整理 | 中文/空格路径，按 CSV 真实月份复制；原件及备注保留，副本逐字节正确 | 17.315s / 7 calls / 3 Bash；不是海量目录性能证明 |
| 真实邮件专家批量 | 60 封、30 个发件人分组计数全部正确；实际调用专家 | 25.942s / 5 calls；与通用路径任务/config 不同，不作严格 A/B |
| 邮件正文更新与行动项 | 日期更正、所需材料、工单、已知/未知负责人关系支持；父级重复读取消除 | 40.864s / 7 calls / 819 字；原一轮 54.337s / 9 calls，非稳定提速承诺 |
| 官方网页 SQLite 留出题（最后一轮） | 两官方页面支持三项核心事实，实际 answer prompt 记录 partial delivery receipts | 82.537s / 12 calls / 0 search / 0.064845920；回答 1,368 字，仍有条件遗漏 |
| 现场搜索版本信息（最后一轮） | Brave 搜索后读取官方发布页面，版本/日期/两项变更关系有支持 | 38.999s / 10 calls / 1 search / 0.032340928；旧摘要归因“缓存”及 redirect 说法未取证 |
| 复杂多 Agent 审计（最后一轮） | **失败**；三个 TaskResult 均 partial，六事实主要来自一个子任务，不满足三个独立核查合同 | 131.354s / 26 calls（1 unknown）/ 保守 0.171972032；无文件修改 |
| 六轮记忆学习/召回/撤回 | 完整六轮，10 项检查通过；撤回条目不因晚到来源复活 | 37.000s / 15 calls / 0.027246080；抽取以确定性路径为主，不代表自由模型抽取准确率 |
| 真实后台自动学习与并发 | 两独立用户来源晋升同一偏好，后台 compact 时前台文件读取和生成继续 | 42.191s / 19 calls / 0.037555168；前台 5.989s，后台 compact 7.032s，自然重叠 |
| 同会话摘要后追问与更正 | 旧原文已离开 raw tail，实际请求完整收到发表版摘要；回答保留旧事实并接受新日期 | 总 37.051s / 7 calls / 0.033052；前台 4.975s、5.005s，但第二次摘要 publication 为 local_fallback，原验收仍失败 |
| 后台关注三轮新/不变/变化 | 同一 identity 更新当前地点，旧值留 previous，partial 覆盖警告可见，独立推送会话 | 69.223s / 18 calls / 0.068048288；三轮仍 partial，不代表完整事项覆盖 |

网页两次最终摘要较旧失败有局部改善，但最终 SQLite 一轮与并行评测共用远端账号；
不能把其耗时变化直接归因某个补丁。后台并发证据是短窗口自然重叠，不是长期负载 SLA。
压缩压力 fixture 使用 512 或 4,096，不改变生产 65,536 历史预算，也不认证生产长窗口。

## 检索与 QA 的负面结果

固定 seed、HotpotQA 与 2Wiki 各 25 题，同一组题三模式，文档检索的结果如下：

| 数据集 / 模式 | support recall@10 | 全跳覆盖 | p95 ms |
| --- | ---: | ---: | ---: |
| Hotpot / keyword | 0.94 | 0.88 | 33.27 |
| Hotpot / hybrid | 0.70 | 0.52 | 65.90 |
| Hotpot / hybrid+rerank | 0.94 | 0.88 | 4,294.76 |
| 2Wiki / keyword | 0.68 | 0.40 | 27.88 |
| 2Wiki / hybrid | 0.42 | 0.08 | 62.12 |
| 2Wiki / hybrid+rerank | 0.66 | 0.36 | 3,365.65 |

这套旧报告属于独立 benchmark profile：当时评测没有传入生产配置的 query_prefix。
因此不能直接等同生产检索配置。收尾补丁新增显式 `--query-prefix` 和实际 adapter profile
记录，仍保持旧空默认、不隐式读取配置、不给 passage 加 prefix，也不叠加 query_embed；
未重测上表，不能据补丁推断新准确率。

配对直接 LLM QA 仅 10 题，closed-book EM/F1=0.30/0.33333，retrieval=0.10/0.25139。
前者 4 次 length 失败，后者 5 次弃答；用的是项目 scorer，且不是完整 Agent/query rewrite
链路。已区分缺证据跳、关系推理错误、合法弃答、字面评分误差，未放宽 gold 检查洗白。
两例后来完整 Agent 答案有关系证据，但不足以证明数据集准确率提升。

本机固定输入 ONNX 小诊断中，每 session threads 默认库设置与 4 线程比较，rerank p95
1,376.75→813.85ms，分数/排序/vector 差异为 0。生产默认限制 affinity CPU 上限 4，
保留显式覆盖、lazy 和 local-files-only。这是 CPU 线程竞争优化，不是全链路或准确率保证。

## 已落地的系统性改进

- **测试与账本**：只给目标的隔离 Linux 工作区/SQLite/独立 Git；持久共享计费、真实
  provider dispatch 与本地 cap 拒绝分开；超时取消父子 run、原始证据/hash/源码指纹保留。
- **工具与上下文**：严格完整决策 envelope；不完整 native/SSE 不能当完成或执行授权；
  工具目录分阶段加载，投影冗余服务端字段；长结果原件缓存，可字段投影、字面查找、
  分页、精确分组计数。answer 边界加入实际已交付文本区间 receipts，upstream 仍 unknown。
- **网页与知识**：结构化可读 HTML、分页完整性与同 fetch 原文续读；字面未命中不能
  宣称语义不存在；知识块长值/脱敏扩张支持续读；full authority 与显式来源授权一致，
  参数级权限仍逐次校验。不在 core 加邮件、网页站点或固定工具调用次序。
- **邮件专家**：路由 schema 修复、专家 executor 身份、工作流实际工具审计接线；
  稳定全量分页、准确分组、常规但需行动的事项保留，覆盖不足不能隐去低优先级任务。
- **多 Agent**：按真实 invocation 分类读写；执行前拒绝不能当已写；子生成失败与预算
  收尾保留 PARTIAL；有界公平交付和原件引用；重规划原子恢复，合同变更必须明确降级，
  不用替代节点“成功”冒充原合同覆盖。控制超时有一次预算内恢复，确认后仅同 run/client/
  model 的控制请求复用关闭推理，不推广到答案、后台或新 run。
- **后台与记忆**：硬溢出从前台同步语义压缩移到持久后台 outbox，原文/水位保留；
  summary 与 raw tail 同快照，CAS/revision/lease 拒绝旧结果覆盖；用户纠错/撤回 fence；
  偏好否定与作用域保留，两个真实来源晋升、句号别名不丢来源；关注 identity/当前状态
  协调，不让静态失败或旧值冒充当前事实。收尾补丁继承同会话、已 covered 原文验证过的
  摘要来源，最多 64 IDs / 4,096 bytes，截断和 legacy incomplete 明示，不信模型自报 ID。

关键代码入口：`app/core/agent_turn.py`、`tool_result_gate.py`、`sessions.py`、
`memory_background.py`、`multi_agent_replan.py`、`app/tool_packages/observation*.py`、
`web.py`、`knowledge.py`、`app/experts/mail.py` 和 `app/integrations/onnx_runtime_policy.py`。

## 收尾验证与 Git

本次停止继续迭代后，只检查现有完成补丁，没有再调用付费模型：

- RAG prefix 新旧针对性测试：**17 passed / 38.76s**。
- 摘要 provenance、async context、outbox、retention、硬压缩和 crash recovery：
  **73 passed / 22.68s**。
- 更宽的后台组合：**76 passed / 1 failed / 24.32s**。失败为
  `test_usage_ledger_prunes_only_nonreserved_rows_older_than_90_days`：旧测试裸 INSERT
  11 个值，当前用户未提交的 workload schema 已扩成 15 列。该失败与摘要来源切片无关，
  没有修改用户 quota/schema 工作或静默将它排除为“全绿”。第一次收尾命令曾引用不存在
  的 `test_memory_background.py`，未运行测试；已按实际文件列表更正，不能计作通过。
- 最后完成切片 Ruff 与 diff whitespace 检查通过。没有运行或声称完整全量测试通过。
- 前轮定向回归、真实复测与独立 reviewer 结论见逐轮记录；最后两个完成切片由 Root
  审查及离线复测，不冒称最终版另有独立代码 reviewer 或真实远程闭环。

分支 `quality/linux-realworld-20261005`，起点 `2428baa`；收尾前已有 59 个本任务提交，
随后补 2 个完成补丁和 1 个报告提交，共 62 个。本任务切片逐次精确暂存，未整库 add、
推送、改写历史或撤销用户工作。用户跨平台/消息模块/workload 等修改仍未提交。

重要提交：`087fb4b` 邮件行动交付；`f726ae4` 持久后台硬压缩；`64d8035` ONNX 线程；
`9185561` grounded memory 别名；`fc27f8f` 原合同降级；`6e5019b` 同 run 控制超时；
`94ee940` 交付 receipts/证据限定；`8accd55` knowledge scope；`b3dedeb` benchmark prefix；
`1d6acb8` 摘要 provenance。正式 8765 服务未重启；新代码不保证已加载在旧进程中。

## 未解决问题（停止时保留）

1. **复杂任务效率与独立合同覆盖**：三个 child 提前因下次控制输入+最终交付预留而
   收尾，知识库空检索、发现开销与重复 controller schema 占预算。实际均为 coordinator；
   fork guidance/catalog 每次增加约 562 tokens、10 次累计 5,620。现有 schema 没有
   per-task leaf kind；这只证明开销，不证明换 leaf 就会成功，不能贸然取消灵活 fork。
2. **接受 skip/degrade 后的历史结果交付 P2**：release 已有 partial 却被 canonical
   aggregate 排除，最终错误标成“未取得结果”，降级 note 也未交付。3 RED / 7 GREEN
   复现已留在私有 `data/quality_runs/linux_20261005/degraded_partial_delivery_pending.py`；
   尚未实施生产修复。文件移出默认 tests 收集，原复现保留，不用 xfail 或改断言假装解决。
3. **语义校准和简洁性**：网页“长读者导致 WAL 增长”缺并发写条件，旧搜索片段被
   擅自归因缓存；fetch 全文不等于模型读全文。背景追问正确接受日期更正，但仍对用户
   自述过度要求外部确认。通用证据契约有帮助，尚不能保证模型始终遵从。
4. **多跳检索/QA**：融合与 candidate 截断、语言/model profile、query prefix 与关系
   推理需分别验证；现有档案缺完整候选池/cosine，不能断言融合算法有 bug。新 prefix
   接线未做同题数据集复测，检索指标与 QA/完整 Agent 指标必须继续分开。
5. **长时后台与压缩真实闭环**：最后 provenance 补丁离线通过，原实际 fallback 失败
   保留；尚未再证明连续 model publication，以及生产 65k 历史长期负载和取消竞态 SLA。
6. **当前工作区回归兼容**：上述 workload 15/11 列测试失配仍在；交付没有合并或修复
   用户并行功能。没有在用户 dirty 工作区上声称一个干净 release 的全量结果。

另外更正此前进度中“后续实际控制请求也复用关闭推理”的证据级别：最后实际 run 捕获
了 timeout recovery 的 false 参数及确认事件，但后两次 provider 请求 flag 未归档；
复用是已测试源码路径推断，不是那两次实际 dispatch flag 的直接观测。

## 本地复核入口

- 完整记录：`docs/quality_iteration_2026-10-05.md`。
- 费用账本：`data/quality_runs/linux_20261005/budget.sqlite3`。
- 最后复杂任务：`parallel-quality-20261005T032549765274-4f5d2d98/parallel_audit_20261005T032549766095/`。
- 同会话摘要追问：`runtime_background_followup_20261005T033238312609/`。
- 实际后台学习/并发：`runtime_background_20261005T020635164693/`。
- SQLite 官方证据：`runtime_web_sqlite_20261005T030605621092/heldout_web_sqlite_20261005T030605625773/`。
- 搜索时效留出：`runtime_web_search_release_20261005T030946490153/web_search_release_20261005T030946494790/`。

上述运行目录均位于 `data/quality_runs/linux_20261005/`，各自 raw report/hash 和独立
审查分开保存。私有证据可能含完整模型上下文和真实邮件，不应直接整体发布。
