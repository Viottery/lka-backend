"""Local TOML configuration for providers and local model settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _is_env_var_name(value: str) -> bool:
    return (
        bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(character.isalnum() or character == "_" for character in value)
    )


def _dotenv_value(name: str, path: Path = Path(".env")) -> str | None:
    if not name or not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        return value.strip().strip('"').strip("'")
    return None


def _resolved_env_value(name: str) -> str | None:
    return os.getenv(name) or _dotenv_value(name)


class LLMClientConfig(BaseModel):
    name: str
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    default_model: str = "gpt-4.1-mini"
    available_models: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180
    supports_stream: bool = True
    supports_json_mode: bool = False
    supports_function_calling: bool = False
    supports_required_tool_choice: bool = False
    function_calling_strict: bool = False
    supports_reasoning_effort: bool = False
    thinking_control: Literal["deepseek"] | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    output_reserve_tokens: int = Field(default=8192, gt=0)
    tokenizer_json_path: Path | None = None
    model_overrides: dict[str, LLMModelConfigOverride] = Field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None


class LLMProviderConfig(BaseModel):
    provider: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 180
    thinking_control: Literal["deepseek"] | None = None
    supports_json_mode: bool = False
    supports_function_calling: bool = False
    default_client: str | None = None
    fallback_client: str | None = None
    default_response_mode: str = "json"
    max_attempts: int = 2
    clients: list[LLMClientConfig] = Field(default_factory=list)
    context_window_tokens: int | None = Field(default=None, gt=0)
    output_reserve_tokens: int = Field(default=8192, gt=0)
    tokenizer_json_path: Path | None = None
    model_overrides: dict[str, LLMModelConfigOverride] = Field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None

    def resolve_model_config(
        self, client_name: str | None, model: str
    ) -> LLMModelConfigOverride | None:
        """Resolve capacity, output reserve and tokenizer for an exact client/model.

        Model-specific entries never leak across models. Default-level fields apply only
        when `model` is that client's configured default model. Unknown overrides return
        `None`, allowing callers to handle capacity as unknown without guessing.
        """
        client = next((item for item in self.client_configs() if item.name == client_name), None)
        if client is None:
            return None
        override = client.model_overrides.get(model)
        if override is not None:
            base = self._default_model_config(client) if model == client.default_model else None
            return LLMModelConfigOverride(
                context_window_tokens=(
                    override.context_window_tokens
                    if override.context_window_tokens is not None
                    else (base.context_window_tokens if base else None)
                ),
                output_reserve_tokens=(
                    override.output_reserve_tokens
                    if override.output_reserve_tokens is not None
                    else (base.output_reserve_tokens if base else client.output_reserve_tokens)
                ),
                tokenizer_json_path=(
                    override.tokenizer_json_path
                    if override.tokenizer_json_path is not None
                    else (base.tokenizer_json_path if base else None)
                ),
            )
        if model != client.default_model:
            return None
        provider_fields = self.model_overrides.get(model)
        return self._default_model_config(client, provider_fields)

    def resolve_context_capacity(self, client_name: str | None, model: str) -> int | None:
        """Return configured context capacity for exactly this client/model, if known."""
        resolved = self.resolve_model_config(client_name, model)
        return resolved.context_window_tokens if resolved is not None else None

    def _default_model_config(
        self,
        client: LLMClientConfig,
        override: LLMModelConfigOverride | None = None,
    ) -> LLMModelConfigOverride | None:
        values = (
            override.context_window_tokens if override else None,
            override.output_reserve_tokens if override else None,
            override.tokenizer_json_path if override else None,
        )
        has_override = any(value is not None for value in values)
        has_client = (
            client.context_window_tokens is not None or client.tokenizer_json_path is not None
        )
        # Provider-level fields describe only the legacy single-client model.
        # Named clients must not inherit another model's capacity/tokenizer.
        has_provider = not self.clients and (
            self.context_window_tokens is not None or self.tokenizer_json_path is not None
        )
        if not (has_override or has_client or has_provider):
            return None
        return LLMModelConfigOverride(
            context_window_tokens=(
                (override.context_window_tokens if override else None)
                or client.context_window_tokens
                or (self.context_window_tokens if not self.clients else None)
            ),
            output_reserve_tokens=(
                (override.output_reserve_tokens if override else None)
                or client.output_reserve_tokens
                or (self.output_reserve_tokens if not self.clients else None)
            ),
            tokenizer_json_path=(
                (override.tokenizer_json_path if override else None)
                or client.tokenizer_json_path
                or (self.tokenizer_json_path if not self.clients else None)
            ),
        )

    def is_disabled(self) -> bool:
        if self.clients:
            return False
        return self.provider == "mock" or not self.resolved_api_key()

    def client_configs(self) -> list[LLMClientConfig]:
        if self.clients:
            return self.clients
        if self.provider == "mock":
            return []
        provider_type = (
            self.provider if self.provider in {"mock", "openai_compatible"} else "openai_compatible"
        )
        client_name = self.default_client or self.provider
        return [
            LLMClientConfig(
                name=client_name,
                provider=provider_type,
                base_url=self.base_url,
                api_key_env=self.api_key_env,
                default_model=self.model,
                available_models=[self.model],
                timeout_seconds=self.timeout_seconds,
                thinking_control=self.thinking_control,
                supports_json_mode=self.supports_json_mode,
                supports_function_calling=self.supports_function_calling,
                context_window_tokens=self.context_window_tokens,
                output_reserve_tokens=self.output_reserve_tokens,
                tokenizer_json_path=self.tokenizer_json_path,
                model_overrides=self.model_overrides,
            )
        ]


class LLMModelConfigOverride(BaseModel):
    """Optional model-scoped context and tokenizer metadata."""

    model_config = ConfigDict(extra="forbid")

    context_window_tokens: int | None = Field(default=None, gt=0)
    output_reserve_tokens: int | None = Field(default=None, gt=0)
    tokenizer_json_path: Path | None = None


class SafetyReviewConfig(BaseModel):
    tool_review_mode: str = "skip"
    manual_wait_poll_seconds: float = 0.5


class AgentInferenceProfile(BaseModel):
    """Server-owned child inference choice; never contains credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=100)
    client_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class AgentConfig(BaseModel):
    max_decision_steps: int = 10
    # Opt-in rollout: choose the first operation in ReAct, without a separate route call.
    unified_entry_enabled: bool = False
    orchestrator: Literal["legacy", "langgraph"] = "legacy"
    checkpoint_backend: Literal["memory", "sqlite"] = "sqlite"
    multi_agent_planning_enabled: bool = False
    # Test/demo workflow is inert unless explicitly enabled by local config.
    mock_workflow_agent_enabled: bool = False
    # Read-only mail specialist; automatic root routing is separate.
    mail_expert_enabled: bool = False
    # External Codex expert is disabled unless a trusted local executable is
    # explicitly selected. It runs in a disposable staged workspace.
    codex_expert_enabled: bool = False
    codex_binary_path: Path | None = None
    codex_workspace_base: Path | None = None
    codex_state_home: Path | None = None
    codex_home: Path | None = None
    codex_permission_mode: Literal["profile", "legacy"] = "profile"
    codex_model: str | None = None
    codex_reasoning_effort: Literal["low", "medium", "high"] | None = None
    # Opt in only to the conservative single-agent policy marker. Other fast
    # path templates require trusted selectors/evidence contracts not yet
    # exposed by the runtime.
    fast_path_single_agent_enabled: bool = False
    max_fork_depth: int = Field(default=2, ge=0)
    max_children: int = Field(default=8, ge=0)
    max_fork_size: int = Field(default=4, ge=1)
    multi_agent_max_concurrency: int = Field(default=2, ge=1, le=16)
    multi_agent_global_max_concurrency: int = Field(default=8, ge=1, le=64)
    multi_agent_max_retries: int = Field(default=1, ge=0, le=5)
    inference_profiles: tuple[AgentInferenceProfile, ...] = ()
    allowed_child_inference_profile_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def inference_profiles_are_unambiguous_and_allowlisted(self) -> AgentConfig:
        if self.codex_expert_enabled and (
            self.codex_binary_path is None or not self.codex_binary_path.is_absolute()
        ):
            raise ValueError("Enabled Codex expert requires an absolute codex_binary_path.")
        if self.codex_expert_enabled and (
            self.codex_workspace_base is None or not self.codex_workspace_base.is_absolute()
        ):
            raise ValueError("Enabled Codex expert requires an absolute codex_workspace_base.")
        if self.codex_state_home is not None and not self.codex_state_home.is_absolute():
            raise ValueError("codex_state_home must be an absolute path when configured.")
        if self.codex_home is not None and not self.codex_home.is_absolute():
            raise ValueError("codex_home must be an absolute path when configured.")
        profile_ids = tuple(profile.profile_id for profile in self.inference_profiles)
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("Agent inference profile IDs must be unique.")
        if not set(self.allowed_child_inference_profile_ids).issubset(profile_ids):
            raise ValueError("Allowed child inference profiles must be configured.")
        return self


class OutlookMailConfig(BaseModel):
    enabled: bool = False
    auth_method: str = "device_code"
    client_id: str = ""
    client_id_env: str = "MS_GRAPH_CLIENT_ID"
    tenant_id: str = "consumers"
    scopes: list[str] = Field(default_factory=lambda: ["User.Read", "Mail.Read", "offline_access"])
    token_store_path: Path = Path("./data/secrets/outlook_token.json")
    sync_folder: str = "Inbox"
    download_attachment_content: bool = False
    startup_sync_enabled: bool = True
    background_sync_enabled: bool = True
    sync_interval_seconds: int = 300
    sync_limit: int = 25
    sync_max_pages: int = 1

    def resolved_client_id(self) -> str | None:
        if self.client_id.strip():
            return self.client_id.strip()
        configured_value = self.client_id_env.strip()
        if not configured_value:
            return None
        env_value = _resolved_env_value(configured_value)
        if env_value:
            return env_value
        if not _is_env_var_name(configured_value):
            return configured_value
        return None


class IMAPMailConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 993
    username: str = ""
    password_env: str = "MAIL_IMAP_PASSWORD"
    use_ssl: bool = True

    def resolved_password(self) -> str | None:
        return _resolved_env_value(self.password_env) if self.password_env else None


class MailProviderConfig(BaseModel):
    outlook: OutlookMailConfig = Field(default_factory=OutlookMailConfig)
    imap: IMAPMailConfig = Field(default_factory=IMAPMailConfig)


class EmbeddingConfig(BaseModel):
    enabled: bool = True
    provider: str = "fastembed"
    index_provider: str = "sqlite_vec"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    dimensions: int = 512
    device: str = "auto"
    cache_dir: Path = Path("./data/models")
    batch_size: int = 16
    normalize_embeddings: bool = True
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    default_retrieval_mode: str = "keyword"
    auto_index_on_import: bool = False
    local_files_only: bool = True


class RerankerConfig(BaseModel):
    enabled: bool = True
    model_name: str = "BAAI/bge-reranker-base"
    cache_dir: Path = Path("./data/runtime/models")
    batch_size: int = 8
    max_candidates: int = 30
    max_query_chars: int = 1000
    max_candidate_chars: int = 1800
    max_concurrent_inferences: int = 1
    queue_timeout_ms: int = 25
    local_files_only: bool = True


class QueryRewriteConfig(BaseModel):
    max_rewrites: int = Field(default=8, ge=1, le=16)
    max_total_chars: int = Field(default=2400, ge=300, le=4800)
    max_parallel_searches: int = Field(default=4, ge=1, le=8)
    max_total_candidates: int = Field(default=240, ge=30, le=800)


class WebSearchConfig(BaseModel):
    provider: Literal["brave"] = "brave"
    api_key_env: str = "BRAVE_SEARCH_API_KEY"
    monthly_request_limit: int = Field(default=900, ge=0, le=1_000_000)
    snapshot_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    page_max_age_seconds: int = Field(default=300, ge=0, le=86400)
    cache_max_entries: int = Field(default=256, ge=1, le=4096)
    cache_max_bytes: int = Field(default=64_000_000, ge=1_000_000, le=512_000_000)
    page_preview_chars: int = Field(default=2400, ge=400, le=4000)
    search_snippet_chars: int = Field(default=360, ge=120, le=500)
    max_parallel_requests: int = Field(default=4, ge=1, le=16)
    max_pending_requests: int = Field(default=16, ge=1, le=64)
    request_timeout_seconds: float = Field(default=12, ge=1, le=60)

    def resolved_api_key(self) -> str | None:
        return _resolved_env_value(self.api_key_env) if self.api_key_env else None


class MemoryConfig(BaseModel):
    background_worker_count: int = Field(default=2, ge=1, le=8)
    enabled: bool = True
    background_enabled: bool = True
    # Controls whether background learning spends an additional model call.
    # Enabling memory/background processing authorizes use of conversation data;
    # this flag is a workload/quality choice, not a privacy-consent gate.
    allow_remote_extraction: bool = True
    extraction_context_messages: int = Field(default=12, ge=2, le=40)
    extraction_context_chars: int = Field(default=12_000, ge=2000, le=24_000)
    auto_publish_min_confidence: float = Field(default=0.85, ge=0.5, le=1.0)
    reconciliation_max_items: int = Field(default=48, ge=8, le=100)
    reconciliation_max_chars: int = Field(default=16_000, ge=2000, le=64_000)
    consolidation_enabled: bool = True
    consolidation_debounce_seconds: float = Field(default=60, ge=0, le=3600)
    consolidation_interval_seconds: float = Field(default=21_600, ge=60, le=604800)
    consolidation_batch_items: int = Field(default=24, ge=2, le=48)
    consolidation_min_confidence: float = Field(default=0.9, ge=0.85, le=1.0)
    max_recalled_items: int = Field(default=8, ge=0, le=30)
    max_recalled_chars: int = Field(default=2400, ge=0, le=12000)
    extraction_debounce_seconds: float = Field(default=5, ge=0, le=300)
    # Zero removes the ordinary cumulative allowance; emergency fuses remain.
    max_job_tokens: int = Field(default=0, ge=0, le=500_000)
    generation_output_tokens: int = Field(default=4096, ge=256, le=131_072)
    recovery_output_tokens: int = Field(default=8192, ge=256, le=131_072)
    background_client_name: str | None = None
    background_model: str | None = None


class MessageModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=100)
    input_cost_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_cost_per_million: float = Field(ge=0, allow_inf_nan=False)


class MessageHistoryConfig(BaseModel):
    """Opt-in per-conversation analysis; no platform credentials live here."""

    schema_version: Literal[1] = 1
    # New analysis is opt-in; enabling a pipeline does not grant any source.
    reading_algorithm: Literal["legacy", "compact", "selected"] = "legacy"
    participant_pool_capacity: int = Field(default=30, ge=1, le=100)
    participant_pinned_capacity: int = Field(default=10, ge=0, le=20)
    profile_cold_days: int = Field(default=14, ge=1, le=90)
    profile_retention_days: int = Field(default=30, ge=1, le=365)
    selector_max_messages: int = Field(default=40, ge=1, le=200)
    selector_exploration_fraction: float = Field(default=0.1, ge=0, le=1)
    enabled: bool = True
    background_enabled: bool = True
    worker_count: int = Field(default=1, ge=1, le=4)
    max_job_tokens: int = Field(default=32_768, ge=8192, le=500_000)
    max_job_calls: int = Field(default=4, ge=1, le=1000)
    max_input_tokens: int = Field(default=8192, ge=1024, le=131_072)
    service_hourly_token_limit: int = Field(default=40_000, ge=0)
    service_daily_token_limit: int = Field(default=200_000, ge=0)
    service_hourly_call_limit: int = Field(default=8, ge=0)
    service_daily_call_limit: int = Field(default=48, ge=0)
    conversation_hourly_token_limit: int = Field(default=20_000, ge=0)
    conversation_daily_token_limit: int = Field(default=80_000, ge=0)
    conversation_hourly_call_limit: int = Field(default=4, ge=0)
    conversation_daily_call_limit: int = Field(default=24, ge=0)
    due_scan_limit: int = Field(default=32, ge=1, le=200)
    yield_delay_seconds: float = Field(default=1, ge=0, le=60)
    max_recovery_restarts: int = Field(default=1, ge=0, le=3)
    fragment_recovery_enabled: bool = False
    model_prices: list[MessageModelPricing] = Field(default_factory=list, max_length=100)
    # Bounds the encoded raw-message array, not the full prompt. Bounded prior
    # summaries/evidence are additional; LLMService enforces model capacity.
    input_chunk_bytes: int = Field(default=12_000, ge=1024, le=32_768)
    generation_output_tokens: int = Field(default=2048, ge=256, le=32_768)
    recovery_output_tokens: int = Field(default=4096, ge=256, le=32_768)
    background_client_name: str | None = None
    background_model: str | None = None

    @model_validator(mode="after")
    def unique_model_prices(self):
        keys = [(row.client_name, row.model) for row in self.model_prices]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate message model pricing")
        return self


class BackgroundConfig(BaseModel):
    request_timeout_seconds: float = Field(default=30, gt=0, le=180)
    max_pending_jobs: int = Field(default=1024, ge=1, le=100000)
    max_llm_concurrency: int = Field(default=4, ge=1, le=32)
    interactive_reserved: int = Field(default=2, ge=1, le=32)
    memory_concurrency: int = Field(default=1, ge=1, le=8)
    message_concurrency: int = Field(default=1, ge=1, le=8)
    io_concurrency: int = Field(default=1, ge=1, le=8)
    hourly_token_limit: int = Field(default=200_000, ge=0)
    daily_token_limit: int = Field(default=1_000_000, ge=0)
    daily_cost_limit: float = Field(default=0, ge=0)
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    memory_task_fuse_tokens: int = Field(default=262_144, ge=1)
    memory_hourly_fuse_tokens: int = Field(default=2_000_000, ge=1)
    memory_daily_fuse_tokens: int = Field(default=10_000_000, ge=1)

    @model_validator(mode="after")
    def reservation_fits(self):
        if self.interactive_reserved >= self.max_llm_concurrency:
            raise ValueError("interactive_reserved must leave at least one background slot")
        if self.daily_cost_limit and not (self.input_cost_per_million or self.output_cost_per_million):
            raise ValueError("cost limit requires configured model prices")
        return self


class LocalAppConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    safety: SafetyReviewConfig = Field(default_factory=SafetyReviewConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mail: MailProviderConfig = Field(default_factory=MailProviderConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    query_rewrite: QueryRewriteConfig = Field(default_factory=QueryRewriteConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    message_history: MessageHistoryConfig = Field(default_factory=MessageHistoryConfig)
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)

    @model_validator(mode="after")
    def inference_profiles_match_llm_clients(self) -> LocalAppConfig:
        clients = {client.name: client for client in self.llm.client_configs()}
        messages = self.message_history
        message_client_name = messages.background_client_name or self.llm.default_client
        message_client = clients.get(message_client_name) if message_client_name else next(iter(clients.values()), None)
        if messages.background_client_name and message_client is None:
            raise ValueError("Message analysis references an unknown LLM client")
        if messages.background_model and (
            message_client is None
            or messages.background_model not in [message_client.default_model, *message_client.available_models]
        ):
            raise ValueError("Message analysis model is unavailable for its client")
        for profile in self.agent.inference_profiles:
            client = clients.get(profile.client_name)
            if client is None:
                raise ValueError(
                    f"Inference profile references an unknown LLM client: {profile.profile_id}"
                )
            models = client.available_models or [client.default_model]
            if profile.model not in models:
                raise ValueError(
                    f"Inference profile model is unavailable for its client: {profile.profile_id}"
                )
            if profile.reasoning_effort is not None and not client.supports_reasoning_effort:
                raise ValueError(
                    f"Inference profile reasoning effort is unsupported: {profile.profile_id}"
                )
        return self


def load_local_config(path: Path) -> LocalAppConfig:
    """Load local TOML config. Missing files intentionally fall back to defaults."""

    expanded_path = path.expanduser()
    if not expanded_path.exists():
        return LocalAppConfig()
    with expanded_path.open("rb") as config_file:
        payload = tomllib.load(config_file)
    return LocalAppConfig.model_validate(payload)
