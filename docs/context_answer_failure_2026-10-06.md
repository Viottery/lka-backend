# 普通聊天空正文排查与修复（2026-10-06）

## 本轮证据

- trace：`agent_turn_dc038ca7b109`；run：`agent_run_f26d0163a5ee`。
- Windows 日志：`data/runtime/agent_logs/agent_turn_dc038ca7b109.md`。
- 用户原始输入为对话结束前的闲聊。route 正常完成，明确选择无需工具、直接按上下文回答。
- `context_answer` 流式调用约 7.7 秒，供应商报告 `finish_reason=stop`、
  `completion_tokens=302`，但后端收集到的 `content_length=0`、`output=""`。
- 后端随后生成旧的本地兜底文字“我还没有为这个请求选择到可执行工具……”，
  写入最终答案及 `final_answer` 事件。这与用户看到的文本一致，问题不是前端丢失 Markdown 或正文。
- 日志没有原始 SSE 帧，无法进一步确定供应商只产生了推理内容、返回空正文，还是存在其他上游协议问题；
  token 使用量不证明已经生成可展示的完整回答。

## 修复

`_answer_from_context_with_llm` 原先直接使用 `_complete_text_with_retry`，
对空正文返回 `None`，继而误用“没有工具”的旧兜底。它现在复用既有
`_complete_answer_with_recovery`：检查正文、完成状态与截断标志，必要时仅恢复一次，
遵守原输出与子任务预算；支持思考开关的已选客户端可在恢复时关闭思考。
恢复收集为完整快照，沿用前端最终答复替换机制。仍失败时记录生成失败，
不发送虚假的成功最终答复，也不把供应商错误伪装成缺少工具。

不新增角色、领域或工具包的核心路由特殊规则。不修改这轮历史答案或记忆。

## 验证与上线

- 定向验证：`test_answer_generation_recovery_quality.py` 加上下文跟进与供应商流错误用例，
  **29 passed，12.68 秒**，包含空正文/截断、stream/text、恢复耗尽及失败审计。
- 扩展组合最初为 68 passed、2 failed。流错误用例原先期望空生成仍返回成功，
  已更新为校验真实失败状态及保留供应商审计，并定向通过。
  另一项既有 `test_langgraph_decision_event_step_matches_working_set_step` 选择 mail 路径、
  无生成客户端，预期事件步骤与当前生成失败事件不符；本次未修改该路径或用例。
- 生产只先部署上下文回复修复，备份位于 Windows
  `data/deployment_backups/context-answer-20261005T191226707760Z/`。
  随后同一安装收到另一批更新，保留全部新文件，并重新核对完整启动清单。
- 最终后端 PID `27260`；健康指纹
  `1084e16e1896e179a6aa48685709d88bde2a018375549c08621c189b1fa8e079`，
  与当前 160 文件清单一致，普通聊天修复仍在。
- 前端 PID `20620` 保持不变，QQ `connected`、同步 `idle`、待同步 `0`、错误为空。
  未停止前端、QQ 或桌宠，也未重放用户的真实对话。

回滚应针对本次上下文方法的差异处理，避免用旧目录覆盖随后收到的新代码。
