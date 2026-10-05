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
