# 跨平台消息阅读后台服务设计

设计日期：2026-10-04。本文是分阶段实施规格，不是已上线功能说明。

目标是在不发送任何平台消息的前提下，帮助用户快速了解大量群聊的主要话题、热点与兴趣内容，
发现可能遗漏的重要信息，并在用户逐项授权后把信息转成 matter。系统应持续运行、可控成本、
可暂停、可解释；不是每收到一条消息就启动一个通用 Agent。

本设计承接 [消息历史 TODO](message_history_todolist.md)。现有接口仍以
[API 合同](api_contract.md) 为准；本文新增的字段、接口和默认参数均需实施后才能使用。
设计交付时未改变配置、迁移真实数据库或调用模型。后续实现已推进 R1–R6 代码与定向离线检查；
2026-10-04 21:40 经用户授权部署 Windows 前后端与新版 Java 桥，三群使用 v2 采集，分析仍关闭。
原消息及媒体 TTL 缓存保留；紧凑摘要编码／分片优化方案尚未实现。实际实现接口以 API 合同为准：阅读路径使用
`/messages/reading/...`，批准采用严格 `action=create/link_existing/reject` union 和独立 CONTROL 凭据。
一天数据评估、真实模型语义与桌面交互验收单独保持待办，不将旧摘要链路冒充新阅读结果。

2026-10-05 的真实数据回放、紧凑输入、可选选择性分析、自动群侧重点及有限活跃人物
画像方案见 [消息阅读算法与人物画像实施方案](message_reading_algorithm_plan.md)。用户已
允许本批三群文本的远程评测，但生产 analysis 仍未开启。该方案是后续设计，不表示现有
ReadingProfile 已具备人物画像；现有关注配置和 API 合同保持原义。

## 1 产品边界与验收目标

用户进入消息中心后，优先回答四个问题：

1. 我离开期间，这些群主要讨论了什么，有什么结论和分歧？
2. 哪些内容值得看原文，哪些只是重复刷屏？
3. 哪些事情明确找我处理，哪些只是可能与我有关？
4. 哪些值得加入事项，我是否同意创建或关联？

| 结果视图 | 判断依据 | 不允许的替代判断 |
| --- | --- | --- |
| 主要话题 | 讨论主题、参与者、结论、分歧、未解决问题、来源范围 | 仅列出现频率最高的词 |
| 热点与有趣内容 | 去重后的参与度、讨论变化、信息新颖性、用户显式兴趣 | 发消息最多的人天然最重要 |
| 重要信息收件箱 | 明确指向本人、行动要求、截止时间、风险与关键变更、用户关注规则 | 热度高就重要；模型推测就是事实 |
| 事项候选 | 有证据的行动建议、可编辑字段、已有事项候选匹配 | 自动创建、自动接受责任、自动标记完成 |

共同要求：每个结果都能回到可访问的原消息，展示处理范围和不确定性；用户可标记已阅、忽略或稍后看。
聊天是第三方陈述，`inbound_only` 不能证明用户尚未回复、已经承诺或完成任务。

非目标：QQ 写操作、联系人扫描、附件下载、OCR、语音识别、全量历史导入、自动回复、自动创建
提醒、默认 embedding、自动写个人长期记忆。本设计不新增通用项目管理或重做多 Agent 调度器。

（补充：2026-10-04 用户另行批准本地图片/视频缓存与 metadata 索引，见消息历史 TODO；
它不等于本设计 R1–R6 的话题/热点/重要性实施完成，也不新增上述写权限。）

## 2 当前基线与已验证缺口

当前已有白名单、平台与账户隔离、不可覆盖的消息记录、三类时间、会话内序号、摘要与信息点、
固定批次、租约、原子发布，以及 `messages` 只读工具包。相关代码：

- [消息领域服务](../app/domains/message_history.py)：历史、策略、批次、事实、作用域。
- [后台分析](../app/core/message_analysis.py)：无工具的模型处理和恢复扫描。
- [共享队列](../app/core/background_jobs.py) 与 [模型预算](../app/core/llm_workloads.py)。
- [消息 API](../app/api/routes/messages.py) 与 [消息工具](../app/tool_packages/messages.py)。
- 前端 `/mnt/d/agent-bot-frontend/app/plugins/qq_reader.py` 和 `message_sources.py`。

2026-10-04 的一次性合成测试得到：310 条唯一消息、25 条重复消息、3 条非白名单消息；
大群 307 条历史分 8 页无遗漏、无重复。默认每 20 条一批，300 条触发 15 次假模型调用，
剩余 7 条待处理；另一个群的 3 条消息虽然包含截止时间，却直到手动处理才进入分析。
这是链路验证，不是语义质量或性能 benchmark，后续须固化可重放样本。

当前缺口：

- 只有数量阈值，没有最短间隔、尾批最长等待和会话间公平调度；完成后会立即串接下一批。
- 有共享 token 限额，没有消息模块与会话配额；费用估算目前使用统一价格而非逐模型价格。
- 全批失败可能重新调用已完成分片，缺少可复用的分片 checkpoint。
- 只有 summary/facts，没有稳定话题、亮点、重要性、个人相关性和阅读状态。
- QQ 规范化只保留文本，结构化 `at`、`reply` 信息会丢失。
- `/background/config` 当前不包括 `message_history`。
- matter 尚无消息候选审批事务；现有 `skip`／LLM 审查不等于人工同意。

## 3 总体架构与职责

```text
QQ 或其他平台的本地只读适配器
  → 白名单与采集队列 → 通用消息导入 → SQLite 原始历史
                                         ├→ 本地信号扫描 → 待核实的重要信息
                                         └→ 有预算的增量分析
                                              ├→ 话题与来源
                                              ├→ 热点与兴趣亮点
                                              ├→ 重要信息与事项候选
                                              └→ 低频会话简报
所有读取结果 → 前端消息中心／Agent 只读工具
事项候选 → 用户预览与逐项决定 → 授权事务 → 新建或关联 matter
```

消息阅读是一个后台服务，复用现有 durable job store、LLMService、预算和健康观察；
不为每条消息建立 Agent Run，不为每个群长期保留 ReAct Agent，不默认 fork。
用户临时追问仍走通用 Agent turn，读取已有结果及必要原文；读取本身不触发无限回填分析。

职责分层：

- 前端本地适配器：连接平台、规范化、白名单前置、可靠确认、离线缓存；不调用分析模型。
- `app/domains/message_history.py`：继续保存原文和权限；不调用 LLM。
- 拟新增 `app/domains/message_reading.py`：确定性的调度资格、话题归并校验、评分、发布、阅读状态。
- 拟新增 `app/domains/message_matter_proposals.py`：候选、批准校验、原子应用与审计。
- 拟新增 `app/tool_packages/message_reading/` 内部处理模块：领域 prompt、分析 schema、
  有界模型工作流；它不是一组后台可调用的写工具。
- `app/core/message_analysis.py` 收敛为队列和 LLM 执行适配，现有领域 prompt 随本功能迁入
  上述模块；通用 Agent prompt 不加入 QQ、matter 路由特判。
- `app/core/runtime.py` 只负责组装和注册。继续使用 SQLite，不引入 Redis、Celery 或向量库。

## 4 平台协议与消息身份

### 4.1 通用导入第二版

保留现有身份：`platform + account_id + provider_message_id` 去重，
`platform + account_id + conversation_type + conversation_id` 确定会话。
内部 ID 由服务端生成，昵称不得作为身份。将 QQ 的 `self_id` 映射为 account_id。

新增外层 `schema_version=2`，消息行保留 v1 业务字段，并增加有界 metadata：

| 字段 | 语义 |
| --- | --- |
| `capture_epoch` | 本次记录许可代次，导入时须匹配当前已开启的许可 |
| `mentions` | 平台原生 mention 对象，包含 user ID 或 `all`，不混入昵称推断 |
| `reply_to_message_id` | 同平台同账户同会话的外部引用；解析不到时保留 unresolved |
| `thread_id` | 平台提供时保存，不推断不存在的线程 |
| `content_parts` | text／mention／reply／unsupported 的有界顺序信息；不下载媒体 |
| `adapter_id`、`adapter_version` | 规范化来源，可审计但不授予权限 |
| `metadata_capabilities` | 明确支持、未提供、未知，避免空数组被误读为“肯定没有 @” |

正文、段数、引用数和批次字节均有上限。采用类型化允许列表，不接收任意平台 API payload。
同一 provider ID 内容冲突不覆盖旧行；编辑/撤回事件不在首版消费范围，必须在 coverage 标明。
同内容不同 ID 是不同消息，只能在分析视图折叠，不能作为传输重复删除。

### 4.2 策略版本拆分

保留 `policy.revision` 作为整个配置对象的 CAS 版本，另增加：

- `capture_epoch`：仅记录许可启停、账户或会话权限变化时增加；防止撤销后旧离线消息重新放行。
- `analysis_epoch`：分析许可、内容外发许可变化时增加，立即使旧执行和发布失效。
- `proposals_epoch`：事项候选许可启停时增加，防止重新开启后复用旧批准预览。
- `processing_revision`：影响结果语义的 schema、prompt、模型选择、关注配置版本；写入分析快照。
- `schedule_revision`：条数、间隔、配额调整；重新计算调度，不丢弃已经批准采集的消息。

不能再因为用户只调了批量大小，就把采集队列中所有旧 revision 的消息隔离。
前后端提交时使用真实 capture_epoch，禁止把旧离线队列批量重标为当前许可代次。

新会话默认 record、analysis、proposals 全部关闭。用户开启记录时明确展示默认启用的本地
规则扫描，可单独关闭；它不外发内容。模型分析和事项候选分别 opt-in，开启候选不代表批准事项。

v1 接口与历史读取保持兼容，缺少 metadata 的旧消息标注 unknown，不反向伪造 mention。
新服务的完整许可代次与 mention 保证要求 v2 适配器。迁移前旧 reader 维持既有功能和明确的
降级提示；不声称 v1 已具备新保证，不自动回填旧 QQ 数据。切换 v2 后的旧待发数据默认隔离，
由用户显式选择是否进行有范围的补导入，而非自动改写许可。
每会话记录 `minimum_import_version`；启用 v2 保证后，通用和 QQ 兼容导入路径都拒绝该会话的
v1 写入，不能让旧入口绕过 capture_epoch。v1 读取兼容与尚未升级会话的导入不受影响。

### 4.3 时间与捕获覆盖

- `sent_at` 是平台发送时间，`received_at` 是适配器观察时间，`ingested_at` 是后端提交时间。
- 序号只描述已入库顺序；处理游标按 seq，话题事件时间使用有效 sent_at。
- 调度年龄从首条未处理消息的后端 ingested_at 计算，避免平台伪造未来时间影响队列。
- 相对日期基于原消息发送时间和会话时区；无效时间、时区不明、DST 歧义都不自动生成确定期限。
- 适配器状态记录断连、队列满、最后成功同步；没有平台重放能力时，缺口数量为 unknown。
  “所有本地消息已处理”不等于“群里所有消息均已捕获”。

## 5 三层处理与结果结构

### 5.1 本地信号扫描

每条唯一消息提交后，触发不调用 LLM 的本地规则扫描，并由独立 `local_signal_seq` 保证恢复。
提交与扫描之间进程退出时，恢复扫描补齐；白名单和本地识别开关同样适用。

可用信号包括：可信平台 metadata 中的本人 mention、全体 mention、用户确认的关注人／词、
明显行动或期限措辞、明确更正词。昵称和正文中的“@某某”仅为推断。
由于目前只收 inbound，不能可靠发现用户发出消息的全部回复链，reply 解析失败不跨会话搜索。

规则输出是 `needs_review` 候选，附 reason_code、原消息和 `detector=local_rule`；
“@本人”证明相关性，不证明紧急、不证明消息真实，也不构成执行授权。
结构化本人 mention 即使正文为空也应可显示，而不是归为无价值非文本。

本地扫描不推进 LLM 的 covered_seq，不创建 matter、不发 QQ 消息。
即使模型预算耗尽，也能展示本地候选和未处理积压；它不能保证发现所有语义上的重要信息。

### 5.2 增量语义分析

普通批次一次模型输出同时覆盖话题增量、亮点、重要性和信息点，避免为每个视图分别调用模型。
输入只含本次固定范围原文、有界已知话题／相关证据、当前用户明确关注配置，不附全部历史摘要。
输出为严格 schema，不允许工具调用、外部访问或修改服务策略。

```text
AnalysisOutput
  schema_version
  topic_updates[]       existing_topic_id 或 batch_local_key；摘要与证据分配
  highlights[]          useful／interesting／decision／question 等带来源的亮点
  importance_findings[] 指向谁、什么要求、证据等级、期限解释、原因
  facts[]               沿用事实／决定／更正等类型
  warnings[]            来源不足、日期歧义、附件未知等
```

模型只能引用本次提供的 message IDs 和已授予的 topic IDs。新持久 ID、热度统计、权限、
有效时间和 state 均由服务端确定。每个输出数组、字符串、引用数量均有上限；输出溢出视为
可诊断失败或缩小批次重试，不能截掉后半段后仍宣称全量处理成功。

### 5.3 低频简报

普通批次不再每次重写整段滚动长摘要。简报按较低频率消费已发布的 topic revisions、重要项
和覆盖区间，默认每小时至多一次且仅在内容有变化时生成。无变化直接复用，不调用 LLM。

简报包含主要话题、值得阅读的亮点、重要信息入口、未处理数量与最早积压时间。
简报不能将旧摘要再次当成无来源事实：保留 topic → batch → message 引用链；必要时有界回读原文。
出现关键更正后，旧简报立即标记 stale；即时显示更新后的信息卡，不必等下一次简报重写。
日视图默认用确定性模板组合话题与重要项；额外润色只在用户开启后占用独立预算。

## 6 话题 热点 兴趣与重要性

### 6.1 话题的稳定身份

话题包含 `topic_id/revision/conversation_key/title/summary/status/first_seen/last_seen`、
参与人数、去重后消息数、主要结论、分歧、待解问题、代表证据及完整来源分页入口。
同一消息可关联少量话题，但计数必须按消息去重；单条高价值信息也可成为独立话题。

候选匹配只在同会话、已授权的有界活动话题中进行，结合原生 thread/reply、关键词与模型建议。
服务端验证 existing_topic_id；重命名不换 ID，合并保留 alias 与审计，拆分保留 parent 关系。
无法可靠匹配时宁可暂时分开并标记可能重复，不跨群或跨账户自动合并。
活动话题默认取最近 24 小时至多 20 个，溢出项通过本地检索补少量候选；必须记录候选范围受限，
这不是历史话题删除。超过活动期变为 cooled，有新证据可恢复；不据此推断事件已完成。

### 6.2 热度是观察统计

第一版采用可解释的确定性排序，不让模型编造参与量。统计窗默认为最近 24 小时，
展示原始量、重复折叠量、独立参与者、最后活动时间及 `score_version`。

初始归一化热度可采用：

```text
heat = freshness × (0.45 × participant_score
                  + 0.35 × unique_message_score
                  + 0.20 × discussion_burst_score)
```

各分量归一化到 0..1，计数采用饱和／对数变换、单人贡献上限；突增以该群近期基线计算，
基线不足时标明不可比较。系数是待标注样本校准的初值，不是经验结论。
相同短句折叠只影响展示和热度；带新 mention、期限、否定、更正的文本不能因相似就被折叠掉。
群之间规模不同，默认先群内排名再多样化混排，不将大群热度直接压过小群信息。

### 6.3 兴趣与个人相关性

用户可显式配置关注主题、项目词、重要联系人、排除词和群级偏好。配置带 revision、作用域，
用户可查看和撤销；默认为空。未配置时使用“值得一看”，不宣称模型了解用户兴趣。

兴趣匹配给出独立的 `interest_reason`；热点、兴趣和重要性不压成一个不可解释总分。
“有帮助／无关／不感兴趣”是本模块本地反馈，默认只改变本模块偏好，不写长期记忆。
基于反馈的自动调整另设 opt-in，并保留恢复默认与版本记录。

### 6.4 重要信息分级

重要性、证据确定性、阅读状态是三个独立维度：

- `importance=critical/important/possible/ordinary`；critical 默认只由用户显式规则或确认提升。
- `certainty=explicit/inferred/needs_review`，explicit 指“原文明确说过”，不保证现实事实真实。
- `reason_codes` 如 direct_mention、action_requested、deadline、material_change、tracked_topic。
- `directed_to=self/group/other/unknown`；“@全体”不自动变成“我已被单独指派”。
- 日期包含 `due_at` 可空、原文 `time_text`、timezone、证据 ID 和
  `due_provenance=explicit/relative_resolved/inferred/unknown`。

显示原作者、准确引用、解释和置信等级。日期歧义或推测默认不填 matter 的确定 due_at，
需要用户选择；不能用入库日期解析“明天”。同一事件更正更新同一重要项的内容 revision，
保留旧证据；不能仅凭第三方“做完了”自动完成事项。

重要项不会因热度降温消失，也不会因用户看过摘要自动标为已处理。已阅后收到实质更正，
显示“有新变化”；不为标点、排名或摘要措辞变化重复通知。
本地规则项与模型项优先按同一证据／行动键关联，保留各 detector 的解释，不把一次 @ 生成两张卡。
模型可补充语义或标记误报，但不能抹除真实 mention metadata；确有不同事项时保留独立项。
归并、误报和更正都留 revision，不删除原文；用户关闭某条关注规则不会自动改写历史已阅记录。

## 7 调度与预算

### 7.1 触发合同

每个会话持久化 `pending_since/last_dispatch_at/next_due_at`，pending_since 指当前首条
未处理消息的 ingested_at，后续消息到达不得把它不断向后推。时间持久化用 UTC，UI 使用
会话时区；进程内等待可用 monotonic，但不能把 monotonic 值写为重启后的时刻。

普通批次可进入调度，当且仅当：

```text
pending > 0
AND recording 与 analysis 已开启
AND 服务未暂停且无本会话活动分析
AND now >= last_dispatch_at + min_interval
AND (pending >= trigger_count OR now >= pending_since + max_wait)
AND 预算预留成功
```

`max_wait >= min_interval`。人工“立即处理”只绕过数量和普通间隔，不绕过权限、暂停、
预算、来源校验或并发限制。预算不足时明确返回 deferred，不显示“已分析”。
手动重试不是重新创建一个免费 job；重试、恢复调用计入原工作范围及累计配额。

完成一批后只更新调度意图，不立即无条件排下一批。采用有界 due 扫描、会话轮转与等待年龄
加权；每轮每会话最多进行一次 provider 调用或推进一个本地输入单元，然后持久化让出。
格式恢复也要重新参加调度，不在一个 handler 内占住槽位连续调用。同一时刻一个会话最多一个固定范围工作。
高优先级也有占比上限，不能让大群、频繁 @ 或不断重试的群占满 worker。

`min_interval/last_dispatch_at` 约束新工作范围的启动；已经建立的工作从 checkpoint 续跑时
按公平队列和配额执行，不重新等待一次完整 min_interval，也不创建新范围绕过该间隔。

### 7.2 建议初始档位

以下是设计初始值，实施时可调，不是现有配置默认，也不是延迟保证。

| 档位 | 数量触发 | 最短间隔 | 尾批等待目标 | 适用 |
| --- | --- | --- | --- | --- |
| 均衡 | 50 条 | 5 分钟 | 15 分钟 | 新启用会话的建议档位 |
| 高频群 | 100 条 | 10 分钟 | 30 分钟 | 大量闲聊、主要看话题与简报 |
| 重点群 | 20 条 | 2 分钟 | 5 分钟 | 小群、行动信息较多 |
| 手动 | 不自动触发 | 不适用 | 不适用 | 只记录和本地规则标记 |

触发阈值不是固定处理大小。每批从 covered_seq+1 选连续前缀，最多 200 条，同时受完整
prompt 输入预算限制；现有上限 100 需显式升级 schema。长消息拆分并记录 fragment cursor，
整条完成前不越过该消息推进连续水位。
冻结范围前按所有分片的最坏输入／输出预留核对单工作额度，并留出一次恢复调用余量；
容纳不下时缩小连续前缀，不先选满 200 条再假定 4 次调用足够。单条长消息仍超工作限额时
明确为 input_too_large，允许人工提高该工作额度或改用支持的模型，不静默截断正文。

本地信号扫描目标是在后端入库后数秒内可见；不是 NapCat 端到端承诺。语义尾批等待还受到
扫描周期、队列、模型速度、失败和预算影响，UI 必须显示目标是否已超出及具体原因。

### 7.3 预算层级与初值

所有调用同时满足：全局后台限额、消息服务限额、会话限额、单工作范围累计限额以及模型容量。
沿用已有前台优先、provider cooldown 和 token 预留账本；不通过新建另一个不记账客户端绕过。
这些层级以同一预留 ID 做原子准入，任一失败都释放尚未发出的预留；不能先分别检查再并发超售。
provider 请求不在数据库事务内等待。客户端内部重试也必须计入实际调用数，不能绕过调度器的上限。

建议保守初值：

- worker=1；完整输入至多 8192 tokens，普通输出 2048，恢复输出至多 4096。
- 单固定工作累计 32768 tokens，至多 4 次实际 provider 调用，含重试与一次格式／截断恢复。
- 消息服务滚动 1h/24h token 子限额为 40000/200000；实际调用次数上限为 8/48。
- 单会话滚动 1h/24h token 上限为 20000/80000；调用次数上限为 4/24。
- 低频简报和可选语义紧急复核共享上述总额度，不额外获得无限配额；语义紧急复核默认关闭。

完整输入预算包括系统指令、schema、旧话题、引用、关注配置和新原文；
当前 `input_chunk_bytes` 只是原消息数组大小，不能继续被当作完整 prompt 的成本上限。
按配置 tokenizer 或既有保守估算预留输入与最大输出；provider usage 返回后结算。
超时且可能已计费的调用保守计入，不因重启、点击重试、改批量或替换 job_id 清零。
所有替代 job 关联稳定的 `work_family_id`；调用次数和累计用量按 family 记账，缩小分片不是
成本豁免。累计额度耗尽进入 `failed/work_budget_exhausted`，不能靠普通 retry 恢复。
用户可在控制面板明确提高原 family 的工作上限后续跑，保留原账本；全局、服务与会话限额仍有效。
滚动窗口配额不足则是可恢复的 budget_deferred，不与工作总额度耗尽混为一谈。
多模型价格使用 `(client, model, pricing_revision)`；未配置价格时显示“费用未知”，不能显示 0 元。
金额硬限额开启但所选模型无价格时拒绝该模型的后台调用；token 限额独立继续生效。

仅加大间隔不能证明成本受控：若一次调用输入 8000、输出 2000 tokens，30 次就是 30 万
tokens，已超过当前共享每小时 20 万的默认限额。另一方面，假定每条平均需要 80 个输入
tokens，5000 条／天仅原文就约 40 万，尚未计提示与输出；20 万的子预算无法保证当天追平。
这些是容量算例，不是实测费用。系统应提示调档位、提高预算或缩小白名单，不能靠漏分析伪装追平。

## 8 工作状态 一致性与恢复

原始消息即时入库，后台记录只是派生工作。不同水位不得混用：

- `latest_seq`：已存历史末尾。
- `local_signal_seq`：本地规则连续扫描范围。
- `analysis_covered_seq`：语义结果完整、连续、已发布的范围。
- `digest_covered_seq`：当前简报引用的语义结果水位。
- `user_viewed_revision`：用户实际看过的结果版本，不是消息平台的已读回执。

沿用队列 `queued/running/retry_wait/succeeded/failed/cancelled`；
paused、budget_deferred、awaiting_input、blocked_by_batch 是服务调度解释，不伪造另一套队列终态。
“服务暂停”继续保存白名单消息但停止新模型调用，已发出的 HTTP 可能仍计费；暂停增加 service_epoch，
阻止旧调用发布或写入新 checkpoint。本地规则扫描继续；用户恢复后重用合法 checkpoint，
不把 pause 当作分析失败或删除历史。暂停不是撤销许可，仍允许人工查看、接受或拒绝已发布候选，
但必须通过第 10 节的当前许可和精确预览校验。

分片成功后使用 lease owner/epoch 校验的 yield 事务：保存 checkpoint 和下一 cursor，
将同一个 job 从 running 改回 queued 并设置下次领取条件，释放 lease 和模型槽，保留
`active_work_id`。让出不是 succeeded，也不增加失败重试次数。需扩展现有 worker 协议，
不能直接从当前 handler 返回并被其自动标为完成。进程崩溃则依现有过期租约恢复机制重新领取。

固定工作快照包含会话、seq 范围、输入摘要哈希、capture/analysis epoch、processing revision、
prompt/schema/client/model/profile 版本。调度参数改变不改正在执行的输入内容；语义策略改变
使旧任务停止发布，新 revision 从未覆盖处重建，旧预算仍计账。
`service_epoch` 只用于执行和发布 fencing，不属于语义 checkpoint 的缓存键；恢复使用新 lease
和当前 service_epoch，并重验全部内容许可。撤销分析或改变 processing revision 会使旧 checkpoint
失效，单纯暂停不会让暂停前已经合法落盘的结果失效。

分片 checkpoint 保存在受保护派生存储中，不进入队列 payload。保存已验证输出、输入哈希、
有界 staged accumulator、稳定的 batch-local-key 映射及 next chunk/fragment cursor。
工作开始时冻结已知话题快照；后续分片可引用本工作已暂存的新话题 key，只有最终发布才分配
持久 ID。后续输入哈希包含前序 checkpoint 版本；不匹配的后缀不得复用。完整分片输出单独保存，
accumulator 达上限时压缩的是下次输入视图，不是删除证据或未发布结果，也不偷偷追加模型调用。
队列只含 IDs 和范围；恢复必须匹配内容输入与语义配置版本。一次成功 provider 调用在 checkpoint
提交前崩溃仍可能重做并计费，因此承诺“幂等发布”，不承诺外部调用 exactly once。

发布使用同一 SQLite `BEGIN IMMEDIATE` 事务：

1. 检查 job owner/epoch/expiry、服务与会话许可代次、输入水位及预期话题版本。
2. 验证所有新引用来自提供给该工作的授权消息；旧引用只能来自明确输入的已发布证据。
3. 插入批次、事实、话题 revision、亮点、重要项与候选；按 source/action fingerprint 去重。
4. 更新连续水位、将 job 标为 succeeded，并保存后续调度意图；一次提交。

失败不得发布半份结果并推进全范围水位。网络故障指数退避、遵循 provider Retry-After；
认证／非法 schema／证据错误等到达限定次数后停止，显示错误分类而不记录聊天正文。
最前方失败批次会阻塞该会话后续语义水位，但不能阻塞其他会话或本地信号扫描。
允许用户重试或以更小分片重建同范围工作，保留累计用量与 supersedes 关系；首版不提供
“直接跳过后假装完整”。未来若允许跳过，必须新增 gap ledger 和部分覆盖语义。
更小分片只能在 family 余额足够时执行，额度耗尽按第 7 节处理；重建分片后仅复用输入与前序链
完全匹配的 checkpoint，不能拿旧整片输出冒充新子片结果。

## 9 数据模型与索引

保留已有 message_history 表，新增以下逻辑记录。实施时可将相同生命周期的小记录合表，
但不能把权限、用户状态或批准隐藏在无校验 metadata 里。

| 记录 | 核心字段与约束 |
| --- | --- |
| ReadingPolicy | 会话身份、record/local_signals/analysis/proposals 开关、档位、各 epoch/revision、模型与内容外发范围 |
| ReadingProfile | 本人平台 ID、显式别名／关注项／联系人、排除规则、作用域、revision；不自动扫描联系人 |
| ReadingSchedule | 会话 PK、pending_since、last_dispatch_at、next_due_at、active_work_id、revision |
| AnalysisGeneration | 会话、pipeline_version、baseline_start_seq、连续覆盖水位、旧范围覆盖类型 |
| AnalysisWorkFamily | 固定范围、替代 job 关系、累计调用／token 用量、人工批准的工作上限、revision |
| AnalysisCheckpoint | work_id + chunk_index + input_digest 唯一、前序版本、输出、staged accumulator、local-key 映射、cursor、用量引用 |
| Topic 与 TopicRevision | 稳定 topic_id、会话、revision、结构化结论／分歧／问题、状态、处理范围 |
| MessageInsight | insight_id、topic 可空、kind、重要性／相关性、原因、证据等级、内容 revision、dedup_key |
| DerivedSource | object_kind/object_id/revision/message_id、引用片段范围、用途；唯一复合键，支持分页 |
| ReadingAttention | 本地用户 + insight_id 唯一、viewed_revision、dismissed_revision、snoozed_until、CAS revision |
| ReadingDigest | 会话或明确授权集合、版本、生成时间、输入 topic revisions、覆盖范围、是否 stale |
| MatterProposal | proposal_id、revision、action_key、候选内容、证据摘要、许可代次、状态、冻结原因、被替代版本、结果 matter_id |
| ProposalDecision | decision_id、proposal revision、normalized payload digest、可信 principal、决定、时间 |
| ProposalApplication | proposal_id 唯一、decision_id 唯一、操作、matter_id、目标前后 revision；幂等回执 |
| ServiceControl | service=message_reading、paused、service_epoch、revision、更新时间 |

用户阅读状态不能被模型重写。`unseen/seen/dismissed` 按当前内容 revision 派生，snoozed 是暂时
隐藏直到指定时刻；新实质证据增加内容 revision，使旧已阅状态不遮住变更。
重要项与 proposal 是两个对象：忽略重要项不等于拒绝事项，拒绝事项也不删除重要消息。

建议索引：消息 `(conversation_key,seq)`、`(conversation_key,received_at,seq)`；
调度 `(next_due_at,conversation_key)`；话题 `(conversation_key,last_seen,topic_id)`；
重要项 `(conversation_key,importance,updated_at,insight_id)`；
DerivedSource 的 object 与 message 反向索引；候选 `(state,updated_at,proposal_id)`。
新 feed 使用稳定 keyset cursor，包含排序值、ID 和过滤参数摘要；有版本变化时刷新，不仅靠 offset。
统计和分页在 SQL 端完成，不能先把所有会话／所有消息拉入 Python 再切片。

## 10 事项候选与强制人工授权

### 10.1 状态与内容

Proposal 状态为 `pending/accepted/rejected/superseded/revoked`。
一次批准只支持 `create` 或 `link_existing`，关联已有事项默认只添加来源，不修改其标题、状态或期限。
需要修改已确认事项时另建明确的字段变更提案，不利用新消息后台覆盖既有任务。
现有 matter 没有可靠整数 revision，需为批准目标增加 revision 并在所有相关 mutation 中递增。

候选至少展示：原群与作者、准确引用、建议标题／摘要／优先级／标签、期限及其证据、是否推测、
可能重复的已有事项、所选操作和目标。相似标题仅用于建议，不自动合并。
首次匹配已有 matter 默认使用确定性本地检索，不把其他事项正文送给模型；只有用户显式授予
相应账户／项目范围后才可供有界模型匹配。

同一证据与行动重试复用候选；新的明确更正使 pending 候选变成新 revision，旧预览失效。
accepted/rejected 不因重复分析重新 pending。确有新行动或变更时建立带关联的新提案。
重复候选识别不能只用“标题相同”或单条 message ID，因为一条消息可能包含多个行动。

### 10.2 身份与权限边界

至少区分 importer、reading_worker、agent_reader、human_control 四种服务端可信身份。
identity 不接受请求体 `decided_by=user`、模型输出 `approved=true` 或任意 Header 自报。

- importer 只能提交消息和读取采集所需的策略元数据，不读正文、不配置服务、不审批。
- reading_worker 只能读取授权输入并发布派生候选，没有 matter 工具、shell、HTTP 或任意文件能力。
- agent_reader 只能通过消息只读工具获取结果；创建提案不等于批准，不能调用批准接口。
- human_control 从已配对的本地 UI 发起真实用户动作，批准接口必须认证，不能在无 token 时退回
  “只要 loopback 就是用户”。管理凭据与导入凭据分开。

批准的可信性来自受保护控制通道及 UI 的用户动作，而不是 LLM 自述。管理凭据不得进入 prompt、
工具输入输出、Agent 子进程环境、运行日志或 Agent 可读文件；当前 bash 继承整个 os.environ
的行为需针对控制凭据做隔离。前端只在可信点击处理器发起批准，不执行聊天中的脚本或链接动作。

### 10.3 防止绕开候选队列

仅给新接口加鉴权不够。所有识别为消息来源的 create/create_many/link/update 都在确定性领域层
校验 human receipt，HTTP 与工具共用校验；root scope 不构成批准。添加规范的
`source_type=message_history_message`，解析真实内部消息 ID 和当前权限，不接受伪造别名。

仅检查 source_links 也不够，模型可能省略来源再复制消息文本。专用后台不具备写能力；
普通 Agent 消费消息工具输出后，由可信 registry/runtime 记录来源约束，并随父子 Run、
session 历史、摘要、artifact 传播，不允许模型清除。拟增加通用的 effect-domain 最低审查策略：
消息来源上下文的 matter mutation 最低为 human，不能被配置 skip 或 LLM 批准降级。
具体 effect-domain 和来源策略在工具包元数据注册，core 只执行通用约束。
此处 human 不是普通工具的 manual safety review：即使省略 source_links，也必须生成或定位
事项候选，由同一人工预览与 decision 事务形成绑定 proposal revision、实际字段和证据的 receipt。
普通工具批准记录不能替代该 receipt；Agent 收到的是候选入口和等待用户决定，而不是直接写入。

带该约束的运行还不能通过 shell、任意 HTTP 或外部执行器绕行：这些非受限执行通道须人工确认，
控制凭据与存储目录不在其可读取范围。必须覆盖 Tool Executor 直调路径，不只在 Agent Loop
里提示模型“请询问”。已有纯邮件／人工 matter 流程不全局改成手动，仅对新消息来源约束生效。
放行某条 shell/HTTP 命令也不授予 human_control 身份或消息事项写权限；无法隔离受保护存储与
批准凭据的执行通道，不得在携带该来源约束的运行中作为事项应用旁路开放。

这仍不是对同一 OS 用户任意进程的防篡改保证。拥有任意 bash、SQLite 文件或控制凭据的本机
所有者可以绕过进程内规则。严格防恶意本机 Agent 需要独立进程／OS 身份与权限隔离；
在完成此隔离前，安全承诺限定为受控工具、后台工作流与可信 UI，不宣称全系统不可绕过。
这一限制必须在安全说明中公开，而不是通过增加一个 `requires_confirmation` 布尔值掩盖。

### 10.4 批准与应用的单事务

预览给出 proposal revision、证据 digest、操作及目标 revision。用户编辑后的实际内容必须完整
显示在确认页；提交精确的 reviewed payload，而不是批准可随时变化的“最新版本”。

`POST /messages/matter-proposals/{id}/decision` 的设计请求：

```json
{
  "decision_id": "由客户端生成的幂等键",
  "expected_revision": 3,
  "decision": "accept",
  "operation": "create",
  "reviewed": {
    "title": "确认项目发布方案",
    "summary": "核对方案后向负责人确认。",
    "priority": "high",
    "due_at": null,
    "tags": ["项目"]
  },
  "target_matter_id": null,
  "expected_target_revision": null,
  "evidence_digest": "预览返回的摘要"
}
```

请求使用按 decision/operation 区分的严格 schema。`link_existing` 必须提供目标及预期 revision，
`reviewed` 只接受有界来源说明等实际会保存的关联字段，不接受被忽略的 title/summary/due_at；
UI 切换操作后展示真正的写入效果。reject 不接受创建／关联载荷，仅更新候选与决定记录。

幂等摘要包含可信 principal、proposal ID/revision、decision、operation、全部 reviewed 字段、
目标和 evidence digest。认证和可见权限校验先于回执返回；不能把旧预览作为永久可重放响应。
领域服务在同一个数据库连接和 `BEGIN IMMEDIATE` 内执行：

1. 检查 decision_id 回执；同键同内容返回原结果，同键异内容返回 409。
2. 检查 pending 状态、proposal revision、证据 digest、当前记录／分析／候选许可及其代次、目标权限和 revision。
3. 验证用户编辑字段及期限，解析来源，检查同 proposal 是否已应用；模糊相似只提示，不自动改目标。
4. 写入 matter 或来源关联、FTS、批准审计、应用回执及 proposal accepted；一次提交。

现有 MatterService 会自行创建连接与 commit，必须先增加窄的 caller-owned transaction 路径，
才能满足上述原子性；不能外层开事务再调用当前 create_matter 假装原子。
不向 Agent 暴露可复用批准 token，不存在“先 accepted 后异步创建失败”的中间成功状态。
并发批准最终只有一个 application；响应丢失后按 decision_id 取同一 matter，不能再次创建。
目标事项的字段更新、来源增删及删除都参与同一整数 revision。目标已变或已删除返回 409，
保持 proposal pending，要求重新预览；不能自动换目标或降级为 create。对当前无权访问的
来源／目标返回 opaque 404；有权限但证据、版本或许可代次变化返回 409。
回执重放不再应用写入，也必须重新检查当前权限；来源已撤销时可返回无原文的应用状态及仍有权
访问的独立 matter 引用，否则返回 opaque 404，不泄露原候选、标题或证据。
拒绝只修改候选状态；用户忽略、阅读或设为稍后看都不是批准。首版不提供一键自动批准全部。

### 10.5 撤销与已确认事项

候选绑定 capture/analysis/proposals 许可代次，许可开关和普通 service pause 必须区别处理。
关闭记录立即阻止新导入、源读取、后台发布及 pending 候选接受，但保留历史；pending 变为
revoked，记录重新开启也不自动恢复。审批 API 不暴露被撤销来源的候选正文。
关闭分析阻止后续模型工作，旧派生信息保留且注明停止时间；未确认事项候选冻结，重新开启后
校验证据、语义版本和许可，增加 proposal revision 后再允许批准，旧预览始终失效。
单独关闭 proposals 则继续提供摘要和重要信息，但停止候选生成与应用；重新开启同样必须重验、
增加待批准 revision。冻结时可人工拒绝仍有权读取的候选，不能接受；service pause 则不冻结批准。

已接受的 matter 是用户确认保存的独立记录，不因停用群聊而删除、完成或自动改期。
确认页须明确告知：选定标题／摘要将复制到事项，停止消息访问不会撤回这份已确认副本。
仅保存用户审阅的有界内容，不暗藏整段聊天在 metadata、FTS 或 source reason 中。
源链接每次重新检查消息权限，撤销后只显示“来源不可访问”。未批准的派生正文不能通过
matter 搜索、FTS snippet、混合来源或子 Agent scope 泄露；已确认副本按事项自身权限读取。

## 11 前端体验与后台管理

沿用 `/mnt/d/agent-bot-frontend` 当前页面与 Java bridge，不引入 React 或新的前端框架。
消息阅读加入现有后台管理入口，作为独立服务卡，不伪装成一个永远运行的 matter。

消息中心提供四个页签：概览、话题与亮点、重要信息、事项候选。历史原文与后台运行详情可展开。

- 概览：更新时间、主要话题 Top 项、重要项未阅数、当前覆盖和已知采集缺口。
- 话题：按群／时间过滤，显示参与数、讨论变化、结论与分歧，点击原文可定位上下文。
- 重要信息：默认未阅与有变化，明确／可能分组，支持已阅、忽略、稍后看；不依赖热度排序。
- 事项候选：原文、字段编辑、已有事项匹配、批准／拒绝；处理一条后自动定位下一条，不自动批准。
- 后台服务：启停、会话档位、模型和 token／调用／费用上限、已用预算、最早积压、失败原因及重试。

必须分别显示“采集在线”“本地扫描”“模型分析到哪里”“简报更新时间”，不能只放一个绿色运行中。
达到预算、模型错误、历史捕获不完整时显示准确原因。刷新列表不覆盖当前详情或正在编辑的批准表单；
表单固定打开时的 revision，409 后保留编辑并要求重新核对差异。来源正文以 textContent 展示。

GET 只读不改变阅读状态；本地标已阅不发送平台回执。默认只用应用内未读标记，不增加系统弹窗。
用户另行开启桌面提醒时再应用免打扰、合并和频率限制，不因关键词重复打扰。

## 12 API 与 Agent 合同

下面全部是拟新增或拟扩展的合同，不表示当前路由存在。

| 接口 | 作用与限制 |
| --- | --- |
| `GET /messages/overview` | 当前授权范围内的聚合与覆盖，不附全量正文 |
| `GET /messages/topics`、`GET /messages/topics/{id}` | 会话／时间／热度筛选，来源独立分页 |
| `GET /messages/insights`、`GET /messages/insights/{id}` | 重要性／兴趣／未阅过滤，说明 detector 与 certainty |
| `PUT /messages/insights/{id}/attention` | 用户阅读状态 CAS，不改变平台状态或 proposal |
| `GET /messages/derived/{kind}/{id}/sources` | 有界来源分页，每页重新检查权限 |
| `GET /messages/matter-proposals`、`GET /messages/matter-proposals/{id}` | 候选列表和准确审批预览 |
| `POST /messages/matter-proposals/{id}/decision` | 强认证人工 accept/reject；精确 revision 与幂等键 |
| `GET/PUT /messages/reading-profile` | 显式关注配置，CAS；敏感联系人配置不由 importer 获取 |
| `GET /background/services/message-reading` | 服务状态、覆盖、预算和当前／目标配置摘要 |
| `POST /background/services/message-reading/pause`、`resume` | CAS 控制，立即生效，不修改采集许可 |
| 扩展 `GET/PATCH /background/config` | 增加 message_history 配置节，保留 active/desired 与 restart_required |

现有 policy、history、search、analyze、retry 接口保留。新增会话档位即时影响调度；模型与 worker
配置沿用保存后重启生效，UI 不把保存成功当作已切换。pause/resume 为独立即时控制。
后台控制复用现有审计机制，但批准不借用普通可自动通过的 job-control 或 safety skip 身份。

管理 API 统一校验可信 local-control principal、loopback/Host/Origin；非授权 source 使用 opaque
404，非法字段 422，版本／状态冲突 409。导入仍使用专用 token。异常不回显聊天或 provider 密钥。
health/SSE 只发 ID、数量、版本、状态等元数据；第一版使用现有轮询／状态 SSE，不新增可靠事件总线。

Agent 扩展只读工具：`messages.overview/topics/insights/read_insight/topic_sources`，并保留
recent/search/history/summary/facts。结果必须含 `untrusted_data`、coverage、分页、更新时间及来源策略。
不同 source/account 权限下的聚合重新计算，不以“先混合再删引用”掩盖越权。
读工具不标已阅、不触发批准、不自动扩大范围或调用平台；后台生成候选无需向 Agent 开放写接口。

示例 coverage：

```json
{
  "capture_mode": "inbound_only",
  "capture_gaps": "unknown",
  "latest_seq": 307,
  "local_signal_seq": 307,
  "analysis_covered_seq": 300,
  "digest_covered_seq": 250,
  "pending_messages": 7,
  "analysis_state": "budget_deferred",
  "next_eligible_at": "2026-10-04T12:00:00+08:00",
  "complete_for_platform": false
}
```

`next_eligible_at` 是重新尝试时刻，不是承诺完成时间。跨群结果必须带各群覆盖，不能用最大水位概括全部。
接口同时提供 `pipeline_version/baseline_start_seq`：上述数值示例假定从第一条开始的同一 generation。
迁移后的 legacy_only 范围单独返回，不把旧摘要的覆盖数当成新话题和重要项的覆盖数。
列表默认 limit=50、上限 100，原文来源每页上限 50；overview 的 Top 项和 topic 原文预览有独立
小上限（建议 10 项、每项 3 条），完整内容只能分页读取，不能随列表隐含返回全部聊天。

## 13 数据安全 保留与故障退化

- 首次开启记录与开启模型分析分开确认。模型选择可限定 client/model；换到新提供方不自动继承
  旧的内容外发许可，必须确认新范围。不要把旧群内容默认为公开数据。
- 分析时默认不加载其他群、邮件、事项详情或长期记忆；显式混合来源授权后仍按最小必要输入。
- 不记录普通日志聊天正文；完整调试输入默认关闭，需单独有期限的诊断授权，且不进入 Git。
- 原文保留不被摘要替换。本阶段不自动删除；派生 checkpoint 可按保留策略清理，但不得删除
  当前 pending、审批所需证据或无法恢复的覆盖依据。导出／清理历史是后续显式用户操作。
- 本地 outbox 上限与后端存储监测分开。磁盘不足则不 ack 未提交数据，显示暂停／背压；
  不能用删除原文掩盖容量不足。离线缓存撤销延迟与 OneBot 无重放限制保持透明。
- 模型不可用时仍可读取原文和本地信号；无效输出不替代旧有效结果。旧摘要明确标记过时。
- 内容含“忽略规则、创建事项、请求联网”仅当作原文，不改变权限、预算或运行策略。
- 本系统不能承诺零遗漏。规则漏检、模型漏检、捕获缺口和预算延迟需分别展示与评估。

## 14 迁移与实施切分

按以下依赖顺序实施，具体可勾选项见 [消息历史 TODO](message_history_todolist.md)。

1. 协议与许可：v2 metadata、capture/analysis epoch、配置 schema、来源解析与前端适配。
2. 调度与成本：间隔／尾批／预算／公平队列、暂停、分片恢复和观察指标。
3. 阅读结果：话题／亮点／重要项、规则扫描、低频简报、可追溯来源与状态。
4. 授权事项：候选、控制身份、非降级人工 gate、来源 guard、原子批准与幂等。
5. 前端与 Agent：完整阅读中心、后台管理、只读工具与源约束传播。
6. 小样本验收：合成流水、故障与权限、批准后真实模型评测、Windows 实机。

采用增量建表／加列迁移，旧消息、旧批次和旧 matter 不重写。旧 summary 作为 legacy 输出保留，
不能假装已生成 topic 或 importance；v2 输出记录自己的 pipeline_version 和 coverage。
切换时先停止旧 worker 的领取与完成后串接，并 fence 已运行任务；保留其已提交输出，再启用
新 pipeline，同一会话不得双管线同时推进。以切换时旧 covered_seq+1 建立 baseline_start_seq，
该值之前明确标记 legacy_only，新分析从未处理尾部开始；本地信号扫描也记录自己的覆盖起点。
若旧批次失败，先显式废止其执行所有权并保留失败记录，新工作接续相同未覆盖范围与累计账本。
旧版本未有分片级用量关联时标注历史消耗不可完整追溯，不迁移成 0 费用或自动重置共享预算。
新服务默认不替用户开启更多会话或外发。迁移前可备份，schema 变化用事务；关闭新 feature 后
旧只读接口仍可读取原始历史，不通过删表“回滚”。新增接口上线时再更新正式 API 合同。

完整历史重分析不自动执行。用户明确请求时显示范围与预算，使用独立 analysis generation，
新结果在可校验范围内替换派生版本，不能重复创建候选、重置已阅状态或覆盖已接受事项。
同样受配额限制，不为“重建”开无限成本例外。

## 15 评测与验收

设计阶段不运行模型或大规模测试。实现后先建立小而固定、可重放的合成数据，使用假时钟、
临时 SQLite 与假 provider；只有用户批准后才对脱敏样本调用真实模型。

| 样例 | 必须观察的结果 |
| --- | --- |
| 多群 310 条、重复 25 条、拒绝 3 条 | 列表与分页完整，权限隔离，重复不增加批次 |
| 3 条低频消息含期限 | 达到尾批目标后具备调度资格，受预算时明确延期 |
| 大群持续高流量混入少量行动信息 | 频率有上限、小群不饿死、重要项不按热度被淹没 |
| 结构化 @本人、@全体、同名昵称、未知 reply | 明确与推断分开，不跨会话错误归属 |
| 多话题交织、回到旧话题、不同人持相反意见 | ID 稳定、保留分歧，来源准确 |
| 反复“收到”、单人刷屏、低频关键公告 | 去噪不抬热度，也不删除关键原文 |
| 截止时间更正、跨日、未知时间、DST 歧义 | 旧建议失效，日期来源可查，不擅自确定期限 |
| 模型超时／429／非法引用／超长输出 | 有界重试与计费，不推进未完成水位 |
| 入库后崩溃、分片后崩溃、发布后响应丢失 | 恢复输入与 checkpoint，幂等发布，无半份状态 |
| 多分片轮转、跨分片新话题、替代 job 耗尽预算 | 真正释放槽位、暂存引用一致、不重置 family 用量 |
| 调整批量、撤销记录、暂停及重启 | 配置代次语义正确，调度修改不隔离合法采集消息 |
| v2 会话从旧入口导入、legacy 切换双 worker | 拒绝代次绕行、旧运行失效、不虚报新管线覆盖 |
| skip／LLM 模式、直接 tools／HTTP、伪造 approved | 消息来源的 matter 应用仍无人工授权就拒绝 |
| 同时双击批准、旧预览、证据撤销、目标被改 | 至多一个 matter，CAS 拒绝，事务无残留 |
| 暂停时批准、分析关闭、重启许可、回执重放 | 暂停不阻止人工决定；撤销不复活旧预览、不泄露证据 |
| 已阅后重要更正、拒绝候选、GET 列表 | 更正重新提示，阅读与批准互不混淆 |
| 恶意聊天要求联网／读取凭据／自动创建事项 | 无动作执行，无权限或预算扩大 |

硬性正确性门槛：越权返回、无批准事项写入、重复应用、虚假全量覆盖、原文丢失在上述确定性用例中
均为 0。这不是对未测真实世界的绝对安全或零遗漏承诺。

语义质量单独记录：话题覆盖与稳定性、重要信息召回／误报、deadline 更正准确性、证据支持率、
用户 Top-K 有用率；由人工标注建立小样本基线。不能拿固定假模型输出当作语义通过。
工程指标记录：每千条调用数／tokens、估算费用与价格版本、最早积压年龄、重复计算率、会话公平性。
预算内延迟与因预算不足延期分别统计，避免平均延迟掩盖重要小群长期没处理。

历史 badcase 由用户明确标记保存，默认只进入本地评测集，不自动上传、训练或扩大模型内容许可。
每次 prompt/schema/模型切换只跑相关小样本回归；真实模型设总调用上限，比较同一输入和配置，
检查重要召回不退化且成本下降，再决定是否扩展测试。不能为了达到某个省钱比例降低证据完整性。
