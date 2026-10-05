# 网页渐进检索优化 TODO

状态：首轮实现、离线工程验收与独立审查完成；后续真实困难复测已运行并发现待修复问题（见下文）。目标是首屏尽快拿到有来源的概要，详细证据稳定续读，不增加摘要／审查模型调用。
执行依赖：快照存储 → 工具接入 → 相关摘录 → 时效与并发 → 定向验收／审查。

后续困难实测发现：持久化 JSON 排序会撤销输出字段优先顺序，find 证据可能仍被 gate 隐藏。
因此上面的离线工程通过不代表真实困难任务已闭合；新的失败复现、真实指标与修复优先级见
[困难案例实测](web_difficult_cases_2026-10-05.md)。后续修复与复测单独记录于
[证据交付优化报告](web_evidence_delivery_optimization_2026-10-05.md)，不覆盖原始失败记录。

## F. 困难案例驱动的后续修复

- [x] 注册工具声明有界 output 字段优先级；实际 gate 从 registry 读取，不信任工具正文同名字段。
- [x] 持久化排序后仍保留证据、版本、续读；超限先去诊断再减条数，不扩大 7000 字符 gate 上限。
- [x] 回归实际 SQLite roundtrip → decision 观察 → 最终 fitted provider prompt，不只检查内存工具输出。
- [x] 截断投影给出转义正确的 read_path/next_offset；数组分页完毕不冒充字段全文已读。
- [x] 字符串续页支持 max_chars，保留旧的默认 4000；可请求 ≤1200 的短证据窗口。
- [x] 原文摘选降低高频词重复优势，优先有区分度的查询词；无语义／跨语言命中保证。
- [x] HTML 表格保留行／单元格边界、更新抽取版本并验证 offset/hash；复杂跨行列合并不冒充已还原。
- [x] 重跑原 3 个真实困难任务，分别评估请求覆盖、事实限定、篇幅、耗时与消耗，不以工具成功率代替准确率。
- [x] 独立审查、本轮报告与定向验证；本轮改动独立 Git 管理，不重启生产、不提交并行用户工作。

本轮工程问题已修复，但真实性能／篇幅尚未改善：Python 扩展 GIL 条款恢复，3.13来源仍缺；
SQLite与uv模型调用增加，详见新报告。后续未闭合项不能用这些勾选自动关闭。

## G. 读取效率第二轮

- [x] 修复 query+max_chars 被隐式page忽略的实际接线错误；显式offset/page保持原分页优先。
- [x] 输出实际view_mode/query_status，page模式不伪称query已执行。
- [x] 注册长文本连续短块预览，减少4000字符页仅交付首尾500字符的情况；保留7k上限。
- [x] 补齐最终provider文本区间与observation.read非零offset映射；结果正文不能改变策略。
- [x] 定向离线回归与只读调查完成；answer阶段才出现的缺口机制详见报告，不用文本关键词猜缺口。
- [x] Python／SQLite原任务实测，记录质量／延迟／tokens；保留Python新协议失败，实际修复后再验证一次。
- [x] 独立审查、报告与本轮Git管理；不重启生产。

第二轮SQLite114.3→40.2秒、Python有效任务74.8→37.4秒；Python首次0工具失败不能算提速。
两个样本均非总体性能结论，简洁性／通用缺口终态协议仍待后续。

## 约束与验收原则

完成控制的后续实现与失败实测见
[通用任务完成控制记录](task_completion_optimization_2026-10-05.md)。已声明缺口的
有限恢复、checkpoint、child partial 传递已实现；这**不等于**真实复杂问题的漏答已解决。
最新单次 Python 复测 103.431 秒、13 次模型调用，未填写可选检查；答案仍有遗漏与冗长，
因此不把上一轮 37.4 秒或本轮离线通过推广为稳定体验改善。

- [ ] 下一阶段：让复杂任务可靠地产生版本化需求／证据交付契约，保留简单任务快路径。
- [ ] 下一阶段：回答阶段发现的缺口经结构化控制通道有限返回执行，不按答案关键词猜测。
- [ ] 下一阶段：对用户所需结论所对应的原文片段做有界交付保留，而非扩大整页／整prompt。

- 保留 web.search/open/find、ToolExecutor 校验、安全门及来源角色；core 不编码网页策略。
- 不更换搜索 provider，不默认预取全部结果，不建设全局网页索引，不扩大上下文窗口。
- 不修改或提交工作区已有用户改动；本轮代码、测试、文档独立管理。
- 网页、搜索摘要均是不可信证据；抓到全文不等于模型读过或理解全文。
- 优先离线固定场景，真实网络／模型语义评测分别报告，未运行不标通过。

## A. 不可变快照与可维护存储

- [x] 复用本地 artifact 表保存抓取边界内完整可读正文；独立索引记录所有者、版本、时限和字节数。
- [x] open/find 共用服务；URL 查询可命中已有快照，snapshot_id 固定版本、失效不得隐式联网。
- [x] 根会话支持同会话跨 turn；child 按 run 隔离，每次检查当前 ToolView／取消／会话状态。
- [x] 保留原始 URL、最终 URL、fetched_at、text_sha256、抽取器版本和实际网络观察。
- [x] 本地分页／find 使用一致 Unicode offsets；刷新只创建新版本，不混用 hash／offset。
- [x] TTL、容量、清理均有界；缓存失败明确反馈，不伪装成空结果或最新内容。

## B. 渐进式工具合同

- [x] search 返回公平分配的短摘要、查询时间和稳定候选引用；完整标准化响应留在本地。
- [x] open 接受 URL／候选引用／快照，首次默认概要，offset 或 page 模式连续分页。
- [x] 可选 query 只在本地选择相关原文，包含相邻段落；不做额外 Brave／模型查询。
- [x] 返回有界目录、位置、上下文省略标记与续读入口；非连续摘录不冒充连续 text。
- [x] find 默认在快照定位，沿用字面零匹配恢复，不把零匹配说成语义不存在。
- [x] tool schemas、描述、package metadata、证据角色同步；短块保留使用已注册有界叶子阈值。
- [x] 完整正文不重复写入每次工具结果，也不注入常规 HTTP／prompt。

## C. 时效与失败行为

- [x] 分开 publication/provider fetch/system fetch/cache read 时间；本地 cache_hit 不诊断上游缓存。
- [x] refresh 与 max_age_seconds 显式控制；过期固定引用报错，URL 请求可重新抓取。
- [x] 刷新失败保留旧快照，失败不能假装成功或升级旧数据时效。
- [x] 同会话追问能复用引用，已删除会话、不同会话、不同 child、过期权限拒绝。

## D. 有界并发和取消

- [x] 同作用域／同请求合并抓取；不同页面并发上限、等待队列与总体 deadline 可配置。
- [x] 所有等待有 deadline；取消检查在排队、网络前后、存储发布前生效。
- [x] 不阻塞 FastAPI event loop；沿用 Graph 的线程执行边界，并验证 async 运行时响应性。
- [x] 不引入默认预取／批量扩权；独立读取可走现有受控多 agent，先消除重复请求。

## E. 测试、评测与交付

- [x] 快速搜索不抓页面、不调用模型；归档响应可恢复摘要遗漏。
- [x] 尾部 >20k、Unicode、相邻条件、长邻居、无命中和有界目录／预算。
- [x] open→find→续读一次 HTTP，刷新新增版本；失败刷新不损坏旧版。
- [x] 同会话跨 turn／重启读取、跨会话／child 拒绝、失效／容量清理、权限撤销。
- [x] 同页并发单次抓取、不同页上限、队列满、deadline、取消后无发布。
- [x] 实际 ToolExecutor schema、规则 gate、provider prompt 交付与异步 Graph 接线。
- [x] 小型离线 A/B：工具耗时、HTTP 次数、视图字符／token 估算、限定条件保留；不外推总响应提速。
- [x] 保留仍 open 的真实网页语义 bad case，不用模拟模型替其关闭。
- [x] 定向测试／Ruff／diff 检查、独立审查、API／用法／结果报告与本轮独立 Git 提交。

## 结果记录

### 实现入口与使用方式

- `app/domains/web_cache.py`：不可变 artifact、独立索引、完整性校验、TTL／容量、
  请求合并／并发许可／有界等待。域层不发网络请求，不调用 LLM。
- `app/domains/web_views.py`：短搜索视图、相关原文＋相邻上下文、分页、有界目录。
- `app/tool_packages/web_resources.py`：当前权限检查、引用解析、时效策略和缓存续读。
- `app/integrations/web_search.py`：原 SSRF／连接固定／TLS／字节边界内的正文获取，
  实际网络元数据、目录抽取及总 deadline。Graph 延续线程边界，不在事件循环做同步获取。
- `app/tool_packages/web.py` 与 runtime／配置：生产注册共享服务；输入 schema、说明、
  证据角色及跨 turn 观察缓存 metadata 同步。单独构造工具且不提供 resources
  的兼容测试入口仍用旧适配器行为；生产 runtime 已接入新服务。

推荐调用顺序（参数示意，不是固定 Agent 工作流）：

1. `web.search(query=..., limit=5)`：只请求搜索 provider，返回短摘要与 search_id／ref_id。
2. `web.open(ref_id=..., query=...)`：按需获取一个页面；用 query 选相关原文，
   而不是让模型先读前 20k 字符。无 query 时返回正文前缀概要，不保证命中关键内容。
3. `web.find(snapshot_id=..., query=...)` 或
   `web.open(snapshot_id=..., offset=..., max_chars=...)`：纯本地稳定续读。
4. 要最新状态则重新按 URL `refresh=true`／`max_age_seconds=0`；
   引用失效要明确重新获取，不能静默换版本。

默认正文 artifact retention 86400 秒、256 项／64MB；URL 自动复用最多 300 秒。
首屏选择预算 2400 字符，其中主原文片段最多 1200；分页可选 1–20000 字符，
仍受系统通用 gate 和整体上下文预算约束，不保证大页全部进入模型。
默认最多 4 个同时获取、16 个在途／等待调用、获取 deadline 12 秒；配置重启生效。

### 验证记录

所有测试只使用固定 HTML／MockTransport／临时 SQLite／脚本 provider，
不读取真实邮件、不访问搜索服务、不调用付费模型。

```bash
.venv/bin/python -m pytest -q \
  tests/test_eval_runtime_web_quality.py::test_real_pipeline_reuses_goal_allows_search_and_retains_raw_excerpts_and_hashes \
  tests/test_web_snapshots.py tests/test_web_views.py tests/test_web_tools.py \
  tests/test_web_page_paging_quality.py tests/test_web_find_quality.py \
  tests/test_web_readability.py tests/test_web_evidence_context_quality.py \
  tests/test_web_find_recovery_quality.py tests/test_tool_result_gate.py
.venv/bin/python -m scripts.eval_web_progressive
```

最终定向测试 **160 passed（15.62s）**；修改代码／测试／评测入口的 Ruff 与 diff 检查通过。
独立只读审查覆盖缓存授权、版本、刷新、并发与接线；相关视图由受限低成本子 agent
实现，再由主 agent 集成审查。未执行全量套件或生产重启。

离线 A/B（2026-10-05，3 个合成尾部证据场景；每次 HTTP 人工延迟 30ms）：

| 场景 | 旧链路 HTTP／耗时 ms | 新链路 HTTP／耗时 ms | 本地续读 median／p95 ms | 首屏正文字符旧→新 | 条件首屏旧→新 |
| --- | --- | --- | --- | --- | --- |
| technical_tail | 3／120.69 | 1／62.25 | 0.84／0.93 | 20000→88 | 缺失→保留 |
| chinese_event_tail | 3／121.43 | 1／59.51 | 0.97／1.12 | 20000→53 | 缺失→保留 |
| unicode_tail | 3／117.56 | 1／62.50 | 0.86／0.90 | 20000→96 | 缺失→保留 |

结果序列化的 UTF-8 字节 token 上界估算：22624 → 1435／1446／1444。
这是**工具视图大小**，不是实际 provider token、总 prompt、总费用或端到端响应指标。
耗时仅为 open→find→续读的离线工具工作流；30ms 网络延迟是模拟值。
旧／新首次读取参数有意分别采用旧默认和新 query 概要，用来验收设计用途，
不代表模型总能自动选对 query，也不能证明事实回答准确率提升。
本轮新增真实搜索请求 0、付费 LLM 调用 0。

### 暴露的问题、解决方法与修复证据

| 不佳场景／原因 | 修复方法 | 验证后的效果 |
| --- | --- | --- |
| open/find/续页各自重新获取，offset/hash 可能对应不同版本 | 一个不可变完整可读快照，多种本地视图；刷新生成新 ID | 同链路 3→1 HTTP，远端内容改变后仍能续读旧版 |
| 关键证据在 >20k 尾部，先读大段前缀仍无关键条件 | query 本地选择原文与邻段，独立摘录带真实位置 | 英文／中文／Unicode 尾部条件首屏可见，实际 answer prompt 接线通过 |
| 结果字段过多，规则 gate 映射上限隐藏正文 | 调整 package 输出字段顺序、注册有界原文叶子阈值；不放宽全局 gate | 正文、引用和续读入口优先交付；强制 gate 后 provider prompt 仍保留条件 |
| 大匹配段／大邻段超预算、后置短条件被漏掉 | 严格字符预算；邻段分离摘录，未覆盖范围明确标记 | 前后短条件在预算内保留；无命中不冒充相关全文；两种视图目录均≤12 |
| Unicode casefold 展开改变位置 | 折叠索引映射回原字符 offsets | ß／中文／emoji 摘录对应原文，续读不移位 |
| 相同创建时钟下刷新可能选回旧版本 | 最新索引添加 rowid 次序 | 刷新后 URL 读取稳定选新版，旧 ID 不改变 |
| 排队许可 API 使用位置参数导致等待不受限 | 显式 timeout 参数；获取许可后记录，再做取消检查 | deadline／队列满／取消测试通过，permit 与 pending 不泄漏 |
| 不同新鲜度要求不能共享可能返回旧缓存的 flight | 获取合并 key 纳入 refresh／max_age | 新鲜度要求不同不借用宽松请求；默认 open/find 仍能合并 |
| 隔离环境 asyncio.to_thread 最小程序退出也挂住 | 比对最小复现与宿主离线验证，不修改生产代码规避沙箱问题 | 同组宿主异步 Graph 响应性测试正常通过 |
| 新增测试错误要求短原文必须有 delivery receipt；现有绑定只追踪≥700字符的长字符串 | 使用足够长的真实摘录并显式 force_gate；断言真实 provider 输入 | 覆盖缓存→gate→answer 边界，不扩大 receipt 范围或为短结果增加包装 |

### 仍需保留的限制／未关闭案例

- 既有 `web_python_multisource_conditions`、`web_sqlite_condition_brevity` 等真实语义
  案例保持 open：本轮不证明多来源蕴含核验、全部条件覆盖、最新版本识别或简短回答已达标。
- 仍不能凭 Age／redirect 或本地 cache_hit 推断搜索服务缓存原因；这类未取证归因错误
  不能因新增缓存而视为已修复。
- 这里只读有界公开页面文本；脚本动态加载、登录、反爬、PDF／图片专用解析仍不支持。
  extraction_complete 只描述抓取边界内的可读抽取，不认证整个网站／全文语义覆盖。
- 相关视图是确定性字面 token 选段，不是小模型摘要、语义重排或 CJK 分词；无命中只能提供
  明确标注的恢复前缀，必要时模型继续查找／分页，不默认预取所有网页。
- 首次 open 仍需完成一次有界正文抓取与抽取；不是服务端 HTML 流式首段。
  search 首屏只等待搜索响应，获取候选页面完全按需进行。
- 当前并发合并与 semaphore 是单 backend runtime 进程内的；多 worker 不共享许可。
  OS DNS／阻塞系统调用不能强制抢占，deadline／取消检查阻止迟到结果发布，但不保证
  所有底层调用在 12 秒准确退出。
- TTL 物理清理在下一次缓存写入执行；过期引用立即拒绝，闲置期间不承诺及时删除磁盘行。
  本轮不新增清理后台作业、不升级既有观察缓存读取授权。
- 新功能需重启 backend 才生效；本轮没有部署、重启或真实网络／模型复测。
