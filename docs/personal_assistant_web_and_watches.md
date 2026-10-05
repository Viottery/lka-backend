# 网络检索与持续关注开发设计

状态：只读网络检索、每日关注后端调度、会话简报已实现；网络已做真实 provider 测试，
持续关注的模型语义质量及前端推送仍需分别验收。真实质量记录见
[Linux 质量迭代](quality_iteration_2026-10-05.md)，不能将工具连通当任务质量通过。

持续关注的长期全局指导使用 `data/instructions/watches/AGENTS.md`；其加载、维护与权限边界见 [Agent 指导文件](agent_instruction_files.md)。

## 目标和边界

系统要从一次性问答发展为持续责任：用户能定义“关注什么、从哪里看、何时检查、什么变化值得提醒”，系统定期查证、记录变化、提供来源与待用户决定的问题。这与 [OpenAI Docs 的 dots](https://learn.chatgpt.com/docs/dots) 所描述的持续跟进、在关键决策时请求输入的方向相似，但本地后端不能像云端服务一样在设备关机时继续运行，除非另行部署常驻服务。

首版只读采集和站内简报，不自动发送邮件、购票、下单、登录网站、修改外部账户或把网页文本当作指令。网页搜索结果与页面正文都是不可信证据；工具不会声称搜索结果代表网络全集，也不会把搜索摘要当作已核验的演出或票务状态。需要人工确认的动作由既有安全门控制。

## 后台任务授权原则

本地个人助理允许用户授权的后台任务主动读取邮件、知识库、工作区或网络，
无需每次定时触发再确认。授权附着于持久任务定义，而非全局的“后台可读取一切”
开关：定义/修改时记录允许的账户、知识源、工作区、工具类别和触发条件；
每次运行以当前任务版本和仍有效的连接/路径权限构造执行 scope，并与已注册的
工具能力求交集。撤销连接、删除或暂停任务后，后续运行不得继续沿用旧授权。
读操作在 scope 内自动执行；发送邮件、修改文件、外部购买等写入或副作用行为
仍走原有 Safety Gate，不因任务已经获准读取而自动获准执行。读取到的邮件、网页
和文件内容是证据，不是新的授权指令。后台记忆提取/会话压缩只有处理会话的默认
授权，不继承关注项的领域源读取 scope。此规则也适用于将来的其他定时工作，
而不只针对 Watch。

## 阶段 A 网络检索工具包

- `web.search(query, mode=web|news, freshness?, limit?)`：可替换搜索 provider；先提供 Brave Search API 适配器（需用户配置服务端密钥）。返回标题、URL、有限摘要、发布时间/抓取时间（若 provider 有）、查询参数、结果数量和可能仍有更多结果的提示。服务不可用、限额或网络错误与“零结果”严格区分。
- `web.open(url, offset=0, max_chars=20000, expected_text_sha256?)`：只读取公开 HTTPS
  文本/HTML，不使用用户登录态、不执行 JavaScript；限制类型、1MB 响应、时间、重定向和
  单页可读文本字符数。拒绝内网、环回、链路本地、保留地址及 DNS 重绑定/重定向 SSRF。
  返回规范化 URL、抓取时间、当前 offset/returned_chars、完整抽取 total_chars、续页位置、
  has_more、text_sha256；offset 是规范化可读文本的 Unicode 字符索引，不是 HTML 字节。
  每次调用重新抓取（snapshot_stable=false），续页携带前页 SHA-256 时，文本变化会拒绝
  返回，需从零重读；不能将未带版本校验的多次抓取声称为一个稳定快照。
- `web.find(url, query, offset=0, limit=5, expected_text_sha256?)`：在完整可读抽取中做
  不区分大小写的字面定位，包括首个 20k 字符之外的内容；不是语义搜索，不执行额外
  模型调用或 Brave 查询。query 仅在本地处理，不发给网页服务器。最多五个 400 字符
  片段，返回原 Unicode 匹配与片段偏移、抽取指纹；offset 是命中序号，与 open 的字符
  偏移不同。用 snippet_start 再 open 可扩读上下文，携带指纹防止混用版本。每次仍
  重新抓取，沿用 open 的网络/SSRF/类型/字节限制和现有来源约束。无命中不证明网页
  没有相关语义，也不能覆盖关注项已收集正文的证据及对应抓取时间。
  `match_status` 区分 phrase_matches、全局 no_literal_match 与有匹配但当前 offset 超范围的
  offset_exhausted。只有 offset=0 的全局零匹配会附同一次 extraction 的 recovery_preview：
  最多三个非重叠 query-token 原文窗口（总≤1200字符），无 token 候选则返回开头≤1200。
  不再为此多发 HTTP/LLM/搜索请求；仍是原 Unicode offset 和同一文本指纹。token 仅取
  最长的四个 Unicode word runs，每个至多64个候选，无 CJK 分词、同义词或完整语义相关
  保证。预览不是短语 matches，不代表核验/完整阅读，complete 仍只指字面检索分页。
- Agent 通过 Tool Package Registry 懒展开并经 ToolExecutor 调用；不在 Agent core 里硬编码网络工具。长结果仍进现有 tool-result gate，模型可按需读缓存。网页与邮件/RAG 同为证据，不赋予网页创建事项或执行命令的能力。
- `observation.search(..., distinct_contexts=true)` 可按不同完整上下文窗口分页，避免近邻
  标签/链接挤占命中数；默认仍按 occurrence 分页。去重窗口不会因单页预算再次缩短，
  放不下时返回较少窗口及 next_offset。缓存的 complete 只描述该缓存内容，不能证明
  web.open 的其他源页已读；用 web.open 的 next_offset 才能读取未抓入当前结果的部分。
- 先用 mock HTTP 结果验证解析、分页、限额、超时、SSRF/重定向及截断；没有密钥时必须明确不可用。真实 provider 的连通性只有在用户配置密钥后才能验收。

当前默认 Brave Search。其 [官方价格页](https://brave.com/search/api/) 标示 Search 计划每 1000 次请求 5 美元，每月附带 5 美元额度；[额度说明](https://api-dashboard.search.brave.com/documentation/resources/help-feedback) 允许预付额设为 0，以免费月额度运行，超限请求会被拒绝。本地默认每 UTC 月最多请求 900 次（可配置），相当于按当前价格计的 4.50 美元用量；这只约束此后端产生的请求，建议同时在 Brave 控制台设预付额 0。每个 Agent 任务最多 8 次工具调用，但工具调用数不等于搜索次数预算；要严格保证“搜索 API 花费不超过 LLM 花费”，仍需把两类实际用量统一记账。未配置密钥时 `web.search` 会明确不可用，不会偷偷切换到付费渠道。

## 阶段 B 持续关注对象与简报

`Watch` 是持久化的关注目标，不与单一会话绑定。字段包含 `watch_id`、目标描述、关注类别（邮件/新闻/网页/本地事项，可组合）、允许的数据源与账户 scope、时区、每日检查时刻、起止/暂停状态、重要性规则、投递策略、版本号。创建与修改是本地持久化写操作，须在 API/Agent 控制面显式确认；不能因自然语言提到“每天”就静默建立永久任务。

运行记录独立于定义：`(watch_id, scheduled_for)` 唯一约束、状态、开始/结束时间、lease owner/expiry、Agent run ID、错误类别、证据引用、内容指纹、资源消耗。一个 watch 的下次执行时间由时区与日历日期计算，不靠上次结束后睡 24 小时。关闭后端期间不会运行；重新启动默认只补最近一次到期检查，避免长时间离线后的任务风暴。每个运行有时长/调用/token 预算、并发上限、重试退避和可取消状态。

`Briefing` 应区分：新变化、仍有效但未变、已无法确认、需要用户决定。每条结论带来源 URL/邮件 ID、来源时间、抓取时间、观察值与上次观察值。跨不同来源的同一事项可提供稳定原始 `subject_key`（1–200 字符），服务端仅哈希一次；它不等于外部消息 ID，也不授予来源或工具权限。旧 event_id 保持兼容；缺稳定身份时仍可能重复提醒，不能声称已自动完成语义对齐。内容指纹记录本次结果，不能因同事项身份相同而忽略观察值变化。搜索没找到结果不能推断“演出取消”或“票已售罄”；票务状态以官方票务/主办方页面为优先证据，并注明可能过期。

用户需要 CRUD 管理、暂停/恢复、立即运行、查看最近执行/失败、已读/归档简报。每次触发/推送创建一个新的独立会话；`Occurrence` 与 `Briefing` 暴露本次 `session_id`，用户在该次会话继续追问。关注项本身不暴露固定会话 ID。前端推送、桌面通知、邮件/消息推送暂未实现；前端可先轮询简报/运行 API。摘要不应存入普通 session metadata；完整证据与 run trace 保留在本地受控存储，前端只取最小必要摘要。

## 阶段 C 安全的后台 Agent 执行

当前 `runtime.run_agent_turn_async()` 是通用顶层 Agent 入口，不能仅靠提示词保证计划任务只读。定时运行必须有独立的、持久且不可扩大权限的 ToolView：从用户确认的 watch scope 构造，再与 Tool Registry 能力交集；执行时 ToolExecutor 强制 `read_only=true`，禁止 `mail.sync`、matter 写入、bash 写操作等。优先复用现有 ChildExecutor/ContextSnapshot 授权机制，或扩展通用 turn 入口支持经校验的根运行 ToolView；不能直接把一个“请只读”的字符串交给无约束顶层 Agent。

调度器只负责唤醒、租约、幂等和预算，不编码邮件/网页领域规划。Agent 为每个 occurrence 新建独立会话，加载该 watch 上次简报与少量相关证据，按需调用已授权只读工具，再形成有来源的变化摘要。后台执行在线程/异步 worker，不阻塞 FastAPI 事件循环；服务停止时取消或标记未完成运行。已有 Outlook 后台同步可作为本地运行生命周期参考，但不能把网络搜索/LLM 阻塞塞进它的单线程循环。

单次关注检查当前最多 40,000 token、6 次 LLM 调用、8 次工具调用、120 秒。token 预算包含输入提示；未配置 tokenizer 时采用保守计数。原 30,000 上限在带先前观察和指导工具的两封短邮件检索/加载流程中可能在最终决策前耗尽，因此提高到仍有界的 40,000。只在 child 为 PARTIAL、唯一缺项为 `child_budget_finish`、没有 failure，且返回非空完整分区 JSON/合法字段时，允许 normalizer 逐项核验并交付已收集的事实。简报必须附覆盖未完成的 unconfirmed 项，保存 child 的 PARTIAL/缺项并记录 watch_partial_delivery；occurrence succeeded 仅指投递成功，不是完整核查成功。失败、阻塞、取消、坏 JSON、空结果或其他缺项不沿用此路径。

当前执行适配器使用受限 Child Agent：只开放经注册的只读工具，分别对本地 source/account
与公开 web 明确授权；同一关注项同时读取私密账户与调用外部搜索时，还必须显式设置
`scope.allow_mixed_private_external=true`。网页内容及简报不能提升工具权限。
当前简报会解析变化/未变/无法确认/需决策，依据实际收集引用和有界原文片段做确定性支持
检查，并与上次观察去重；重要性阈值、包含/排除关键词和来源时效规则生效。无法解析、
伪造/缺失引用、片段不支持及检索失败进入 unconfirmed，不算“没有变化”。这是保守的
字面证据检查，不是完整语义事实验证，也不具备自动官方来源优先级。票务/演出仍需复核。
已核验项的 claim 和 current_observation 各自必须是引用来源的一整句/行/分句，保留否定、
条件、数字、符号和问号；不能通过截子串、改写或拼接推断获得核验。未知结论可无引用，
但只放 unconfirmed。不支持的 title 不拼进事实摘要，未收集的 refs 不作为展示来源。
上次完整观察留在本地供确定性比较；给模型的历史预览仍限每个字段 240 字符，避免长来源
挤占后台检查预算。可选字段类型验证不是完整 JSON schema 或 semantic entailment verifier。
已获准源工具之后，watch ToolView 还允许当前 child 的 observation.read/search/group；
缓存导航不是独立信息源，不能让无注册源工具的任务启动，也不能读其他 occurrence 缓存。

### 当前使用方式

1. 在 `config/local.toml` 的 `[web_search]` 配置 `api_key_env = "BRAVE_SEARCH_API_KEY"`，在本地环境或 `.env` 设置对应密钥；未配置密钥时可先用 `web.open` 阅读公开页面，含 web/news 的定时任务会明确失败。
2. 向 `POST /watches` 提交例如 `{"title":"演出票务","goal":"每天核查某演出官方票务状态并给出处","timezone":"Asia/Shanghai","daily_time":"09:00","categories":["web","news"],"scope":{"web_enabled":true}}`。响应给出长期 `watch_id`，此时不创建会话。
3. 到点后后端启动最多两个后台 worker；也可调用 `POST /watches/{watch_id}/run-now`。每个执行记录及简报都有独立 `session_id`；查询 `GET /watches/{watch_id}/runs`、`GET /watches/briefings` 或通用会话接口查看结果，并在该次会话追问。`POST /watches/{watch_id}/pause` 暂停。前端可轮询这些只读接口后自行决定推送；后端不发送外部通知。

当前只有每日时刻与手动运行两类触发；结构化变化、引用检查、指纹去重及基础重要性规则
已有后端实现。隔离邮件的四次真实 Graph/tool-executor 执行覆盖新增、未变、新旧并存和
检索失败，每次简报创建独立会话；使用脚本模型，只证明接线/规则，不代表真实模型语义
准确率。不应直接用于自动购票、库存告警或其他需要高准确率的决策。后续仍需事件触发、
前端推送游标与真实邮箱/票务语义评测，尤其长来源、状态否定及多日变更。
2026-10-05 另做实际配置模型的三-slot 隔离 replay：新增南楼、南楼未变、新邮件北楼变化
均有来源且负责人保持未知；每轮独立会话、冻结授权与原数据完整性检查通过。总 85.55 秒
/18 调用，后两轮仍明确 PARTIAL。该小样本说明预算内可交付已核验信息，不代表完整覆盖、
真实三天连续运行或普遍票务准确率；失败与改善指标均见 Linux 质量迭代记录。

## 实施顺序与验收

1. 网络工具与配置：mock 测试 + Registry 集成 + Agent 可发现；无密钥失败清晰。
2. Watch/Run/Briefing SQLite 与 API：事务性 claim、唯一 occurrence、启动补跑、暂停和本地 inbox；暂不自动执行 Agent。
3. 只读 scoped Agent 运行适配器：拒绝任何非只读工具、账户越权与上下文跨 watch 泄漏；预算/超时/失败可追踪。
4. 邮件、新闻、演出/票务三类端到端样本：检查来源可信度、变化去重、时间新鲜度、假阳性、LLM 成本和每日简报质量。真实 provider 与真实邮箱验证需要用户主动配置授权。
5. 进一步做主动跟进：用户反馈、重要性学习、跨多日状态和待确认动作；始终保留暂停、撤销未来检查和审计入口。
