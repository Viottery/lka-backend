# 偏好存储与生产对齐报告（2026-10-06）

## 修复结果

| 之前的 bad case | 原因与处理 | 验收结果 |
| --- | --- | --- |
| “以后以某种口吻交流”被写进全局 AGENTS.md | Windows 仍用旧工具说明/模板；同步现有分层设计，工具还核对当前持久化用户消息是否明确编辑指导文件 | 普通偏好、引用、疑问、无当前来源/子任务写入拒绝；显式编辑仍可用，保留 SHA CAS 和安全审查 |
| 一次搜索要求被记成“以后搜索” | ordinary wish 为“我希望你…”擅加未来含义 | 无长期限定的对助手任务请求不产生本地候选 |
| “我希望你以后…”仅产生候选，附带能力要求丢失 | 直接长期句式未识别中间主语，协调从句/句号边界截断 | 支持中间主语，保留性格/口吻及“同时保持工作能力”；其他角色名称和普通交流风格同样测试，不匹配固定角色 |
| “保存了”却没有可核验结果 | Agent 只有只读记忆工具，靠另一个写工具兜底 | `memory.remember` 只接受当前用户完整本地断言，返回 active/candidate/撤回及文件同步状态；共享后台来源和去重键 |
| Linux 已优化、Windows 实际跑旧版 | 同步不是按 Git HEAD 发布，健康版本一直为 0.1.0 | 对应用、入口及锁文件计算内容指纹，Windows personal 启动加载标识并在 `/health` 返回 |
| 清理会话后侧写残留，或无 checksum 的旧来源未撤回 | review/针对测试发现仅显式选定记忆 scope 被刷新，单一 checksum 身份不足 | 清理关联 scope 的侧写，撤回该消息所有来源身份并留下 canonical 延迟发布围栏 |

代码改动在通用来源读取、工具包边界及记忆提取中；没有新增角色答案提示、邮件/QQ 专用
Agent core 分支或额外模型判断。普通后台学习继续异步执行；即时记忆工具只做本地提取/落库。

## 测试记录

所有测试使用隔离临时数据库、合成输入和脚本模型；未发起新的真实 LLM、联网搜索或 QQ 操作。

- 首轮核心测试：139 passed、1 failed，失败是测试访问不存在的 `MemoryRecord.dedupe_key`，
  改为验证实际存储列，未调整生产记忆合同。
- 受限执行环境的 157 例检查：153 passed、4 个后台线程退出/排空失败；另一个含远程 fake
  的旧测试组合也停在异步线程处，已中止，未计为通过。
- 宿主 Linux 单独复测该后台路径及 runtime quality：9 passed，19.44 秒。
  相同代码在宿主通过，受限网络环境的异步唤醒/退出异常不作为生产代码修复依据。
- 宿主 Linux 的完整本轮定向组合：184 passed、1 failed，56.04 秒；唯一失败暴露了
  仅会话清理时 legacy/no-checksum 来源未撤回，已修复。
- 修复后仅重跑受影响的清理/即时保存/跨会话/发布指纹/启动测试：21 passed，13.73 秒。
  没有重复执行已通过的全部组合；上述 185 例均有最终通过记录，不伪称单次命令 185 passed。
- 最新消息协议、metadata、API 工具、live-source cache 的隔离兼容检查：30 passed，5.17 秒。
- 独立低成本只读 review 检查写入来源、子 Agent、回执和清理边界，发现的侧写遗漏已修复。
- Python 编译、PowerShell 语法及 `git diff --check` 检查通过；不覆盖/提交无关工作树修改。

## 实际生产状态

- 唯一日常生产入口：`C:\Users\xc133\projects\lka_backend`，原生 `.venv`、personal profile，
  `127.0.0.1:8765`，原 `config/local.toml` 和 `data/runtime/lka.sqlite3`。
- WSL `/home/viottery/lka_backend` 是开发源；同步包含现有未提交优化，不复制测试数据、
  评测、Linux 虚拟环境、私有配置、`.aws` 或临时 SQLite/session 文件，不使用 `--delete`。
- 源/生产 159 个应用源码、启动入口和依赖锁定文件一致。启动中的后端健康标识：
  `b806fcc4188b93800af75220995128bc4579605a544cdc660b4dd6ec7eb19423`。
- 后端旧 PID 26980 → 新 PID 30704；前端 PID 20620 保持不变。没有停止 QQ、SnowLuma
  或桌宠，没有改变采集/阅读授权、处理算法、预算、默认模型或密钥。
- 重启前 Agent/后台正在执行任务均为 0、outbox 为 0。只切换后端；前端持续采集。
  复核时 `enabled=true`、`connection_state=connected`、`sync_state=idle`、`pending_count=0`、
  `last_error=null`，并收到 2026-10-06 02:15:21（Asia/Shanghai）的新消息。
- `message_reading_control` 仍未暂停，revision=3；原两个 `message_analysis` retry_wait 任务
  和 11 个 succeeded 任务保留。retry_wait 不是清理对象，也不是本轮宣称已解决的问题。
- 私有 `.env` 与 `config/local.toml` 内容 SHA 和部署前备份完全一致。依赖锁内容原已相同，
  未重新安装依赖或换掉原生虚拟环境。

## 清理与恢复

按用户确认保留 `session_b19fc1daf076`（“你能看到我的QQ消息吗？”）及其原始消息。
仅以下三个测试会话软删除、移入回收站：

- `session_875fbf84fd6d`：Windows 文件验收及“先给结论”测试。
- `session_afc8aa7b078c`：Windows 邮件检索验收。
- `session_91999f833880`：身份/角色偏好测试。

三条测试记忆（包括两个 candidate）全部撤回，目前 active/candidate 为 0。
全局指导恢复为新版短模板，误写的角色资料/偏好移出活动指导；全局 MEMORY.md 刷新为空侧写。
原始会话消息、trace 和审计不物理擦除；恢复会话不会自动复活已撤回偏好。

清理事务前后对 31 个 `message_*`/`qq_*` 表及非记忆任务集合计算内容哈希，完全相同。
消息正文、媒体、画像、话题、分析结果、授权、关注/采集任务未被该清理修改。

备份均在 Windows 本地，含私有信息，不入 Git：

- 旧代码/配置：`data/deployment_backups/code-20261005T180954474391Z/`。
- 原生 SQLite backup、原指导与记忆文件、精确清理清单：
  `data/deployment_backups/preference-cleanup-20261005T181256413733Z/`。
- 发布清单：`data/runtime/production_release.json`。

恢复单个会话可走现有回收站 API；需要恢复记忆须重新明确确认。
不要整库覆盖备份来恢复几个测试条目，否则会覆盖持续采集的新消息；灾难恢复另需先停写并
核对期间增量。脚本提供预览、未知 ID/活动任务拒绝、指导 SHA CAS 和受保护内容检查。

## 后续配置同步与启用（2026-10-06）

按用户要求，将 WSL 私有配置合并到 Windows 生产安装并单独重启后端：

- `.env` 同步 `BRAVE_SEARCH_API_KEY`，密钥值不进入报告、日志或 Git。
- `config/local.toml` 同步 `llm.supports_json_mode`、`llm.tokenizer_json_path`、
  `embedding.default_retrieval_mode`、`mail.outlook.client_id_env`。
- 复制公开 tokenizer 文件至 Windows 的 `data/tokenizers/deepseek-v4.1-flash/tokenizer.json`，
  SHA-256 为 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`。
- 保留 Windows 独有的 Agent/记忆/后台阅读设置、预算、QQ 导入与控制鉴权、媒体目录、
  数据目录及前端连接配置。Windows 尚无 Outlook 登录令牌，保留其关闭状态与本机授权路径，
  不迁移 Linux 的 OAuth 登录会话。
- 备份：`C:\Users\xc133\projects\lka_backend\data\deployment_backups\config-sync-20261005T182625328080Z\`，
  包含原 `.env`、原 `config/local.toml` 和不含凭据的同步字段清单。
- 原生 Python 配置校验、tokenizer 加载及合并字段完整性检查通过。
  159 个应用/入口文件仍与源一致，部署指纹保持
  `b806fcc4188b93800af75220995128bc4579605a544cdc660b4dd6ec7eb19423`。
- 后端 PID 30704 → 16732，健康接口验证通过；前端 PID 20620 保持不变。
  重启前正在执行 Agent/后台任务均为 0；重启后 Reader `connected`、同步 `idle`、
  `pending_count=0`、`last_error=null`，未停止 QQ、采集或桌宠。
- 使用相同 Windows 配置的 Brave 适配器执行一次公开查询，返回 1 条结果，
  按既有配额机制计入一次请求。未新增 LLM 会话或 QQ 操作。
- 运行中 `/knowledge/search` 响应正常，默认请求模式为 `hybrid`；当前没有符合条件的
  语义向量，按既有策略回退到 `keyword` 并返回明确提示。本轮未下载 embedding 模型或重建索引。

## 已知边界

- 指导编辑检查是工具侧的保守本轮目标检查，不是通用自然语言授权模型；模糊编辑需澄清。
  不是 OS 文件写入沙箱，也不保证所有 bash/文件工具无法改指导文件。
- 复杂、隐含偏好仍可能留在后台 candidate；即时工具不能把未识别断言或网页角色资料
  伪装为已确认用户偏好。角色详细资料应走知识/资料存储，不在本轮新增角色专用模块。
- 测试证明来源、持久化、跨会话装配及代码版本一致，不代替真实 LLM 任意措辞的质量评测。
- 首次上线时 Brave Search 密钥未配置；后续按用户要求同步私有配置并验证搜索成功，
  见上节。直接网页读取与搜索能力仍分开，搜索配额沿用既有设置。
