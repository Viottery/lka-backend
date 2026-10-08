# 配置模型与数据连接

## 配置放在哪里

| 内容 | 默认位置 |
| --- | --- |
| 密钥及环境变量 | 仓库根目录 `.env` 或启动进程的环境 |
| 模型、工具、邮件及后台基础配置 | `config/local.toml` |
| 数据库、会话、知识、记忆及日志 | `data/runtime`，可由 `LKA_DATA_DIR` 修改 |
| 完整配置字段与注释 | [配置模板](../config/local.example.toml) |

真实配置和运行数据只保存在本地。更新应用时不要用模板覆盖已有文件。
`personal` 启动器加载私有 `.env`，但不覆盖进程中已明确设置的环境变量。

## 接入模型

后端内置 mock 和 OpenAI-compatible 两类 provider。下面是需要填写的最小片段，
请修改现有 `[llm]`，不要重复添加同名 TOML 表：

```toml
[llm]
provider = "openai_compatible"
base_url = "https://your-provider.example/v1"
api_key_env = "LKA_MODEL_API_KEY"
model = "your-model-id"
```

`base_url` 与 `model` 替换为服务商实际值。在本地 `.env` 中为 `LKA_MODEL_API_KEY`
设置密钥；上述片段不含真实凭据。也可以配置多个命名客户端及精确模型的容量、
输出预留和 tokenizer 路径，字段见模板。

模型选择顺序为：当前请求覆盖 → 会话选择 → 后端默认。
改变默认模型不代表现有会话也会强制切换。流式输出、JSON、工具调用及推理控制
需要与所用服务商实际能力匹配。

## 邮件

- Outlook 使用 Microsoft Graph 设备授权；在 `[mail.outlook]` 配置启用状态、client ID
  和授权文件位置。按 `/mail/outlook/auth/start` 返回的地址和验证码登录，再通过
  `/mail/outlook/auth/complete` 完成授权。详细字段见 [API 说明](api_contract.md)。
- IMAP 为可选接入，配置主机、账号及 `password_env`；应用密码放环境变量，不写入仓库。
- 启动、后台轮询及按需同步只负责更新本地邮件，阅读和整理仍在普通 Agent 会话中进行。
- 邮件工具会告知模型当前自动同步开关和轮询间隔；启用后通常直接读取本地邮件，无需先调用同步。显式同步／立即刷新请求仍可调用 `mail.sync`，此提示不保证最近一次同步成功。
- 只读邮件专家由 `[agent].mail_expert_enabled` 控制，需要时单独开启。

## 网络搜索

当前搜索使用 Brave Search API。在环境中设置 `BRAVE_SEARCH_API_KEY`，
并核对 `[web_search]` 的 provider、查询配额、快照保留及超时设置。
缺少密钥时搜索会返回不可用；读取公开 HTTPS 网页不需要搜索密钥。

## 本地检索模型

`[embedding]` 管理语义索引，`[reranker]` 管理本地重排序，
`[query_rewrite]` 管理多查询数量和检索并发。
模板默认以关键词检索起步，模型权重优先从本地缓存读取。
已有文档的语义索引通过 `/knowledge/semantic-index/sync` 同步；
首次下载模型需要显式允许，不会在每次检索时自动下载。

## 消息采集与分析

后端接收外部采集器上报，记录与模型分析是不同的开关。
先配置允许记录的会话，再根据需要开启分析。QQ 登录、桥接和媒体缓存由独立采集端负责。

导入凭据、管理读取凭据和人工控制凭据分别用于不同操作：
`LKA_MESSAGES_IMPORT_TOKEN`、`LKA_MESSAGES_API_TOKEN`、`LKA_MESSAGES_CONTROL_TOKEN`。
不要复用 QQ 桥接凭据，不把这些值交给模型或放进浏览器脚本。
原生前端启动器可能使用 Windows CurrentUser DPAPI 配对控制凭据；新电脑应重新配对。

`[message_history]` 可设置算法、每批输入输出、工作累计额度、服务与会话的滚动配额。
累计工作额度不是模型上下文大小；普通重试和恢复不会清零已用量。
`message_encoding = "compact_records"` 对 `compact` / `selected` 的新工作共享重复元数据默认值，
每条消息的来源别名、作者和正文仍直接绑定，保留原时间和原生元数据；它不改变消息采集许可或阅读范围。
工作首次冻结输入时保存编码选择，已有检查点仍按原编码续跑，恢复原格式只需设回 `records`。
`codec_v2` 保留为实验及旧检查点兼容格式；无损编码不保证模型正确关联字典索引和消息来源，
不能仅凭格式校验成功将它放量。

`reading_output_style = "concise"` 让 `compact` / `selected` 的新工作使用短摘要，减少原文复述和
高亮／重要性条目的重复；不同关键事件、日期、条件、归属和证据仍须保留。参与者与焦点候选
需要核验的精确引文保持原样。长度是表达目标，不是本地裁剪或丢弃关键内容的规则。
`concise_output_tokens` 默认 `8192`，为此风格的首次生成和修复分别设置输出上限；
`standard` 与 `legacy` 仍使用 `generation_output_tokens` / `recovery_output_tokens`。
输出风格及输出上限随新工作的检查点冻结，旧检查点保持原约定；提高上限会提高调用预留，
仍受原有工作、服务、会话及后台池额度约束，不会清零已用量或追加恢复次数。

`selector_algorithm = "multi_lane"` 配合 `selector_rollout_percent`，可对 `selected`
模式的新工作按稳定比例试用关键事件、话题候选代表和独立探索选择；默认比例为 `0`。
比例调整不改变已冻结工作的选择器，回退为 `0` 后新工作使用原选择器。
候选聚类不是语义理解，未选中或正文未知的消息不能计为模型已读；灰度比例不授予新来源许可。

## 记忆、后台与可选专家

- `[memory]`：学习开关、模型提取、近期整理上下文、记忆召回及压缩配置。
- `[background]`：分池后台预算、共享并发、排队、请求时间和异常消耗熔断设置。
- `[agent]`：多 Agent、邮件专家、外部代码专家及任务执行配置。
- `[safety]`：工具审查模式；`manual` 等待人工决定，`llm` 使用模型审查，
  `skip` 仍记录审查但自动放行。Agent 的每次工具调用（包括只读调用）都会经过所选模式；
  单次请求可提高审查级别，不能降低本地配置要求。
  安全审批不额外设置生成 token 上限，输出预留只用于上下文和并发预算计量；
  模型自身容量、超时、取消与显式子任务额度仍生效。若模型输出仍被截断，
  工具保持不执行，记录为审批未完成，而非模型明确拒绝。

多 Agent 规划需要 `orchestrator = "langgraph"` 和
`multi_agent_planning_enabled = true`。外部 Codex 专家需单独安装、认证并显式开启；
Windows 的 `codex_binary_path` 指向原生 `.exe`，不能使用 `.cmd` / `.bat` 启动器。

基础 TOML 配置重启后加载。通过 `/background/config` 保存的配置具有 active / desired
两种视图，管理端应展示是否待重启；暂停、恢复和取消是独立的即时控制操作。

记忆提取和自动上下文压缩使用 `background_memory`，消息分析使用
`background_message`，两者不共享 token／费用额度，但仍共享模型并发及前台优先级。
`memory.max_job_tokens` 默认 `0`（不设常规任务累计额度）；旧配置显式正数仍生效。
`background.hourly_token_limit`、`daily_token_limit` 和 `daily_cost_limit` 分别约束每个
非记忆后台池，不约束记忆／压缩。消息模块原有服务、会话及工作累计限额继续生效。

异常消耗保护不能关闭：`memory_task_fuse_tokens` 默认 262144，
`memory_hourly_fuse_tokens` 默认 2000000，`memory_daily_fuse_tokens` 默认 10000000，
均须为正数。它们是紧急停止阈值，不是达到后自动恢复的普通配额。
预约和实际用量均检查；触发后持久化告警，取消同池在途调用，阻止新调用，重启不解除。
健康接口与 SSE 提供状态，用户显式确认后通过预算重置接口恢复。未知实际用量保守计量，
取消不能保证服务商不再收费。单次输出、模型容量、网络超时和有界恢复仍保留。

入库前旧记忆匹配通过 `reconciliation_max_items`（默认48）及
`reconciliation_max_chars`（默认16000）控制候选视图，全文分页查找旧条目，
不只截取最近记录。全局和项目共同使用视图预算；完整条件不会被截断。
`consolidation_enabled=true` 默认启用独立后台整理，变化后去抖60秒，另每6小时补查；
分别由 `consolidation_debounce_seconds`／`consolidation_interval_seconds` 配置。
`consolidation_batch_items=24` 限制每个模型视图，`consolidation_min_confidence=0.9`
控制自动发布阈值。采用当前配置的记忆模型及记忆计量池，不另固定模型。
关闭远程提取时仍可整理字面重复，语义整理不调用模型。

## 路径与数据保管

Windows TOML 路径使用 `"D:/Work/project"` 或单引号的 `'D:\Work\project'`；
Windows 后端应收到实际可访问的 Windows 路径，而不是 `/mnt/d/...`。
WSL 后端的配置则使用 Linux 路径；读取 Windows 文件时使用实际挂载路径，
工作区、模型/tokenizer 缓存、授权文件及媒体目录都要分别核对，不能照搬 Windows 配置。
工作区根目录可通过 `LKA_WORKSPACE_ROOTS` 配置，多个目录以分号分隔。

数据本地保存不代表模型请求完全离线。远程模型会收到所选对话及资料片段，
搜索服务会收到查询；完整日志也可能包含这些内容。将配置、授权和运行数据按个人资料保管。
