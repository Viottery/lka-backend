# 网页困难案例实测：2026-10-05

目标不是重复全量可用性测试，而是检查“证据能否快速被找到、真正交付给模型，并支撑
完整且简短的回答”。本轮仅新增评测／复现与记录，**不修改生产逻辑，不部署或重启后端**。

## 1. 测试条件与预算

- 真实模型仅用已配置 DeepSeek Flash；29 次调用、2 次 Brave 查询。
- 三个任务使用现有 AgentGraph／ToolExecutor／SQLite artifact／规则 gate／answer
  链路；每项上限20模型调用、3搜索、180秒，provider timeout 30秒。
- 使用隔离 SQLite 与空测试工作区，关闭真实邮件同步、记忆及消息后台分析；不读真实邮件。
- 沿用共享 $50／3000查询账本；本轮 LLM 费用 $0.154445728。账本搜索费用未计价，
  其0美元字段不能当作免费证明；按项目此前记录 $5/1000次估算，两次约$0.01，非账单实付。
- 本轮运行前 LLM 累计 $6.702263936，运行后 $6.856709664；搜索累计9→11。
- 真实任务在当前工作树运行，不是纯净 HEAD：已有用户消息／Windows 改动保留。
  首次两个命令在 import 阶段遇到知识模块并行编辑造成的临时 SyntaxError，未 dispatch；
  确认该工作线编辑完成、语法正常后各运行一次。未替用户修复或撤销那个模块。
- 三次报告的 runner 所列源码指纹与场景指纹前后相同；现有 runner 指纹列表尚未覆盖
  新增 web_cache/web_views/web_resources，不能宣传整个工作树都被冻结。
- 本轮 text 模式无 streaming 首个最终 token 指标；下表是完整任务 wall time，不是 TTFT。

## 2. 真实困难任务结果

| 任务 | wall 秒 | LLM／工具调用 | 搜索 | 输入／输出 tokens | 缓存输入 tokens | LLM $ | 回答字符 | 严格评价 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SQLite WAL：单写者、读快照、长读对checkpoint影响；至少两官方页 | 60.821 | 9／6 | 0 | 69519／9559 | 40576 | 0.054392416 | 1659 | 核心事实基本正确；附加条件与简洁性仍不足 |
| Python 3.13／3.14：默认构建、支持级别、扩展重新启用GIL；至少两官方页 | 59.872 | 15／10 | 1 | 138288／8095 | 75008 | 0.077728128 | 1222 | 未完成原合同；关键正文找到却未交付 |
| uv最新稳定版本、日期、两项变化与搜索／官方冲突 | 21.084 | 5／2 | 1 | 29170／3071 | 13824 | 0.022325184 | 1025 | 事实本样本正确；仍明显冗长 |

三项旧 runner 的机械检查均通过、工具失败均为0。**这不是3/3语义成功，更不是系统准确率。**
每项只跑一次，是已暴露任务，非未见holdout、无重复统计或严格A/B因果结论。
token来自provider用量，输出包含推理；不同于上一轮离线工具视图字节上界。

### 2.1 Python：最有价值的失败

现象：回答自己说明3.14支持级别缺官方正文、第三方扩展条款未核实；也没取3.13专属页。
同时先把“实验性→正式支持”写成结论，后文又撤销它的已核实地位，交付不一致。

逐层证据：

1. overview 的长查询含多种通用词。howto首摘录落在thread-local对象段；What’s New
   首摘录落在annotation段。它们进入prompt是完整的，却不是任务最需要的内容。
2. `web.find(query="third-party", snapshot_id=...)` 已拿到1134字符片段，其中确有
   扩展重新启用GIL的解释。缓存命中，未重新抓网页。
3. Graph执行后、observe前将pending ToolResult保存到artifact；SQLite JSON serializer
   `sort_keys=True`。网页工具的 `_reading_order` 因而不能决定持久化后字典顺序。
4. 随后的gate只取字典前12字段：这些位置现在被cache时间等字段占据，`matches`和
   `snapshot_id`都可能被省略。真实decision与answer输入里该扩展条款均不存在。
5. 模型再读`/output/matches`并投影5个字段。`observation.read`本就只对记录字段提供
   有界预览，snippet最多约440字符；1134字符片段中部的实际条款仍被省略。
   `has_more=false`仅表示匹配记录分页完毕，不表示字段正文完整读完。
6. 最终receipt对该1134字符snippet记录`covered_chars=0, coverage=partial`。
   模型最后选择说明缺口，而不是继续读精确字符串路径／snapshot位置；任务终止但未完成。

可用恢复能力仍在：直接读`/output/matches/0/snippet`能取完整有界文本，或用snapshot_id
和snippet_start续读。新增离线测试已证明一次HTTP后即可恢复，不必重搜或换模型。
所以这是**交付策略＋读取决策的组合问题**，不应全归因于LLM知识或网络质量。

另一个成本问题：Python最后answer调用24.710秒、5321输出tokens，其中4768是推理，
约89.6%；实际用户得到的仍是未完成且1222字符的回答。记录现象，不据单次样本收紧推理配置。

官方人工核对依据：

- [3.13 free-threading](https://docs.python.org/3.13/howto/free-threading-python.html)：实验性、可选安装／构建、未显式适配扩展的运行行为。
- [3.14 free-threading](https://docs.python.org/3.14/howto/free-threading-python.html)：可选构建与第三方扩展的说明。
- [3.14 What’s New](https://docs.python.org/3.14/whatsnew/3.14.html)：支持级别变化的正文入口。

### 2.2 SQLite：查到证据仍不等于好用

优点：两官方正文，原页4次后续读取均cache_hit，核心单写者／固定读快照／checkpoint
影响的证据已取到，未声称完整通读原网页。

不足：用户要求简短，输出1659字符、大段英文引文，另加了未要求的操作建议。
提到WAL可能持续增长，却仍没有显式补上持续写入的必要条件；`context_complete=false`
的starvation片段对应缺邻段，也未按其区间补读。此处评价为“缺限定／简洁性不足”，
不把“可能”强行标成无条件必然错误。

核对来源：[WAL文档](https://sqlite.org/wal.html)、
[Isolation文档](https://sqlite.org/isolation.html)、
[checkpoint接口](https://sqlite.org/c3ref/wal_checkpoint_v2.html)。
长读限制checkpoint回收／进度，不会凭空产生新写入；页内相邻条件值得完整读取。

### 2.3 uv：首屏可用，但摘要篇幅失控

Agent在一次search后按`max_age_seconds=0`取官方CHANGELOG前4000字符，
确定0.12.23／2026-10-03和两项变化，并明确未交叉读取release/tag。
人工额外检查[官方发布页](https://github.com/astral-sh/uv/releases/tag/0.12.23)，
版本、日期与两项变化吻合。人工浏览不计入应用Brave账本；不能算模型自己完成交叉核验。

不足：回答1025字符，追加两项bugfix和一整段覆盖说明；任务只要求两项变化与简短来源。
还说第三方页“未同步”，但只证明版本不同，没有核验其更新机制或不同时间范围。
官方优先结论有据，第三方差异成因仍不应过度诊断。

## 3. 固定困难复现

新增`tests/test_web_progressive_challenges.py`：

- 持久化往返后仍应保留find证据／snapshot入口：当前**strict xfail**，不是通过。
  相同原始ToolResult在直接gate时保留条款，经真实SqliteAgentRunStore往返后丢失。
  已知失败单独标记，未来修复需移除xfail；不能放宽断言或当它已闭合。
- 记录字段投影虽结束分页仍可能截断snippet；精确字符串路径读全：通过，一次HTTP。

命令：

```bash
.venv/bin/python -m pytest -q tests/test_web_progressive_challenges.py
.venv/bin/python -m scripts.eval_web_progressive_challenges
```

离线对抗评测共9项：7项满足各自工具判据、2项失败；首视图命中3项、显式恢复6项。
这些指标涵盖不同能力判据，**不能解释为7/9语义准确率**，也不是系统随机任务成功率。

| 对抗场景 | 首屏／首次结果 | 后续／判据结果 |
| --- | --- | --- |
| 相隔80段的资格排除条款 | 没有排除条件 | 本地find定位→分页读回，一次HTTP |
| 通用高频词挤占结果 | 命中重复背景，漏掉限制 | 用具体锚点find恢复限制，一次HTTP |
| 中文自然问句，英文OTP说明在尾部 | 明确字面无命中，前缀也没关键内容 | 调用方改用OTP锚点可本地恢复；不是自动翻译／语义召回 |
| 45k字符单段的终末标识 | ≤1200字符视图命中末尾 | 有界选段判据通过 |
| 两词分散、整句不匹配 | no_literal_match | 返回非语义token上下文，未伪称整句匹配 |
| HTML表格行／列边界 | `RegionLimitNorth12South4` | **失败**：单元格与行没有分隔，关系还原不可靠 |
| Unicode／组合字符搜索摘要压缩 | 短摘要 | 本地恢复700字符标准化摘要，无追加provider请求 |
| URL近期快照过时、刷新503 | 旧版可用 | 刷新明确失败，旧固定引用仍可读；两次HTTP |
| 完整ToolResult经真实SQLite往返再gate | 匹配／snapshot入口被隐藏 | **失败**：原文有条款，交付视图却没有 |

后续操作是脚本指定的诊断动作，输入锚点来自场景设计；不代表真实Agent会自主选到它们。
自然语言跨语言测试只验证诚实零匹配＋显式新锚点可恢复，当前字面工具不支持自动翻译。
HTML表格失败是信息结构损失，不据此断言模型一定读错所有表格。

最终定向pytest：**30 passed、1 strict xfailed（9.96s）**，范围为新增困难复现和既有
web_snapshots；xfail就是仍存在的已知失败，不计入通过。新脚本离线执行完成，9项逐项
记录；Ruff与diff检查通过。无全量套件、无新的付费重试。

## 4. 下一轮优先级（本轮不实施）

1. **P1：持久化后的证据交付。** 不再靠dict插入顺序表达证据优先级。由注册工具声明
   有界证据／导航字段，generic gate从metadata选取；序列化／checkpoint不改变效果，
   不在core写死web字段，不直接扩大整体gate。真实Graph往返成为必测层。
2. **P1：缓存读取决策。** 明确区分记录预览与字符串阅读，投影字段返回具体续读路径／
   截断状态；先消除重复匹配窗口和重复无效投影，再按用户需要读取原文。不会默认读全网页。
3. **P1：相关片段选择。** 通用高频词／URL文字会抢分；用短锚点、词区分度、标题／
   段落结构与多核查点覆盖预算，而不是为Python／SQLite写关键词分支。
   表格抽取要保留行／列／表头边界；当前简单拼接会损坏票务、日程、价格等实际数据关系。
4. **P2：回答交付。** 保留必要限定和可追溯链接，减少逐段审计、重复免责声明与不必要引文；
   未取证主张不能先确定再撤回。原任务覆盖不足不能靠关键词／两URL标机械成功。
5. **评测配套。** runner指纹补全新增网页模块，增加持久化→gate→provider层与字段回读
   场景；将机械通过、事实支持、任务完成、简洁性和性能分开评分，不合成虚高准确率。

## 5. 私有运行产物与复现入口

真实复测命令（会调用付费模型，**不是普通离线回归命令**）：

```bash
.venv/bin/python -m scripts.eval_runtime_web_quality --remote --root-go --case heldout_web_sqlite
.venv/bin/python -m scripts.eval_runtime_web_quality --remote --root-go --case heldout_web_multisource
.venv/bin/python -m scripts.eval_runtime_web_quality --remote --root-go --case web_search_release
```

本轮原始report／web_analysis留在本地未提交：

- `data/quality_runs/linux_20261005/runtime_web_sqlite_20261005T115249932210/heldout_web_sqlite_20261005T115249943614/`
- `data/quality_runs/linux_20261005/runtime_heldout_web_multisource_20261005T115254029695/heldout_web_multisource_20261005T115254036056/`
- `data/quality_runs/linux_20261005/runtime_web_search_release_20261005T115326717743/web_search_release_20261005T115326722907/`

runner的`semantic_review=pending_root_review`是原生成状态；本文件是随后完成的人工式
根agent复核记录，未改原report/hash，不把它升级为独立人类gold或统计准确率。
旧Python／SQLite语义bad case继续open，本轮没有因工具均成功就关闭它们。
