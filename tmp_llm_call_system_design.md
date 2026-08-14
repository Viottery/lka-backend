# LLM 调用系统临时设计笔记

## 目标

把当前 `app/core/llm.py` 中的单一同步 provider helper，升级为应用级、用户可配置、可切换、
支持异步与流式输出的 LLM 调用系统。

这份文件是实现前的临时思路整理，不是最终架构文档。

## 关键原则

- Model 不应写死在代码里，也不应只由后端启动配置固定。
- 用户应能在运行时切换当前会话或当前请求使用的 client / model。
- Provider client 只负责调用外部模型；重试、fallback、请求模式、事件记录由应用级
  `LLMService` 统一处理。
- Agent Harness 不直接依赖具体 provider。
- LLM 调用应使用 async API，避免阻塞 FastAPI event loop。
- 对用户可见的长输出支持 streaming；对会触发工具调用的 JSON decision 阶段必须先收完整
  内容并解析通过，不能根据半截流式输出执行工具。

## 建议文件结构

```text
app/core/llm/
  __init__.py
  models.py
  errors.py
  client.py
  registry.py
  service.py
  openai_compatible.py
  mock.py

app/core/agent_runs.py
app/api/routes/agent.py
app/core/runtime.py
app/core/local_config.py
config/local.example.toml
```

说明：

- `models.py`：`LLMRequest`、`LLMMessage`、`LLMResponse`、`LLMStreamEvent`、
  `LLMResponseMode`。
- `errors.py`：保留并扩展当前错误分类。
- `client.py`：定义 `AsyncLLMClient` 协议。
- `registry.py`：注册 provider factory，按配置创建 named clients。
- `service.py`：统一完成 client 选择、model override、response mode、retry、fallback、
  stream event 归一。
- `openai_compatible.py`：OpenAI-compatible chat completions non-stream / stream 实现。
- `mock.py`：测试稳定的 async mock client。
- `agent_runs.py`：后续支持后台 run、事件队列、SSE 消费。

## 配置方向

```toml
[llm]
default_client = "packyapi"
fallback_client = "mock"
default_response_mode = "json"
max_attempts = 2

[[llm.clients]]
name = "packyapi"
provider = "openai_compatible"
base_url = "https://www.packyapi.ai/v1"
api_key_env = "PACKY_API_KEY"
default_model = "deepseek-v4-flash"
available_models = ["deepseek-v4-flash", "gpt-4.1-mini"]
timeout_seconds = 60
supports_stream = true
supports_json_mode = false

[[llm.clients]]
name = "mock"
provider = "mock"
default_model = "mock"
available_models = ["mock"]
supports_stream = true
```

运行时请求可覆盖：

```json
{
  "session_id": "session_001",
  "user_input": "...",
  "llm": {
    "client_name": "packyapi",
    "model": "deepseek-v4-flash",
    "response_mode": "stream"
  }
}
```

会话级偏好可以后续存入 session metadata：

```json
{
  "llm_client_name": "packyapi",
  "llm_model": "deepseek-v4-flash"
}
```

优先级建议：

```text
request override
  -> session preference
  -> config default_client/default_model
  -> fallback_client
```

## Stream 回复处理

分两层：

1. Provider stream：解析 OpenAI-compatible `data:` chunks，归一成 `LLMStreamEvent`。
2. Agent event stream：面向前端输出标准事件，包括 progress、LLM delta、tool start/end、
   final answer、run completion。

SSE 事件形状：

```text
event: run_started
data: {"run_id":"...","session_id":"..."}

event: llm_started
data: {"stage":"answer","client_name":"packyapi","model":"deepseek-v4-flash"}

event: llm_delta
data: {"stage":"answer","delta":"发票"}

event: tool_started
data: {"tool_name":"mail.search","input":{"query":"..."}}

event: tool_completed
data: {"tool_name":"mail.search","status":"completed"}

event: final_answer
data: {"answer":"..."}

event: run_completed
data: {"trace_id":"...","log_path":"..."}
```

处理规则：

- `route`、`decision`、`decision_repair`、`tool_result_check`、`context_summarize` 第一版默认
  non-stream async 调用。
- `answer` 阶段可以 stream token 给用户。
- 如果未来对 decision 阶段启用 provider stream，也只能用于调试展示；必须等完整 JSON 拼接、
  解析和 schema 校验通过后，才能执行工具。
- stream 中途失败时：
  - decision 类阶段 fail closed，不执行工具。
  - answer 阶段记录 partial content 和 warning，并尝试按策略 fallback 或结束。
  - run log 仍记录完整事件和已收集的 partial content。

## 实施顺序

1. 增加 async LLM 核心包和配置模型，保持现有 `/agent/turn` 行为不变。
2. 将 `AgentTurnLoop` 的 `_complete_text_with_retry` 迁移到 `LLMService`，保留事件记录。
3. 增加 request/session 级 `client_name` 和 `model` 覆盖能力。
4. 增加 `/agent/turn/stream` SSE，第一版只对最终回答阶段输出 token delta。
5. 增加后台 run manager，让前端可创建 run 后通过事件流持续消费进度。
