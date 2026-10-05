# P1 / P2 可用性优化执行记录（2026-10-05）

实施队列：[详细 TODO](linux_usability_p1_p2_todolist_2026-10-05.md)。
仅记录本轮实际执行，不覆盖历史报告中的失败或把离线测试当真实模型闭环。
用户要求减少无关测试；不运行全量 suite、不新增高开销模型。

## PRE-00 / P0

- 基线提交：`1bf5f3c`。现有消息、Windows、workload 等 dirty 改动属于用户，未纳入本轮提交。
- 使用项目 `.venv/bin/pytest`，不是系统 Python；未重启 8765、未更改生产库。
- P0 单批八个代表实例：Bash 实际 READ 拒绝写与普通读、继承 shell hook、条件只读发现
  与实际调用、native 无控制动作、SSE 无终态恢复、执行前拒绝、执行后失败审计。
- 结果：**8 passed / 1.54s**。未扩展安全矩阵、未调用远程模型。
- 旧 workload 15/11 列测试不属于本轮切片；未重复运行或修改用户功能。

## UX-01：降级后的历史 partial 与说明

场景：一项子任务已有 partial 后接受 skip/degrade，另一项仍失败；旧最终回答过滤了
前者，错误称未取得结果，并丢失降级说明。

改进：从服务端接受的 patch history 和真实 parent 子任务事件恢复交付用历史。
核对 plan/session/parent/correlation/child/attempt，校验 patch snapshot hash；
历史只用于交付，不改变 canonical aggregate、验证状态或失败门槛。
历史摘要和当前摘要共享原 5500 字符公平预算，明确历史、未独立核验与缺失义务。
mixed/all-skipped recovery 也携带独立 historical_task_results，不作为当前完成结果。

验证：

- 原显式复现 + 相邻 partial 交付：**19 passed / 0.60s**，原三红例转绿。
- 新增默认回归 `tests/test_degraded_partial_delivery_quality.py`：**10 passed / 0.45s**。
- 当前切片 Ruff：通过。

状态：implemented / offline_verified；未进行真实远程模型重放，不标 live_verified。
持久原件、失败状态、用户改动均保留。后续独立审查发现问题在本记录继续追加。
