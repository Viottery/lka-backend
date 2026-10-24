# Codex 交接：Ubuntu 云端与新的 Windows 电脑

这是给接手部署的 Codex 的操作说明，配合专用私有仓库内的 `deployment.json`、
`Prepare-Deployment.ps1` 和加密 Release 数据包。不要把部署说明当成已完成部署的证明。
两个产品仓库分别读取自己的 `AGENTS.md`、README 和相关部署文档。

## 目标和已确定的约束

- 云服务器 Ubuntu 22.04；云端运行唯一助理、账号、设备登记、共享记忆与静态工作台。
- 将交付包内的既有 WSL 日常主库完整迁到云端，保留会话、来源、记忆、检查点、预算、
  发布水位和现有账号。不能用测试 hub 或新空库替代，不能手工合并表或清空账本。
- 这是一台**新的 Windows 电脑**，允许原生 Windows 后端作为独立 replica 节点；
  原开发电脑的 WSL 日常运行策略不要求这台新电脑安装 WSL。
- Windows 运行桌宠、前端适配服务和独立节点；节点新建自己的数据库，
  不复制云端主库。普通会话、工作区直连节点；助理相关会话经云端中转。
- 共享记忆自动同步/去重；镜像用于本地读取。启用自动学习同步时沿用来源生效状态，
  矛盾保留待核对。模型凭据按机器配置，断云时已授权普通会话仍需可用模型服务。
- Tailscale 提供两台主机的连通及 HTTPS，应用登录和节点授权仍需保留。
  不开启 Funnel，不关闭 HTTPS/CSRF/Origin 检查来绕过错误。
- 新节点审批默认 manual，shell disabled。不要继承旧测试节点的自动审批授权。
- 不启动/迁移 QQ 宿主、不发送消息、不重置采集水位。QQ 后续单独接入。

## 先收集最少的本机参数

只向用户索取尚缺的值：云端 SSH 主机/别名、用户名/端口、sudo 能力、
两台设备的 Tailscale DNS/HTTPS 地址、Windows 安装目录和批准的工作区目录。
检查已有 SSH、GitHub、Tailscale 登录及服务后再问，不索取私钥内容。
解密密钥由用户单独提供，不能写入仓库、命令行参数、日志或 PR。
云端已安装其他服务、已存在 LKA 目录或数据库时先盘点并备份，停止自动首次安装。

## 1. Windows 获取固定代码和迁移包

安装/核验 Git、**原生 Python 3.12+**、Windows OpenSSH、Tailscale；桌宠另需 Java 21。
GitHub 私有仓库访问使用该用户自己的登录。下载器依次使用 GH_TOKEN/GITHUB_TOKEN、
已有 `gh auth token` 或 Git 凭据管理器；不要将 token 写进下载 URL 或源码。

在私有交付仓库执行：

```powershell
.\Prepare-Deployment.ps1 -Python python -InstallRoot C:\LKA `
  -CloudHost my-cloud -CloudUser ubuntu -CloudPort 22
```

`my-cloud` 替换为真实 SSH 主机/别名，不要求用户手动传任何文件。
未决定 SSH 地址时先省略 CloudHost；之后可重跑同一命令完成传输。
首次 SSH 主机指纹需按正常流程核验；不能设置 StrictHostKeyChecking=no。
脚本使用指定提交创建干净 main checkout，安装依赖，自动下载校验加密分片，
可经 SCP 上传云端 SSH 用户家目录的 `lka-deployment-incoming/`。
复用时只接受完全相同且干净的 checkout，不覆盖另一份已有安装。
网络中断可重跑；已校验的分片跳过，`.partial` 支持续传。
它不启动服务、不写真实业务配置，也不修改已有云端数据。

## 2. 在云端解密、核验并恢复同一份主数据

核验服务器所有权和已安装服务，准备专用 lka 用户、Python 3.12、uv、Git、GnuPG。
Ubuntu 22.04 的系统 Python 3.10 不够运行当前后端及快照工具；
用 uv 安装独立 3.12，不替换系统 Python。
按 `deployment.json` 的完整提交获取两个官方仓库到 `/opt/lka-backend`、
`/opt/lka-desktop`；前端仅用于静态文件，无需在云端安装 Java 或前端 Python 依赖。
产品代码可直接从 GitHub 拉取，私人数据保持加密直到在云端解密。

通过受保护的 SSH stdin 把单独提供的密钥写到云端临时 0600 文件，
不要把密钥拼进 shell 命令、进程 argv、公开输出或聊天回执。
使用已准备的 3.12 解释器：

```bash
python3.12 /opt/lka-backend/scripts/deploy/github_handoff.py decrypt \
  --input ~/lka-deployment-incoming/migration-RELEASE_TAG \
  --key-file /private/temporary-migration-key \
  --output ~/lka-deployment-incoming/unpacked
python3.12 /opt/lka-backend/scripts/data_snapshot.py verify \
  --snapshot ~/lka-deployment-incoming/unpacked/snapshot
```

`RELEASE_TAG` 取 deployment.json；若 Python 由 uv 管理，用其真实路径或 `uv run --python 3.12`。
解密只写全新的暂存目录，先校验分片和 GPG 完整性再展开；
失败不在同一目录覆盖重试，保留现场并选新的暂存目录。
核验成功后删除云端临时密钥文件，保留用户自己的恢复密钥。
snapshot 中的符号链接按原样保留，**不包含其外部目标**；
查看 metadata/external-paths.private.json 盘点旧路径，不访问/打印凭据内容。

严格按后端 `docs/deployment_migration.md` 恢复到首次部署的空路径：
`/var/lib/lka/runtime`、`/etc/lka/local.toml`、`/etc/lka/backend.env`。
如这些路径已存在，先调查，不能覆盖或自动合并。完整快照保留为恢复点。
在迁移配置上定点修改路径，保留原模型与其凭据；
旧工作区、媒体、模型缓存和托管 Python 的绝对路径按实际服务器核对。
历史会话保留不等于其旧目录在新系统上可执行。

## 3. 配置并启动云端

以当前配置 schema 为准，至少核验：

```toml
[auth]
enabled = true
require_https = true
secure_cookie = true
allow_bearer_sessions = true
allowed_origins = ["https://CLOUD.ts.net", "https://WINDOWS.ts.net"]
[devices]
enabled = true
[sync]
enabled = true
role = "hub"
memory_write_enabled = true
memory_aggregation_enabled = true
[connections]
enabled = true
[management]
enabled = true
autonomous_enabled = false
default_task_parent = "/var/lib/lka/tasks"
[remote_execution]
enabled = true
[client]
workbench_dir = "/opt/lka-desktop/app/web/pet"
```

这些是差异示意，不能覆盖整个既有 TOML；用精确真实来源替换示意域名。
首次核验期间保留自主唤醒和后台模型整理关闭，确认原有状态与预算后再按用户意愿恢复。
关闭旧消息采集/分析，但保留旧源、游标、水位、额度及历史。
环境路径参考 cloud.env.example。安装 `lka-cloud.service.example` 对应的单进程服务，
必须 `personal --require-auth`，后端只监听 127.0.0.1:8765。
Tailscale Serve 提供 tailnet 内有效 HTTPS；先检查现有 Serve 配置，不重置其他服务。

已有账号原样保留。若主库没有管理员，使用 `scripts/manage_accounts.py create-admin USERNAME`
在本机终端 getpass 交互创建，不硬编码默认密码，不重置已有账号。
启动前确认原主机不会同时受理同一主库的任务。当前交付是已停写的历史快照；
若原主机在快照后继续使用，不能宣称包含后续增量，须先另行确定最终切换点。

## 4. 登记新电脑并配置独立 replica

通过已认证云端 API 获取真实 `/sync/status` 的 hub_id，
使用 `POST /devices` 登记唯一 device_id、Windows environment_id、workspace_ids、
capabilities 和新电脑自己的 `https://HOST.ts.net`（当前 advertised_url 只支持默认 443）。
用户工作目录的本机路径不上传为云端执行路径；云端只登记 workspace ID。
最少 scopes：heartbeat、sync.read、connections.poll；远程派发增加
execution.poll、execution.run、execution.result；共享编辑增加 sync.memory.write；
自动学习同步增加 sync.memory.aggregate。不要无理由扩大工具或原生 shell 权限。
设备令牌仅返回一次，直接写入新节点私有环境文件，不返回聊天或浏览器。
同名设备已存在时先调查；不自动撤销或旋转正在使用的设备。

在新 Windows 后端配置 replica、真实 hub_url/hub_id/token_env、
connections.node_id/allowed_origins/workspace_roots、
remote_execution.environment_id/workspace_mappings/allowed_tools。
management.enabled=false，review_policy=manual，shell_trust=disabled。
approved workspace_roots 与 LKA_WORKSPACE_ROOTS 必须一致。
共享编辑和自动学习同步开关与云端一致；本机模型从已有配置私下迁移必要 provider 信息及
其引用的环境值，不复制云端账号会话或消息/控制凭据；各设备使用自己的凭据。

前端 .env 设 `LKA_CLOUD_ORIGIN=云端HTTPS源`、
`LKA_WORKBENCH_BACKEND_URL=http://127.0.0.1:8765`、
`LKA_CLOUD_SESSION_PATH=本机.runtime下的DPAPI文件`。
保持 LOCAL_RAG、shell 和 QQ 插件按默认关闭。新 Windows 用户重新登录，不迁移旧 DPAPI。
前端和后端均指向各自独立的本机数据目录，禁止把解密后的 snapshot 复制给节点。

启动原生节点和桌宠：

```powershell
C:\LKA\lka-desktop\run-lka-native-windows.ps1 `
  -BackendRoot C:\LKA\lka-backend -RequireNode
```

首次可加 `-NoPet -OpenBrowser`，核验后再启动 Java 桌宠。
这个参数要求节点认证配置，否则后端拒绝启动；复用旧进程时也核验 --require-node 标记。
为该节点配置 Tailscale Serve 443 到本机 8765，手机/其他设备才能直连普通会话和工作区。

## 5. 固定更新方式和验收

在前端 `.runtime/update-settings.json` 设置 backend_mode=native、真实 backend_root、
backend_data_dir、backend_config_file 和实际端口；额外私有数据路径加入 backup_paths。
用户以后双击 Check-LKA-Update.cmd 或 Update-LKA.cmd 即可；云端仍单独按原部署流程维护。
云端静态页面与 Windows 使用相同配套前端提交，核对 Service Worker/浏览器缓存。

完成后向用户报告实际地址、提交号、服务名、恢复点与以下验收结果：

- 原会话/消息/记忆/检查点数量及 ID 与快照一致；附件可读；没有清空水位、额度或来源。
- 云端及节点 HTTPS、云端匿名访问 401、账号登录/退出、CSRF/Origin 和节点兑换连接正常。
- 云端助理能派发到指定 Windows environment/workspace，审批/结果经云端中转；
  Windows 普通会话和工作区归属清楚，可从手机直连、上传和下载测试文件。
- 云端与节点的共享记忆同步及重复投递去重；短时断云时已有节点连接能进行普通会话。
- 管理员口令、模型密钥、节点 token 和迁移密钥均不在 Git、日志或回执。

真实模型验收只做用户授权范围内的小任务，先设定少量预算；
模拟/接口通过不能代替模型语义质量。不能用自动审批来掩盖配置错误。
