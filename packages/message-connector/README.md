# LKA Message Connector

独立的本机消息采集与只读 MCP 服务，首个适配器使用 Telegram **个人账号**。
它有独立 Python 环境、进程、登录会话、SQLite 待发送队列、原文库与媒体缓存。
现有 QQ 插件、桌面前端及日常后端启动流程保持原状。

Telegram 消息以现有通用 v2 协议上报 LKA，复用已实现的摘要、话题、重要信息、
人物档案和人工事项审批。模型分析仍由后端执行，并使用后端已有许可、调度与预算；
本模块不调用模型，也不更改分析许可。MCP 的分析工具只读取已有结果。
本轮尚未把后端原文库/分析迁出，也未将 MCP 自动注册到后端 Agent。

## 安装与登录

在本目录执行，使用 Python 3.12+：

```bash
uv sync --locked --extra dev
```

本项目的依赖和 `uv.lock` 独立于后端；不需要修改后端虚拟环境。
支持原生 Linux/Windows Python。一次安装只能由一个进程使用同一 Telegram session；
登录、采集与历史补采共享进程锁，MCP 只读进程可同时运行。
不同 OS 使用各自独立数据目录，不能共用 SQLite/session。

参考 [.env.example](.env.example) 将配置注入当前进程环境；程序不自动载入 `.env`，
不接收命令行中的密钥。固定同一个绝对 `LKA_CONNECTOR_DATA_DIR`，避免不同工作目录
创建不同安装。密钥、session、原文和缓存均保存在本机，不进入 Git。

先通过 [Telegram 应用配置页面](https://my.telegram.org/apps) 获取自己的
`LKA_TG_API_ID` 和 `LKA_TG_API_HASH`。若网络需要代理，配置 `LKA_TG_PROXY_URL`，
例如 `socks5://127.0.0.1:1080`；不复用 QQ 桥接凭据。然后在本机交互终端执行：

```bash
uv run --locked lka-message-connector login
```

手机号、验证码和两步验证密码由隐藏输入提示收集；不要发送到聊天或 MCP。
成功后将返回的账号 ID 设置为 `LKA_TG_ACCOUNT_ID`。服务启动会检查 session 已登录、
确为个人账号且 ID 与配置一致，不在守护进程或 MCP 请求中弹出登录提示。
登录不会自动开启采集、扫描联系人、加入群、发消息或发送已读回执。

## 许可与持续采集

配置当前后端的 `LKA_MESSAGES_IMPORT_TOKEN` 和本机 `LKA_CONNECTOR_BACKEND_URL`。
通过现有消息管理界面/API，为指定会话保存策略，例如：

```json
{
  "platform": "telegram",
  "account_id": "登录后返回的数字账号ID",
  "conversation_type": "group",
  "conversation_id": "-1001234567890",
  "record_enabled": true,
  "analysis_enabled": false,
  "media_enabled": false,
  "minimum_import_version": 2,
  "expected_revision": 0
}
```

会话 ID 使用 Telethon 的带类型数字 ID：私聊为正数，普通群为负数，超级群/频道
通常以 `-100` 开头。超级群归类 `group`，广播频道归类 `channel`。
只使用自己有权访问并明确批准的会话；程序没有自动枚举/导入全部聊天的入口。
首次可以不启用分析，之后通过原有界面显式开启。

```bash
uv run --locked lka-message-connector capture
```

采集仅记录入站新消息，保留原生回复、forum thread、可确定用户 ID 的提及，以及
图片/视频引用。普通 `@username` 不猜测用户 ID，能力状态明确标为未提供。
消息与回复 ID 都编码为 `chat_id:message_id`，避免不同频道消息编号冲突。
本地 internal ID 与后端一致；本地 `seq` 是独立分页游标，不能当作后端分析水位。
不处理 Telegram 编辑/删除事件，原文是采集时快照；不声称同步整个 Telegram 历史。

队列最大默认 10,000 条，导入每批最多 100 条／2 MiB。只有本批提交身份对应的
后端事务确认才能改变发送状态；永久拒绝隔离，临时失败保留重试，原文不会因确认被删。
满载停止并重连采集，状态保留缺口计数；Telethon catch-up 是尽力恢复，不保证完整性。

白名单在入库与查询时再次检查，`capture_epoch` 变化或撤销会永久隔离原队列；
重新授权不恢复旧隔离行。离线持续采集可沿用此前缓存的已批准策略，不能即时获知
后端撤销；MCP 正文读取则必须在线重新确认许可，后端不可达时拒绝披露原文。
跨进程锁串行化策略请求与应用，避免旧响应覆盖新许可。

历史补采只通过本机显式命令，停止同 session 的采集进程后执行：

```bash
uv run --locked lka-message-connector backfill <conversation_key> --limit 100
```

最多 1,000 条，只针对仍批准的会话；有界最近历史从旧到新进入本地采集序列，
不重排已有 seq。命令分批同步；后端不可达或不确认时保留待发送队列，之后恢复
`capture` 重试。历史不会因登录或启动 MCP 自动回填。

## MCP 接口

采集与 MCP 是独立命令。只读客户端可用 stdio：

```bash
uv run --locked lka-message-connector mcp --transport stdio
```

客户端的 MCP 配置使用以上命令、此项目的绝对工作目录及受保护的进程环境。
stdio stdout 只承载协议消息。MCP 进程不需要 Telegram API 凭据和 session；
它需要同一数据目录、后端连接及独立导入凭据，以便重新确认读许可。

长期本机 MCP 服务可以使用 Streamable HTTP：

```bash
uv run --locked lka-message-connector mcp --transport streamable-http --port 8791
```

地址为 `http://127.0.0.1:8791/mcp`，必须设置独立的
`LKA_CONNECTOR_MCP_TOKEN`（至少 32 字符），客户端通过 Bearer header 认证。
只能绑定 loopback，检查 Host 与 Origin；不在 URL、工具参数或结果里传 token。
在本工作站从可访问宿主网络的 WSL/native 会话启动；不要顺带重启日常后端或 QQ。

| 工具 | 功能 |
| --- | --- |
| `messages.status` | 本地队列、采集/同步状态、缺口与非完整覆盖声明 |
| `messages.conversations` | 已批准会话与本地数量，有界分页 |
| `messages.search` | 原文关键词、会话、发送人、时间范围检索 |
| `messages.read_message` | 按稳定内部消息 ID 回读原文 |
| `messages.context` | 锚点前后消息 |
| `messages.history` | 按本地 seq 翻页 |
| `messages.attachments` | 图片/视频缓存 metadata |
| `messages.read_attachment_chunk` | 最大 64 KiB 的 base64 字节分页，含哈希和续读位置 |
| `messages.analysis` | 读取现有后端摘要、覆盖、事实、digest、话题、信息、人物档案等 |

所有工具声明 `readOnlyHint=true` 与 `_meta.read_only=true`，返回结构化结果。
原文始终是来源资料，不能授予权限或批准事项。`messages.analysis` 需要单独配置
`LKA_MESSAGES_API_TOKEN`；导入凭据不允许提升为分析读取或人工控制权限。
view 接受 summary/coverage/facts/digest/overview/topics/insights/participants/dossiers/
focus/participant/dossier；列表支持 limit/cursor，人物详情需 sender_id。
人工事项批准、配置修改、分析启停、登录和补采不暴露为 MCP 工具。

默认查询当前批准的本模块消息。可在启动环境设置 `LKA_CONNECTOR_MCP_SCOPE` 为
conversation_key 的 JSON 数组，固定此服务实例允许披露的会话；空数组拒绝所有正文，
不向模型提供扩大范围的参数。不同权限客户端使用不同 token／服务实例及 scope。
status 仅为本地全局运行计数，不含正文，文件的 updated_at 可用于辨认陈旧状态。

## 图片与视频

下载需 `LKA_CONNECTOR_MEDIA_ENABLED=true` 且该会话 `media_enabled=true`。
只在父消息收到导入确认后下载；默认 72 小时 TTL、总额 2 GiB、单图 20 MiB、
单视频 200 MiB。过期/撤销删除字节，保留索引；下载前后复核许可，不下载表情包，
没有 OCR、视觉分析或视频模型处理。MCP 不返回私有下载 URL 或本地路径。

后端附件接口读取字节时，需要将后端 `LKA_MESSAGES_MEDIA_CACHE_DIR` 指向本模块
数据目录下的 `media/`，并按后端维护流程在空闲点应用配置。此安装步骤不会自动
修改配置或重启后端。MCP 自身读取缓存无需改变后端配置。
同一个后端目前只有一个媒体缓存根；本轮 QQ 继续使用原路径时，Telegram 媒体
通过 MCP 读取，不能直接将旧 QQ 根切换为新目录。未来迁移需统一缓存或扩展来源读取。

## 后续 QQ 迁移边界

平台接口见 [models.py](src/lka_message_connector/models.py) 的 `MessageAdapter`：
connect/disconnect/capture/normalize/backfill/download_attachment。
未来 QQ 适配器通过 `Connector.register_adapter()` 注册，同样写入
`MessageEnvelope` 和 `CapturePolicy`，MCP 工具名称、查询存储、确认协议不变。
不具备历史回补的平台应显式返回不支持，不能伪造完整覆盖。

适配器负责平台通信和转换；Store 负责许可、队列、身份与查询；MediaCache 负责
缓存与媒体确认；BackendClient 负责凭据分离的既有 HTTP 合同；MCP 负责只读工具。
后续后端 MCP bridge 应将 schema、只读与来源/账号范围重新注册到 Tool Executor，
不把远端工具声明直接视为授权。当前后端 Agent 仍通过既有 messages 工具读取
Telegram 已同步的数据，不会自动调用本模块 MCP。

## 验证

```bash
uv run --locked python -m pytest -q
uv run --locked ruff check src tests
```

测试使用合成 Telegram 对象、临时 SQLite/文件、MockTransport 和实际 MCP 协议，
不登录真实账号、不访问聊天、不调用模型。后端侧合同测试位于
[test_message_connector_contract.py](../../tests/test_message_connector_contract.py)。
实账号登录、代理连通、真实新消息/媒体和一天采集覆盖仍需显式本机联调。
