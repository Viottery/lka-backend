# 网页困难案例：证据交付优化与复测

日期：2026-10-05。承接 [原始失败报告](web_difficult_cases_2026-10-05.md)。
原目标／原始回答不改写；工程交付、事实支持、用户请求完成和耗时分别判断。

## 本轮方案与验收

1. **模型拿不到缓存中的限定条件**：增加注册生产者的顶层字段优先级。
   Core 不识别 web 或领域字段，只消费 registry 的有界元数据；结果同名字段无授权。
   gate 在持久化加载后仍优先证据／定位／版本／分页；超过原7000字符上限先裁诊断，
   再减少数组预览条数，保留局部／省略标记和原始缓存。不扩大窗口、不增加模型调用。
2. **读完数组但没读完字段**：截断字段给出原始 JSON Pointer 的 read_path 与 next_offset。
   增加可选字符串 max_chars（1–4000），旧默认和原授权边界不变。
   字符串续读省略 fields；数组 has_more 描述记录页，不认证字段文本完整。
3. **相关段落排错／表格连接**：本地词频有界饱和，按段落逆频率提升区分词；
   表格按行分隔、同一行的单元格清晰分开。仍是可追溯原文，不生成摘要或语义断言。
4. **评测之前漏过真实故障**：已知失败测试转为正常回归；真正经过 SQLite 保存／加载，
   再构建 decision 观察与 fitted answer provider 请求。评测源码指纹补齐 cache/views/resources。
   原严格 xfail 不再当作“允许失败”保留；真实模型回答由根 agent逐项复核。

## 不变约束

- 短结果兼容、未知工具/MCP 默认行为、原始结果缓存、source roles、安全门、ToolView 不变。
- 缓存可读不等于全文已进入模型；context_delivery 只证明缓存字符串区间进入拟合请求，
  不证明来源全文覆盖、推论正确、最新状态或用户任务已完成。
- 本轮不增加搜索预取、摘要模型、语义审核模型或领域关键词硬编码。
- 只使用配置的 DeepSeek Flash 和既有共享预算，原三个目标各复测一次，不自动刷模型重试。
- 并行用户的消息／Windows／知识模块变更不归本轮所有，不回滚、不提交。

## 工程验收

本轮覆盖：持久化后限定条件／引用交付、最终provider receipt、未知工具与名称不匹配
拒绝借用优先级、大小压力下7000上限、数组条数省略、字段路径转义／字符续页与权限回归。

最终定向回归 **260 passed，30.90s，无xfail**：

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_progressive_challenges.py tests/test_web_evidence_selection.py \
  tests/test_web_snapshots.py tests/test_web_views.py tests/test_web_tools.py \
  tests/test_web_page_paging_quality.py tests/test_web_find_quality.py \
  tests/test_web_readability.py tests/test_web_evidence_context_quality.py \
  tests/test_web_find_recovery_quality.py tests/test_tool_result_gate.py \
  tests/test_observation_reader.py tests/test_observation_projection.py \
  tests/test_observation_search.py tests/test_observation_group.py \
  tests/test_context_delivery_quality.py tests/test_child_control_working_set_quality.py \
  tests/test_answer_evidence_quality.py tests/test_eval_runtime_web_quality.py
.venv/bin/python -m scripts.eval_web_progressive_challenges
```

9个离线对抗场景：原 **7通过／2失败 → 9通过／0失败**；首视图判据命中3→5，
显式恢复6→7。表格判据现在严格要求表头、North与South各自同一行的单元格分隔，
不再仅用值存在或换行猜关联。gate场景使用实际registry→AgentTurnLoop而非直调无元数据预览。
不表示9/9回答准确率；远隔限定条件、高频单锚点、中文问句仍可能首屏漏选，需要后续定位。

修改文件Ruff与diff检查通过，未运行全套、未部署或重启。低成本子agent负责两个不重叠
单元：摘选／表格实现、只读交付审查；根agent完成集成、测试及真实回答复核。
审查发现既有超长JSON键可能使fallback突破7k：现已限制路径长度（超长返回null而非伪造
截断地址），再有界减少目录项；300与10000字符键回归通过，独立复核未发现剩余中高风险。

## 效果与剩余问题

真实问题与上一轮完全相同，各运行一次，三个机械检查仍均通过且工具失败0。
报告中的semantic_review保留生成时pending_root_review；本节是随后根agent复核，
不是独立人类gold或未见题准确率。所有列入runner的源码与场景指纹前后相同，
新增三个网页模块已纳入；并行用户工作树仍非纯净HEAD，未保证所有无关模块冻结。

| 任务 | wall秒 旧→新 | LLM／工具 旧→新 | 搜索旧→新 | 回答字符旧→新 | 实际评价 |
| --- | --- | --- | --- | --- | --- |
| Python 3.13／3.14 | 59.872→74.812 | 15／10→12／7 | 1→1 | 1222→1445 | 扩展GIL条件和3.14官方支持已交付；3.13实验性级别仍未核实，比较任务未完整 |
| SQLite WAL | 60.821→114.271 | 9／6→16／11 | 0→0 | 1659→1249 | 单写者／固定读快照／checkpoint原文有据，不再追加缺持续写入条件的增长推断；仍冗长且读取更多 |
| uv当前发布 | 21.084→66.344 | 5／2→10／6 | 1→2 | 1025→1238 | 本次自行读取官方release与tag，版本／日期／两项变化有据；仍追加bugfix及长篇覆盖说明 |

| 任务 | 新输入／输出tokens | 新缓存输入tokens | LLM费用旧→新美元 |
| --- | --- | --- | --- |
| Python | 99182／12216 | 47744 | 0.077728128→0.081005504 |
| SQLite | 154131／18477 | 76288 | 0.054392416→0.122621408 |
| uv | 82081／10994 | 44032 | 0.022325184→0.066324512 |

用量来自provider，输出包含推理。本轮新增 **38模型调用、3 Brave查询、LLM $0.269951424**，
共享账本累计LLM $6.856709664→$7.126661088，搜索11→14。搜索账本未计价，不能说免费；
若按此前项目估计$5/1000次则约$0.015，非发票实付。text模式没有最终token TTFT指标。
各仅一次且并发运行，网络／模型行为有波动；不能断言代码必然造成全部耗时增加，
但本轮**没有端到端速度改善的证据**，更不能宣称“修复后更快更便宜”。

### 具体实质进展

- Python原扩展说明已在缓存找到却完全没进入prompt；本次未显式标记支持的C-API扩展会
  重新启用GIL这一条确实进入最终answer请求并被正确回答。真实receipt有843字符匹配片段
  完整交付（complete只指该缓存字段）；3.14正式支持条款也出现在最终请求。
  不代表3.13所有比较项已验证：模型仍未主动取3.13专属官方页，回答明确承认这个缺口。
- SQLite原先首尾／匹配视图难以掌握，现可本地继续查证、两官方正文进入回答。
  但仍花11工具调用，其中末尾多次find广泛术语，并未形成高效的完成判断。
- uv本次明确取release/tag，不再仅依赖CHANGELOG前缀；搜索摘要差异按官方源处理。
  代价是更多读取和第二次search，不能把“来源更强”误称“相应更快”。

### 剩余bad case与下轮局部TODO

- [ ] **P1完成判断**：多项／跨版本任务仍可能在显式缺一个要求时结束。
  基于既有task contract/answer_checks推进要求—证据—缺口交接与局部补证，不解析领域关键词，
  不把“两个URL／事实词出现”作为完成。测试版本侧来源缺失、官方证据只覆盖一半、
  预算不足与已明确无需继续的情况。不能盲目强制多读或默认增加模型审查。
- [ ] **P1有效读取预算**：网页大page仍可能进入gate后只剩首尾；本次Python有若干
  /output/text receipt仅覆盖500字符。模型没有稳定利用短页／条件局部上下文，
  SQLite末段又反复find宽泛词。进一步设计可见证据增量／重叠窗口去重与短页续读合同；
  避免仅重复改提示词、改更强模型或放大gate来掩盖问题。
- [ ] **P2简洁性**：这三项均未达到用户想要的简短结论，uv与Python字符反而增加。
  必要条件／用户要求与审计元数据应分开交付，限制无关扩展而不删真实限定；
  不靠关键词硬编码或无条件字符硬截断。测试用户要求短答、详解、精确JSON及缺口一次说明。
- [ ] **P2推理/协议开销**：Python一次decision输出3223tokens，其中3144为推理；
  SQLite多个decision有约2800–3800推理tokens，两次decision_repair。
  这是观测证据，不是对推理上限的授权变更。后续从协议可用性与避免无增量决策入手。
- [ ] **P2复杂表格／跨语言语义**：当前简单行分隔不重建rowspan/colspan，
  词法摘选不做跨语言语义召回；长距离限制首屏仍可能漏掉。保持显式限制并单列难题分组。

## 私有复测产物

原report、实际provider请求、usage/hash仅留本地，不提交提示词、密钥或网页正文：

- `data/quality_runs/linux_20261005/runtime_heldout_web_multisource_20261005T123723988539/heldout_web_multisource_20261005T123723996231/`
- `data/quality_runs/linux_20261005/runtime_web_sqlite_20261005T123725105967/heldout_web_sqlite_20261005T123725111303/`
- `data/quality_runs/linux_20261005/runtime_web_search_release_20261005T123724975916/web_search_release_20261005T123724981410/`

真实复测沿用原报告的三条付费命令。本轮不为刷到更好答案自动追加付费重试。

## 第二轮：query接线与连续证据窗口

本轮新发现的实际原因：WebResources.open按输入中是否含max_chars隐式选page，
导致模型传query+max_chars=6000时query完全被忽略。上一轮改善的词法排序因此未在这些
真实调用上执行。这是模式选择bug，不是单纯“模型选错关键词”。

修复与兼容边界：

- query+max_chars且无显式view/offset → 自动相关概要；max_chars只控制摘选预算。
- 显式offset（包括0）/view=page仍连续分页；仅max_chars也维持原page行为。
- 增加view_mode/query_status；page里的query明确not_applied_page，不伪称执行。
- 保留工具原始返回与存储。新增registry拥有的output_preview_text_mode，web.open和
  observation.read注册contiguous_pages；未知/MCP保持head_tail默认，正文同名字段无效。
  长字符串不再只取首350/尾150，按叶子上限给最多4个连续原文块；size-pressure下减块数，
  剩余部分带准确next_offset/省略标记，原7000 gate上限不扩张。不是默认读完整网页。
- fragment offset指该原始cached value，不是source整页；网页分页须加output.offset。
  observation.read的callback重读同run原缓存并验证body/offset/hash，再把每个fragment映回
  原字符区间；通用receipt同样核验实际文本，最终拟合缺失/变更不能得到complete。

新增测试test_web_read_efficiency覆盖：实际query+预算路由、显式page/offset优先、中部限定
经过SQLite→gate→provider完整交付、7k/Unicode窗口、篡改fragment拒绝、未知tool策略不升级、
observation.read非零offset映射。定向 **152 passed，25.10s**；原9个离线对抗判据仍9通过。
Ruff与diff检查通过。不跑全量，原Python与SQLite各实测一次，结果完成后补齐。

### 明确缺口却结束：本轮调查结论

低成本只读子agent复核上一轮Pythontrace：decision未提交answer_checks就宣称足够，
之后answer才承认3.13来源未读取。普通Graph的final_answer只拦未解决的多agent计划，
answer→verify→finalize不把结构化剩余工作回送decision；自由答案verification目前不判语义。
因此run completed/机械检查通过不表示用户要求完成。不是缓存receipt可修复的语义错误。

后续独立局部TODO（尚未实施，不能宣传本轮已修复）：

- [ ] 在既有调用内建立有界、稳定ID的要求覆盖与缺口交接，不新增每轮planner或独立LLM审查。
- [ ] 使用版本化结构化remaining_work，而非扫描“待核/缺口”等正文关键词。
- [ ] 可继续缺口且仍有预算才返回现有decision；权限/超时/预算/无增量时只交付明确部分结果。
- [ ] 与用户JSON输出和stream delta终态兼容：不能直接强迫所有answer输出私有JSON，
  更不能先发送final_answer再静默重启；旧checkpoint/reason-only finish必须兼容。
- [ ] 离线回归补证后一次终态、缺口未覆盖不能被receipt清除、预算耗尽不循环、
  无工具直接短答、严格JSON、取消/恢复、子agent预算与旧协议。

本轮先消除明确的读取接线问题，不为简单任务增加一轮LLM，也不修改推理开关或模型配置。

### 第二轮真实结果与新故障（不得丢弃失败样本）

| case | 上一轮wall/模型/工具 | 本次首次wall/模型/工具 | 复核 |
| --- | --- | --- | --- |
| SQLite | 114.271s／16／11 | 40.156s／8／5 | 两官方正文支撑三个核心结论；951字符仍偏长 |
| Python | 74.812s／12／7 | 14.580s／4／0 | **失败**：没有检索证据；不能把早停的短耗时算提速 |

SQLite新输入/输出62165/5369tokens，缓存输入34048，LLM $0.040219168；新增1次search。
其query+max_chars=6000/8000调用现在确实产生相关概要，最后1600字符本地分页也完整进入
answer request；真实receipt涉及1600字符连续页和多个原文片段，不只是工具扫描到了。
本次相比上一轮模型调用约减半、wall降低约65%；只有一次样本，非严格因果A/B或统计提速。
仍有长引文/覆盖说明，尚未完整解决简洁性与完成判断。

Python首次input/output12441/1870tokens，缓存1664，LLM $0.014632224，search0。
trace显示初始decision混入推理与重复JSON；独立repair调用生成了完整operation但尾部多一个
`}`。strict parser失败后没有任何工具dispatch，最后answer说“未取到官方页”，并问是否重跑。
网络未执行，不能称抓取失败或把这次作为检索质量验证；mechanical page checks为false。

针对这项新缺陷新增局部修复：**仅在decision_repair阶段**，若strict解析失败，用JSONDecoder
从头读取一个完整非空对象；仅余一个多余闭合`}`才本地恢复。嵌套重复key、多对象、前缀、
夹带文本、两/更多尾括号、截断控制输出仍拒绝。记录decision_repair_suffix_normalized事件，
保留原始/修复输出；所有工具仍经过schema/范围/safety gate。普通decision和用户输出合同
的strict parser不放宽。不修改模型/推理参数，不新增付费repair调用。

独立审查指出force_gate小结果连续分块会让5984字符膨胀超过7k，已修复：超限退回保留
全部结构的head/tail字符串投影，不删除status/gap/list条目；新增20状态字段与缺口回归。
repair新测试初次也发现strict parser失败返回{}而非None，已修正入口/夹具，后续安全与
legacy测试实际通过，不拿未走到gate的测试作安全证明。

新修复后只增加一次Python验证，既有失败样本保留；不是原样重试刷好结果。
原始产物均仅本地：

- SQLite：`data/quality_runs/linux_20261005/runtime_web_sqlite_20261005T125344834441/heldout_web_sqlite_20261005T125344839874/`
- Python首次失败：`data/quality_runs/linux_20261005/runtime_heldout_web_multisource_20261005T125344549312/heldout_web_multisource_20261005T125344554484/`

### 第二轮最终验收

最终定向集成 **164 passed（27.56s）**、无xfail；包含上面的152项基础组、新增force_gate
夹具、9条repair边界测试与既有malformed-call/repeated-successful-call两条回归。
Ruff／diff检查通过。独立审查已确认本轮最终hunk无剩余中高风险；未运行全套或重启生产。

Python修复后的验证：**37.357秒、7模型／4工具、1次搜索、0工具失败**，新输入/输出
54117/5796tokens，缓存29696，LLM $0.038559136，回答1027字符（上一有效样本1445）。
本次实际读取3.13 What's New、3.14 What's New及free-threading HOWTO，覆盖此前缺失的
3.13侧来源；三个请求项——默认/可选、实验性到官方支持、扩展重新启用GIL——均得到回答。
关键限定原文进入最终request，receipt有1134/767/720字符完整交付的缓存字段。
模型还输出了未要求的性能数字及覆盖说明，简洁性仍未完全解决。

这次rerun没有触发多余尾括号恢复（事件0）：因此只能用新增回归证明该恢复边界，不能
声称真实模型已稳定复现并经此规则恢复。新的有效回答与首次失败均保留，算入全部成本；
禁止删除失败样本并把14.580秒早停写作提速。Python来源缺口在本样本消除，
不表示通用任务完成判断机制已实现。

| 有效任务对比上一轮 | wall秒 | 模型／工具 | 输入tokens | 输出tokens | LLM美元 | 回答字符 |
| --- | --- | --- | --- | --- | --- | --- |
| Python | 74.812→37.357 | 12／7→7／4 | 99182→54117 | 12216→5796 | 0.081005504→0.038559136 | 1445→1027 |
| SQLite | 114.271→40.156 | 16／11→8／5 | 154131→62165 | 18477→5369 | 0.122621408→0.040219168 | 1249→951 |

本轮实际总预算含失败：**19模型调用、2 Brave查询、LLM $0.093410528**；共享累计
$7.126661088→$7.220071616，搜索14→16。搜索账本未定价（不是免费），按此前$5/1000的
项目估算约$0.01，非发票费用。text模式仍没有首个最终token TTFT。所有本轮runner指纹与
场景指纹前后相同；测试使用当前含用户未提交工作的隔离runtime，不宣称纯HEAD部署验收。
两个有效任务都是单样本，不能外推“系统平均提速50%/65%”或统计准确率；uv本轮未复测。

Python最终产物：
`data/quality_runs/linux_20261005/runtime_heldout_web_multisource_20261005T125958977266/heldout_web_multisource_20261005T125958987072/`。
原report中的pending_root_review是原生成状态，本节是随后根agent复核，不改原产物/hash。
