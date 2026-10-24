# 更新、备份与故障检查

## WSL 主环境与 Windows 功能验证

采用 WSL 作为主环境的开发电脑，源码修改、评测和日常后端都在 Linux 中进行。
Windows 保留独立后端副本用于平台兼容与功能验证，不同时维护另一套日常实例。
这不影响其他电脑使用原生 Windows 安装。

| 用途 | 环境与启动方式 | 数据与端口 |
| --- | --- | --- |
| 开发、评测、日常后端 | WSL/Linux；`uv run python scripts/start_backend.py personal` | Linux 原生虚拟环境、已确认的日常数据；默认 8765 |
| Windows 功能验证 | Windows 原生 Python；`python scripts/start_backend.py test` | 默认临时数据、mock 模型；默认 8766 |
| 桌面界面、桌宠、消息采集 | 可以继续运行在 Windows | 日常流量连接 WSL 后端，测试流量单独配置 |

Linux 更新后在仓库根目录运行 `uv sync --locked`，依照下文先等待任务结束、备份、再重启。
不要把原生 Windows 的虚拟环境复制到 WSL，也不把每次修改后的 Windows 同步作为日常部署。

已配置用户级服务的 WSL 开发电脑，日常后端由 `lka-backend.service` 管理，
不要再启动同端口的手动实例。检查与重启在 WSL 执行：

```bash
systemctl --user status lka-backend.service
# 先确认任务空闲、完成必要备份，再执行：
systemctl --user restart lka-backend.service
journalctl --user -u lka-backend.service -n 50 --no-pager
```

服务使用 Linux checkout 中的原生 `.venv`；私有服务配置保留本地，不随 Git 同步。
配套 Windows 前端可通过本地 `.runtime/lka-backend-mode.json` 选择 WSL 模式
（`backend_mode: "wsl"`、`wsl_distro`、`wsl_user`），由启动器启动该用户服务并复用前端。
没有该本机标记的其他 Windows 安装仍按原生方式运行。WSL 发行版必须已启用 systemd；
此配置不会保证 Windows 关机、休眠或停止 WSL 后仍运行后台任务。

Windows 测试入口：

```powershell
.\.venv\Scripts\python.exe scripts\start_backend.py test --port 8766
```

省略 `--test-data-dir` 时，测试服务结束后清理本次临时目录；确实需要保留测试数据时，
指定新的独立目录，不能指向已有日常数据。测试配置不加载私有模型 TOML，
但这不是操作系统沙箱：不要给测试进程注入日常凭据或连接真实采集流。
真实模型兼容测试须另行使用隔离配置与明确选取的数据。

首次从 Windows 日常后端切换到 WSL 时，先确定数据来源并备份两边，不自动合并数据库。
等待旧实例空闲后切换，逐项核对授权文件、模型/tokenizer 缓存、指导文件、工作区和媒体路径。
Windows 路径不能原样作为 Linux 配置路径；控制凭据的 Windows DPAPI 解密仍由原生配对端完成，
不能假定复制授权文件就能在 Linux 使用。

Windows 端必须实测 `/health`、SSE 和消息导入目标；WSL 内健康检查通过不等于 Windows 前端已连通。
日常前端启动器应使用外部后端连接方式，不能同时自动拉起原生 Windows 日常后端。
服务仍以本机访问为默认，不通过随意扩大监听地址代替连通性核查。

## 使用 Git 更新源码安装

这套步骤用于 Git 源码安装，不用于直接覆盖便携包。
前端桌宠是独立项目，需要独立更新。

1. 确认在正确的后端目录，运行 `git status --short`，保留未提交的代码修改。
2. 运行 `git fetch origin`，查看准备安装的已验证提交或发布分支。
3. 记录旧提交：`git rev-parse HEAD`。等待 Agent 和后台任务完成，再停止对应实例。
4. 备份应用、配置和运行数据，然后在已选定的发布分支上执行：

```powershell
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

`git pull` 需要当前分支已配置 upstream。尚未配置远程时，先由发布者确认仓库地址及分支；
不要用强制覆盖、`reset --hard` 或直接删掉本地目录来解决更新问题。
Linux 在更新后运行 `uv sync --locked`，再使用原来的启动方式。

5. 启动新版本，检查 `/health`，并执行一次与日常用途对应的小任务。
   `.env`、`config/local.toml`、数据库和用户资料不应被模板或其他电脑的文件覆盖。

Git 只更新已提交的文件。工作区内部的工程资料、真实配置和本地历史不会由 Git 同步。
配套 Windows 前端源码安装提供 `Check-LKA-Update.cmd` 和 `Update-LKA.cmd` 双击入口，
可检查版本、备份并更新前端，也可明确配置为同时更新原生 Windows 后端；
具体配置、失败续作与备份范围见前端仓库的 `docs/windows-updates.md`。
WSL/云端后端仍由本节的原部署流程管理，Windows 更新入口不会自动部署 Linux 或重启服务。
更新前先结束任务并停止对应服务；旧版本删除的文件由 Git 按提交处理，
不要手工覆盖代码后假设升级完成。

## 回答生成超时

用户可见主运行的回答或纯上下文回复默认限制网络等待为 60 秒；服务商的更短超时和运行期限仍然有效。
超时且尚未输出流内容时，优先改用本轮推理模型，再考虑兼容的配置默认模型，最多追加一次调用。
切换记录为 `answer_model_fallback_started`，实际选择保存到运行的 `answer_timeout_fallback`，
恢复时继续使用该选择；原回答偏好保持不变。没有可用替代模型或禁用切换时，保留一次同模型重试，
记录 `llm_retry_scheduled`。只重新生成回答，不重新执行已经完成的工具；
取消信号、剩余额度和显式单次调用限制仍然有效，子运行与静默内部回答不自动换模型。
已经输出部分内容的流不自动重启，避免重复显示；再次超时保留 timeout 错误分类。
该机制不为路由、工具或其他模型阶段增加通用重试。配置见 [模型配置](configuration.md)。

## 后台额度与熔断

记忆／压缩与消息分析分池计量，查看 `/background/health` 中的
`llm_workloads.budget_pools`，不要将保留历史的 `last_24h` 当成重置后的额度。
熔断的原因、时间与当前版本可以通过健康接口、后台 SSE 和服务日志查看。
先排查异常来源、等待相关调用结束并备份，再由用户显式确认通过
`POST /background/budgets/reset` 提交所选池的版本与原因。它会开启新的滚动计量窗口并
解除所选池熔断，不删除账本、来源或检查点，不提高消息工作累计额度。
409 时重新检查在途任务和版本，不直接改数据库或反复强制重置。
重启不解除熔断；取消已发出的请求不保证退回服务商费用。

## 备份什么

迁移现有日常数据到云端并接入其他电脑，见[部署与旧数据迁移](deployment_migration.md)。
`scripts/data_snapshot.py create --offline` 提供完整数据与私有配置快照，
`verify` 校验传输后的文件；停服、目标路径和外部文件仍须按该指南核对。

备份实际配置的整个数据目录，另保存 `.env`、`config/local.toml`、
它们引用的邮箱授权及外部密钥文件。默认主要数据在 `data/runtime`，
可能包含主数据库、检查点库、日志、记忆、知识文件和模型缓存。
前端的历史、QQ 媒体缓存及桌宠偏好另有数据目录，后端备份不包含它们。

正在写入的 SQLite 不能只复制主库文件。使用 SQLite backup API，
或确认所有写入进程停止后备份完整数据。`scripts/production_release.py snapshot`
会备份应用和本地配置，但不备份业务数据库。

备份包含私人资料及凭据，保存在本地或受保护的私人备份位置，不放进 GitHub。

## 停止、重启与部署校验

手动前台启动的实例在对应终端按 Ctrl+C 停止。
便携包使用自己的 `Stop-LKA.cmd`，不要用它管理另一份源码安装。
使用独立前端启动器时，遵循该前端的进程管理说明，不批量结束所有 Python / Java 进程。

`restart_windows_backend.ps1` 适用于配套原生启动器：依赖前端的进程记录和 QQ outbox 检查，
只切换核对过的后端，不是任意安装都可用的通用停服脚本。
路径和端口不同，需要明确传递参数。

使用部署清单的安装，`/health` 中 `deployment_id` 表示启动时载入的清单身份。
普通 `git pull` 不自动生成新清单，旧标识不能证明新代码已加载。
完整记录/校验入口是 `production_release.py`；只有健康状态也不代表所有任务效果正确。

## 在另一台电脑上使用

源码安装应重新创建原生 `.venv`，不要复制 Linux 虚拟环境。
本地配置可私下迁移，模型缓存可以按实际路径复制；调整工作区、缓存与授权文件位置。
Windows CurrentUser DPAPI 凭据需重新配对，邮箱或外部模型服务也可能要求重新登录。

私人便携包需完整解压，保留启动器、应用、运行环境和模型目录的相对关系，
按随包的检查工具校验。它可能含真实凭据，不作为公开下载包上传。
软件更新不会自动同步两台电脑的会话、记忆或资料。

## Ubuntu 22.04 云端共享核心

云端沿用原生 Python、SQLite 和 systemd，不使用 Docker。此节描述部署操作，
更新源码本身不会自动迁移数据、发布网络入口或重启已有服务。

1. 使用专用非 root 用户 `lka`，准备源码目录 `/opt/lka-backend`、私有配置目录 `/etc/lka`
   和数据目录 `/var/lib/lka`。仅该用户和管理员可读私有配置与运行数据。
   项目要求 Python 3.12 及以上；在该用户下安装 uv，按
   [uv 的 Python 管理说明](https://docs.astral.sh/uv/guides/install-python/)准备独立解释器，
   不替换 Ubuntu 系统 Python。在源码目录执行：

```bash
uv python install 3.12
uv sync --locked --python 3.12
```

2. 从模板创建新的 `/etc/lka/local.toml`（已有文件只做定点修改），配置真实模型，
   启用 `[auth]`、`[devices]`、`[sync] role="hub"`，并把 auth.allowed_origins 设为实际前端 HTTPS origin。
   需要云端长期助理时启用 `[management].enabled=true`；其自主唤醒仍按原配置独立控制。
   embedding/reranker 的 cache_dir 可设为 `/var/lib/lka/models`，不要沿用 Windows 路径。
   模型下载及远程模型调用不包含在启动验证中，按用途单独配置和验证。

3. 创建权限受限的 `/etc/lka/backend.env`，至少包含：

```dotenv
LKA_DATA_DIR=/var/lib/lka
LKA_LOCAL_CONFIG=/etc/lka/local.toml
# 在这里按配置填写模型密钥等；设备令牌用于各自节点，不共用账号密码。
```

在交互终端以相同数据/配置路径创建首个管理员，密码由 getpass 输入，不写在命令行：

```bash
LKA_DATA_DIR=/var/lib/lka LKA_LOCAL_CONFIG=/etc/lka/local.toml .venv/bin/python scripts/manage_accounts.py create-admin owner
```

4. 按服务器路径检查 [systemd 单元示例](../scripts/deploy/lka-cloud.service.example)，
   安装为系统服务 `lka-cloud.service`。其 `--require-auth` 要求现存配置明确启用账号、HTTPS 和
   secure cookie，缺文件或关闭认证直接拒绝启动；不可省略该参数来消除云端启动错误。
   代理转发头只信任 127.0.0.1，监听也保持 127.0.0.1:8765，单实例运行，不能另加多个 worker。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lka-cloud.service
sudo systemctl status lka-cloud.service
```

5. 云端加入 tailnet 后，通过 [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
   将 HTTPS 入口反向代理到本机服务。例如在确认该服务器现有 Serve 配置后：

```bash
sudo tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

使用 tailnet 内的 HTTPS 入口，不开启公网 Funnel。后端明文端口不直接对外放行。
Tailscale 负责网络准入，应用仍要求账号或设备凭据；不能把转发的用户头当成账号登录。
同站前端入口、Origin、cookie、CSRF、SSE 和代理头需要在真实手机/电脑逐项验证，
不能只依靠本机健康检查。健康检查公开最小状态，未登录 `/sessions` 应为 401，
直接 HTTP 请求登录接口应为 403；错误转发头配置应修复代理，不关闭 require_https。

6. 登录后登记电脑及其系统/工作区，显式授予 heartbeat、sync.read（需要候选上报时另授 sync.propose）。
   妥善保存一次性返回的设备 token，在该电脑环境中设置 LKA_NODE_TOKEN。
   从已认证的 `/sync/status` 获取 hub_id，配置电脑为 replica；先运行
   `python scripts/cloud_sync.py run --max-pages 8`，核对游标，再启用本地后台轮询。
   大量初始数据可能需要多次分页。提案提交失败不阻止下一轮拉取，状态会保留错误及 pending 计数。

### 多电脑节点与手机入口

启用多机工作前，按[配置说明](configuration.md#设备直连与远程任务)增加 connections 和 remote_execution。
节点的每个对外后端入口使用稳定独立的 node_id、自己的数据目录与 HTTPS origin；
若同一物理电脑分别运行 Windows/WSL 后端，直连时应分别登记为可辨认的节点入口，
不能让两个进程共用 SQLite 或用相同地址假装可以互换。
执行目录只在节点本地配置，云端记录 workspace_id。登记的 execution scopes 和节点 allowed_tools
均需明确批准。原生 shell 默认关闭；仅在理解操作系统用户权限后设置 shell_trust=host_user，
另授 bash.run，并逐次人工审批。cwd 不限制进程访问范围，受限来源或挂载工作区不授予此权限。

Linux 节点可按 [节点 systemd 示例](../scripts/deploy/lka-node.service.example)安装独立服务，
其环境文件包含本机 LKA_DATA_DIR、LKA_LOCAL_CONFIG、LKA_NODE_TOKEN 与本机模型凭据。
示例使用 `--require-node`，缺失配置或缺少节点认证边界时拒绝启动；代理仍只信任 127.0.0.1。
其他 Windows 电脑使用原生环境时，可运行
`python scripts/start_backend.py personal --require-node`，不要在当前以 WSL 为日常主后端的电脑另启第二套日常实例。
本轮不自动安装开机任务、覆盖凭据或调整 tailnet 策略。

云端和节点都可由自己的 HTTPS origin 打开 `/client`，不需要在手机安装后端。
云端入口登录后使用统一助理，或选择设备进入节点入口。连接授权由节点轮询接收，默认最长 120 秒有效，
页面等待期间应保持节点服务在线；地址中一次性授权仅用 fragment，页面读取后立即移除。
已连接节点的普通会话、文件及记忆缓存全部通过该节点处理；关闭云端不会使普通请求改走云端。
节点连接 cookie 默认有效 30 天，本地退出、撤销或到期后需重新建立连接；
独立本机账户须提前在本机配置，不能把“断云可工作”解释为离线接受新的云端账户认证。
云端改密、禁用账号或撤销连接会增加授权代次，节点重新成功同步后撤销旧连接。
断网期间保留已有连接；如需当场撤销，应在目标节点本地撤销或停止对外入口。
共享记忆正式编辑需双方启用 memory_write_enabled 和节点 sync.memory.write scope；
升级前备份各自数据库，保留节点待提交队列，不将它当作已发布记忆。
自动学习记忆同步需云端和节点均启用 sync.memory_aggregation_enabled，并授予节点
sync.memory.aggregate；默认关闭，与人工编辑开关独立。它由正常运行时的后台 worker
上传和拉取，沿用 active/candidate 状态并做精确去重，不用复制 SQLite 文件。
启用范围、项目映射及删除传播见[账号与同步配置](configuration.md#可选云端账号设备与共享同步)。

节点执行 worker 会分别报告环境在线状态，不能只看设备心跳推断某个系统能执行任务。
从云端查看任务摘要、回报与审批；禁止直接在节点普通入口打开助理派生会话。
断云时远程任务在后续执行/发布检查停止，未知结果不重放；普通节点任务继续使用本机模型配置。
远程任务支持检查点恢复和同会话续作，续作保留原设备与工作区；
在途任务、未知外部效果或无法安全继承的累计预算会阻止相应操作。
跨机器累计预算迁移、真正远程挂载及完整安装更新器仍不提供。
内置连接入口覆盖基本对话、文件和任务回报；现有桌宠工作台的全部高级界面尚未接入多机身份体系。

云端数据库放服务器本地磁盘，电脑数据库各自独立，不通过 SMB/NFS/SSHFS 共享 SQLite 文件。
普通会话保留本地；共享副本不能直接当成云端全量备份或热备调度器。
如需把现有日常助理迁往云端，另行确定权威数据、等待任务空闲、备份两边，
一致迁移数据库及所需本地资料，核对路径、凭据、用量和发布水位后再切换唯一协调器。
不要把空云端库自动覆盖本机数据，不同时运行两套同身份的自主助理。

## 数据库耗时排查

后端启动时默认向实际数据目录的 `logs/database_timing.jsonl` 写入 JSONL。
单文件上限 10 MiB，保留 3 份轮转文件（`.1` 至 `.3`）；不依赖 Uvicorn 的访问日志格式。
配置开关、采样率与慢阈值见[配置说明](configuration.md#配置放在哪里)。

每条记录有 UTC `timestamp`，`kind` 区分：

- `sqlite`：连接、SQL 执行、结果读取、显式写锁获取、提交或回滚的明细；
  普通明细默认采样 1%，达到 50 ms 或失败的操作全部保留。
- `sqlite_summary`：全部受监测操作的固定分桶统计，持续有操作时约每 60 秒输出，
  正常关闭时补记剩余统计。`count`、`errors`、`total_ms`、`max_ms` 与
  `bucket_counts` 不经过采样；`p50_upper_ms`、`p95_upper_ms`、`p99_upper_ms`
  是对应分桶的上界估计，非精确分位数。最后一桶和分位上界的 `null` 表示超过 10 秒。
  样本单位是一次阶段 API 调用；`fetchall` 按一批计，游标迭代按每次取行计。
- `http`：请求的响应头耗时 `headers_ms`、完整响应耗时 `elapsed_ms`，以及
  各数据库阶段的次数与累计耗时 `database_phases`。响应头 `X-Request-ID` 与
  日志 `request_id` 关联；记录路由模板，不记录路径参数、查询参数或请求正文。
  慢请求、5xx、未处理异常以及含慢／失败数据库操作的请求会额外保留。

数据库阶段 `connect` 仅测连接建立，初始化 PRAGMA 计入 `execute`。
`acquire_write` 测量显式 `BEGIN IMMEDIATE`／`BEGIN EXCLUSIVE`，包括获取写锁的等待和语句执行，
不能当作纯锁等待时间。`execute` 包括语句内部的锁等待；`fetch` 包括 SQLite 后续步进、
取行和 Python 行转换。`script` 包括整个 `executescript` 及其隐式提交；
上下文管理器的提交失败可能包含 SQLite 自身的回滚。各阶段不应被解释为数据库内部 CPU 时间。

SQL 明细只记录操作类别与语句哈希 `statement_id`，不保存 SQL 原文、绑定参数、结果、
异常消息或数据库路径。语句哈希未做模板归一化；字面量不同可能产生不同哈希。
线程池通过继承的请求上下文关联；无请求上下文的后台操作 `request_id` 为 `null`。
并行数据库操作的累计时间可能大于请求墙钟时间，SSE 的完整响应时间还包含连接保持时间。
不要从偏向慢操作的明细样本直接计算全体操作的分位数，应使用完整分桶统计。

监测覆盖后端通用 SQLite 连接、消息历史、记忆、后台任务、额度账本和 LangGraph 检查点库。
异步检查点操作由 aiosqlite 独立线程执行，不关联 HTTP 请求 ID。
独立消息连接器和 Windows 前端不在 SQL 计时范围内；
前端发往后端的请求可通过后端 HTTP 记录排查。日志写入失败不改变业务结果。

## 常见情况

| 现象 | 先检查 |
| --- | --- |
| 健康检查失败 | 进程是否启动、端口是否一致、是否绑定本机及启动日志 |
| 对话仍像模拟回答 | 模板默认 mock；核对当前请求、会话和默认模型选择 |
| 搜索不可用 | 搜索密钥、服务商网络、查询配额；不是没有搜索结果 |
| 邮件或消息缺少新内容 | 数据源是否连接、会话是否允许记录、同步范围、采集及分析水位 |
| 分析或记忆等待 | 后台开关、暂停状态、模型服务、各层配额与当前工作累计额度 |
| 修改配置未生效 | 是否修改了正在运行的安装；是否需要重启或仍有数据库 desired 配置 |
| 网页追问读到旧内容 | 查看抓取时间；需要最新信息时明确刷新 |
| Windows 路径或命令失败 | 后端实际可访问的路径、TOML 转义、PowerShell 而非 bash 语法 |

排查时保留错误和运行记录；日志可能含个人内容，不直接贴入公开 Issue。
如需回退，先核对依赖和数据库格式是否兼容。恢复旧数据库可能丢失升级后的写入，
不能只切回旧源码就认定完成恢复。
