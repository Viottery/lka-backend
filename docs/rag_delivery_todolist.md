# RAG 工具交付 TODO（2026-09）

本清单是本轮实现和验收记录，继承 `rag_evolution_todolist.md`、
`multi_agent_runtime_architecture.md`、`context_driver_design.md` 的边界：RAG 是独立的
只读工具/领域服务，不建立第二套 Agent 图；模型给检索意图，服务端负责授权、预算和执行；
子 Agent 只能看到其继承的 scope，完整原文不进 planner 状态。与长期演化路线相比，
本轮聚焦知识库路由、多路召回、本地轻量重排、可靠证据和离线评测。

## 验收原则

- [x] 旧 `knowledge.search`、`load_chunks`、`load_document` 调用保持兼容；新增参数可选。
- [x] scope 在候选召回前生效；未授权文档不参与结果、不泄露存在性，load 同样受限（Agent 工具路径；直连管理 API 仍是单用户本地信任边界）。
- [x] keyword、semantic 可独立运行；hybrid 在任一通道不可用时明确降级，不能伪造分数。
- [x] reranker 是本地可选能力；模型不可用时有可观测的 fusion 回退，不阻塞事件循环。
- [x] 相同数据、scope、配置和查询得到稳定次序；证据含可加载的引用和来源。
- [x] 评测有固定数据集、top-k 指标、延迟分布与失败率，并与基线比较；不以单一平均值宣称可靠。
- [x] 不引入远程 embedding/rerank 或未经审查的数据出境；模型下载必须显式配置。

## P0 盘点与接口冻结

- [x] 盘点现有多 Agent、知识检索、工具、语义索引、配置和测试接口。
- [x] 盘点评测：5 个本地知识 case；HotpotQA / 2WikiMultiHopQA / MuSiQue 各 200 个 query，
  MultiHop-RAG 标准化 JSONL 实有 528 个 query（文件名沿用 600），此前未连入评测 runner。
- [x] 记录当前检索基线（5-case suite 为 3/5；公开数据 keyword 基线见末尾）。
- [x] 冻结响应兼容层：保留原字段；新增可选 `source_id`、`rerank_score`、
  `rerank_applied`，沿用 `retrieval_warning` 报告降级。

## P1 Source routing 与权限边界

- [x] 定义检索请求的 query、mode、source_types/source_ids/account_ids、预算、scope；
  区分模型建议的过滤器和服务端强制 scope，求交集而不扩权。
- [x] 为本地工作区、用户知识库、邮件镜像定义确定性 source route；显式过滤器优先，
  无过滤器时默认搜授权来源；不靠关键词授予权限。
- [x] 把过滤推到 FTS/候选 SQL 之前；语义通道应能按 scope 过滤或有有界 overfetch，
  且过滤后仍满足候选数。空 scope 快速返回。
- [x] 统一 Agent 工具 search/load 的 source、account、workspace 约束与审计；覆盖子 Agent 继承 scope。
- [x] 测试候选池被大量未授权结果占据、跨 workspace/account、无授权来源、load 越权。

## P2 多路召回与结果整理

- [x] 保留 SQLite FTS5 词法召回，完善匹配、稳定排序、短查询/符号/中文输入处理。
- [x] 保留本地 embedding + sqlite-vec 语义召回，避免每请求重复装载模型；
  索引缺失或 provider 错误时明确降级并记录原因。
- [x] 用可配置的各路 candidate_k + RRF 融合；去重、保留 channel 诊断、
  同分稳定排序；有界总候选和响应条数。
- [x] 执行可选 document 去重与每文档 chunk 上限，防单一长文档挤占证据窗口。
- [ ] 检查 secret/deny 内容在词法、语义、重排、摘要、日志每一层均不外泄；现有隐私测试覆盖主要工具路径，尚缺全链路动态渗透测试。

## P3 本地重排与性能

- [ ] 提供可注入的 reranker 接口和本地 FastEmbed cross-encoder 实现；模型名、
  缓存目录、启用状态、候选上限、排队超时和失败回退已配置，默认不偷偷下载模型；
  仍缺可强制中断的单次推理硬超时。
- [x] 多语言数据优先选兼容中文/英文的模型；如果体积或许可不合适，
  保留可配置模型选择并在离线评测后确定默认值。
- [x] 仅对经过权限/隐私过滤的有界候选重排；原文长度受限；异步边界不阻塞 API。
- [x] 重排分数与 fusion 分数分开保存；相同分数稳定排序；失败时保持融合结果。
- [ ] 测冷/热路径 p50/p95、模型初次加载、并发请求、无模型离线模式和 CPU 内存占用；当前已测 warm p50/p95 和预热时间，缺 CPU 内存/真实并发量化。

## P4 工具与上下文集成

- [x] `knowledge.search` 保持旧 schema 兼容，暴露安全的 source 过滤和检索质量配置；
  结果包含 chunk_id、document/source 引用、排序阶段、降级原因，便于按需 load。
- [x] 工具侧仅缓存 scope + source/index version + query/config 指纹相同的候选/证据引用；
  摄取、重建索引、删除或权限变化时失效；不跨 session/scope 复用敏感正文。
- [x] ContextDriver 侧按预算选证据引用与少量 snippets，必要时再 load；
  运行时从父 run 已完成的 knowledge 工具结果提取最多 20 个引用，从当前索引重载并重新授权，
  再按子 Agent scope/token 预算裁剪；planner/subagent 不复制整份检索结果。
- [x] 检查检索结果进入 `untrusted_data`、run log、子 Agent 回传后的隐私与引用一致性（结构校验；不等于语义真值校验）。

## P5 离线评测与质量门

- [x] 把标准化公开 JSONL 接入独立离线检索 runner；限定样本数、seed、top-k，
  可在无 LLM/无网络下运行。数据集记录不可预测地更改时输出 manifest/hash。
- [x] 指标至少 Recall@k、MRR@k、nDCG@k、supporting-doc/all-hop recall、
  forbidden-leak count、fallback rate、p50/p95、失败率；按数据集/source/mode 切片。
- [x] 本地 5-case suite 继续作为工具和 Agent 集成冒烟；QA 增加引文存在、
  未支持回答与应当弃答的确定性检查；语义评判若使用 LLM，单独标明非门禁。
- [ ] 保存 keyword / semantic / hybrid / hybrid+rerank 对照报告，含模型版本、
  数据集版本、硬件、是否 warm cache；用户已决定默认尝试重排，但全量质量/时延门
  尚未通过，不能把“默认开启”写成已验收。
- [x] 测试路由、召回、重排、隐私、降级、指标计算；运行相关集成测试和最终 diff review。

## 本轮报告格式

记录：已完成条目、未完成及原因、接口变化、质量与时延基线/对照、
所跑命令及结果、风险/后续建议。未经过真实模型和公开数据集验证的效果不得写成已达标。

## 本轮执行记录（持续更新）

已落地：`knowledge.list_sources` 来源目录与 `search` 的显式 source_id 路由；
词法候选前置 source/account 过滤，sqlite-vec 语义索引前置 scope 过滤
（其他索引实现有界 overfetch），空 scope 拒绝；
独立的本地 FastEmbed cross-encoder 适配器、配置开关与融合排序回退；
并发重排槽位及有界排队，忙时迅速回退，避免请求堆积；
子 Agent 把检索所得 evidence refs 结构化回传；公开 JSONL 离线 runner、
top-k / all-hop / 延迟 / 失败率指标与数据集 hash。
Agent eval runner 已有可选的引文合法性/字面弃答检查，但不是语义事实正确性门。

追加完成：配置工作区 `.md/.txt` 的受限摄取、重索引与删除文件清理；
Agent 工具上的工作区 source 授权；sqlite-vec KNN 前置授权过滤；
仅保存候选 ID/排序诊断的 session/scope/version 缓存；各路 `candidate_k` 和
每文档 chunk 上限；ContextDriver 可注入的证据引用/摘要/snippet 预算机制；
公开评测切片、质量门和确定性 QA 引文/弃答检查。

尚未完成：多用户身份 ACL（本项目目前是本机单用户服务）、
端到端语义事实校验、真实模型全数据集/并发/内存验收。
真实模型的 HotpotQA 20-query 小切片提升了召回但 warm p95 约 4.15 秒。
按后续用户决策，默认尝试本地重排；模型缺失时无网络下载并回退召回顺序。
该延迟仍未达标，保留上方未勾选项，不以默认开关代替生产可用。

`keyword`、top-10、seed 20260929、每集 200 query 的基线
（报告 `/tmp/lka-public-rag-baseline.json`；顺序为 Recall@10 / all-hop recall / 查询 p95 毫秒）：

- HotpotQA：0.890 / 0.790 / 28.05。
- 2WikiMultiHopQA：0.6825 / 0.375 / 23.01。
- MultiHop-RAG：0.745 / 0.490 / 21.09。
- MuSiQue：0.425 / 0.055 / 32.73。

四组均无执行失败、无标注 forbidden leak。MuSiQue 多跳覆盖过低，不能宣称
“内容可靠”；真实模型对照与答案质量门仍未通过。

缓存的 `BAAI/bge-small-zh-v1.5` 在英文 HotpotQA 同一 20-query 切片上，
`hybrid` Recall@10 为 0.775、all-hop 0.55、p95 56.97 ms；`keyword`
对应为 0.85、0.70、24.99 ms。这个小切片不是普适模型排名，但足以阻止
未经分语种验证就默认启用该中向量模型的混合检索。正式门禁应在全量、
中文/英文分层和真实 reranker 对照后设定。

本地部署步骤：用 FastEmbed 官方 `TextCrossEncoder` 在部署机上显式将所选模型下载到
`[reranker].cache_dir`，并确认许可与模型大小；默认已 `enabled = true`，
保持 `local_files_only = true`。服务进程只从本地缓存加载，缺失模型会在
`retrieval_warning` 中说明并回退融合结果，不会在请求期间联网。推荐先运行
`evals/lka_evals/public_retrieval.py --mode hybrid_rerank` 对照质量与 p95，
衡量实际收益与延迟；需要低延迟时可在本地配置显式设 `enabled = false`。
中英混合语料不能只凭英文小模型的体积选择模型。
