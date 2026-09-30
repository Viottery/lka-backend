# Codex 专家执行器（实验性）

Codex 在这里是独立的外部专家执行器，不是通用 ReAct Agent 的工具包。Planner 可以通过统一的 Child Run/ContextSnapshot 合同选择它；外层运行时负责权限裁剪、审批队列、取消和结果汇总。Codex 的原生通知与请求完整写入本地私有轨迹库，普通运行事件只保留脱敏摘要。

在 `config/local.toml` 的 `[agent]` 下显式设置 `codex_expert_enabled = true`、`codex_binary_path` 和 `codex_workspace_base`。这些路径必须是绝对路径；暂存目录必须位于源 Git 工作区之外。`codex_permission_mode = "profile"` 是默认值：每个子进程获得唯一命名 profile，默认拒绝读取宿主文件，仅允许 Codex 可执行文件与最小运行时只读、当前隔离副本可写，网络命令默认不可用；thread 响应必须确认激活了请求的 profile。旧版 CLI 可显式选 `"legacy"`，但它必须支持受限 `readOnlyAccess`，否则 fail closed。可选 `codex_state_home` 将 SQLite 状态放到私有可写目录；可选 `codex_home` 指定独立私有 profile，不复制现有登录凭据，须单独认证。在受限的外层沙箱内，只有两者都可写时 Codex 才能完成启动握手；普通宿主环境可以保留已登录的默认 Codex home。Windows ACL 私密性尚未验证。可选 `codex_model` 和 `codex_reasoning_effort`。专家默认关闭，不要求普通单 Agent 用户安装 Codex。

当前执行流程为：从一个 Git 工作区复制受限文件到隔离暂存目录，启动独立 `codex app-server` 进程，在该目录开启 thread/turn，并把原生事件按 Child Run 顺序持久化。进程只继承基本操作系统环境变量，不继承后端 API 密钥。源工作区不会被 Codex 自动修改。Codex 完成后，文件改动及文本 diff 记录于私有轨迹，结果为 `partial`，提示人工检查与应用；目前没有受控的应用接口，不能把 `ChildRun.completed` 解释为“源文件已交付”。

文件改动审批只有在所请求的 `grantRoot` 落在隔离副本内时才进入现有 FIFO 安全审批队列，并回复给同一个 Codex turn。命令升级、网络/额外权限、用户输入及 MCP elicitation 仍拒绝或中断。取消会请求中断外部 turn；运行时重启后不尝试恢复旧进程或旧审批。原生轨迹只能经受信任的 `Runtime.list_codex_native_trace` 内部接口读取，不能经普通 Agent API 返回，因为其中可能包含源码、提示词和命令输出。

本机 Codex CLI `0.155.0-alpha.16.3` 的 `readOnlyAccess` 旧字段已不可用；适配器现走命名 profile，并在可丢弃 Git 仓库通过了真实 HTTP→Planner→ChildRun→Codex 只读及暂存写入测试。真实 `command/exec` 也验证了暂存区内读写可用、区外文件与默认 Codex 登录凭据不可读。运行可选的真实集成测试前需先在本机登录 Codex，然后设置 `LKA_CODEX_LIVE_BINARY` 为本机 Codex 可执行文件的绝对路径，运行 `uv run pytest -q tests/test_codex_api_integration.py::test_live_codex_expert_runs_through_agent_http_api`。普通测试不依赖登录，会跳过这两个 live case。

这些验证只证明当前 CLI/宿主组合下的命令沙箱边界；Codex app-server 进程自身、模型服务及其他非命令能力不受同一 profile 的文件/网络规则约束。仍需完成独立进程隔离与跨平台验证、受控的改动预览/应用、权限化轨迹导出/保留策略和重启恢复。以上完成前不要把适配器视为生产就绪。
