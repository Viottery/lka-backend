# Windows 原生适配计划与 TODO

日常同步、启动停止、配置及跨电脑迁移请看 [Windows 操作手册](windows_operations.md)。
以下保留各迁移阶段的实施与验收记录。

日期：2026-10-03。基线：`2428baa`。目标是在 Windows 原生 Python 下完成当前后端
应用的等效运行，保留 Linux 支持，并用真实 Windows 执行结果验收。

范围更新：用户明确只迁移应用，测试相关部分不需要。因此停止测试套件移植和完整
pytest，撤回本轮测试文件修改；Windows 应用副本省略 `tests/`、`evals/`、评测脚本及
开发测试依赖。验收使用隔离数据下的实际安装、原生依赖加载、HTTP/Agent/后台任务
与退出清理。源仓库的测试和评测保持原样。

## 环境与交付边界

- 源工作区：`/home/viottery/lka_backend`，开始时 Git 干净。
- Windows 应用：`C:\Users\xc133\projects\lka_backend`，新建本地克隆，以稀疏工作树
  省略测试/评测；Git 历史保留。应用修改也保留在源工作区。
- 原生环境：Windows 11 build 26200、Python 3.12.0、Windows PowerShell 5.1、本地 NTFS。
  应用解释器为 `.venv\Scripts\python.exe`，与 Linux 虚拟环境完全独立。
- 同步应用、说明、配置模板与 `uv.lock`。不迁移个人 `.env`、`config/local.toml`、数据库、
  日志、邮箱 token 或模型缓存；Windows 缺失配置从 example 初始化，默认 mock/邮箱关闭。
- 工具名 `bash.*` 和 HTTP 合同兼容，实际 shell 语法由平台工具元数据提供。
  Windows 验收使用原生 Python/PowerShell，不经 WSL 或 Git Bash 执行应用。

## 实施顺序

| 阶段 | 工作 | 验收路径 |
| --- | --- | --- |
| 1 环境与清单 | 创建 Windows 副本，安装锁定运行依赖；扫描运行代码及启动工具 | 原生 Python、DLL 和时区加载 |
| 2A 命令执行 | PowerShell 编码、同步/后台终端、stdin、Ctrl-C、超时及进程树清理 | 实际 Windows 命令/终端，保留 POSIX PTY |
| 2B 文件访问 | 路径、junction/reparse/ADS、句柄式读取与目录浏览 | 真实 NTFS 链接及替换竞态，拒绝越界 |
| 2C 其他依赖 | 外部专家 stdio、环境变量、SQLite 向量扩展、时区 | 原生协议往返、扩展加载及实际数据操作 |
| 3 集成 | 同步生产实现与安装工具，独立复核文件和进程边界 | 修复复核发现，原生进程探针复验 |
| 4 应用验收 | Windows/Linux 启动真实 HTTP 服务，使用隔离数据 | Agent、SSE、导入、记忆、时区、活动终端退出 |
| 5 交付 | 更新平台说明、合同、README 与本 TODO，核对双工作区 | 编译/静态检查、文件一致、清理验证产物 |

2A、2B、2C 在环境准备后独立推进；3 汇总生产改动，4、5 完成集成验收和交付。

## TODO

- [x] 确认 Windows 原生执行通道、Python、PowerShell 和目标路径。
- [x] 检查仓库规则、Git 状态、平台目标与 API 合同。
- [x] 创建 Windows 应用副本及独立运行环境，保留源工作区。
- [x] 记录并处理生产实现中的 POSIX/Linux 依赖。
- [x] 命令执行迁入平台适配器，提供 PowerShell 等效执行及编码处理。
- [x] 后台终端支持 stdin、分页输出、Ctrl-C、状态及清理。
- [x] 同步超时、后台终止、后端关闭时清理对应进程树。
- [x] Windows 保守只读白名单，保留既有安全审查和 Linux 分类。
- [x] 文件预览/列表使用句柄链与句柄枚举，拒绝 reparse/junction 越界和替换竞态。
- [x] 检查盘符/UNC 解析、大小写、空格/中文、ADS、编码与换行；UNC/SMB 集成边界见下。
- [x] 外部专家使用原生 stdio 进程、必要的 Windows 环境继承、完整 Job 清理。
- [x] Codex 工作副本扫描接入安全目录枚举，拒绝根目录被替换及敏感目录绕过。
- [x] 验证 SQLite 向量扩展、FastEmbed/ONNX Runtime 加载与 IANA 时区数据。
- [x] 按范围更新停止测试套件迁移，撤回源测试修改并移除 Windows 开发测试依赖。
- [x] 提供 Windows 环境准备脚本、应用同步排除规则及原生启动说明。
- [x] Windows/Linux 编译、变更相关静态检查及 diff 检查。
- [x] Linux 实际应用流程通过。
- [x] Windows 原生 HTTP/Agent/后台任务及运行终端随服务退出通过。
- [x] 独立复核关键安全变更并修复发现，复验最终关闭代码。
- [x] 更新文档、记录限制、核对应用副本及最终 diff，清理 Windows 验证产物。

## 差异与处理结果

| 位置 | 初始差异 | 最终处理 |
| --- | --- | --- |
| `tool_packages/bash.py` | `/bin/bash`、POSIX PTY、killpg 直接耦合工具 | `platform/commands.py` 选择 PowerShell/bash；元数据说明实际语法 |
| `platform/windows_process.py` | Windows 无等效 PTY/进程组 | ConPTY、挂起创建后先加入 Job 再运行、限定继承的管道句柄、kill-on-close |
| `core/runtime.py` | 正常服务关闭未主动停止终端 | runtime stop 关闭 BashSessionManager |
| `api/routes/session_files.py` | Windows 仅路径检查、遗漏 junction | `safe_files.py` 逐层父句柄相对 NtCreateFile、句柄目录枚举、拒绝 reparse |
| `platform/filesystem.py`、`domains/workspace_knowledge.py` | os.walk 不足以排除 junction；普通路径读取 | 显式排除 reparse；知识文本使用安全打开及有界读取 |
| `core/codex_workspace.py` | 符号链接检查不能覆盖 junction、按路径枚举 | reparse/ADS/设备别名拒绝，安全打开与安全目录扫描；错误保持失败关闭 |
| `core/codex_transport.py`、`platform/processes.py` | Windows 根进程终止不清理后代，环境键大小写假设 | 原生 `.exe`、环境白名单不区分键大小写、异步 stdio、Job 清理 |
| `integrations/local_semantic.py` | Windows SQLite 默认禁止扩展加载 | 仅在 sqlite-vec 加载期间临时授权，随后关闭；失败关闭连接 |
| `pyproject.toml`、`uv.lock` | Windows 不自带 IANA 时区库 | 仅 Windows 增加锁定的 `tzdata`；未升级其他依赖 |
| `scripts/setup_windows.ps1` | 缺少独立原生安装路径 | 项目内 bootstrap uv、锁定运行依赖、仅初始化缺失配置 |
| `scripts/sync_to_windows.sh` | 排除锁文件、包含开发/测试产物 | 保留 uv.lock，排除测试评测、环境、缓存、scratch 和个人数据 |

复核发现并修复的两类问题：

1. Windows 目录共享锁不能阻止通过 `WRITE_ATTRIBUTES` 原地设为 junction。
   最终实现不再依赖共享锁保证 reparse 安全，而是相对已验证父句柄打开并从句柄枚举；
   会话预览、目录浏览及 Codex 工作副本均检查过真实原地替换场景。
2. 子进程不读 stdin 时，阻塞 WriteFile 持有写锁；同步关闭 stdin 会卡住事件循环。
   最终实现异步关闭 stdin、对 EOF/退出使用统一宽限期限、超时终止 Job，释放阻塞写入；
   异常或取消仍释放原生资源，并避免双重关闭和已释放句柄的延迟等待。

## 实际验证记录

安装命令已在 Windows PowerShell 执行成功：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 `
  -Python 'C:\Program Files\Python312\python.exe'
```

安装锁定运行依赖成功；pytest、ruff 及仅供开发使用的辅助包已移除。
原生加载 FastEmbed 0.8.0、ONNX Runtime 1.29.0、sqlite-vec v0.1.9 成功；实际创建向量
虚表、插入和读取成功，`ZoneInfo('Asia/Shanghai')` 正常。

实际 HTTP 验收以隔离临时数据启动 uvicorn，通过本机 HTTP 请求调用公开 API。
使用确定性推理替身驱动真实 Agent/工具执行，不调用收费模型或个人邮箱。
Windows Python 3.12.0（PID 12172）与 Linux Python 3.12.3（PID 16793）均 10 项通过：

1. `/health`。
2. 项目、会话绑定、中文与空格路径、目录列表和 CRLF 文件预览。
3. 邮件导入。
4. 知识导入。
5. Watch 的 IANA 时区与调度参数。
6. Agent 执行实际平台命令（Windows Get-Location / Linux pwd）。
7. Agent SSE 正常完成。
8. 后台记忆提取完成并发布可查询记忆。
9. 后端关闭时清理仍在运行的 ConPTY / PTY 后台终端。
10. 服务正常退出、临时 SQLite/工作区数据可清理。

最终 Windows stdio 探针：正常协议往返及 EOF 退出码 0；16 MiB 堵塞写入关闭耗时
0.187 秒，事件循环在等待期间仍响应 19 次；写入返回预期传输错误，流、进程与 Job
全部释放。原生文件探针验证普通目录和 `.codex` 在原地 junction 修改后不能枚举外部
文件，缺失目录保持既有错误合同；Linux 普通扫描及根目录链接替换拒绝也通过。

编译 `app`、`debug_frontend` 和应用启动/单轮脚本通过；新增平台模块的 Ruff 检查、
变更生产文件的 E9/F63/F7/F82 检查、Shell 语法、TOML 解析、`git diff --check` 通过。
本轮未完成、也不声称完成 Windows 全量 pytest。早期定向验证仅作为发现差异的证据；
用户收窄范围后不再迁移或执行测试套件。临时运行探针和日志保留在源工作区忽略的
`scratch/windows-native/`，不作为 Windows 应用内容交付。

## 运行方式与实际限制

Windows 项目目录下启动：

```powershell
.\.venv\Scripts\python.exe scripts\start_backend.py personal
```

默认地址 `http://127.0.0.1:8765`；`--host`、`--port` 可配置。完整说明见
[平台支持](platform_support.md)、[README](../README.md) 和 [API 合同](api_contract.md)。

- 本轮验收为本地 NTFS；UNC 支持路径解析，SMB 网络共享与 macOS 没有本轮实际验收。
- 文件预览拒绝所有 reparse point，包括 OneDrive 占位文件；不会自动跟随或下载。
- 外部 Codex 尚未在该 Windows 环境安装/认证；验证的是本地 stdio、文件边界与生命周期，
  不能替代真实 provider 验收。邮箱/远程模型凭据和本地模型缓存须独立配置。
- 外部 `D:\agent-bot-frontend\run-lka-windows.ps1` 仍是 WSL 启动器，本轮后端仓库不修改
  外部前端；前端可连接原生后端的同一 HTTP 地址。
- 适配的是应用命令能力；用户提供的 Bash 脚本不会自动翻译成 PowerShell。

## 2026-10-04：从空库进行真实前后端验收

用户要求先清空 Windows 数据库，并明确不同步 Linux 数据；随后单独授权只迁移
Linux 现有模型配置与所需密钥，用于真实推理。此次实际运行配置已从 mock 更新为
`packyapi/deepseek-flash`，不再沿用上面 2026-10-03 的推理替身验收方式。

### 数据准备

- 停止经 PID、可执行文件和启动时间核验的 Windows 前后端，以及连接它们的桌宠。
- Windows 后端 `data/runtime/lka.sqlite3`、前端 `data/memory/conversations.db`、
  前端 `data/qq/reader.db` 及 WAL/SHM 移出运行位置，在 Windows 本地备份到
  `C:\Users\xc133\projects\lka_backend\data\reset-backups\20261004-001558`。
  不处理 QQ 客户端/桥接器自己的数据库。
- 重新启动后确认会话、消息、运行记录、邮件、知识文档、项目和记忆业务数据为空。
  Linux 数据库、历史、记忆和模型文件均未复制。
- 仅将 `[llm]` 配置和 `PACKY_API_KEY` 写入 Windows 忽略的本地配置；其他配置保持原值。
  tokenizer 数据没有迁移，Windows 使用已有的保守 token 计数方式。

### 真实操作与证据

使用 Windows 原生 Chrome 独立 profile，通过真实页面操作完成第一条任务；没有替换
网络响应或模型客户端。第二条任务使用真实 HTTP 非流式入口，覆盖同一服务的另一条链路。

| 验收 | 结果 |
| --- | --- |
| 项目登记、项目内会话、真实模型选择 | UI 操作成功，中文及空格目录正确绑定 |
| Agent 创建并读取 Windows 文件 | `bash.run` 使用 PowerShell，exit_code=0、未超时、stderr 为空；文件内容核对通过 |
| SSE 与工具安全门 | 页面显示本轮完成，持久化事件含 llm_delta/run_completed；写命令存在 read_only=false、skip 模式 approved 审查记录 |
| 文件预览与刷新恢复 | 中文文件预览正确，刷新后恢复同一会话及完整答复 |
| 后台记忆 | memory_extract 作业 succeeded；“以后回答先给结论”成为已生效记忆，记忆中心与高级设置显示正常 |
| 邮件助手真实检索 | mail.search 从 Windows 新建样本找到交付编号 4271；只读任务没有发送邮件 |
| 确定性业务 API | 邮件导入/检索、知识导入/检索/正文、事务关联与状态更新、Watch 时区/暂停、后台健康、SQLite 完整性共 7 项通过 |
| 文件访问边界 | 相对路径穿越、绝对 Windows 路径、ADS 访问均拒绝 |
| 重启恢复 | 前后端重启后，会话、运行状态、记忆、项目、文件、邮件/知识、Watch 状态、SQLite 完整性和前端配置共 10 项通过 |
| 桌宠 | 重启后 Java 原生桌宠连接前后端成功，健康与页面诊断通过 |

两条真实任务共 8 次 `packyapi/deepseek-flash` 调用，全部 completed：

- 文件任务：`agent_run_8c011da338d2`，会话 `session_875fbf84fd6d`。
- 邮件任务：`agent_run_871157fe6c6e`，会话 `session_afc8aa7b078c`，耗时 26.3 秒。

邮件任务依据 mail.search 返回的未截断片段答复，未调用完整邮件加载工具；这里不将
该任务计为完整正文加载覆盖。邮件与知识内容均为本次新建验收样本，没有连接真实邮箱。

### 本次发现与修复

新增前后端启动脚本 `D:\agent-bot-frontend\run-lka-native-windows.ps1` 最初未在前端
导入前加载 `.env`，直接读取进程环境的插件因此看不到本地配置。已在启动参数中按文件
是否存在增加 `--env-file`；实际重启后 QQ 插件从错误的 disabled 变为正确的 enabled。
保留脚本的进程身份校验、健康检查、日志与重复启动复用行为。

当前仍有以下实际限制：

- QQ 插件已经启用，但事件源连接返回 websocket_error/offline，sync_state 为
  not_configured；没有把这部分标为端到端通过，也未修改外部桥接器。
- Windows 本地重排模型缓存尚未准备，检索走已验证的关键词路径；没有从 Linux 复制模型
  数据或在本轮下载模型，因此不声称完成语义检索/重排验收。
- 外部 Codex、真实 Outlook/IMAP、UNC/SMB 不在此次已通过的真实场景内。

截图和脱敏结果保存在 Windows `C:\Users\xc133\projects\lka_backend\data\acceptance-20261004\`：
`01-before-real-turn.png` 至 `06-memory-settings.png`、`audit-outcome.json`、`audit-report.json`、
`api-report.json`、`mail-agent-report.json`、`restart-report.json`。浏览器驱动最初的记忆页
等待条件和 SSE body 提取失败由正确入口复查及后端持久化审计补齐，并非产品失败。

重启核对后，运行库仅含本次 Windows 新建数据：2 个会话、4 条消息、2 个 Agent run、
1 封样本邮件、2 份知识文档（含邮件镜像）、2 条验收事务、1 个项目及 1 条偏好。
这些验收数据保留供复查，旧库只保留在本地备份中。当前原生前后端和桌宠运行中。

### 2026-10-04：修正清库后仍显示旧会话的前端缓存问题

用户反馈旧会话仍可见后，复查确认 Windows 原生 `/sessions` 只有本次验收的
2 条记录；桌宠 Java WebView 的 `localStorage` 却保留 7 条缓存，包括 5 条旧记录。
此前数据库重置没有处理这层客户端缓存，独立 Chrome 配置的验收未覆盖旧用户配置。
旧数据未写入新后端数据库；问题是前端一直合并本地缓存与服务端列表，没有淘汰
服务端已不存在的会话。

- 修复 `D:\agent-bot-frontend\app\web\pet\chat.js`：对最新列表以外的缓存逐条
  查询，只有确认 HTTP 404 才移除历史缓存；空列表同样执行核对。
- 保留分页以外仍存在的会话、离线/服务错误下的缓存、空白本地会话、未发送草稿、
  正在执行或核对期间发生更改的会话；切换失效的当前会话时同步刷新消息和工作区。
- 更新 `chat.html` 的脚本版本参数。已打开的旧网页/桌宠窗口需要刷新一次加载新脚本。
- 原生 Windows Chrome：真实后端配合注入的旧浏览器缓存，验证自动移除和刷新后不复现；
  后端仍只有 2 条验收会话，无额外业务写入、无 JavaScript 异常。
- 另验证 11 项针对性场景，包括分页、HTTP 503、断网、草稿、执行中任务和请求期间的修改；
  全部通过，未新增或迁移项目测试套件。
- 桌宠缓存的原生 SQLite 一致性备份及验证报告保存在 Windows
  `data/acceptance-20261004/session-cache-fix/`；没有复制到 Linux 数据目录。
