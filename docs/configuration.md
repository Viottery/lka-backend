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

## 记忆、后台与可选专家

- `[memory]`：学习开关、模型提取、近期整理上下文、记忆召回及压缩配置。
- `[background]`：共享后台预算、并发、排队和请求时间等设置。
- `[agent]`：多 Agent、邮件专家、外部代码专家及任务执行配置。
- `[safety]`：工具审查模式；`manual` 等待人工决定，`llm` 使用模型审查，
  `skip` 仍记录审查但自动放行。

多 Agent 规划需要 `orchestrator = "langgraph"` 和
`multi_agent_planning_enabled = true`。外部 Codex 专家需单独安装、认证并显式开启；
Windows 的 `codex_binary_path` 指向原生 `.exe`，不能使用 `.cmd` / `.bat` 启动器。

基础 TOML 配置重启后加载。通过 `/background/config` 保存的配置具有 active / desired
两种视图，管理端应展示是否待重启；暂停、恢复和取消是独立的即时控制操作。

## 路径与数据保管

Windows TOML 路径使用 `"D:/Work/project"` 或单引号的 `'D:\Work\project'`；
Windows 后端应收到实际可访问的 Windows 路径，而不是 `/mnt/d/...`。
工作区根目录可通过 `LKA_WORKSPACE_ROOTS` 配置，多个目录以分号分隔。

数据本地保存不代表模型请求完全离线。远程模型会收到所选对话及资料片段，
搜索服务会收到查询；完整日志也可能包含这些内容。将配置、授权和运行数据按个人资料保管。
