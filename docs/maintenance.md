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
目前没有自动更新器；旧版本删除的文件应由 Git 按提交处理，而非手工覆盖后假设升级完成。

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
