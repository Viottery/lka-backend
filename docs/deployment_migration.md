# 部署云端和其他电脑，沿用现有数据

本文用于把一个已有日常实例迁为云端主库，再连接多台独立电脑和手机。
先确定唯一主数据源，保留原安装与测试数据；不把多份 SQLite 合并，也不在新设备克隆整个主库。
代码安装见[开始使用](getting_started.md)，接口和设备 scopes 见[客户端交付](distributed_client_handoff.md)。

## 数据归属与迁移范围

| 内容 | 迁移后位置 | 保留方式 |
| --- | --- | --- |
| 现有助理、记忆、待办、项目、历史消息、预算和水位 | 云端主库 | 同一份完整数据快照，保留原 ID、来源、版本和账本 |
| 原日常实例的普通会话历史 | 随主库迁云端 | 云端账号可查看；不会自动出现在新电脑的普通会话列表 |
| 新电脑的普通会话、检查点和工作区 | 各自电脑 | 新的独立数据目录；普通会话和文件直连所属电脑 |
| 共享记忆 | 云端权威，各电脑镜像 | 正式同步与本地缓存；不会复制整套数据库 |
| 原项目目录、知识源文件、QQ 媒体、外部凭据 | 原所属电脑或显式迁移的位置 | 单独盘点、拷贝和校验，不在数据库迁移中假定已覆盖 |
| 前端 `.env`、桌宠偏好、QQ 数据、DPAPI 凭据 | Windows 本机 | 私下备份；换 Windows 用户/电脑后重新配对和登录 |
| 另一个测试 hub / 测试节点 | 独立保存 | 不覆盖日常主库，也不把测试设备令牌用于新主库 |

数据库保留历史不等于旧任务能直接在另一操作系统继续。旧工作区、媒体、索引、模型缓存、
托管 Python 环境及其符号链接可能引用原主机绝对路径；先核对映射和实际文件。
不存在通用的“只把助理表合并到另一数据库”的迁移工具，也不通过手工修改 SQLite 表来实现。
要保留历史任务的可执行目录，须显式迁移相应文件并在目标主机提供兼容路径；
否则保留历史供查阅，后续操作在新设备批准的工作区另开任务。

## 固定配套代码版本

后端与前端是两个仓库：

- 后端：`https://github.com/Viottery/lka-backend.git`
- 前端：`https://github.com/Viottery/lka-desktop.git`

记录两个 `git rev-parse HEAD`，目标安装使用相同提交，不只写 `main` 或旧 `deployment_id`。
本地提交需先推送到自己的远端，或以 `git bundle` 私下交付；提交代码不会同步运行数据。
旧部署清单属于原安装，不应作为新服务器已加载配套版本的证据。
在已有 checkout 更新时先保存本地改动，不用强制重置来覆盖配置或工作区。

## 创建和核验主数据快照

1. 等待普通、助理及后台任务空闲，停止**这份数据对应**的后端和其他写入者。
   记录原数据目录、私有 TOML、环境文件和代码提交。停服操作见[维护说明](maintenance.md)。
2. 用原安装的 Python 创建快照；输出必须是全新目录，不能放到源数据目录内：

```bash
.venv/bin/python scripts/data_snapshot.py create --offline \
  --data-dir /path/to/existing/runtime \
  --env-file /path/to/backend/.env \
  --config-file /path/to/backend/config/local.toml \
  --output /path/to/private-backups/daily-snapshot
.venv/bin/python scripts/data_snapshot.py verify \
  --snapshot /path/to/private-backups/daily-snapshot
```

Windows 使用 `.\.venv\Scripts\python.exe` 运行相同子命令，传入该电脑的原生路径。
`--offline` 是操作者确认已停止写入，工具不会停止服务；只做空闲及拷贝期间的变化检查，
不能阻止另一个进程重新启动。出现写入或验证失败时，不使用未完成的快照。

快照包含 `data/`、可选的 `private/backend.env` / `private/local.toml` 和 `manifest.json`。
数据库通过 SQLite backup API 生成独立副本，包含已提交 WAL 内容，不靠只复制主库；
检查点、来源、水位和账本不清零。其余文件逐个校验 SHA-256，原数据不会被改写。
工具拒绝覆盖输出目录，`verify` 会检查遗漏、多余文件、文件损坏及路径越界。

快照和清单包含私人数据及路径，私有配置可能含密钥，不能提交或上传到公开仓库。
传输可用 SSH/rsync 或保留符号链接的私有 tar 包；到目标服务器再运行 `verify`。
清单记录符号链接但不会跟随拷贝外部目标；检查链接及配置引用的外部文件是否另已迁移。
后端根目录的 `.venv` 不迁移；数据内的托管 Python 环境也需按目标解释器核对或重建。
`production_release.py snapshot` 只备份代码/配置，不能替代本工具的数据快照。

## Ubuntu 22.04 云端

使用原生 Python 3.12+、单进程 SQLite 和 systemd。不要替换 Ubuntu 自带的系统 Python。
在专用用户 `lka` 下准备后端 `/opt/lka-backend` 和前端 `/opt/lka-desktop` checkout，
按已记录的两个提交安装；前端在云端只提供 `app/web/pet` 静态页面，不运行 Java、QQ 或 Windows 前端服务。

```bash
# 在 /opt/lka-backend 中，按维护文档准备 uv 后：
uv python install 3.12
uv sync --locked --python 3.12
```

将快照传到私有暂存目录并核验，然后在首次部署的**空目标**安装数据：

```bash
set -e
SNAPSHOT=/srv/lka-transfer/daily-snapshot
.venv/bin/python scripts/data_snapshot.py verify --snapshot "$SNAPSHOT"
sudo install -d -m 700 -o lka -g lka /var/lib/lka /etc/lka
sudo test ! -e /var/lib/lka/runtime
sudo test ! -e /etc/lka/local.toml
sudo test ! -e /etc/lka/backend.env
sudo cp -a "$SNAPSHOT/data" /var/lib/lka/runtime
sudo chown -R lka:lka /var/lib/lka/runtime
sudo install -m 600 -o lka -g lka "$SNAPSHOT/private/local.toml" /etc/lka/local.toml
sudo install -m 600 -o lka -g lka "$SNAPSHOT/private/backend.env" /etc/lka/backend.env
sudo install -d -m 700 -o lka -g lka /var/lib/lka/tasks
```

已有云端数据时先停服并另外备份；不要把以上拷贝改成覆盖原目录。
当前工具不自动恢复、重写数据库路径或创建账号。保留传输前的完整快照，
再定点修改复制后的配置，不能用示例 TOML 覆盖原模型、密钥和偏好。
环境路径参考 [cloud.env.example](../scripts/deploy/cloud.env.example)。

在原 TOML 的对应段落内修改，以下是**配置差异示意，不能直接替换整个文件**：

```toml
[auth]
enabled = true
require_https = true
secure_cookie = true
allow_bearer_sessions = true
allowed_origins = ["https://cloud.your-tailnet.ts.net", "https://desktop-a.your-tailnet.ts.net"]
[devices]
enabled = true
[sync]
enabled = true
role = "hub"
[management]
enabled = true
autonomous_enabled = false
default_task_parent = "/var/lib/lka/tasks"
[remote_execution]
enabled = true
[client]
workbench_dir = "/opt/lka-desktop/app/web/pet"
```

首次验收时保持自主唤醒、记忆后台模型整理及旧消息采集关闭，核对历史与路径后再按原意启用。
重新设置 `LKA_DATA_DIR=/var/lib/lka/runtime`、`LKA_LOCAL_CONFIG=/etc/lka/local.toml` 和该主机的
`LKA_WORKSPACE_ROOTS`。模型与外部服务凭据沿用私下保存的值；更新原机专属缓存/媒体/授权路径。
如果原安装尚未启用账号，先按[云端维护步骤](maintenance.md#ubuntu-2204-云端共享核心)
交互创建管理员；如果已有账号，保留账号表，不重置密码或另建同名身份。

用 [lka-cloud.service.example](../scripts/deploy/lka-cloud.service.example) 安装 `lka-cloud.service`，
其 `--require-auth` 不可省略。后端保持 `127.0.0.1:8765`，通过已有 Tailscale Serve
配置提供可校验 HTTPS，不启用公网 Funnel。每台设备地址加入精确 `allowed_origins`。
云端 `GET /sync/status` 返回新主库的真实 `hub_id`；登记节点时使用它，不沿用另一测试 hub 的 ID。
若迁移的本来就是既有 hub，则账号、设备、hub_id、来源和水位须作为同一主库一起保留。

## 其他电脑作为独立节点

每台电脑安装相同后端版本并重建本机 `.venv`。Windows 使用 `scripts/setup_windows.ps1`；
Linux 按[安装指南](getting_started.md)安装。每个新节点使用新的独立数据目录，
不要把云端主库、其他电脑的 SQLite 或测试节点的 `.env` 复制过去。
原日常电脑若转为节点，也使用新数据目录；原库与快照保留，避免运行第二个同身份助理。

1. 登录云端登记唯一 `device_id`、`environment_id`、工作区 ID 和该电脑的 Tailscale HTTPS 地址。
   普通连接需要 `heartbeat`、`sync.read`、`connections.poll`；远程派发另需
   `execution.poll`、`execution.run`、`execution.result`。
   人工记忆编辑另授 `sync.memory.write`，自动学习同步另授 `sync.memory.aggregate`。
   设备令牌只显示一次，单独保存到该节点私有环境文件，不能发给浏览器。
2. 配置 `sync.role="replica"`、真实云端 `hub_url` / `hub_id` 和 `token_env`。
   该节点 `management.enabled=false`，不能创建第二个管理协调器。
3. 在 `connections` 配置该电脑自己的 node_id、HTTPS origin 和 workspace_roots；
   在 remote_execution 的 workspace_mappings 使用相同 ID/路径，设置自己的 environment_id。
   工作区包含于该节点 `LKA_WORKSPACE_ROOTS`，Linux 路径不能照抄到 Windows。
4. 仅授予所需工具。审批默认 manual，shell 默认 disabled；不能把当前测试机的授权
   当成其他电脑的自动审批授权。启动后验证工作区与实际系统归属。

模型配置由各电脑独立维护，普通会话离线运行仍需可用的模型服务。节点使用
`personal --require-node` 启动自己的日常数据；`test` 模式只用于可丢弃的兼容测试。
Linux 可使用 [lka-node.service.example](../scripts/deploy/lka-node.service.example) 和
[node.env.example](../scripts/deploy/node.env.example)，自行调整路径。
Windows 可先用独立终端运行：

```powershell
.\.venv\Scripts\python.exe scripts\start_backend.py personal --require-node --port 8765
```

新电脑的前端按 `lka-desktop/docs/deployment.md` 安装，使用本机节点和 `LKA_CLOUD_ORIGIN`。
Windows 本机服务可用 `LKA_CLOUD_SESSION_PATH` 保存当前用户 DPAPI 加密会话；
新机器重新登录，不复制旧 DPAPI、账号密码库或节点连接 cookie。
手机通过 Tailscale 打开云端 `/desktop-pet/connect.html?layout=mobile`，无需复制数据库。

## 切换验收与回滚

切换前对照快照清单核对会话、消息、记忆和检查点数量与原 ID；校验附件字节及原文件是否可访问。
随后检查 HTTPS 登录、匿名 401、节点授权兑换、普通会话隔离、工作区读写、云端助理中转、
双向记忆同步，以及云端断开时节点普通会话和缓存的行为。数据数量一致不代表路径和执行权限已正确。

验收前只启动一个权威主库的助理调度器，不让原实例和云端同时对外受理同一批任务。
原数据不删除，首次云端写入前保留可回滚点。若需回滚，先停云端、保留其新数据快照，再选择唯一主库。
云端已有新消息、记忆或副作用后，直接恢复旧库会丢失增量；不能自动把两边历史合并或重放旧任务。
