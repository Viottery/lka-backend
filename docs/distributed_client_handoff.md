# 多终端客户端接口交付

面向桌宠、Java 原生桥、浏览器工作台和手机客户端的接入说明。
本交付定义现有后端接口与客户端验收条件，前端改造由客户端项目实现。
字段与接口细节见 [API 合同](api_contract.md#账号设备与共享同步)，
服务配置见 [配置说明](configuration.md#设备直连与远程任务)。

## 本次范围与路由

当前需要连接其他电脑的会话和工作区：请求发送到那台电脑，由那台电脑处理。
文件仍在所属电脑，客户端可以浏览、上传和下载；不要求在当前电脑挂载远程文件系统。
原生挂载和跨设备累计预算属于长期计划，不是本次客户端接入的前置条件。

| 操作 | 目标地址 | 使用身份 |
| --- | --- | --- |
| 登录、账号管理、设备目录和连接授权 | 云端 origin | 云端账号 |
| 助理主会话、助理派发任务、远程结果与审批 | 云端 origin | 云端账号 |
| 普通会话、运行、文件、工作区绑定 | 当前选中节点的 origin | 该节点的连接会话 |
| 共享记忆镜像与离线编辑 | 当前选中节点的 origin | 云端授权后兑换的节点连接会话 |
| 节点心跳、同步与执行领取 | 节点后台连接云端 | 设备 token；客户端不持有 |

`node_id` 标识对外后端入口；`environment_id` 标识它批准的执行环境；
`workspace_id` 是该节点本地配置映射的目录 ID。它们均不能用电脑显示名或目录字符串替代。
同一物理电脑的不同后端入口须分别管理 origin、身份和本地数据库。

客户端的连接上下文至少包含 `{role, origin, node_id, environment_id?, workspace_id?, auth_kind}`。
会话索引、草稿、文件列表、运行、事件游标和待审批项按 origin/node_id 隔离。
切换目标时取消旧界面的订阅，再加载新目标的数据；不能把旧 session_id 自动发送到新节点。

### LKA Desktop 的实现方式

- 浏览器与手机打开目标服务自身 origin 下的 `/desktop-pet/connect.html`
  （后端 `[client].workbench_dir` 或路径转发，见[配置说明](configuration.md#同源托管工作台)）。
  云端入口登录后列出设备；进入设备时把一次性 grant 与非秘密设备目录放在 fragment 中跳转。
- 节点页面只保留普通会话、`/local/workspaces` 文件、共享记忆镜像与连接管理，
  审批读取当前运行的 `/agent/runs/{id}/safety-reviews`。
- 云端离线时，入口页面由 Service Worker 缓存外壳，列出此浏览器之前进入过的设备；
  每台设备用自己的 cookie 校验，失效时提示回云端重新进入，不改连其他电脑。
- 桌宠与本机工作台经前端服务的 loopback `/cloud` 网关使用云端，前端服务以原生客户端登录，
  账号 token 由本机服务持有，默认保存在进程内存；Windows 可显式配置独立的 DPAPI
  加密会话文件以便重启恢复，不保存密码或向页面传递 token。本机后端为 replica 时，真理、待办、项目与记忆走 `/cloud`，
  普通会话仍走本机 `/workbench`。Java 窗口不提供设备跳转。

## 浏览器与原生认证

### 浏览器

优先直接打开目标服务的 HTTPS `/client`，或使用同源反向代理。
独立桌面前端若从 localhost 页面请求远程服务，会涉及跨站 cookie 限制；
仅添加 `credentials: include` 和 CORS 不能绕过 SameSite=Strict。
应选择同源部署或由原生桥持有原生凭据并代理请求。同站的不同子域仍涉及 CORS，
副本只启用节点连接时也不能假定已经开启跨源凭据响应。

云端 `POST /auth/login` 请求 `{username,password}`，返回
`{account,session_id,csrf_token,expires_at}` 并设置 HttpOnly/Secure/Strict cookie。
浏览器请求须携带配置允许的精确 Origin；写请求还须 `X-CSRF-Token`。
密码不保存到浏览器存储。`GET /auth/me` 返回账号和会话安全字段；有效的浏览器 cookie
会话可同时恢复 `csrf_token`，新标签页不必重新登录。CSRF 可按 origin 保存在当前标签页
sessionStorage；旧版会话未采用派生规则时仍需重新登录。原生账号令牌请求不会返回此字段，
也不能把 csrf_hash 当作请求 token。节点的 `/connections/me` 同样支持恢复 CSRF。
`POST /auth/logout` 返回 204；`POST /auth/password` 会撤销该账号的云端登录会话。

### Java 或其他原生客户端

云端需明确开启 `auth.allow_bearer_sessions`。
登录时携带 `X-LKA-Client: native`，不发送 Origin；响应额外返回 token。
后续请求优先使用 `X-LKA-Account-Token: <token>` 和 `X-LKA-Client: native`。
`Authorization` 留给邮件、消息等独立领域凭据，账号身份不会替代这些权限。

节点原生连接使用另一个凭据头：`X-LKA-Connection-Token: <token>`，
同时发送 `X-LKA-Client: native`，不发送 Origin，且节点须允许原生连接。
云端 token 与各节点 token 分开保存，不放入页面 URL、Java 进程参数或日志。
不要把应用 token、一次性 grant 和后台设备 token 混用。

## 从云端进入另一台电脑

1. 登录云端，`GET /devices?limit=100&offset=0` 获取 `{items:[...]}`。
   显示 node_id、名称、平台、已批准系统/工作区及在线状态，不保存私有磁盘路径。
2. 用户选择节点和允许访问的工作区，向云端调用：

   ```http
   POST /devices/desktop-a/connections
   Content-Type: application/json
   X-CSRF-Token: <cloud-csrf>

   {"workspace_ids":["work"]}
   ```

   201 响应包含 `{grant,grant_id,node_id,origin,expires_at,delivery}`。
   `delivery=pending_node_poll` 表示节点尚须通过后台轮询取到授权哈希。
3. 浏览器导航到响应中的已批准 HTTPS Tailscale origin，路径 `/client`，
   将 grant 放入 fragment。页面必须先读取并清除 fragment，再发起其他网络请求。
   当前 `/client` 已实现这一过程。不得把 grant 放入查询参数、Referer 或持久存储。
4. 在目标节点 `POST /connections/redeem`，正文 `{grant}`。
   浏览器提供节点配置允许的 Origin；原生客户端提供上述 native header。
   成功建立独立节点会话，返回 `{session_id,node_id,csrf_token,expires_at,scopes}`；
   浏览器收 cookie，原生响应额外含 token。一次授权只可兑换一次。
5. `GET /connections/me` 验证实际连接的 node_id，读取 workspace_ids、有效期与 CSRF。
   新标签页也可用它恢复 CSRF，不需要重新连接云端。

grant 尚未安装时兑换可能返回 403，当前客户端在有效期内有限重试；
原生客户端也应限制次数与总等待时间。成功后停止兑换，后续操作使用节点会话。
没有节点身份或连接过期时回到连接选择，不静默改用另一台电脑。

节点后台必须已获 `heartbeat`、`sync.read` 和 `connections.poll`，
连接 origin 与本机 workspace_id 映射须由操作者预先配置。
设备的 `online` 不代表每个执行系统在线；助理执行目标另查 execution_online。
直连 grant 绑定节点和工作区，不单独绑定执行 environment_id；`/client/info` 也不返回环境 ID。
系统选择与核验属于助理远程执行流程，不能把直连页面显示的平台推断为执行系统承诺。

## 普通会话与工作区

以下请求都发到已选择的节点；节点会过滤助理、助理派生和远程执行会话。
助理相关会话不能通过节点普通接口读取或控制。

| 接口 | 最小请求与响应 |
| --- | --- |
| `GET /sessions?limit=100&offset=0` | `{sessions:[...]}`；保留会话 ID 与来源信息 |
| `POST /sessions` | `{title:"新的普通会话"}`，返回 `{session,messages}` |
| `GET /sessions/{session_id}` | `{session,messages}`；workspace 为后端可访问的本机绑定 |
| `PATCH /sessions/{session_id}` | `{title:"新标题"}`，返回会话详情 |
| `DELETE /sessions/{session_id}` | 软删除，204；并发保护参数见 API 合同 |
| `GET /sessions/deleted?limit=100&offset=0` | 当前连接可见的已删除普通会话 |
| `POST /sessions/{session_id}/restore` | 恢复批准范围内的普通会话，返回详情 |
| `GET /sessions/{session_id}/runs` | 已受理运行历史，用于断流/丢响应后核对 |
| `GET /local/workspaces` | `{items:[{workspace_id}]}`，只含本连接批准的映射 |
| `GET /local/workspaces/{id}/status` | `{kind,available,read_only?}`，不返回私有路径 |
| `POST /sessions/{session_id}/workspace-id` | `{workspace_id:"work"}`，返回绑定的 workspace 对象 |
| `GET /local/workspaces/{id}/entries?path=` | `{workspace_id,path,items}`，最多 200 项 |
| `GET /local/workspaces/{id}/file?path=relative` | 文件下载，最多 32 MiB |
| `PUT /local/workspaces/{id}/file?path=relative` | 原始字节新建文件，最多 32 MiB；同名 409 |
| `POST /local/workspaces/{id}/directories` | `{path:"relative"}` 新建目录 |

路径一律为所选工作区内的相对路径。URL 的 workspace_id 和 path 分别编码，
不能把 Windows 路径发送给 Linux 节点，也不能把前端电脑的本地路径当作目标节点目录。
当前直接文件接口支持新建，不提供覆盖、重命名、删除文件或递归目录传输。
需要修改文件时，使用该节点普通 Agent 及其审查流程。

节点普通对话可调用 `POST /agent/turn/stream`：

```json
{"session_id":"selected-node-session","user_input":"查看工作区的项目结构","resumable":true,"llm":{"response_mode":"stream"}}
```

使用 Fetch 读取 SSE，使 POST 请求能带 CSRF。只将 `payload.display_target=assistant_answer`
的输出增量显示为回答；收到 final_answer 时用最终完整回答替换增量，不把内部推理显示给用户。
`resumable:true` 显式使用检查点运行，后续暂停、恢复和确认续作需要该能力；
不能假定默认 legacy 运行后补一个暂停请求就会获得检查点。保存返回的 run_id；
断流后查询运行状态和消息历史，不把同一用户输入自动重新提交。
普通 `/agent/turn[/stream]` 没有请求幂等键；首次响应丢失时先查询该会话 runs 和消息历史，
仍不确定是否受理则提示核对，不可盲目重发。`/sessions/{id}/inputs` 仅用于默认助理 FIFO，
不能作为节点普通对话的幂等发送接口。
仅取消流的读取不会取消后台运行；取消任务使用 `POST /agent/runs/{run_id}/cancel`。
已有运行可通过 `GET /agent/runs/{run_id}/stream?after_sequence=N` 订阅后续事件，
按 sequence 去重，终态再核对历史与结果。
后端不读取 Last-Event-ID header，客户端必须显式传 after_sequence；心跳可能重复最后的序号。
运行状态、检查点暂停/恢复、用户问答及本地审批沿用 [Agent 接口](api_contract.md)。
旧宽权限运行不能借新的窄范围连接直接恢复，应明确开始新一轮。

## 云端助理及远程任务

云端 `POST /managers/default`（正文 `{}`）获取默认助理的 session/controller。
通过助理输入队列提交，client_message_id 必须在网络重试间保持不变：

```http
POST /sessions/<manager-session-id>/inputs
Content-Type: application/json

{"client_message_id":"stable-client-message-id","user_input":"在指定终端检查项目","attachment_ids":[],"llm":{"response_mode":"stream"}}
```

202 响应中的 `input.run_id` 可能尚未分配。通过同路径 GET 查询队列，按稳定消息 ID 找到
已分配的 run_id，再连接云端 `/agent/runs/{run_id}/stream`。
助理排队/暂停不是消息丢失，不能因此换一个消息 ID 自动重发。

`GET /remote-execution/targets` 提供批准的设备/系统/工作区与执行在线状态。
实际派发由注册工具 `management.start_task` 执行：execution_target 省略为云端本机，
`{}` 为自动选择，或指定 `{device_id,environment_id,workspace_id,capabilities}`。
设备偏好 `/devices/selection` 的保存/解析不等于任务已经锁定；
最终派发约束以受理的 execution_target 和任务回执为准。
当前没有把浏览器任意工具参数直接执行的通用远程 RPC。

远程任务索引的 `session_id`、`title`、`node_id`、`environment_id` 与 `workspace_id` 用于按实际
执行设备展示“真理派发”。云端保存管理记录不代表在云端执行；节点页面从云端读取该索引，
不向节点 `/sessions` 请求远程执行会话。普通会话响应的 `metadata.remote_execution` 也提供目标引用。
`approval` 返回 `requested_mode`、`effective_mode` 和 `source`（request、node_policy、host_execution、legacy）。
节点领取前或旧节点尚未回报时 `effective_mode` 为 null，界面应显示待确认策略。
节点 `remote_execution.review_policy=inherit` 才允许沿用派发时模式；默认 manual。
审批策略变化不追溯修改旧运行，原生主机执行仍要求人工审批。

| 云端接口 | 行为 |
| --- | --- |
| `GET /remote-execution/tasks?owner_session_id=&limit=100&offset=0` | 助理的任务摘要，按 next_offset 翻页 |
| `GET /remote-execution/tasks/{id}?limit=20&offset=0` | 任务状态、分页回报及 next_offset |
| `POST /remote-execution/tasks/{id}/cancel` | 取消并使后续执行检查失效 |
| `POST /remote-execution/tasks/{id}/pause`、`/resume` | `{command_id}`，稳定 ID，原检查点控制 |
| `POST /remote-execution/tasks/{id}/answer` | `{command_id,question_id,answer}` |
| `POST /remote-execution/tasks/{id}/instruction` | `{command_id,expected_run_id,purpose}`，已知终态后原会话续作 |
| `POST /remote-execution/tasks/{id}/reviews/{review_id}` | `{decision:"approve"或"reject",reason?}` |

只展示最新回报中仍待处理的 question/reviews；未读完分页时不要把历史审批当成当前请求。
`resuming` 是恢复已入队、等待节点报告；`unknown` 是效果不明，不能自动重放或换节点重做。
补充指令仍绑定原设备、系统和工作区，不能通过修改 UI 选择偷偷改变既有任务目标。

## 共享记忆与离线状态

`GET /local/cache` 读取本地镜像水位、新鲜度及 stale 标志，不会同步调用云端。
普通会话允许读取旧镜像；空镜像应显示未同步，不能伪装成已获最新记忆。
手工共享记忆的本地接口见 [共享记忆接口](api_contract.md#共享记忆正式编辑与离线队列)。
mutation_id 在重试间保持不变；queued/conflict/rejected 不等于已生效，accepted 后仍须等镜像拉取。
冲突保留 proposal/current，用户重新核对后用新 ID 提交，不自动覆盖或恢复删除内容。
独立节点本机账号不自动取得云端用户的共享编辑身份。

自动学习记忆按原节点已有状态同步，由共享数据层保留来源并执行跨节点精确去重；
矛盾内容保留待核对，不新增每条记忆的人工确认步骤。客户端不扫描或合并 SQLite，
也不通过复制会话数据库实现记忆共享。
`GET /local/cache` 的 memory_aggregation 显示自动发布开关、接管状态与队列计数；
云端 `GET /sync/memory-records/conflicts` 提供自动同步待核对记录。
`/node/sync/memory-records` 是后台设备协议，浏览器/Java 用户客户端不调用它。
详情见 [自动记忆数据库同步合同](api_contract.md#自动记忆数据库同步与去重)。

## 断线、撤销和错误处理

| 情况 | 客户端行为 |
| --- | --- |
| 云端暂不可达、节点可达 | 保持节点连接；普通会话/文件/旧记忆继续由该节点处理 |
| 节点不可达 | 显示当前目标离线，保留草稿；不静默改发云端或其他电脑 |
| 401 | 当前 origin 的身份过期或撤销，只清理该身份并要求重新连接 |
| 403 | 检查范围、Origin/CSRF、native 配置或授权是否失效；不关闭后端校验 |
| 404 | 功能关闭/资源不可用，或该连接不可见；隐藏不可用功能 |
| 409 | 并发版本、同名文件、当前运行状态或命令冲突；刷新后明确处理 |
| 422 | 输入不符合合同，显示字段/操作错误，不自动改写后重发 |
| 429 | 限流，等待后允许重试；稳定操作 ID 不变 |
| 请求发送后断网 | 先按稳定 ID 或运行状态核对结果，避免重复写入 |

`GET /connections/sessions` 列出本人在当前节点的连接；
`DELETE /connections/sessions/{session_id}` 本地撤销，`POST /connections/logout` 退出。
云端 `POST /devices/{node_id}/connections/revoke` 撤销本人既有节点授权。
改密、禁用账号和云端连接撤销在节点下次成功同步授权代次后生效；
断网不清除已有本地连接，离线节点不会即时收到撤销通知。

## 客户端验收清单

- 云端与两个节点使用不同临时数据目录；同名会话、草稿、审批和文件列表不会混用。
- 浏览器和原生登录均覆盖；密码、token 和 grant 不进入日志、URL 查询或浏览器持久存储。
- 从云端进入 A、退出后进入 B，实际请求 origin、node_id 和工作区 ID 一致。
- 直连可以新建/继续普通会话、绑定工作区、浏览/上传/下载文件；助理派生会话拒绝直连。
- 关闭云端后，已有 A 连接仍能新建普通会话并读取旧缓存；新云端授权和助理派发显示不可用。
- SSE 断开重连不重复回答；写请求丢响应时按稳定 ID 查询，不盲目重发。
- 云端审批、暂停/恢复、问答和补充指令不会绕到节点普通接口。
- 390px 手机布局、键盘遮挡、长任务状态和文件错误可用；Java 桥独立校验实际连接目标。
- 改密/撤销后重连云端会使旧节点连接失效，网络故障本身不误退出。

后端已有合成多节点 API 和浏览器回归；部署时仍需验证真实 HTTPS、代理头、
tailnet 连通与客户端凭据存储，不将这些本地测试视为真实多机上线记录。
