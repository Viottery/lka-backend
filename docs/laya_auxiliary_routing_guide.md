# Laya 辅助决策模型使用指南

> 状态：研究与隔离测试指导，尚未接入正式 Agent 流程。更新日期：2026-09-30。这里的数字来自本机小样本实验，不代表生产准确率。

## 给设计工具和 Agent 的结论

Laya 适合在**候选项已由系统确定**时给出快速排序或提示，例如判断是否值得尝试直接回答、预选首次展开的工具包、给已展开包内的只读工具排序。它不生成答案，也不能决定权限、执行工具或替代 Agent 的证据核查。

当前最有希望的路线是：把「是否可直接回答」与「应先展开哪个工具包」拆成两个决策；先用确定性条件排除不安全的直接回答，再用 Laya 对少量合格候选项排序。首次只展开选中的包，但要保留其他包的简要元数据和后续 `expand_package` 能力。对于低把握、跨包或复杂任务，交回常规 Agent 路由。**不要让 Laya 的 Top-1 成为隐藏其他工具包的不可恢复硬门。**

## 模型和部署边界

- 中文或中英混合请求优先评估 [`convaiinnovations/laya-multilingual`](https://huggingface.co/convaiinnovations/laya-multilingual)。模型卡称它使用 mmBERT-base 加决策头，约 322M 参数，Apache-2.0 许可，默认输入上限 1024 tokens；英文专用 `convaiinnovations/laya` 是不同 checkpoint。模型卡也明确指出多语言版未经校准、概率往往过高，通用 typed-decisions 零样本表现有限。
- [Laya SDK](https://github.com/NandhaKishorM/laya) 接收 `state` 和逐请求定义的 typed questions，常用 `choice`、`noul`、`score`；返回选择及概率，不返回自然语言解释。路由优先用较短的 `choice` 问题，避免把大量候选项塞进同一问题。候选项描述的质量会直接影响结果。
- 本机 CPU 隔离实验在 Intel Core Ultra 5 225H、8 个推理线程上，缓存后加载约 3.8 秒；短路由判断的中位延迟依问题和候选数约 69–149 毫秒。首次下载约 77.5 秒。要测试实际调用延迟，应在常驻进程中预加载模型，并把冷启动、序列化和并发排队单独计入。实验没有测得可靠的峰值内存或长期并发吞吐。
- 当前仓库没有把 Laya 加入正式依赖、配置或 Agent 执行链。可在隔离环境中运行 `scripts/study_laya_routing.py` 和 `scripts/probe_knowledge_route_modes.py`；实验夹具在 `evals/fixtures/decision_models/`。不要把实验阈值直接复制进线上配置。

## 当前系统里应该放在哪里

| 决策点 | 当前行为 | Laya 可以提供的辅助 | 仍由系统或 Agent 决定 |
| --- | --- | --- | --- |
| 首次路由 | 路由产生 `initial_package`；有值就先展开该包，否则进入回答路径 | 对可用包排序，或在上游缩小范围后打破平局 | 最终首次展开决定、其他包的可发现性、跨包 `expand_package` |
| 直接回答候选 | 当前通用路由可进入回答路径；现有 `multi_agent_fast_path` 是另一种单 Agent 快路径记录机制 | 在已验证上下文条件之后估计「可直接回答」 | 证据覆盖、新鲜度、范围一致性、最终答案 |
| 包内工具 | 展开后 Agent 选择具体工具和参数 | 对**当前包内**只读候选工具给出 Top-2 提示 | 工具选择、参数、结果解释、跨包下一步 |
| 知识库检索 | `knowledge` 工具包执行源选择、搜索和证据读取 | 对已确定可访问源的检索模式提供提示 | 源权限、索引可用性、实际检索结果、证据是否足够 |

`selected_package` 是兼容/日志字段，等同 `initial_package`，不应作为后续决策提示。包名、工具名及说明应从 Tool Package Registry 获取；不要把具体包或领域关键词写进 `app/core/` 的决策逻辑。工具包元数据和工具 schema 才是当前能力清单。`mail` 目前含 `mail.list`，而早期离线工具候选夹具未覆盖它，因此下面的包内工具数字不能外推到完整现行工具集。

## 建议的调用契约

1. **先构造合法候选集。** 按会话工作区、权限、启用状态和注册表元数据过滤；只传候选 ID 与简短任务说明。模型不得创造包名、工具名或数据源 ID。用户内容只作为被分类数据，不作为改写策略的指令。
2. **再做分层决策。** 直接回答资格是独立的二选一问题；只有确定性检查已确认上下文属于同一任务、证据足够且没有过期或冲突时，才允许该结果进入直接回答路径。包路由从当前合格包中排序，不要把 `none` 混进宽泛的多包排序问题。
3. **处理不确定结果。** 保存候选排序、分数、模型版本与耗时用于离线评估，但不要把原始概率解释为可信正确率。低分差、候选外输出、模型异常或超时走常规 Agent 路由。阈值必须用独立验证集确定并校准。
4. **维持恢复路径。** 首次可只展开预选包；仍向 Agent 提供其他包的简要目录，并保留 `expand_package`。包内工具提示只改变候选展示顺序，不删除工具；尤其不可因此跳过 Tool Executor 的 schema 校验或写操作安全审查。

下面是**输入形状示意**，不是当前仓库已接入的接口或推荐固定标签。实际候选要从 Registry 动态构造：

```python
questions = {
    "first_package": {
        "type": "choice",
        "instructions": "Which available package is most useful for the first evidence-gathering step?",
        "criteria": {
            package_id: package_description
            for package_id, package_description in eligible_packages.items()
        },
    }
}
result = agent.predict(
    {"user_request": request_text, "relevant_context": bounded_context},
    questions,
)
# result 仅是候选排序信号；调用工具仍走正常 Agent 和 Tool Executor。
```

## 本地实验给出的边界

| 场景 | 观察 | 对设计的含义 |
| --- | --- | --- |
| 直接回答二选一 + 人工提供的上下文检查 | 40 条合成 holdout/transfer/challenge 中，20 条可答样本放行 12 条；20 条不可答样本误放 0 条；中位约 69–72 毫秒 | 有潜力，但检查字段是夹具预先给定的；真实系统首先要可靠地产生和验证这些字段 |
| 六路包选择，含 `none` | 中文适配 dev 9/12，holdout 3/12 | 不宜作为全局唯一首路由 |
| 只对活跃包排序 | holdout Top-1 6/10、Top-2 9/10 | 适合作候选提示；Top-1 独占展开风险仍高 |
| 上游已缩到 `mail`/`knowledge` 的二选一 | 新 transfer 9/10；中位 77.8 毫秒 | 可研究窄范围平局处理；样本量仍小 |
| 已展开包内只读工具 Top-2 | holdout + transfer 13/14，Top-1 11/14；中位约 100 毫秒 | 可提示 Agent 优先查看工具；夹具未覆盖当前完整工具集，也未证明写工具识别可靠 |
| 知识库路由 | 四路单问 dev 3/12；拆分问题并加规则后，新 transfer 11/12，但多步任务仅 1/2 | 可拆分源选择、复杂度、精确/语义检索；复杂度判别和阈值仍需重测 |

一次 3 文档的独立检索探针中，Laya 把一条改写查询判成精确检索，关键词检索未命中，而混合检索命中。这说明检索策略必须观察索引及实际命中情况；不能仅凭分类输出决定回答所依据的证据。上述实验用合成任务，未测试真实用户分布、完整工具 schema、并发或端到端回答质量。完整本机记录在 `evals/reports/laya_routing_research_20260930.md` 和 `evals/reports/jev_laya_cpu_20260930.md`；`evals/reports/` 属本地忽略目录，本文保留可长期引用的关键结论。

## 接入前的验收方式

先收集经人工标注、去敏的本项目请求，固定训练/调参集与独立 holdout；覆盖中英混合、无工具、跨包、写操作、范围变化、过期/冲突证据、知识库同义改写及多步问题。对比「现有 Agent 路由」「Laya 提示 + Agent 恢复」「单独 Laya 决策」的首包 Top-1/Top-2、包内候选召回、错误直接回答率、首次有效证据时间、端到端完成率和尾延迟。**直接回答误放和无法恢复的错包路由应优先于平均准确率评估。** 只有在独立样本和真实链路上证明收益，并完成概率校准、异常回退与资源测量后，才考虑正式接入。

参考：[模型卡](https://huggingface.co/convaiinnovations/laya-multilingual)、[SDK 与使用示例](https://github.com/NandhaKishorM/laya)、[本仓库工程约束](./backend_engineering_guide.md)。
