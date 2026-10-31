# MySQL 后端：配置、迁移与运行边界

支持显式选择 MySQL 8.4 作为独立节点的主存储（当前验证版本为 8.4.13）。SQLite 仍是默认存储；选择 MySQL 不会自动导入、合并或恢复 `data/runtime/lka.sqlite3`，也不会切换现有服务。先用独立测试库验证，再按单独的迁移方案处理实际数据。

云端或某个独立节点可以显式选择 MySQL 主存储。手机和其他电脑继续通过 API 连接，不直接访问数据库端口；共享信息仍由同步协议分发，本地镜像和离线缓存继续使用独立 SQLite 文件。

## 安装可选依赖

MySQL 驱动、schema 转换工具和 LangGraph MySQL checkpointer 作为可选依赖安装；SQLite 用户不需要这些包。

```bash
uv sync --locked --extra mysql --extra dev
```

## 建立隔离测试库

创建一个专用于测试的空 MySQL 8.4 数据库。准备两个私有 JSON 凭据文件：一个供 schema 安装使用，一个供应用连接使用。格式如下；不要把实际凭据放进仓库、命令行参数、日志或聊天：

```json
{
  "host": "127.0.0.1",
  "port": 3306,
  "user": "lka_test",
  "password": "从本机私密配置读取",
  "database": "lka_test"
}
```

两个文件可以连接同一个测试库，但应使用不同账号。schema 安装账号需要该库的建表、索引、触发器等 DDL 权限；当前应用账号需要库范围内的 `SELECT, INSERT, UPDATE, DELETE`，以及读取 `INFORMATION_SCHEMA.TRIGGERS` 所需的 `TRIGGER` 权限；该权限本身也允许管理触发器，应仅交给受信任的服务器运行账号。已验证无需 `CREATE, ALTER, DROP, INDEX` 或 `SUPER` 即可核验结构并执行普通业务。协调器 fencing 表由显式迁移安装，运行时不执行 DDL。不要给应用账号 `SUPER` 或修改 MySQL 服务的全局 binlog、信任或安全设置。实际 grant 应按当前 schema 与测试覆盖核对，不能把本地实验账号权限直接复制到生产。

运行下面的命令安装版本化业务 schema、协调器 fencing 表和 LangGraph checkpoint schema，不会复制 SQLite 数据，也不会修改 MySQL 服务的全局 binlog、信任或其他服务级设置：

```bash
.venv/bin/python scripts/migrate_mysql.py \
  --connection-file /path/to/private/mysql-migration.json
```

该迁移器会核验已存在对象的定义并拒绝接管不匹配对象。它不是数据导入或回滚工具。不要把生产库、日常 SQLite 文件或包含个人数据的库用于此步骤。

## 通用后端配置

常规启动入口通过环境变量选择主存储。`LKA_STORAGE_BACKEND` 默认为 `sqlite`；选择 `mysql` 时必须设置密码，连接失败会报错，不回退到 SQLite。

| 环境变量 | 默认值／含义 |
| --- | --- |
| `LKA_STORAGE_BACKEND` | `sqlite`，可选 `mysql` |
| `LKA_MYSQL_HOST` / `LKA_MYSQL_PORT` | `127.0.0.1` / `3306` |
| `LKA_MYSQL_DATABASE` / `LKA_MYSQL_USER` | `lka` / `lka` |
| `LKA_MYSQL_PASSWORD` | 必填私密凭据 |
| `LKA_MYSQL_POOL_SIZE` | `8`，应用存储连接池容量 |
| `LKA_MYSQL_POOL_QUEUE_CAPACITY` | `128`，FIFO 等待队列容量；超限快速拒绝 |
| `LKA_MYSQL_POOL_WAIT_SECONDS` | `5`，等待连接许可上限 |
| `LKA_MYSQL_LOCK_WAIT_SECONDS` | `5`，InnoDB 行锁等待上限 |
| `LKA_DB_WRITE_TRANSACTION_SECONDS` | `30`，事务期限、空闲连接兜底及 SELECT 服务端执行时间上限 |

运行时在启动时核验迁移版本和校验和；普通池内连接复用这一已验证的存储身份，避免每次访问重复扫描 schema。迁移命令会占用同一协调器命名锁，正在服务的实例存在时拒绝 DDL。MySQL checkpoint 使用独立的、有并发上限的同步／异步连接，不与 SQLite checkpoint 文件混用。
业务池仅在驱动确认连接仍打开、autocommit 开启且无活动事务时省略归还时的空 rollback；
未知状态仍执行 rollback，失败连接丢弃，checkout 继续校验连接健康。查询行仅在同一结果集内共享只读列索引，
不缓存业务行或权限，也不跨语句复用分类结果。
兼容路径仅缓存有大小和条目上限的纯 SQL 解析模板；每次转换使用独立 AST，
表结构、rowid 和冲突目标回调继续重新核验。它不缓存参数、查询结果或授权，
也不把 SQLite 兼容层变成全量原生 MySQL Repository。

检查点连接也设置锁等待和 SELECT 上限，异步 saver 的单次 cursor 操作有取消期限；事务只包围单次 saver 操作。连接本身会跨越模型调用，空闲超时至少为 300 秒，以容纳默认 180 秒模型请求。该空闲上限与主业务连接的事务期限不同，不表示 checkpoint 持有事务等待模型；这些保护也不构成任意同步 DML 或网络写入的硬期限。

## 启动 mock API

`start_mysql_lab.py` 会清理进程内 LKA 和供应商凭据环境变量，写入私有临时配置，并断言运行时 `data_dir` 与后端选择正确。它拒绝使用默认日常数据目录或已存在 `lka.sqlite3` 的目录，默认仅绑定回环地址，并使用 mock 模型。

```bash
.venv/bin/python scripts/start_mysql_lab.py \
  --connection-file /path/to/private/mysql-runtime.json \
  --data-dir /tmp/lka-mysql-lab \
  --port 8766
```

加 `--hub` 可在回环地址启用独立测试账号、设备、共享同步与远程执行 API，供工作台验收。该选项使用本地 HTTP 测试 cookie，不能直接作为远程 HTTPS 生产配置；账号通过已有的账号管理流程建立。`--workbench-dir` 可指向独立前端仓库的 `app/web/pet` 静态目录，无需启动或重启桌宠。启动器安装仅限本脚本的确定性回复 fixture，让浏览器能验证成功回复、SSE 与持久化；回复会明确标识为隔离测试，不选择工具，也不调用供应商。

测试数据目录与 MySQL 测试库都必须是一次性隔离目标。关闭测试进程不会把数据转回 SQLite；切换 `LKA_STORAGE_BACKEND` 也不会搬迁或同步两边的数据。测试中的附件、工作区和本地派生缓存仍由指定的测试数据目录管理；其他电脑上的 SQLite replica/cache 仍是本地副本，不是 MySQL 事务的一部分。

该启动器关闭自主管理执行、记忆后台维护、消息历史后台处理和 embedding/reranker。它适合验证存储与并发合同，不代表邮箱、搜索、记忆和真实模型的线上配置已验证。

## 并发、连接与故障恢复

主业务连接池与检查点池分别管理。检查点池默认同步 4、异步 4；逻辑 saver 不绑定连接，
每次游标操作才借用、开启事务、核对 coordinator 身份，提交或回滚后归还。
异步连接按事件循环隔离，取消和失败仍完成连接清理；显式迁移负责安装 schema，运行时只核验。
模型和工具等待期间不持有检查点连接。

根任务默认 4 个并行，其中 2 个位置留给交互；额外子任务默认全局 2 个。
容量配置和 HTTP 拒绝合同见 [配置说明](configuration.md) 与 [API 说明](api_contract.md)。
这不是跨设备全局并发控制，也不改变模型额度或同会话一个根运行的持久约束。
同一会话的多个输入写事务还会在借连接前进入有界 FIFO；等待方不持有数据库连接，
实际事务继续锁会话／队列行并核验幂等与容量。事务结束并归还连接后才放行下一位，
其参数与拒绝响应见配置/API说明。管理策略的请求内读取复用同一连接，不缓存权限。
消息追加在插入子消息之前锁定父会话行，避免并行插入的外键共享锁与
父会话时间戳更新形成锁升级死锁。锁仅保留到当前事务结束，
不跨模型等待，也不改变调用方已有的会话准入和任务锁顺序。
账号初始化与登录限流的种子行通过不改变计数的重复键更新先取得排他锁，
多限流键按固定顺序取得；重复请求不会重置限流窗口或次数。

记忆和消息后台采用事件唤醒与低频补漏，空领取只读检查；过期任务维护按批量和时间间隔限流。
知识重复导入会重新核验隐私、来源权限、分块及索引一致性；一致时不删除重建 chunks，
标题和 ACL 变更仍生效，敏感策略或分块元数据变化走更新路径。

首次图执行前失败会释放会话占位并保留失败记录。周期恢复仅收敛有失败派发证据的根运行，
以及证明从未开始且没有派发、检查点、工具和事件证据的旧 queued 根运行；
不删除失败历史，不自动重放未知副作用，不释放合法审批和暂停状态。

## 模拟压力测试

压力测试使用单独安装的、名称包含独立 `stress` 段的测试库。存储阶段需要独占该库的协调器锁；HTTP 阶段在它结束后再启动，避免两种负载混合计时。存储脚本固定池容量 8，执行 1/4/8/16/32 worker、热 scope、混合读取、10,000 条终态历史和有限池等待场景，最多 20,000 条合成任务/历史记录，单阶段 120 秒、整体 8 分钟。

```bash
.venv/bin/python scripts/stress_mysql_storage.py \
  --connection-file /path/to/private/stress-runtime.json \
  --output docs/_engineering/mysql-storage-stress.json

.venv/bin/python scripts/start_mysql_lab.py \
  --connection-file /path/to/private/stress-runtime.json \
  --data-dir /tmp/lka-mysql-stress --hub --port 8768 --reply-delay-ms 100
```

`--reply-delay-ms` 为脚本专用 mock 每次模型调用增加 0–2000 ms 延迟，不持有数据库事务。通过已有账号管理流程在该库建立测试账号后，在另一个终端运行：

```bash
.venv/bin/python scripts/stress_mysql_http.py \
  --base-url http://127.0.0.1:8768 \
  --login-file /path/to/private/stress-login.json \
  --output docs/_engineering/mysql-http-stress.json
```

HTTP 脚本只接受回环 HTTP 地址，使用 cookie/CSRF；覆盖完整会话、混合历史读取、同会话冲突、SSE 重连与在途流，以及 64 条唯一输入加 6 次精确重复提交的持久队列。它按发布合同验证请求关联 ID，只有队列的消息 ID 承担幂等去重。报告分别统计请求、受理、完成和拒绝，核验容量拒绝没有留下运行，并验证拒绝后可恢复；区分预期 409、结构化容量拒绝与意外失败，不自动重试写请求，失败仍写入报告。HTTP 吞吐包含核验过程且采用闭环客户端；百分位来自有限样本，不能当成生产到达率容量或真实模型速度。

## 运行真实 MySQL 并发合同测试

测试 runner 会清除现有 LKA/provider 环境变量，使用 mock 模型和临时 import-time 数据目录，并核验路径隔离。并发测试只插入有随机前缀的合成记录，结束后按这些 ID 精确清理；不应使用测试脚本去清空数据库或恢复备份。

```bash
.venv/bin/python scripts/run_mysql_tests.py \
  --connection-file /path/to/private/mysql-runtime.json \
  tests/test_mysql_concurrency_live.py \
  tests/test_mysql_management_publication.py
```

若某个测试另需临时触发器，可提供独立 DDL 凭据：

```bash
.venv/bin/python scripts/run_mysql_tests.py \
  --connection-file /path/to/private/mysql-runtime.json \
  --migration-connection-file /path/to/private/mysql-migration.json \
  tests/test_name.py
```

未设置 `LKA_MYSQL_TEST_CONFIG` 时，live MySQL 测试会跳过。优先使用上述 runner；它会设置隔离配置。测试通过说明被覆盖的 MySQL 合同成立，不等于已测得生产吞吐，也不等于所有 SQLite 功能均已移植。

## 并发与恢复模型

- 当前部署模型是单个云端协调器进程与其多个 worker 线程。协调器使用 MySQL 命名锁和持久 token 保护单所有者；本项目不声明支持多个 API/coordinator 实例共同运行。
- 同步只读分页使用单次 `REPEATABLE READ` 一致快照，保证版本头、实体和依赖属于同一页面快照；池默认仍为 `READ COMMITTED`，普通写事务不改变隔离级别。页面发送前通过新连接读取当前设备授权，避免快照隐藏期间的撤权。
- MySQL 主存储不使用 SQLite 的全库 FIFO 写队列。会话 admission、输入队列、job scope、run、工具 invocation、管理发布和模型预算使用各自的行锁、唯一键或 revision/lease 条件更新。不同会话或 job scope 可并行；同会话输入和 root run 仍按原合同串行。
- Background job 使用 scope guard、短事务领取、租约 owner/epoch fencing 和状态条件更新。`SKIP LOCKED` 用于跳过其他 worker 正在领取的 scope/job；它不提供严格全局 FIFO。
- 模型预算依照 pool/quota 维度锁定持久计数行并在同一事务中预留用量。不要通过清账、提高额度或放宽熔断恢复运行。
- 数据库租约不能证明外部副作用是否已发生。工具执行结果未知时保持 uncertain，不因换库或 worker 超时自动重放邮件、HTTP 写操作或文件修改。
- 死锁（1213）与行锁等待超时（1205）会回滚整个应用事务并使当前连接租约失效。即使调用方捕获异常，也不能在该连接继续写入或提交半套修改；需要由明确的业务恢复流程处理，连接层不自动重放。
- SQLite 仍承担本地副本、部分缓存/派生索引和本地运行文件；这些内容不因主存储切换自动同步到 MySQL。MySQL 错误也不会静默回退到 SQLite 主库。
- SQLite `INSERT OR IGNORE` 只在 schema 能确认唯一键且没有触发器的语句上转换为“仅重复键时忽略”；not-null、check 等其他数据错误继续报错。这种转换比 SQLite 更严格，不会把类型或数据校验失败静默吞掉。

## 排查锁与慢查询

先在测试库观察，确认账号有读取 Performance Schema 的权限。查询应限定到 LKA 进程和测试时间窗；生产上先按运维权限流程执行。

```sql
EXPLAIN FORMAT=JSON
SELECT job_id
FROM background_jobs
WHERE kind = 'example'
  AND scope_id = 'synthetic-scope'
  AND status IN ('queued', 'retry_wait')
ORDER BY priority DESC, available_at, created_at, job_id
LIMIT 1;

SELECT
  w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
  w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx,
  r.OBJECT_SCHEMA, r.OBJECT_NAME, r.INDEX_NAME, r.LOCK_MODE AS requested_lock,
  b.LOCK_MODE AS blocking_lock
FROM performance_schema.data_lock_waits AS w
JOIN performance_schema.data_locks AS r
  ON r.ENGINE = w.ENGINE
 AND r.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID
JOIN performance_schema.data_locks AS b
  ON b.ENGINE = w.ENGINE
 AND b.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID;

SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SHOW ENGINE INNODB STATUS;
```

`EXPLAIN` 验证实际数据分布下的索引选择；`data_lock_waits`/`data_locks` 用于定位当前等待双方与锁对象；`SHOW ENGINE INNODB STATUS` 的死锁段是最近一次死锁的诊断快照，不是长期审计。访问 Performance Schema 可能受账号权限限制。不要仅凭一项全局计数断言哪个业务事务造成锁竞争。

MySQL 原生 ngram 索引在实测的记忆批量更新中曾出现内容已更新、`MATCH` 仍为零分的漏召回。记忆查询保留原生匹配和排序，并对既有受限引号查询启用字面匹配兜底；兜底保留原查询的权限、来源和状态条件，按零分排在原生结果之后。它不代表任意 SQL 或全部 FTS5 语法的兼容，也不会放宽被拒绝的查询语法。当前邮箱、事项与知识领域的更新检索另有实库回归。

现有 `database_timing.jsonl` 的数据库阶段统计面向 SQLite；MySQL 应结合本页的 Performance Schema 查询和独立并发基准观察。不能把没有 SQLite 阶段记录解释成 MySQL 没有数据库访问。

会话意图的来源快照使用原生绑定 `JSON_TABLE` 展开来源序号，并继续验证同会话、内容哈希、角色、trace 和时间。
这是一条显式移植的查询，不表示通用方言层支持所有参数化 `json_each`。
共享同步在单次读取内批量解析实体及其来源，表结构通过 `information_schema` 检查；有效记忆的来源激活仍按 JSON 类型和值判断，字符串或容器不能冒充通过状态。

## 当前验收限制

仅已运行的 MySQL 8.4.13 隔离库合同测试可以作为实际引擎证据。mock/SQLite 回归只验证相应层的逻辑；不要将其描述为真实模型质量、生产并发容量或多 API 实例验收。实际压测指标、长时间恢复测试和未覆盖的领域需单独记录后再扩大部署范围。

## 现有 SQLite 数据迁移

迁移不会由应用启动自动触发。先保存当前源码版本、服务配置和完整私有数据备份；
在独立 MySQL 库用一致性 SQLite 副本演练。最终迁移前停止所有源库写入，
重新备份主库、checkpoint 库、附件和其他数据文件。保持实际数据目录及工作区路径，
避免附件、记忆文件和凭据引用失效。备份不得提交 Git。

安装 schema 后，分别导入业务数据与 LangGraph checkpoint：

```bash
.venv/bin/python scripts/import_sqlite_mysql.py \
  --sqlite-snapshot /private/offline/lka.sqlite3 \
  --runtime-connection-file /private/mysql-runtime.json \
  --migration-connection-file /private/mysql-migration.json \
  --snapshot-confirmed --source-validation-receipt /private/source-validation.json --check-only

.venv/bin/python scripts/import_sqlite_mysql.py \
  --sqlite-snapshot /private/offline/lka.sqlite3 \
  --runtime-connection-file /private/mysql-runtime.json \
  --migration-connection-file /private/mysql-migration.json \
  --snapshot-confirmed --source-validation-receipt /private/source-validation.json \
  --execute --report-file /private/business-import.json

.venv/bin/python scripts/import_sqlite_mysql_checkpoints.py \
  --source /private/offline/agent_checkpoints.sqlite3 \
  --connection-file /private/mysql-runtime.json \
  --migration-connection-file /private/mysql-migration.json \
  --snapshot-confirmed > /private/checkpoint-import.json
```

导入器仅接受空的已迁移目标，逐表验证行数与摘要，保留业务 ID、同步水位、预算账本、
完整 checkpoint 历史及 pending writes。知识库 `sqlite-vec` 向量按原 ID 和 float32 内容迁移到
V11 `knowledge_vector_blobs`；MySQL 语义检索在权限过滤后分批计算余弦相似度，
不调用模型重新生成向量。SQLite 的虚表及其内部 shadow 表不作为普通业务表复制。
V12 为全文检索的文本列设置大小写不敏感、重音敏感的排序规则，业务 ID 的比较规则保持不变。
原生 ngram 对部分罕见汉字短语可能漏召回；支持的单短语会通过原生全文候选与精确文本匹配补查，
保留原查询的权限过滤和分页。会话历史的字面补查仅限当前会话且最多检查 5000 条映射，
超出范围会报告检索不完整；可继续使用历史读取或其他检索方式，不把未覆盖范围当作无结果。
失败导入保留现场和状态标记，默认启动拒绝使用未验证目标；应排查并使用新的隔离目标，
不要删标记强行启动。完整通过导入、mock 功能和身份/同步校验后才能切换服务。

连接等待使用有容量上限的 FIFO 队列，排队不占用数据库连接；连接建立、锁等待与事务期限
分别受限。关闭或异常时回滚并丢弃不可确认的连接。事务时间限制属于协作式检查，
不能强制安全中断任意已提交的操作；提交结果不确定时不得自动重试写入。
管理助手仅对确定未执行模型或工具的存储等待失败进行有限退避重试；
开始执行后遇到存储异常需要显式恢复，保留原运行、工具及用量审计。

原 SQLite 专用云端更新器会拒绝 MySQL 环境，避免依据旧 SQLite 判断空闲或备份错误的数据源。
MySQL 后续更新必须使用相应数据库的备份、空闲检查和 schema 迁移流程。
切换后原 SQLite 文件保留离线；若 MySQL 已产生新业务数据，不能直接回切旧文件而丢失新数据。

源库预检可使用私有 `--source-validation-receipt` 缓存成功的完整性和外键检查。复用时重新核对
整个输入文件的 SHA256 与大小；内容变化、未知格式或不完整凭证会拒绝导入。逐行数据摘要、
目标外键、版本和空库检查仍逐次执行。离线导入会临时延长已提交、无事务时的会话空闲期限，
退出时恢复，恢复失败的连接丢弃；普通业务的事务、锁等待和连接池期限保持配置值。
复制同时按行数和约 3 MiB 的数据量分批，检查点按主键流式迁移，避免全库大排序。

MySQL 协调器的命名锁有独立心跳，使用原连接续租。连接丢失后不自动重新获取，
事务 fencing 会拒绝旧 owner 的写入，健康检查报告不可用。心跳的网络操作不会占住
前台读取内存 ownership 状态的锁。

运行指标采集器支持 `--mysql-connection-file /private/mysql-readonly.json`；应使用单独的
只读账号。MySQL 大小为近似值，WAL 字段为 null。显式指定 MySQL 或 health 报告 MySQL 后，
缺少配置、超时或读取失败会报告 unavailable，不读取旧 SQLite 文件代替当前数据库。
