# 消息知识检索与群 metadata：前端对接

本轮只实现后端 HTTP／Agent 接口，不修改 UI、前端代理、NapCat 或线上服务配置。
QQ 和其他平台均是本地已采集消息的来源；没有 OneBot action、发送、联系人查询或群管理接口。

## 身份和来源

会话身份是 `platform + account_id + conversation_type + conversation_id`，后端生成
`conversation_key`。人物身份是 `conversation_key + sender_id`，不能按昵称跨群合并。
显示名称由人工别名、人工显示名、导入缓存群名依次覆盖，未知时显示已有白名单标签／平台 ID。
昵称／群名片是作者名，不是群名；当前后端不会主动获取 QQ 群信息。

HTTP 读取需 `X-LKA-Messages-Token: <API 或 CONTROL token>`，也支持既有 Bearer 方式；
只允许本机访问、合法 Host／Origin，保护响应 `Cache-Control: no-store`。
人工修改只允许 CONTROL；采集器 metadata 上报只允许 IMPORT。凭据不要传给模型或写入页面脚本。
前端应沿用自己的安全代理，并为下面的路径补充精确转发规则；本轮未修改已有代理允许列表。

## 群名和别名

- `GET /messages/conversations/resolve?query=项目组`：精确、大小写／Unicode／空格归一化匹配
  名称、别名或平台 ID；返回 `matches/ambiguous/has_more`。重名时让用户选择，不能默认取第一项。
- `GET /messages/conversations/{conversation_key}/metadata`：返回平台、账号、类型、真实 ID、
  group_id、display_name、user_alias、cached_display_name、manual_display_name、platform_name、
  cache_provenance、revision、updated_at。
- `PATCH` 同一 metadata 路径：`{expected_revision,user_alias?,display_name?}`，CONTROL 专用；
  revision 是人工标签 CAS 版本，过期返回409。导入缓存不覆盖人工标签。
- `POST /integrations/messages/conversations/metadata`：IMPORT 专用，最多100项、请求体最多128KiB；
  `{metadata:[{platform,account_id,conversation_type,conversation_id,capture_epoch,
  platform_name?,display_name?,cache_provenance?}]}`。
  必须对应现有、仍允许采集且 capture_epoch 一致的白名单，返回 accepted/rejected。
  此接口不创建或扩展白名单。来自现有合法本地缓存的群名称可由前端采集器上报；
  没有缓存时也可让用户手工标注。

## 统一 search

采用 live source adapter：消息 SQLite 是原文权威，不镜像入另一套知识表，不做 embedding。
新消息入库后可立即参加 `knowledge.search`，无额外索引任务；消息 source_type 是 `chat_message`。
来源 ID 沿用现有 message source inventory，和 parent／child 的 source/account scope 一致。

- `GET /knowledge/search?q=服务器&source_type=chat_message&limit=10`：仅消息检索。
- 带阅读凭据的 `GET /knowledge/search?q=服务器`：邮件、本地知识和消息混合检索。
  无凭据的旧接口只返回原有非消息来源；显式请求消息来源需要身份验证。
- `POST /knowledge/chunks/load`：`{chunk_ids,max_chars_per_chunk?,offset?}`，读取命中的消息正文分页。
- `GET /knowledge/documents/{document_id}?include_text=true`：有界会话最新消息页，
  **不是完整聊天导出**。按 metadata.coverage 查看页范围与是否有更多记录。

结果有 `source_ref`、稳定 chunk_id、带 capture_epoch 的 document_id；metadata 包含会话、
作者、时间、群显示名等。统一知识接口沿用 personal/redact 隐私门，原始证据在专用消息接口读取。
消息目前是关键词检索；混合模式中的语义通道不含消息，不能宣称消息已向量化。
每次 search/load 都重新检查记录许可、账号和 capture_epoch，旧引用不会在重新授权后复活。
会话历史中的 live-source 工具结果不复用为观察缓存；普通本地文档缓存不变。

## 原文、上下文和人物档案

- `GET /messages/search?query=...`：沿用原路径，可按 conversation_key、sender_id、sender、
  since/until（Unix秒）过滤；时间优先 sent_at，否则 received_at。结果是分页，不是完整平台历史。
- `GET /messages/records/{message_id}`：内部稳定 ID，返回原文、回复引用、会话 metadata。
- `GET /messages/records/{message_id}/context?before=10&after=10`：前后各最多25条，时间序列
  按采集 seq 排列；含 anchor_message_id、gap、has_more 与当前 capture coverage。
- `GET /messages/reading/dossiers?conversation_key=...&limit=30&offset=0`：本地持久档案目录。
- `GET /messages/reading/dossiers/{conversation_key}/{sender_id}?limit=30&offset=0`：有界观察分页，
  claims 是可修订的有来源候选；machine_notes 是未通过语义强度校验的待核实摘记。
  source_count、observation_count、has_more/next_offset 与人工纠正独立展示。
- `GET .../dossiers/{conversation_key}/{sender_id}/sources?limit=30&offset=0`：证据原文分页，最大50条。

档案来自运行库旁 `message_profiles` 的私密本地持续文档，但读接口重新查 SQLite 权限、
原作者、当前 capture、逐字引用，以及最新 hide/delete/correct 控制；导出更新延迟不会绕过这些控制。
接口不暴露文件系统路径，不读取独立人工 notes.md，不更新已读状态或后台水位。
原有 participants 热池、topics、insights、summary／coverage 接口继续使用；持久档案与热池不同，
允许按真实人物 ID 回看已形成的档案，失活本身不会将历史观察自动认证成永久特征。

## Agent 工具与事项边界

新增 `messages.resolve_conversations/conversation_metadata/read_message/context/dossiers/dossier/dossier_sources`，
扩充 `messages.search` 的时间上界和作者名过滤；全部 `read_only=true`。
通用 `knowledge.list_sources/search/load_chunks/load_document` 可以发现／读取消息；
child 仍需对应来源和账号，两类空 grant 均不是无限制。

消息是 untrusted inbound evidence，不是操作指令。经专用消息工具或统一 search 读取消息前，
Tool Executor 从注册工具／provider 取得并持久化消息来源约束；后续直接 matter 写入、
无约束命令执行不会因换工具、fork 或 context compaction 绕过授权。
事项仍走现有 `/messages/matter-proposals` 的人工批准链路；本轮没有自动创建事项能力。

## 验证与上线

使用临时 SQLite 和合成消息作定向接口验证，不调用远程模型、NapCat 或生产库。
本轮完成源代码接口；运行中的 Windows 后端需单独受控升级，前端负责人再对接代理和 UI。
