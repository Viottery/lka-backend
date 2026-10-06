# Cross-Platform Support：Windows / Linux Native Backend

同一套 Python 后端支持 Windows/Linux 原生运行；平台差异集中在 `app/platform/`。
WSL 是可选开发方式，Windows 原生后端不依赖 WSL 或 Git Bash。Docker 不再维护。
本轮交付范围是应用与运行依赖，不包含测试/评测目录迁移或开发依赖安装。
实际执行记录与待验收项目见 [Windows 原生适配清单](windows_native_todolist.md)。
同步、启停、配置、另一台电脑迁移及故障排查见 [Windows 操作手册](windows_operations.md)。

## 1. 运行环境

- Windows 10 1809+ / Windows 11 / Windows Server 2019+，Python 3.12+，Windows PowerShell 5.1。
  后台终端依赖 ConPTY；同步命令同样使用原生 PowerShell。
- Linux，Python 3.12+，`/bin/bash`；后台终端保留 POSIX PTY。
- macOS 平台识别和路径分支仍保留，本轮未做原生验收。

Windows 11、Python 3.12、PowerShell 5.1 和本地 NTFS 已完成原生应用验收：HTTP、文件、
邮件/知识导入、Agent 原生命令/SSE、后台记忆、时区、活动终端退出清理等 10 项通过，
Linux 同一组应用流程也通过。具体执行记录见迁移清单。
UNC 路径解析已支持，SMB 共享访问尚未实测。

## 2. 平台层与运行边界

```text
app/platform/
  base.py / detect.py   # 平台信息与识别
  paths.py             # workspace 路径解析及 Windows→WSL 桥接
  filesystem.py        # 元数据扫描、hidden 与链接/reparse 过滤
  safe_files.py        # POSIX 目录描述符、Windows 目录句柄链与枚举
  commands.py          # bash / PowerShell 同步命令、PTY / ConPTY 终端
  windows_process.py   # Windows 原生进程、管道、ConPTY 与 Job 生命周期
  processes.py         # 外部专家 stdio 进程创建与进程树清理
```

`bash.*` 工具名称、Agent tool-call 和 HTTP 合同保持兼容。Windows 实际执行
PowerShell，通过 UTF-16LE `EncodedCommand` 传递脚本，并配置 UTF-8 输入输出；
Unix shell 脚本和 Unix 命令选项不会自动转换。PowerShell 环境变量用
`$env:WORKSPACE_ROOT`，例如 `Get-Location`、`Get-ChildItem`、`Get-Content`、`Select-String`。
Linux 实际执行 bash，环境变量用 `$WORKSPACE_ROOT`。

同步命令与后台终端分别支持超时、分页输出、stdin、Ctrl-C 和终止。
Windows Job 管理子进程树，关闭/超时/终止时清理后代；后台终端使用 ConPTY。
POSIX 使用进程组和 PTY。`bash` 的 cwd 限制不是 OS sandbox，命令仍可访问当前用户
有权限访问的资源；白名单外命令及终端写入/中断/终止必须进入既有安全审查。
PowerShell 只读白名单只接受保守的字面命令；表达式、管道、变量展开和重定向进入审查。

## 3. 原生启动

### Windows PowerShell

从项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
.\.venv\Scripts\python.exe scripts\start_backend.py personal
```

`setup_windows.ps1` 使用现有 `uv`；缺少时通过原生 Python 创建项目内 `.bootstrap`
并安装 uv，再执行 `uv sync --locked --python <Python>`。它只在缺失时复制
`.env.example` 和 `config/local.example.toml`，保留已有个人配置。
可用 `-Python 'C:/Path/To/python.exe'` 选择 Python 3.12+ 可执行文件。
依赖安装需要可访问锁文件中的包源；不需要激活虚拟环境。

在另一窗口验证服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

### Linux

```bash
cp .env.example .env
cp config/local.example.toml config/local.toml
uv sync --locked
uv run python scripts/start_backend.py personal
```

复制 example 前先确认本地配置尚不存在。个人服务默认监听 `127.0.0.1:8765`，
使用 `config/local.toml` 和 `data/runtime`。需要隔离的运行检查时可启动 `test` profile：

```powershell
.\.venv\Scripts\python.exe scripts\start_backend.py test
```

该 profile 默认监听 `127.0.0.1:8766`，强制使用临时数据目录和不存在的本地配置，
使用 mock provider，避免同步个人邮箱或调用真实模型；不会自动执行 pytest。
需要保留隔离数据时传 `--test-data-dir ./data/test-runtime`。停止服务使用 Ctrl-C。

## 4. 配置与路径

- `LKA_PLATFORM=auto` 根据运行中的 Python 进程识别平台。
- `LKA_DEFAULT_SHELL` 是保留配置项；当前工具按实际操作系统选择 PowerShell 或 bash，
  该项不提供任意 shell 切换。
- `LKA_DATA_DIR` 默认 `./data/runtime`，保存 SQLite、导入知识、邮件状态、日志、token 和模型缓存。
- `LKA_WORKSPACE_ROOTS` 使用分号分隔可选根目录限制；当前会话 workspace 是工具默认根目录。
- `LKA_WSL_WINDOWS_MOUNT_ROOT` 默认 `/mnt`；Linux 接受 Windows drive-path 时可映射到
  `/mnt/c/...`，该桥接不支持 UNC。
- `LKA_ALLOW_SYMLINKS=false` 默认不跟随扫描中的 symlink/reparse。
- `LKA_SKIP_HIDDEN=true`、`LKA_MAX_SCAN_FILES=50000` 控制元数据扫描。

Windows API 路径应是后端可访问的绝对盘符或 UNC 路径，推荐 JSON 使用 `/`：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/workspaces/index `
  -ContentType 'application/json' `
  -Body '{"workspace":"C:/Users/name/project","source_frontend":"windows-native"}'
```

TOML 路径使用双引号正斜杠字符串（`"C:/Users/name/project"`），或不解释反斜杠的
单引号 literal 字符串（`'C:\Users\name\project'`）。双引号内的反斜杠需要 TOML 转义。
中文和空格路径无需改名；文件正文保留原有 LF/CRLF，不靠平台默认编码转换。

## 5. 文件访问与可选集成

会话文件预览/列表只接受工作区内的相对路径。POSIX 使用逐层 no-follow 目录描述符；
Windows 使用从已打开父目录相对执行 `NtCreateFile` 的句柄链并通过句柄枚举目录，
没有降级为先检查路径再打开的分支。Windows 拒绝所有 reparse point（包括 symlink、
junction 和 OneDrive 占位项）、ADS、DOS 设备别名及尾随点/空格路径。
遇到这些对象返回拒绝，不自动下载或跟随 OneDrive 占位文件。
索引扫描在不允许链接时也会跳过 Windows reparse；启用扫描链接不会放宽预览边界。
本地 NTFS 已执行验证，UNC/SMB 的权限和句柄行为仍需实际环境验证。

Windows 的 IANA 时区数据由 `tzdata` 运行依赖提供。sqlite-vec 只在加载扩展期间
临时开启 SQLite extension loading，加载后立即关闭。Embedding/reranker 模型仍按本地
缓存与显式下载配置运行；平台迁移不会自动下载模型或启用远程服务。

外部 Codex 工具仍须显式启用并独立安装/认证。Windows 配置
`codex_binary_path` 应指向原生 `.exe`；`.cmd`、`.bat` 和 `.ps1` 启动器会被拒绝。
stdio 协议与进程退出检查不代表真实 Codex provider 验收；本轮未安装/认证真实 Codex。

## 6. 同步与前端

`scripts/sync_to_windows.sh` 从 WSL 同步应用与 `uv.lock`，默认 dry run；`--apply` 才写入，
`--delete` 仅在源工作区确为权威副本时使用。测试、评测、开发缓存、环境、数据和个人配置
均排除；Windows 副本通过 `setup_windows.ps1` 独立准备依赖和缺失配置。

外部 `D:\agent-bot-frontend\run-lka-native-windows.ps1` 启动原生后端、前端与桌宠，
默认使用 8765/8780 端口，支持 `-NoPet`、自定义端口和 `-BackendRoot`。
它会复用身份与健康检查通过的已启动服务；代码/配置更新后应先停止再启动。
旧 `run-lka-windows.ps1` 仍是 WSL 入口。同一端口只运行一个服务。

后端同步不包含独立前端和它的依赖。另一台 Windows x64 电脑可使用已交付的原生便携包，
其中包含 Python、Java、桌宠、模型权重与私人配置，排除业务使用数据。
使用 `Start-LKA.cmd` / `Stop-LKA.cmd` 管理该包，交付与验收见
[Windows 便携包交付](windows_portable_delivery.md)。
