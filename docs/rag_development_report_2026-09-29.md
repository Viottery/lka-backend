# RAG 工具开发报告（2026-09-29）

范围：审查现有多 Agent 边界，制定 `rag_delivery_todolist.md`，交付来源路由、
多通道召回、本地重排、工作区摄取、证据预算与离线评测。本报告区分功能可用与
质量门达标：检索工具可用，但尚不能宣称复杂多跳 QA 的可靠性已达标。

## 已实现

1. 新增 `knowledge.list_sources`，返回当前工具 scope 的来源目录；
   `knowledge.search` 支持显式 `source_ids`，与子 Agent 服务端 grant 取交集。
   简单查询可继续直接搜索，不强制先列目录。
2. FTS 候选 SQL 在排序/截断前执行 source type/id/account 与 active 状态过滤；
   load、document load、语义索引同步也执行 active 来源检查。
   sqlite-vec 语义 KNN 在计算 top-k 前按授权 source/account 过滤，
   其他索引实现保留有界扩窗降级，且每次 scoped 查询只计算一次 query embedding。
   空 scope 快速返回，保留审计。
3. 现有 FTS/substring、sqlite-vec、RRF 路径继续兼容；候选与结果数量有界，
   同分排序稳定。支持分别指定 lexical/semantic `candidate_k`，以及每文档
   chunk 上限；语义 provider/index 不可用时返回明确 warning 和 keyword 回退。
4. 新增可注入 FastEmbed cross-encoder：仅本地缓存、懒装载、文本与候选数上限、
   分数独立字段、初始化失败记忆、并发槽位与短排队；不可用/繁忙时保留原融合次序。
   只将权限和 Privacy Gateway 通过的证据文本送入重排。
5. 子 Agent 检索所得的已授权 citation refs 进入结构化 `TaskResult.evidence_refs`。
   EvidenceRef 同时保存可展示 `source_ref` 和用于权限求交的 `source_id`；
   `load_chunks` 结果也补齐可选 `source_id`，防止拿显示引用误当授权 ID。
   ContextDriver 增加按 scope、token 预算选引用/摘要/snippet 和可注入按需 resolver，
   子 Agent 提示明确将检索文本标为不可信。运行时从父 run 的 durable knowledge
   工具结果提取最多 20 个证据引用，按当前 source/account scope 重新加载与隐私过滤，
   再交给 ContextDriver；已删除或撤权的旧观察不会继续注入子 Agent。
6. 四份公开 JSONL 接入隔离离线检索 runner，可选本地 embedding/reranker（绝不自动下载），
   输出数据 hash、Recall@k、MRR@k、nDCG@k、all-hop recall、泄漏数、失败率、
   降级率、查询 p50/p95、数据集/模式/来源/语言切片及基线质量门。
   Agent eval runner 增加可选 citation/弃答/fixture 标注的 unsupported claim 检查；
   不把它解释为自动语义事实正确性评估。
7. 配置工作区 `.md/.txt` 可通过 `index_workspace(..., index_knowledge=true)` 摄取，
   带文件数/字节上限、symlink 防逃逸、稳定 URI、工作区 source 授权。
   完整重扫可清理已删除文档；受限或失败扫描不清理，避免误删。
8. 检索候选缓存仅保存 chunk ID/分数/通道，键包含 session/scope、来源/索引版本、
   query/mode/过滤器/候选预算；正文每次重新从数据库读取并执行隐私检查。

兼容性：旧工具仍可使用。`knowledge.search` 增加可选输入 `source_ids`；响应增加
`source_id`、`rerank_score`、`rerank_applied`；新增 `knowledge.list_sources`，
以及可选 `keyword_candidate_k`、`semantic_candidate_k`、`max_chunks_per_document`。
新示例配置与无本地配置时的默认检索模式改为 `keyword`，避免未经质量门验证的
hybrid 静默成为默认；已有用户 `config/local.toml` 不被改动。
后续用户明确要求召回后默认重排，因此配置默认值与示例中的
`reranker.enabled` 调整为 `true`；仍只从本地缓存加载，缺模型则保持召回顺序并
返回 warning。当前仓库的 `config/local.toml` 没有显式 reranker 段，会继承新默认值。

## 评测证据

本地 5-case knowledge suite：3/5，通过 lexical、privacy、search-load；失败是
paraphrase case 未构建语义索引，以及 real-agent case 缺 LLM 配置。不能把这两个
失败归因于本次改动，也不能把 3/5 称为整体通过。本轮复跑的报告为
`evals/reports/knowledge_retrieval_runtime_20260929T105039Z.md`。

公开数据：`keyword`、top-10、seed 20260929、每集 200 query。报告在
`/tmp/lka-public-rag-baseline.json`；查询延迟不包含导入/建索引。

| 数据集 | Recall@10 | all-hop recall | 查询 p95 ms | 失败 |
| --- | ---: | ---: | ---: | ---: |
| HotpotQA | 0.890 | 0.790 | 28.05 | 0 |
| 2WikiMultiHopQA | 0.6825 | 0.375 | 23.01 | 0 |
| MultiHop-RAG | 0.745 | 0.490 | 21.09 | 0 |
| MuSiQue | 0.425 | 0.055 | 32.73 | 0 |

MultiHop-RAG 标准化文件实际包含 528 条 query，虽然文件名含 `600`。
四组被标注 forbidden-leak count 均为 0；此指标只覆盖数据集标注的禁止文档，
不是通用隐私安全证明。

本地已缓存的 `BAAI/bge-small-zh-v1.5` 在相同英文 HotpotQA 20-query 切片：
`keyword` Recall@10 0.85、all-hop 0.70、p95 24.99 ms；`hybrid` 对应
0.775、0.55、56.97 ms。这个切片足以暴露当前中向量模型与英文语料不匹配的风险，
但不足以做普适模型排名；中文和全量对照仍需运行。

真实本地 `BAAI/bge-reranker-base`（FastEmbed/ONNX，预先显式下载到忽略提交的
`data/runtime/models`）在 HotpotQA 相同 seed 的 20-query 切片、top-10：
keyword Recall@10 0.85、all-hop 0.70、warm p95 25.99 ms；
keyword+rerank 0.95、0.90、warm p95 4146.53 ms（p50 3690.16 ms）。
重排模型预热约 3096.65 ms，均不含语料导入。该切片质量提升但延迟比约 160 倍，
未通过原定的 2 倍延迟质量门；后续按用户决策仍默认尝试重排，
但不能因此宣称达到低延迟目标。小样本不能外推全量。
原始机器报告位于 `/tmp/lka-hotpot-rerank.json`。
[模型发布页](https://huggingface.co/BAAI/bge-reranker-base)标注了中英适用和
MIT 许可；这不替代部署方对模型文件/依赖的许可审查。

## 验证与 review

- 知识服务/工具、本地重排、配置、child-agent、公开数据适配与评测、
  多 Agent 集成的 targeted pytest 均通过；`ruff check`（所改 Python 文件）和
  `git diff --check` 通过。
- 审核重点包括：子 Agent 检索证据回传、工具 scope 交集、隐私过滤发生在重排之前、
  缺模型和拥塞的快速降级、事件循环不运行同步检索。
- 所有新增文件与局部编辑保留了进入本轮前已有的脏工作区修改；未提交或推送。
- 本轮全量 `pytest -q` 为 **424 passed, 2 failed**：失败分别是
  `test_langgraph_cancelled_run_stops_before_context_or_llm` 的取消异常类型，
  与 `test_agent_turn_passes_request_llm_options_to_client` 的直接回答路径；
  两处均在本轮未编辑的 Agent turn/graph 路径，不归入 RAG 验收通过。
- 新增 RAG/ContextDriver/评测的定向回归为 **52 passed**；
  工作区 source policy 与文档多样性补充测试分别单独 **1 passed**；
  source ID 契约修复后的知识/子 Agent/ContextDriver 回归 **44 passed**；
  父 run 旧证据重载及多 Agent 集成 **11 passed**；
  最终组合回归（知识、工作区、ContextDriver、子 Agent、多 Agent、公开评测）
  **70 passed**；所改 Python 文件 Ruff 通过。

代表性验证命令（均使用仓库内 `.uv-cache`）：

```bash
uv --cache-dir .uv-cache run pytest -q tests/test_knowledge_semantic.py tests/test_knowledge_service.py tests/test_knowledge_tools.py tests/test_local_reranker.py tests/test_public_retrieval_benchmark.py tests/test_child_agent.py
# 36 passed
uv --cache-dir .uv-cache run pytest -q tests/test_multi_agent_integration.py tests/test_multi_agent_runtime_controls.py tests/test_agent_turn_fork.py
# 15 passed
uv --cache-dir .uv-cache run pytest -q tests/test_evals_smoke.py tests/test_public_dataset_adapter.py tests/test_public_retrieval_benchmark.py tests/test_rag_grounding.py
# 17 passed
git diff --check
```

## 未完成与上线门槛

- 已验证真实模型的小切片 warm 路径，但未完成全量、多语言、真实并发与 CPU/内存验收；
  重排推理本身无可强制中断的硬超时，不能以短排队超时代替推理时限。
- 持久化 workspace/session source scope 可用于 Agent 工具，但没有用户身份认证，
  直连知识管理 API 仍基于本机单用户信任，绝不等于多租户 ACL。
- ContextDriver 的证据预算/按需 resolver 已具备，父 Agent 工具结果已自动注入；
  答案 grounding 只能做结构/fixture 回归，
  不验证所有自然语言陈述是否被引文实际支持。
- MuSiQue all-hop 过低，不能宣称“内容可靠”。下一质量门应按中英语种与来源切片，
  对比 keyword/semantic/hybrid/rerank 的 Recall、all-hop、p95 和答案引文；
  默认开关是用户选择，不代表已满足质量与时延约束。

后续具体条目见 `rag_delivery_todolist.md`。本报告中的 benchmark 是当前工作区
与当前机器结果，不是跨硬件性能承诺。
