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

数据库耗时记录默认开启，环境变量 `LKA_DB_TIMING_ENABLED=false` 可关闭。
`LKA_DB_TIMING_SAMPLE_RATE=0.01` 默认采样 1% 的普通请求及无请求归属的数据库操作；
`LKA_DB_TIMING_SLOW_MS=50` 指定慢操作／请求阈值（毫秒），慢记录和数据库错误不受采样限制。
修改这些变量后需在任务空闲时重启后端。记录说明见[数据库耗时排查](maintenance.md#数据库耗时排查)。

应用内的 SQLite 写事务按数据库文件排队，独立于耗时日志开关，读取保持并发。
`LKA_DB_WRITE_QUEUE_CAPACITY=128` 限制每个库的等待写入数；
`LKA_DB_WRITE_QUEUE_WAIT_SECONDS=5` 限制取得写许可前的等待秒数；
`LKA_DB_WRITE_TRANSACTION_SECONDS=30` 限制取得许可后的整个写事务时长，连续写入不会续期。
三者均须为正值；等待上限为300秒，事务上限为3600秒。修改后在任务空闲时重启。
到期事务会被打断并回滚，清理完成后才交出写许可，旧连接不能继续提交。
超时不是业务操作可安全重放的保证，恢复规则见[维护说明](maintenance.md#数据库耗时排查)。

## 可选云端账号、设备与共享同步

这些能力默认关闭。个人云端实例可启用：

```toml
[auth]
enabled = true
allowed_origins = ["https://host.example-tailnet.ts.net"]
# 非浏览器客户端需要时另设 allow_bearer_sessions = true。

[devices]
enabled = true

[sync]
enabled = true
role = "hub"
```

应用账号使用密码登录，首个账号由 `scripts/manage_accounts.py create-admin owner` 在服务器终端创建。
所有账号都是共享同一套个人数据的受信任管理员，不提供不同用户之间的数据隔离，也不开放匿名注册。
密码使用带随机盐的 PBKDF2-SHA256（600000 次）；改密/重置会撤销目标账号全部会话。
浏览器使用 HttpOnly/Secure/SameSite=Strict cookie；写请求需要已配置的完整 Origin 和登录返回的 CSRF token。
`require_https`、`secure_cookie` 默认 true；反向代理只信任明确的本机入口，不能通过外部任意转发头绕过。
认证保护普通 API、附件、文件、文档和 SSE；公开健康接口只返回最小服务状态，
`/client` 及其固定静态资源提供不含凭据的登录入口。
消息 IMPORT/API/CONTROL 凭据继续单独校验，应用登录不代替这些凭据。

设备登记时分别批准系统环境、能力、工作区 ID 和 scopes。`heartbeat` 允许报告状态，
`sync.read` 明确授予读取整套共享投影，`sync.propose` 只允许提交待核对提案；后两者默认不授予。
设备令牌仅在登记/轮换时返回一次，服务器只保存哈希。固定选择在目标离线时返回冲突，不自动换电脑；
自动选择只在批准的候选设备、环境、能力及工作区中解析目标。当前选择接口不执行远程命令。

另一台电脑的本地副本配置如下，密钥放本机环境变量，不写入 TOML：

```toml
[sync]
enabled = true
role = "replica"
hub_url = "https://host.example-tailnet.ts.net"
hub_id = "从已认证的云端同步状态读取的hub_id"
token_env = "LKA_NODE_TOKEN"
poll_seconds = 15
page_size = 100
request_timeout_seconds = 10

[management]
enabled = false
```

replica 模式将读取副本保存在自己的 `data_dir/shared-replica.sqlite3`，不覆盖本机 `lka.sqlite3`。
后台线程主动心跳和拉取有界分页，固定 hub 身份，拒绝重定向和环境代理；设备需授予 heartbeat 与 sync.read。
可用 `scripts/cloud_sync.py status` 或 `run --max-pages 8` 检查/执行单次同步。
`queue --file proposal.json` 将形如 `{"kind":"memory_candidate","text":"..."}` 的 JSON 提案持久排队，
kind 也可为 `assistant_note`；
可另给稳定 `proposal_id`、`target_ref` 和 `base_revision`；重试同 ID 不得改变内容。提交还需要 sync.propose。
提案不会自动成为有效记忆或执行指令；云端只支持标为 reviewed/rejected，正式记忆修改仍走原领域接口。

同步包括有效记忆及来源状态、助理身份/目标、事项、委派和交付索引；普通会话消息、运行租约、
模型配置、私有路径、凭据、附件及文件内容均不复制。副本读取会检查源有效性、期限和删除状态。
普通 Agent 的上下文与 memory.search/read 会读取本地镜像，允许脏读，不因云端断线阻塞。
本地与云端记忆 ID 分开，不覆盖本地记忆表；项目记忆用 sync.memory_project_bindings
显式映射本地记忆 project_id 到云端 project_id。sync.memory_cache_stale_seconds 默认 60，
只影响新鲜度显示，不是过期授权；已知删除、来源失效及记忆本身的 expires_at 仍过滤。
云端与节点可显式打开 `sync.memory_write_enabled=true`，并给受信任节点授予 `sync.memory.write`。
节点 `/client` 的共享记忆面板支持人工确认的新增、更正和撤回；离线编辑持久排队，
直到云端接受并重新拉取前不影响本地检索。更新携带预期版本，冲突保留提案和当前版本，
由用户重新选择提交，不采用最后写入覆盖。仅启用人工编辑时，自动学习的记忆仍保留本机；
自动同步需另启用下述数据库同步开关。
这个 scope 信任节点为已登录用户证明人工操作；它不是端到端用户签名，不能授予不受信任节点。

自动学习记忆的数据库同步使用独立开关 `sync.memory_aggregation_enabled=true`，
云端与节点均须启用，节点另需 `sync.memory.aggregate`。
该 scope 明确授权受信任节点后台复制已发布记忆的 active/candidate 状态，
不要求每条重新人工确认，也不向模型开放伪造确认身份的参数。
各节点独立保存记忆数据库，以事务变更记录和持久队列上传；云端按规范化内容、类型、
全局/项目范围、敏感级别和期限核对重复，合并来源关联，保留冲突待核对。
正常记忆可共享，敏感记录及未绑定项目不自动导出；不会复制完整会话或 SQLite 文件。
撤回、来源失效和隐私变化要传播，已被云端接管的本地条目不能在共享删除后作为旧副本复活。
此开关与原手工共享编辑开关独立，默认关闭；启用代表批准该节点参与记忆数据库汇总。
`scripts/cloud_sync.py run` 用于镜像拉取和普通提案检查，不代替记忆归集或人工编辑队列的后台 worker。
记忆发布由正常运行时统一执行，可从节点 `/local/cache` 的 memory_aggregation 查看状态。

### 设备直连与远程任务

云端另启用 `[connections].enabled=true`、`[remote_execution].enabled=true` 和管理助理。
账号登录后可在 `/client` 查看设备、获取单节点连接授权和访问助理。
登记设备时，按用途授予 connections.poll、execution.poll、execution.run、execution.result；
不授予这些 scope 的设备不能领取对应授权/任务。advertised_url 填节点自身的 Tailscale HTTPS origin。

节点在上面的 replica 配置基础上增加：

#### 同源托管工作台

云端与节点都可以在自己的 HTTPS origin 下提供 LKA Desktop 浏览器工作台，二选一：

- 后端托管：`[client].workbench_dir` 填前端仓库 `app/web/pet` 的绝对路径，页面位于
  `/desktop-pet/`，入口为 `/desktop-pet/connect.html`。静态文件与 `/client` 一样无需登录，
  只允许已知静态类型，拒绝隐藏文件与越界路径；所有 API 仍走原账号/节点认证。
- 路径转发：不改后端配置，用 Tailscale serve 或反向代理把 `/desktop-pet/` 映射到同一目录，
  其余路径转发到后端，例如 `tailscale serve --https=443 --set-path=/desktop-pet /path/to/app/web/pet`
  再加 `tailscale serve --https=443 http://127.0.0.1:8765`。

两种方式都要让后端看到 `https` scheme（`require_https`、节点 cookie 依赖它）；
`scripts/start_backend.py` 在要求认证时只信任 127.0.0.1 的代理头。节点
`connections.allowed_origins` 填节点自己的 HTTPS origin。页面先读 `/client/info` 判断
hub/replica 角色；replica 另返回 `sync.hub_url`，节点页面据此直接连接云端，
在任何一台电脑上都保留真理与待办入口（云端可达时）。为此云端 `auth.allowed_origins`
除云端自身外还要列出各节点的 HTTPS origin（云端 CORS 随之放行带凭据请求）。
Tailscale 的 `*.ts.net` 名称属于同一站点（ts.net 是公共后缀），SameSite=Strict 的云端
cookie 在节点页面的请求中仍会携带；`/auth/me` 为 cookie 会话返回由会话凭据派生的
CSRF，同一浏览器的新标签页不需要重新登录。

云端本机执行的助理任务需要服务器上的目录：设置 `[management].default_task_parent`
为云端主机的绝对路径（每个任务在其下建独立子目录），或为助理配置
`default_execution_workspace`。都没有时任务仍可做数据类工作，但派发回执带
`workspace_notice`，文件与命令工具不可用，不会回退到后端源码目录。
`[management].autonomous_enabled` 控制任务完成后是否在后台自动唤醒助理汇报；
界面中的“启用管理”只表示允许整理事项与派发任务。

```toml
[connections]
enabled = true
node_id = "desktop-a"
allowed_origins = ["https://desktop-a.example-tailnet.ts.net"]
workspace_roots = { work = "/home/me/work" }

[remote_execution]
enabled = true
environment_id = "linux"
workspace_mappings = { work = "/home/me/work" }
allowed_tools = ["filesystem.read_file"]
poll_seconds = 3
```

同一 workspace_id 在两个映射中必须指向相同本机路径，并包含于该节点的 LKA_WORKSPACE_ROOTS；
Linux 路径不能直接复制到 Windows。节点已登记的环境必须批准该 workspace_id。
allowed_tools 是节点操作者明确授予远程任务的工具；空列表不提供工具，不因云端任务要求扩大。
远程任务使用原安全审查，节点默认强制 manual；明确允许 inherit 后按下文的节点策略执行。
权限、租约和审批不用脏缓存判断。
跨机累计后台预算尚不能转移，带该类预算的委派明确拒绝；不重置额度。

节点轮询接收一次性连接授权的哈希，客户端在节点 `/connections/redeem` 兑换本地会话，
浏览器不持有设备 token。已有节点会话、普通任务和缓存读取可在云端离线时继续，
模型仍需该节点自己的可用配置；这不承诺云端下线时外部模型供应商也可离线使用。
本地 cookie 有效时新标签页可以从 `/connections/me` 恢复 CSRF；本机账户也可独立配置，
不复制云端账户/密码数据库。新云端授权及助理任务派发仍需云端在线。

助理派发时 execution_target 可包含 device_id、environment_id、workspace_id、capabilities，
workspace_path 仅用于原本机执行，不能与远程 target 混用。远程任务会话从云端查看，
普通会话及文件管理从节点 `/client` 访问。远程任务支持取消、审批、检查点暂停/继续和用户问答。
节点 `[remote_execution].review_policy` 默认 `manual`，强制人工审批；明确设置为 `inherit` 后，
新任务沿用派发时冻结的 `skip`、`llm` 或 `manual`，`skip` 仍保留逐调用安全审查记录。
任务详情返回 `approval.requested_mode`、`effective_mode` 和 `source`，说明派发请求与节点实际策略；
节点领取前生效模式为 null，旧节点没有回报时不能据请求模式推断已经自动批准。
配置修改只影响新领取任务，现有运行继续沿用原策略；原生主机执行仍强制人工审批。
已确认终态后可在原会话补充指令，执行目标和工作区保持绑定；未知副作用不自动恢复。
Linux 节点可使用操作者已建立的原生挂载：在 workspace_roots 登记挂载根目录，
再以 `connections.workspace_mounts.<ID>` 配置 expected_filesystem、expected_source、read_only
及 health_probe_timeout_seconds（默认 2 秒）。根目录必须与 Linux mountinfo 中挂载点精确匹配。
未挂载、来源变化或探测失败时不可使用，直连文件接口使用挂载身份与目录描述符，
不会向卸载后露出的本机目录回退写入。数据库和检查点目录不得与挂载工作区重叠。
当前不安装或自动建立挂载，也不支持 Windows 挂载身份验证；通用 Agent 工具向挂载目录写入
暂时拒绝，创建文件/目录须经直连工作区接口。探测不能证明提供程序缓存的新鲜度，
也不能瞬时停止已提交给内核的 I/O；需要写回缓存的挂载不能当作严格断线拒写方案。
通用工具的路径读取仍有卸载竞态；需要固定挂载身份的读取应使用直连文件接口。

`remote_execution.shell_trust` 默认 `disabled`。明确设置 `host_user` 且批准 `bash.run` 后，
没有继承来源/副作用限制的任务可获得冻结的原生执行授权，每次调用都须人工审批并重验活租约。
这允许以节点操作系统用户身份访问文件和网络，工作目录不构成沙箱。配置撤销后后续调用被拒绝；
挂载工作区和受限来源任务不会因此获得原生执行权。

节点 `/client` 可列出并撤销自己的连接。云端撤销既有连接、修改密码或禁用账号后，
节点下一次成功拉取授权代次时撤销旧会话；断网本身不清除本地连接，也不保证离线即时撤销。

## 接入模型

管理特殊会话的后端试用入口默认关闭。仅在需要时设置：

```toml
[management]
enabled = true
```

这启用管理会话、长期目标 API、管理工具包和已接受独立任务的持久派发 worker。
自动管理轮次另需 `autonomous_enabled=true`，且 controller 为 active；默认关闭。
`[management].safety_review_mode` 可单独设置管理会话默认审批，以及新派发任务、事务线和续作的审批模式。
设为 `"llm"` 后，新派发工作冻结模型审批，不受主管本轮界面审批选择影响；不设置时沿用原有继承规则。
普通会话偏好、历史运行和已有待人工审批不因此改变。远程节点须允许 `remote_execution.review_policy="inherit"`。
`[management].safety_review_policy` 默认 `"standard"`，保留模型未批准时转人工的行为。
配合 `safety_review_mode="llm"` 设为 `"autonomous"` 后，管理助理及新派发工作使用自主审查策略：
授权目标内的常规读取、必要的可恢复编辑和本地记录更新通常允许，不要求用户逐项点名每个实现步骤；
审查仍拦截越权、不可逆破坏、未经授权的对外发送／发布／财务操作、凭据外泄和安全权限变更。
模型拒绝、输出无效或普通审查错误时，原工具不执行，拒绝原因返回 Agent 以选择安全替代步骤，不创建人工审批。
强制人工确认的调用在此策略下直接被拒绝，不能由模型代批；额度、取消、来源及租约限制仍按原规则停止执行。
策略在运行、队列受理和事务线创建时冻结，历史运行与已有人工审批保持原状。
远程节点只有允许 `review_policy="inherit"` 才沿用此策略，强制人工的节点仍保留其本地策略。
管理会话的 client/model 可通过 `/managers/{session_id}` 保存；显式请求覆盖优先，
每次运行冻结自己的设置，恢复继续使用原设置。管理会话开放 management、history 包及 Matter 读取和轻量主管写入工具，
主管同时可发现来源授权范围内的只读工具和支持只读参数的工具；后者实际调用仍强制按参数验证只读，不能借此执行写命令。
主管角色在首次路由前生效：复杂执行工作默认下发，简单搜索或明确的单次操作可以直接完成。该角色策略冻结在本次运行中，不加入普通执行会话，也不改变独立安全审查器。
管理会话不自动绑定自身工作区；controller.default_execution_workspace 独立保存客户端提供的默认执行父目录。新派发会话自动建立独立目录并冻结路径；未配置时允许纯数据任务，但不开放工作区工具，不回退到后端目录。普通任务不开放管理工具；独立事务线子 agent 具有受限管理能力，继承委托时冻结的模型、工具与工作区设置。
controller 新建为 paused；设为 active 才能发出创建、介入或控制命令，读取仍可用。
管理生命周期接口沿用现有本机／记忆管理 token 保护，工具写操作仍经统一安全审查。
智能体创建的事务线及执行会话不会提升权限：高风险或显式 requires_confirmation 工具要求人工批准，skip/llm 不能代批；自主审查策略拒绝此类调用并反馈给 Agent。普通恢复不能绕过原审批。
管理派发要求 `agent.checkpoint_backend="sqlite"`（默认）；memory 模式拒绝新任务／指令／恢复派发，
已接受的重复命令仍返回原回执。不要在有待运行任务时改为 memory；待派发任务会明确失败，避免静默降级。

自动管理响应统一关注巡检、已关联任务终态、目标到期及显式配置的提醒／周期策略，不订阅自己的回答或每个 token。
`poll_seconds` 默认 5 秒；普通自主轮的 `min_wake_interval_seconds` 默认 60 秒。
`attention_sweep_interval_seconds` 默认 1800 秒，统一巡检全部有效 active/waiting 关注目标，无需逐个配置定时器；设为 0 可关闭周期巡检，保留任务／定时事件。启用或首次发现管理会话后等待一个间隔再开始巡检，漏过的周期合并，未消费的同版本目标不重复入队。大量目标沿现有事件队列分批处理，会受到最小唤醒间隔、配额和故障停止条件约束；不保证每个周期内完成全部检查。事务线子管理会话不另外启动统一巡检。
`max_daily_rounds` 默认不设上限，可选配置按 UTC 日计算；`max_causal_rounds=6` 防止普通自动事件循环派生。
管理、目标和执行额度继续使用持久账本；当前任一关联额度耗尽时，沿用保守停止该主管后续自动轮的策略，不会因新巡检周期绕过或重置额度。
显式派发的事务线和它的回报继续运行，不要求打开 autonomous_enabled，也不受普通自主轮间隔／轮数限制；
父与子 controller 仍须 active。`failure_limit=3` 保留连续失败停止机制，需检查原因后显式介入。

`internal_items_enabled=false` 默认禁止创建内部事项或把用户事项转为内部事项。
所有客户端先兼容 visibility 与完整同步并重建缓存后，才启用该开关；它不是权限开关。
任务根据持久租约独立派发，不设置管理专属固定 worker 数或就绪释放槽位。
旧 `dispatch_workers` 输入仍可读取但不再影响运行。队列保留依赖和写资源互斥。
模型调用使用通用 `background_agent` 分类，无管理专属模型并发上限，也不受 background.io_concurrency 的单通道限制；
仍共享应用已有的总模型并发、前台预留和用量记录；用户显式委派的 agent 工作不消耗消息／IO 整理任务的滚动维护配额。
静音／稍后、watch 订阅和队列暂停由管理工具保存到 SQLite，不修改私人 TOML；静音不停止捕获。

管理层累计额度字段 `manager_max_calls`／`manager_max_tokens`、`goal_max_calls`／`goal_max_tokens`、
`execution_max_calls`／`execution_max_tokens` 默认均不设限，可按需要显式配置。
以前已经冻结的额度和累计用量会保留，重启、恢复、任务替换不会清账或扩大原额度。
测试的真实模型调用另有独立评测预算，不改变产品配置。无安全检查点的中断仍需人工检查，不能重放未知副作用。

后端内置 mock 和 OpenAI-compatible 两类 provider。下面是需要填写的最小片段，
请修改现有 `[llm]`，不要重复添加同名 TOML 表：

```toml
[llm]
provider = "openai_compatible"
base_url = "https://your-provider.example/v1"
api_key_env = "LKA_MODEL_API_KEY"
model = "your-model-id"
```

`[llm].max_attempts` 控制普通 LLM 传输调用的尝试次数，包含首次调用；设置为 1 可关闭该层重试。
它不是整轮调用上限：工具审查、后续决策、控制生成的一次有界恢复和不完整回答恢复仍是独立请求。
本地预算或资源拒绝会保留明确错误类别并立即停止当前轮，不通过 repair／回答降级继续消耗调用；
正常 provider 超时与格式恢复仍遵守原有边界。评测等需要整批调用限额时应使用持久账本。

`base_url` 与 `model` 替换为服务商实际值。在本地 `.env` 中为 `LKA_MODEL_API_KEY`
设置密钥；上述片段不含真实凭据。也可以配置多个命名客户端及精确模型的容量、
输出预留和 tokenizer 路径，字段见模板。

模型选择顺序为：当前请求覆盖 → 会话选择 → 后端默认。
改变默认模型不代表现有会话也会强制切换。流式输出、JSON、工具调用及推理控制
需要与所用服务商实际能力匹配。

桌面设置的“高级设置”支持分别选择推理模型与最终回答模型。决策、工具处理与内部检查
使用推理模型，最后生成用户回答时使用最终回答模型；默认“跟随推理模型”保持单模型行为。
普通任务保存到共享 UI 默认设置，管理会话单独保存，设置变更用于后续新运行。
运行开始后冻结选择，恢复与排队执行沿用冻结值。最终回答使用所选模型自己的容量和能力，
不会把推理模型的原生推理参数直接传给另一模型。接口字段见 [API 说明](api_contract.md)。
输入图片或音频时，推理与回答模型分别检查模态能力；兼容选择保持不变，不兼容时依次尝试
配置的音频默认模型（含音频时）、全局默认模型、`fallback_client` 的默认模型。
兜底模型须同时支持本轮全部输入模态，并配置自身的上下文容量；没有合适默认模型时仍返回明确错误。
兜底只作用于本轮，不修改会话／UI 偏好；排队消息按最终合并的附件选择，首次执行时冻结实际模型。

`[agent].answer_timeout_seconds` 默认 60 秒（大于 0、不超过 600），限制最终回答请求的网络等待，
服务商配置的更短超时和运行剩余期限仍然优先；它不是整轮任务或持续出字流的总时限。
`answer_timeout_fallback_enabled=true` 默认开启回答超时切换：尚未输出内容时，优先尝试本轮推理模型，
再尝试上述已配置的兼容默认模型，排除刚失败的精确客户端／模型组合。
候选模型必须有自己的容量并支持全部输入模态；切换后重新计算提示预算，最多追加一次超时尝试。
该机制只用于用户可见主运行的最终回答，不改变会话偏好，不重做工具；取消、剩余额度和
`llm.max_attempts=1` 仍限制后续调用。无可用候选或关闭切换时保留原来的一次同模型重试。

同一端点可混用 Chat Completions 与 Responses 协议。在 `[llm]` 或对应命名客户端
设置 `api_protocol = "responses"` 可切换整个客户端；默认仍为 `chat_completions`。
只切换指定模型时，设置 `model_api_protocol_overrides = { "模型ID" = "responses" }`，
并将型号加入 `available_models`，为其配置独立的 `model_overrides` 容量。
Responses 支持文字、JSON、图片及函数调用的请求／解析与流式输出，用量进入现有审计；
不支持音频输入，不自动在协议间重试。请求使用 `store = false`，不依赖服务商保存历史响应。

图片输入需要在对应 `[llm]` 或命名客户端配置 `supports_vision = true`。
同一客户端有不同能力的模型时，使用 `vision_model_overrides = { "模型ID" = true }`
指定精确模型；未验证的客户端默认不支持视觉。模型名称和官方网站的能力说明不能证明
第三方中转端点也支持图片输入。`vision.inspect_image` 会向选定模型发送本地图片，
经过工具审查；可以显式指定视觉客户端和模型，否则沿用当前运行的模型选择。

## 音频文件输入

WAV、MP3 和 M4A 使用会话附件入口。当前模型不支持音频时，优先临时使用配置的音频模型，
不修改会话或默认模型；已支持音频的选择保持不变。普通文字和文档仍遵循原选择顺序。
在现有 `[llm]` 表内设置：

```toml
audio_model = "gemini-3.8-flash"
# 音频与日常模型共用端点和密钥时省略 audio_client。
# audio_client = "your-audio-client"
audio_model_overrides = { "gemini-3.8-flash" = true }

# 保守的应用容量上限；需与端点实际能力相容。
[llm.model_overrides."gemini-3.8-flash"]
context_window_tokens = 131072
output_reserve_tokens = 8192
```

命名客户端的 `audio_model_overrides` 必须放在对应客户端表内；`audio_model` 和
`audio_client` 放在 `[llm]`。精确模型能力默认关闭，也可以为仅含音频模型的客户端
设置 `supports_audio = true`。模型 ID 来自配置，不因名称自动认定支持。
与其他模型一样，音频模型必须配置自己的 `context_window_tokens`，不可沿用默认
文本模型的容量。命名客户端的容量设置放在对应 `model_overrides` 表。
接入使用 OpenAI-compatible `input_audio` 请求，服务商端点必须实际支持；
[Google 官方兼容接口](https://ai.google.dev/gemini-api/docs/openai#audio-understanding)
说明了这一请求格式。音频回复直接进入当前会话及 SSE，模型来源和用量沿用运行审计。

M4A 在本地由锁定依赖 `imageio-ffmpeg` 提供的 FFmpeg 转换；小文件使用 PCM WAV，
大音频使用完整的 128 kbps MP3 输入，转换不联网、原文件保留，不截取前六分钟。
当前只做文件输入及文字输出，不进行录音、语音播放或精确声学测量。
本步使用有界内联传输，单个原文件最多 256 MiB；实际模型输入和混合输入限制见
[会话附件合同](api_contract.md#会话文件附件)。大文件不自动使用供应商 Files API。

## 推理深度与模型映射

用户请求通过 `llm.reasoning_level` 选择 `off`、`low`、`high`、`max`，新运行默认 `low`。
业务决策与回答跟随用户选择，纯路由尝试关闭推理，工具结果检查、上下文整理、记忆和安全审查
使用内部低档策略。内部关闭不受支持时使用声明的低档；未声明能力的调用明确记录为未控制。
这些策略不改变审批权限，也不增加用于选择档位的模型调用。

在 `[llm]` 或对应命名客户端声明能力。已验证支持 DeepSeek Chat 协议的端点可设置
`thinking_control = "deepseek"`：关闭映射为 `thinking.type = "disabled"`，其他三档发送
`thinking.type = "enabled"` 与 `reasoning_effort = "low" / "high" / "max"`。
声明仅适用于该客户端默认模型；其他模型需要精确覆盖，不能根据模型名字猜测支持情况。
通用兼容端点使用 `supports_reasoning_effort`、`supports_reasoning_off` 和 `reasoning_effort_map`：

```toml
# 放在对应客户端表内，仅填写该端点实际支持的值。
supports_reasoning_effort = true
reasoning_effort_map = { off = "none", low = "low", high = "high", max = "xhigh" }

# 混用模型时，对每个额外模型单独声明。
[llm.model_reasoning_overrides."another-model-id"]
supports_off = false
effort_map = { low = "low", high = "high", max = "high" }
```

命名客户端使用 `[llm.clients.model_reasoning_overrides."模型ID"]`。
`max = "high"` 表示最高档别名，审计实际档位为 `high`；不支持的用户显式档位返回错误。
旧 `supports_reasoning_effort = true` 且未配置映射时保留 low/high，max 明确映射为 high。
运行冻结档位及首次使用的能力映射；旧检查点未保存档位时保留旧行为。
`[agent].decision_thinking_enabled` 仅用于没有新档位的旧运行，不覆盖新请求的选择。
不完整输出恢复可有界调整推理策略，实际参数及回退原因保留在调用审计中。

## Agent Python 环境

Agent 使用独立于后端 `.venv` 的 uv 虚拟环境，默认位于 `<data_dir>/python`。
首次显式准备环境：

```bash
.venv/bin/python scripts/manage_agent_python.py prepare
.venv/bin/python scripts/manage_agent_python.py status
```

准备命令需要 uv，可下载 Python 和依赖；工具调用和后端启动不会自动安装。
默认选择 Python 3.12，环境记录实际解释器版本，依赖解析结果保存在带哈希的
`requirements.lock` 中，后续准备复用该锁文件。预装 NumPy、pandas、Matplotlib、
openpyxl 和 Pillow。`--root` 可用于准备隔离验证环境；运行时默认使用配置的数据目录。
环境与锁文件留在本地，不能作为后端运行依赖或同步到另一台电脑的虚拟环境。

`bash.python_environment` 返回实际解释器、Python／包版本以及当前工作目录。
`bash.run` 选择 `python_environment = "default"` 后注入 `LKA_PYTHON`，并过滤继承的
服务凭据。虚拟环境隔离依赖，不是 OS 文件或网络沙箱；脚本仍经过现有执行审查。
执行前检查实际虚拟环境、Python 版本与锁文件中的依赖版本；准备失败或仍在进行时
环境不可用。准备命令使用进程锁，避免并发安装互相覆盖，并支持失败后重试。

## 邮件

- Outlook 使用 Microsoft Graph 设备授权；在 `[mail.outlook]` 配置启用状态、client ID
  和授权文件位置。按 `/mail/outlook/auth/start` 返回的地址和验证码登录，再通过
  `/mail/outlook/auth/complete` 完成授权。详细字段见 [API 说明](api_contract.md)。
- IMAP 为可选接入，配置主机、账号及 `password_env`；应用密码放环境变量，不写入仓库。
- 启动、后台轮询及按需同步只负责更新本地邮件，阅读和整理仍在普通 Agent 会话中进行。
- 邮件工具会告知模型当前自动同步开关和轮询间隔；启用后通常直接读取本地邮件，无需先调用同步。显式同步／立即刷新请求仍可调用 `mail.sync`，此提示不保证最近一次同步成功。
- active 助理主管会话可直接调用 `mail.sync` 刷新邮件；同步仍是写入本地库的操作，沿用 skip／llm／manual 安全审查。paused 主管不开放同步，已有运行继续沿用冻结权限，新运行使用更新后的工具范围。
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
  默认策略下，`llm` 未批准、审查输出无效或供应商审查失败时，保留模型审查记录并转入人工审批；人工批准后才执行原调用。
  上述管理自主审查策略改为拒绝调用并向 Agent 反馈，不转人工。配额耗尽、取消和权限失效仍执行原有停止规则。
  单次请求可提高审查级别，不能降低本地配置要求。
  安全审批不额外设置生成 token 上限，输出预留只用于上下文和并发预算计量；
  模型自身容量、超时、取消与显式子任务额度仍生效。若模型输出仍被截断，
  工具保持不执行，记录为审批未完成，而非模型明确拒绝。

多 Agent 规划需要 `orchestrator = "langgraph"` 和
`multi_agent_planning_enabled = true`。外部 Codex 专家需单独安装、认证并显式开启；
Windows 的 `codex_binary_path` 指向原生 `.exe`，不能使用 `.cmd` / `.bat` 启动器。

基础 TOML 配置重启后加载。通过 `/background/config` 保存的配置具有 active / desired
两种视图，管理端应展示是否待重启；暂停、恢复和取消是独立的即时控制操作。

后台上下文压缩不下发 `max_tokens` 输出上限；`generation_output_tokens` /
`recovery_output_tokens` 仍用于其他记忆生成任务，压缩中只作为缺失用量时的预算估算。
压缩使用 `[memory].compaction_timeout_seconds`（默认 180 秒）作为每次生成的兜底期限，
不再套用通用后台 30 秒网络限制；所选模型客户端自身的网络超时仍生效。
服务商默认输出限制及模型窗口仍然存在；摘要完成后仍按现有摘要预算裁剪，
不把不限生成长度等同于无限上下文。异常消耗熔断、取消与发布租约检查保持有效。

原子会话上下文通过 `[memory].atomic_context_mode` 渐进启用，默认 `off`。
`shadow` 在后台保存有来源的原子记录，不改变前台摘要；`active` 读取这些记录的可读投影，
并保留处理水位后的原始消息。不需要每轮前台整理模型调用。旧摘要和完整历史均保留，
自动更新始终把最新一轮完整问答留作原文，避免最新纠正只剩派生表达。
改回 `off` 可回退；这些配置也通过 `/background/config` 和 schema 管理，重启生效。
`atomic_context_input_tokens=24000` 限制单次后台输入，`atomic_context_projection_tokens=4096`
限制前台原子视图，`atomic_context_debounce_seconds=30` 合并连续触发。
没有足够空间装载完整新增消息或原子候选时，任务失败而不推进水位，不截断来源后宣称处理完成。
原子内容是模型整理的语义信息，不要求照抄原文；可引用多个消息片段。
后台输入提供服务端生成的 `passage_id`，模型只选择 ID；系统解析并保存对应原文、
字符范围、时间、角色和哈希。片段 ID 绑定会话及消息版本，不授权读取其他会话。
旧逐字引用记录继续兼容；新生成协议不让模型重新抄写引文。
结构不符合 schema 或选择了未提供的片段 ID 时，最多追加一次模型修复并重新校验整批结果；
修复也受输入容量和后台资源控制约束。来源角色、权限和版本错误仍直接拒绝。
来源合法不代表原子结论正确，仍需语义质量评测。失败保留旧视图和处理水位。
涉及多来源事件、用户陈述混入 agent 来源或带数值的进度提案时，后台追加一次小范围来源支持复核，
仅传入提案、相关旧记录及已引用片段，不重复发送全部历史。复核结果仍须通过同样的来源、角色和版本检查；
这会增加实际模型用量，简单单来源任务不额外调用，前台不等待复核。
前台原始尾部放不下时退回已有摘要链路，context-status 提供诊断。原子记录保持会话范围，
不自动升格为全局记忆或修改 AGENTS.md；已有长期记忆整理继续独立运行。
`context.read` 返回可回访的 `source_fragments`；`context.source` 可以按 `passage_id` 直接分页读取原片段，
也兼容按 `seq` 读取旧消息。分页 offset 相对所选片段或整条消息，修改/撤回来源后旧 ID 不可继续使用。

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

本地文档转换的 MarkItDown 版本及 Office 依赖由项目锁文件管理，安装／更新使用
`uv sync --locked`。文件处理策略在 `app/domains/file_processing.py` 中集中维护；不需要
额外 API key，默认关闭转换器插件、图片 OCR、音频转录和 Azure 转换。索引权限和隐私
过滤沿用知识库规则，语义向量是否同步仍由 `embedding.auto_index_on_import` 控制。
工作区知识索引是显式操作，不因安装转换器而自动扫描已有资料。单次最多 500 个候选文件、
单文件 2,000,000 bytes、总读取 20,000,000 bytes；PDF 整份索引最多 200 页，提取文本
最多 2,000,000 字符，超限会报错，不静默截断入库。直接 PDF 阅读仍按工具的分页限额进行。

Windows TOML 路径使用 `"D:/Work/project"` 或单引号的 `'D:\Work\project'`；
Windows 后端应收到实际可访问的 Windows 路径，而不是 `/mnt/d/...`。
WSL 后端的配置则使用 Linux 路径；读取 Windows 文件时使用实际挂载路径，
工作区、模型/tokenizer 缓存、授权文件及媒体目录都要分别核对，不能照搬 Windows 配置。
工作区根目录可通过 `LKA_WORKSPACE_ROOTS` 配置，多个目录以分号分隔。

数据本地保存不代表模型请求完全离线。远程模型会收到所选对话及资料片段，
搜索服务会收到查询；完整日志也可能包含这些内容。将配置、授权和运行数据按个人资料保管。
