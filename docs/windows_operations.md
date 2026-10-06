# Windows 同步、运行与迁移操作手册

更新：2026-10-04。适用于当前原生 Windows 前后端与 Java 桌宠。
本文记录操作流程；实现和验收证据见 [平台支持](platform_support.md)、
[原生适配记录](windows_native_todolist.md) 和 [便携包交付记录](windows_portable_delivery.md)。

## 1. 先确认使用哪一份程序

| 用途 | 当前电脑上的位置 | 入口 |
| --- | --- | --- |
| 后端开发源代码 | WSL：`/home/viottery/lka_backend` | 修改后单向同步到 Windows |
| Windows 原生后端 | `C:\Users\xc133\projects\lka_backend` | 自有 `.venv\Scripts\python.exe` |
| Windows 前端与桌宠 | `D:\agent-bot-frontend` | `run-lka-native-windows.ps1` |
| 供另一台电脑使用的便携包 | `D:\LKA-Releases\LKA-Windows-x64-20261004.zip` | 解压后的 `Start-LKA.cmd` |

这些路径是当前机器的实际位置，其他电脑应替换用户名和盘符。
WSL 中对应的 Windows 路径为 `/mnt/c/...`、`/mnt/d/...`，盘符目录使用小写。
前端是独立工作区，后端同步脚本不会同步或安装前端。

原生运行需要 Windows 10 1809+ / Windows 11 x64 和 Windows PowerShell 5.1。
源码安装还需原生 Python 3.12+；桌宠使用 Java 21 和前端自己的构建产物、依赖。
当前电脑的前端环境已准备完成。另一台电脑直接使用便携包，其中已包含运行环境、
桌宠和当前本地模型，无需另装 Python、Java、Gradle、WSL 或 Docker。

默认后端地址为 `http://127.0.0.1:8765`，前端为 `http://127.0.0.1:8780`。
旧脚本 `run-lka-windows.ps1` 会启动 WSL 后端；原生运行请选择带 `native` 的脚本。

## 2. 从 WSL 同步后端到 Windows

### 同步范围

`scripts/sync_to_windows.sh` 复制当前工作树中的应用、文档、配置模板和 `uv.lock`，
包括尚未提交的修改；它不是按 Git commit 发布，也不会自动提交或合并 Windows 修改。

排除项包括 `.git/`、`.venv/`、`.bootstrap/`、缓存、`scratch/`、`data/`、
`tests/`、`evals/`、评测/探针脚本、`.env`、`config/local.toml`、
`config/secrets*.toml`、`config/tokens*.json` 和 `config/cache/`。
因此不会带入 Linux 数据库、会话、记忆、邮件正文或原虚拟环境，也不会安装测试依赖。
完整排除规则以脚本为准；自定义凭据文件放在其他路径时，需要先检查预览，
不能假定所有名字带 `secret` 的文件都会自动排除。

### 首次同步或日常更新

1. 等当前任务结束，按第 4 节停止 Windows 前后端和桌宠。
2. 如果曾直接修改 Windows 代码，先保留这些修改并合并回源工作区。
3. 在 **WSL Bash** 中预览，再实际同步：

```bash
cd /home/viottery/lka_backend
bash scripts/sync_to_windows.sh /mnt/c/Users/xc133/projects/lka_backend
bash scripts/sync_to_windows.sh /mnt/c/Users/xc133/projects/lka_backend --apply
```

默认只预览，只有 `--apply` 才写入；脚本需要 WSL 中已有 `rsync`。
目标是原生后端目录，不是前端、Windows 用户目录或便携包里的 `backend`。
普通更新不会删除目标独有文件。确实需要同步源文件删除时，先审阅包含删除的预览：

```bash
bash scripts/sync_to_windows.sh /mnt/c/Users/xc133/projects/lka_backend --delete
# 确认预览中的删除都是本次发布需要的操作后：
bash scripts/sync_to_windows.sh /mnt/c/Users/xc133/projects/lka_backend --apply --delete
```

`--delete` 不适合在目标还保留独立开发成果时直接使用。不要将这个脚本用于便携包更新：
便携包有专用启动器、路径处理、授权文件和校验清单，应按第 8 节重新打包。

4. 首次安装，或 `pyproject.toml` / `uv.lock` 有变化时，在 **Windows PowerShell** 运行：

```powershell
Set-Location 'C:\Users\xc133\projects\lka_backend'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

如系统的 `python` 不是所需的原生解释器，显式指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 `
  -Python 'C:\Program Files\Python312\python.exe'
```

脚本使用已有 uv，或在项目 `.bootstrap` 安装 uv，再按锁文件创建/更新 Windows `.venv`。
首次安装需要能访问依赖源；只在缺失时生成 `.env` 和 `config/local.toml`。
不要复制 Linux `.venv`，也不要用 example 覆盖已有真实配置。
新生成的示例配置使用 mock 模型、关闭邮箱；真实推理需要另外配置模型和密钥。

5. 按下一节启动，检查前后端健康、模型选择和工作区；前端文件有更新时重新打开聊天窗口。

## 3. 本机原生前后端的启动与检查

在 **Windows PowerShell** 中启动前后端和桌宠：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File 'D:\agent-bot-frontend\run-lka-native-windows.ps1' `
  -BackendRoot 'C:\Users\xc133\projects\lka_backend'
```

脚本依次启动后端、检查健康、启动前端、检查聊天页面，然后启动桌宠。
后端使用 `personal` profile 和该 Windows 后端的 `data\runtime`、`config\local.toml`；
前端会加载自己的 `.env`，本启动方式关闭前端旧的 `LOCAL_RAG_ENABLED` 路径。
后端 `personal` profile 同样将私有 `.env` 加载到进程环境（不覆盖显式环境变量），
供消息导入/控制凭据等使用；`test` profile 不加载该私有文件。

| 参数 | 用途 |
| --- | --- |
| `-NoPet` | 只启动服务并打开浏览器聊天页面 |
| `-NoPet -NoOpen` | 只启动服务，不打开界面 |
| `-OpenBrowser` | 启动桌宠时也打开浏览器 |
| `-BackendPort 8875 -FrontendPort 8890` | 更换两端端口，启动器同时调整聊天 URL 和后端 CORS |
| `-PetProfileId 'truth-book-build'` | 指定桌宠角色，默认即此角色 |
| `-PetWidth 360 -PetHeight 520` | 指定桌宠窗口尺寸 |

重复执行会复用经进程身份和健康检查确认的服务；它不会使运行中的后端重新加载代码或配置。
更新后需要先停止再启动。遇到其他程序占用端口会报错，不会强制结束占用者。

### 已授权的后端单独切换（持续采集期间）

2026-10-06 起，生产发布可用 `scripts/production_release.py snapshot` 备份原应用与配置，
同步后执行 `record`，以应用源码、入口与 lockfile 的 SHA-256 清单核对源/目标完全一致。
清单保存在 `data/runtime/production_release.json`；personal 启动读取 release_id，
`GET /health` 增加可选 `deployment_id`，是启动时加载的版本标识，不是热加载承诺。
清单包含未提交/未跟踪源码，因此不能只凭 Git HEAD 判断部署版本；私有配置、业务数据
和虚拟环境仍不从 WSL 覆盖。test profile 清除生产标识，继续使用隔离数据。

用户明确要求上线且前端 QQ outbox 有余量、Agent/后台无正在执行任务时，可使用
`restart_windows_backend.ps1 -Action Stop/Start` 只切换身份核验后的后端；不结束前端、
QQ、SnowLuma、桌宠或进程树。前端持续将获批消息保存到本地 outbox 并重试导入；
这只覆盖短暂后端停机，不保证 QQ 断连或 outbox 满时重放。切换后必须核对健康中的
deployment_id、前端原 PID、Reader 连接/队列及后台续作，不用旧的同时停两端脚本。

`clean_personal_test_state.py` 仅接受显式测试会话/记忆 ID，默认只预览；apply 先做
原生 SQLite backup 并备份指导/记忆文件，再软删除会话、撤回来源及记忆并刷新侧写。
`message_*`/`qq_*` 表与非记忆后台任务在同一事务内做内容哈希保护，不清空整个数据库。
指导文件重置另需当前 SHA，防止覆盖检查后的新编辑。原始消息、trace、回收站和备份
保留供恢复；这不是物理删除或隐私擦除工具。实际操作记录见偏好生产对齐报告。

检查默认端口（使用自定义端口时同步替换）：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/health'
Invoke-RestMethod 'http://127.0.0.1:8780/health'
```

后端应返回 `status: ok`、`service: local-knowledge-agent-os`、`version: 0.1.0`。
浏览器聊天地址：

```text
http://127.0.0.1:8780/desktop-pet/chat.html?backend=http%3A%2F%2F127.0.0.1%3A8765
```

只调试后端时可在前台运行，日志直接显示在当前窗口，按 Ctrl+C 停止：

```powershell
Set-Location 'C:\Users\xc133\projects\lka_backend'
.\.venv\Scripts\python.exe scripts\start_backend.py personal --host 127.0.0.1 --port 8765
```

前台命令与完整启动器不要同时争用同一端口。健康检查只证明服务可用；还应在界面发起一次
与需求对应的实际任务，确认选中的真实模型、工作区文件和结果均正常。

原生启动器为消息阅读控制通道配对独立 `LKA_MESSAGES_CONTROL_TOKEN`。未显式设置启动
环境时，它按当前安装路径与端口在 `%LOCALAPPDATA%\LKA\native-credentials` 保存
CurrentUser DPAPI 加密文件，并只向前后端服务进程注入解密值，不输出到日志或写入工作区
`.env`。同一 Windows 用户、同一安装地址的后续启动复用该凭据；迁移电脑需重新配对。
如果不使用完整启动器，须自行向两端注入相同的独立控制凭据，不复用 QQ／导入 Token。

三个获批群已于 2026-10-04 21:40 升级为 v2 采集。以后协议升级前仍需排空旧文本及媒体
同步队列。旧已确认媒体采用独立缓存保留许可，不会因 v1→v2 自动丢失；不重盖旧消息
采集代次、不重传旧媒体，原 TTL／额度与撤销清理继续生效。

## 4. 本机原生安装的停止与重启

当前分开放置的后端/前端只有启动器，没有统一停止脚本。关闭启动 PowerShell 窗口
不会自动停止它创建的后台服务。先等待任务完成；如需关闭桌宠，在任务管理器“详细信息”中
显示“命令行”列，确认包含 `SpinePetGdxLauncher`、此前端目录和当前后端地址的 Java 进程，
只结束该桌宠进程，再停止下面两个服务。
现有桌宠菜单“退出桌宠与后端”仍调用旧的 `stop-native-local.ps1`，涉及旧 PID / 8000 端口，
不应作为此原生安装的停止入口。
便携包的 `Stop-LKA.cmd` 仅管理其自身目录，不能用来停止这份分开放置的安装。

以下 **Windows PowerShell** 片段按启动记录核对安装目录、解释器、PID 和启动时间，
只停止本机原生启动器记录的两个服务及子进程。它是结束进程操作，执行前先完成当前任务。
如果使用了其他目录或端口，先修改开头四个变量。

```powershell
$ErrorActionPreference = 'Stop'
$lkaBackendRoot = 'C:\Users\xc133\projects\lka_backend'
$lkaFrontendRoot = 'D:\agent-bot-frontend'
$lkaBackendPort = 8765
$lkaFrontendPort = 8780
$lkaServices = @(
  @{ Name = 'frontend'; Root = $lkaFrontendRoot; Port = $lkaFrontendPort },
  @{ Name = 'backend'; Root = $lkaBackendRoot; Port = $lkaBackendPort }
)
foreach ($entry in $lkaServices) {
  $recordPath = Join-Path $lkaFrontendRoot ".runtime\lka-native-$($entry.Name)-$($entry.Port).json"
  if (-not (Test-Path -LiteralPath $recordPath)) { continue }
  $record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
  $expectedExe = Join-Path $entry.Root '.venv\Scripts\python.exe'
  if ($record.root -ne $entry.Root -or $record.executable -ne $expectedExe) {
    throw "Installation does not match: $recordPath"
  }
  $serviceProcess = Get-Process -Id $record.process_id -ErrorAction SilentlyContinue
  if ($null -ne $serviceProcess) {
    if ($serviceProcess.Path -ne $expectedExe -or
        $serviceProcess.StartTime.ToUniversalTime().ToString('o') -ne $record.started_at) {
      throw "Process identity does not match: $recordPath"
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $serviceProcess.Id /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop service.' }
  }
  Remove-Item -LiteralPath $recordPath
}
```

没有记录的手动启动实例，回到其终端用 Ctrl+C 停止。记录不匹配时先查明进程来源，
不要按所有 `python.exe` / `java.exe` 或所有 8765 端口进程批量结束。
服务停止后再同步、修改配置或备份，最后重新执行第 3 节的启动命令。

## 5. 配置、数据与日常操作

下表中的后端/前端根目录取决于第 1 节所用安装；便携包分别为包内 `backend` 和 `frontend`。

| 内容 | 位置或处理方式 |
| --- | --- |
| 后端密钥、环境配置 | 后端 `.env` |
| 模型、邮件、检索和后台基础设置 | 后端 `config\local.toml` |
| Outlook 授权 | 以 `mail.outlook.token_store_path` 为准；原生模板为 `data\runtime\secrets\outlook_token.json`，便携包为 `config\secrets\outlook_token.json` |
| 会话、消息、记忆、邮件等 | 后端 `data\runtime`；主要数据库 `lka.sqlite3`，还可能有 checkpoint 数据库、知识文件和日志 |
| 完整 Agent 运行日志 | 后端 `data\runtime\agent_logs` |
| 前端接入与可选 QQ 配置 | 前端 `.env`；QQ 桥接服务需要单独运行 |
| QQ 图片/视频 TTL 缓存 | 前端 `data\qq\media`；后端 `LKA_MESSAGES_MEDIA_CACHE_DIR` 指向该目录 |
| 前端独立历史 | 前端 `data\memory\conversations.db`；不等于 LKA 后端会话库 |
| 桌宠角色与偏好 | 前端 `data\pet\profiles.json`、`state.json` |
| 全局 UI/学习设置 | 一部分由后端数据库保存，一部分为浏览器偏好；便携包通过 `settings` 导出并在首次启动导入 |
| 本地 embedding / reranker 权重 | 以 TOML 的 `cache_dir` 为准；原生模板为后端 `data\runtime\models`，便携包为根目录 `models` |

当前本机原生副本已迁入真实模型配置和所需密钥，并保留了 Windows 验收样例；
它不是空数据库。其邮箱仍是关闭的示例配置。正式便携包保留了有效邮件配置及授权，
交付时不包含业务数据库或使用数据。这两份安装的状态不同。

QQ 持续采集依赖 QQ 登录、SnowLuma/NapCat 桥接和这两个本地服务同时运行。
关闭浏览器窗口不影响采集；关机、注销或休眠会中断事件流，断线不保证补拉。
当前获批的一天采集没有自动停止任务；缓存到期仅删除媒体字节，消息和附件索引仍保留。
媒体默认 TTL 72 小时/总额度 2 GiB，由前端私有 `.env` 的 `QQ_MEDIA_*` 控制，修改后重启生效。
仅启用指定白名单会话的 `record_enabled/media_enabled`，`analysis_enabled=false` 不消耗 LLM。

### 界面操作

1. 打开聊天窗口，点“新建”或“新会话”，确认顶部“当前工作区”。“项目内新建”复用当前项目目录。
2. 点齿轮进入“工作设置”，选择“默认模型”和“安全审查”；默认模型也可跟随后端配置。
   模型优先级是请求覆盖、会话偏好、后端默认，修改 TOML 默认值不会强制覆盖已有会话的选择。
3. “新会话工作区位置”只影响后续创建的会话。原生安装留空时使用默认文档目录；
   便携启动器将工作区基址及允许根目录设为包内 `workspaces`，界面选择的目录应位于该范围。
4. 发起任务；需要确认的工具操作按界面提示处理。“文件”页签查看和预览当前工作区文件，
   “记忆”或“记忆与自动学习”查看、纠正记忆并管理学习设置。
5. 邮件工作通过正常 Agent 对话和 `mail` 工具执行；没有单独的 `/mail/process` 工作流。

Windows 工作区填写 `D:/Work/project` 等后端实际可访问的路径，不填 `/mnt/d/...`。
TOML 推荐正斜杠路径或单引号 literal 字符串，避免未转义反斜杠。
`bash.*` 工具名保留，但 Windows 中执行 PowerShell，例如 `Get-ChildItem`、`Get-Content`、
`$env:WORKSPACE_ROOT`；Linux 命令和 `.sh` 脚本不会自动翻译。

### 邮箱需要重新授权时

先确认 `mail.outlook.enabled`、client ID 和相关环境变量，再重启后端。
已有 token 失效时，在 **Windows PowerShell** 发起设备授权：

```powershell
$lkaBackendUrl = 'http://127.0.0.1:8765'
$mailAuth = Invoke-RestMethod -Method Post -Uri "$lkaBackendUrl/mail/outlook/auth/start"
$mailAuth | Select-Object verification_uri, user_code, message
```

在浏览器打开返回的地址、输入验证码并完成登录，然后在同一 PowerShell 窗口执行：

```powershell
$mailAuthBody = @{ device_code = $mailAuth.device_code } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$lkaBackendUrl/mail/outlook/auth/complete" `
  -ContentType 'application/json' -Body $mailAuthBody
```

返回 `authorized` 才表示完成；`pending` 时先完成浏览器授权，按返回的 `interval`
等待后再提交，过期则重新发起。按需做一次有限同步：

```powershell
Invoke-RestMethod -Method Post -Uri "$lkaBackendUrl/mail/outlook/sync" `
  -ContentType 'application/json' -Body '{"folder":"Inbox","limit":25,"max_pages":1}'
```

同步会下载邮件并写入当前安装的数据目录。若原有自动同步已开启，启动后也会重新拉取邮件；
“迁移包无历史数据”不意味着联网使用后的数据库仍为空。API 细节见 [接口合同](api_contract.md)。

## 6. 在另一台 Windows 电脑上使用便携包

1. 复制整个 ZIP，完整解压到有写权限的本地目录，如 `D:\LKA`，不要在压缩软件里直接启动。
   保持 `backend`、`frontend`、`runtime`、`models`、`settings`、`scripts` 与启动文件的相对位置。
2. 首次启动前可双击 `Verify-Package.cmd`；也可对 ZIP 运行下列命令，与随包 `.zip.sha256`
   或 [交付记录](windows_portable_delivery.md) 比较：

```powershell
Get-FileHash 'D:\LKA-Releases\LKA-Windows-x64-20261004.zip' -Algorithm SHA256
```

3. 双击解压目录中的 `Start-LKA.cmd`；首次启动自动重定位 Python 环境、导入全局偏好并创建新数据库。
4. 使用完毕双击 `Stop-LKA.cmd`。移动整个目录前先停止；移动后仍从 `Start-LKA.cmd` 启动。

在便携包根目录打开 **Windows PowerShell**，也可使用：

```powershell
.\Start-LKA.cmd -NoPet
# 同机有另一份安装时，选择另一组端口：
.\Start-LKA.cmd -BackendPort 8875 -FrontendPort 8890
.\Stop-LKA.cmd -BackendPort 8875 -FrontendPort 8890
```

上述启动命令是不同用法，按需要选择一条。`-NoPet -NoOpen` 只启动服务。
停止时必须使用与启动一致的端口。修改端口只隔离监听地址；从同一个目录启动多个实例
仍会共用数据，需要并行独立运行时应使用不同解压目录和不同端口。

启动后配置、邮箱 token、`pyvenv.cfg` 等会变化，之后逐文件校验出现这些差异不等于 ZIP 损坏。
`settings` 是首启导入用的配置快照，运行后改设置应通过界面或实际配置文件；
首启标记在 `frontend\.runtime\portable-config-imported.json`，不要把删除标记当作日常保存设置的方式。

包内保存真实 API 密钥和邮箱授权，作为私人备份保管。远程推理、邮件和网络检索仍需联网；
QQ 客户端/桥接登录和外部 Codex 不在包内，需目标电脑另行安装、登录或认证。

## 7. 日志、旧会话与常见故障

启动日志在前端 `logs`，服务身份记录在前端 `.runtime`：

```text
logs/lka-native-backend-8765.out.log
logs/lka-native-backend-8765.err.log
logs/lka-native-frontend-8780.out.log
logs/lka-native-frontend-8780.err.log
.runtime/lka-native-backend-8765.json
.runtime/lka-native-frontend-8780.json
```

自定义端口反映在文件名中；桌宠相关日志也在 `logs`。例如查看后端最近错误：

```powershell
Get-Content 'D:\agent-bot-frontend\logs\lka-native-backend-8765.err.log' -Tail 80
```

| 现象 | 排查与处理 |
| --- | --- |
| 端口已占用 | 查清原实例，按其入口停止；或选新端口。不要同时启动旧 WSL 入口和原生入口占用 8765 |
| 缺少 `.venv\Scripts\python.exe` | 原生后端执行 `setup_windows.ps1`；前端需独立准备。便携包检查是否完整解压并从根目录启动 |
| 改配置后没生效 | 先停止服务再启动；重复启动会复用旧进程。核对所选模型和实际连接的后端地址 |
| 新环境仍显示旧会话 | 先检查后端 `GET /sessions`，再刷新或关闭重开聊天窗口，确认加载了新前端脚本 |
| 只有示例回答或推理失败 | 查看实际模型选择、`config/local.toml`、密钥环境变量、网络及后端错误日志 |
| 语义检索未生效 | 检查 embedding/reranker 的模型名与缓存路径。原生同步不带模型权重；便携包附带当前权重 |
| 文件预览被拒绝 | 检查是否在当前工作区内，以及是否为 junction、符号链接、OneDrive 占位项等 reparse 文件；使用已落地的普通本地文件 |
| 桌宠不可见或启动失败 | 查看 Java/桌宠日志和图形驱动；先用 `-NoPet` 验证网页是否能正常工作 |
| PowerShell 命令找不到文件 | 核对执行环境、引号和盘符；WSL `/mnt/...` 命令与 Windows `C:\...` 命令不能混用 |

历史会话曾同时存在于后端和 Java WebView 的 localStorage；仅清后端数据库不会立即清掉
旧窗口中的列表。前端现已按后端确认的 404 清理失效缓存，同时保留分页外会话、草稿和离线内容。
当前电脑旧 WebView 缓存位置为
`%APPDATA%\java\webview\localstorage\http_127.0.0.1_8780.localstorage`。
排查时先刷新并核对 API，不要直接删除整个浏览器配置、所有草稿或 Linux 数据。

日常同步不清空 Windows 数据。若确实要重置，先停止所有相关实例并备份整个运行数据目录，
同时考虑 SQLite 的 `-wal` / `-shm`、checkpoint、知识/记忆文件、前端独立存储和界面缓存；
不要在服务运行时只删除一个 `.sqlite3` 文件。只保留配置的迁移使用新便携包更直接。

## 8. 代码更新后重新生成迁移包

仅同步原生后端不会更新已经生成的 ZIP。先完成本机代码同步、依赖安装、前端更新和实际运行检查，
再用 `scripts/build_windows_portable.py` 生成一个新名称的包。
构建脚本要求原生 Windows x64 Python、已安装的两套依赖、已编译的 Java 桌宠，
并从构建用户的 `%USERPROFILE%\.gradle\caches\modules-2\files-2.1` 收集所需 JAR；
它不会下载依赖，也不适合仅凭干净源码目录直接打包。

以下 **Windows PowerShell** 示例使用当前原生应用代码/依赖，沿用已交付包中的有效邮件配置、
授权与模型权重。如果之后更新过邮件账号或模型，应将对应参数改为最新的有效来源。

```powershell
$lkaSeedBundle = 'D:\LKA-Releases\LKA-Windows-x64-20261004'
$lkaReleaseName = 'LKA-Windows-x64-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
Set-Location 'C:\Users\xc133\projects\lka_backend'
.\.venv\Scripts\python.exe scripts\build_windows_portable.py `
  --backend-root 'C:\Users\xc133\projects\lka_backend' `
  --frontend-root 'D:\agent-bot-frontend' `
  --python-home 'C:\Program Files\Python312' `
  --java-home 'C:\Program Files\Eclipse Adoptium\jdk-21.0.10.7-hotspot' `
  --output-directory 'D:\LKA-Releases' `
  --name $lkaReleaseName `
  --mail-config "$lkaSeedBundle\backend\config\local.toml" `
  --mail-env "$lkaSeedBundle\backend\.env" `
  --mail-source-root "$lkaSeedBundle\backend" `
  --model-cache "$lkaSeedBundle\models"
```

`--mail-config` 只替换邮件配置，模型提供商和其他配置仍取自 `--backend-root`。
`--mail-env` 只提取邮件配置引用的环境变量；`--mail-source-root` 解析相对 token 路径。
若邮件配置和授权已在原生后端维护，可省略这三个邮件参数。
启用 Outlook 但缺少其授权文件时构建会失败，应先完成授权。

最初交付曾从 Linux 提取有效邮件配置、凭据和模型权重，没有复制 Linux 使用数据。
若再次从 Linux 缓存构建，需在 WSL 侧先将模型快照软链接展开为普通文件，再提供给原生构建器；
直接让 Windows Python 读取 WSL 模型软链接不是已验证路径。

脚本拒绝覆盖同名输出，生成目录、ZIP、`.zip.sha256` 和包内 `manifest.json`；
`--skip-zip` 只生成目录。它导出全局配置，排除会话、记忆内容、邮件正文、工作区、日志和业务数据库。
不要直接压缩已经使用过的整份程序目录来代替此流程，那会把新的使用数据一起带走。

新包应在首次启动前校验文件，再解压到独立目录、使用不同端口验证前后端、聊天界面、桌宠、
工作区和所需模型；停止后核对原实例未受影响。记录实际验证结果，不直接沿用旧包的验收结论。
2026-10-04 交付已验证中文/空格迁移路径、原生浏览器与 JavaFX WebView、离线 embedding/reranker、
启动复用及停止隔离；未在第二台物理电脑、真实邮箱收发、真实外部 Codex 或 SMB 共享上完成验收。
