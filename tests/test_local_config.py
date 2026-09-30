from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.local_config import (
    AgentConfig,
    AgentInferenceProfile,
    LLMClientConfig,
    LLMProviderConfig,
    LocalAppConfig,
    load_local_config,
)


def test_missing_local_config_uses_safe_defaults(tmp_path):
    config = load_local_config(tmp_path / "missing.toml")

    assert config.llm.provider == "mock"
    assert config.mail.outlook.auth_method == "device_code"
    assert config.mail.outlook.scopes == ["User.Read", "Mail.Read", "offline_access"]
    assert config.mail.outlook.startup_sync_enabled is True
    assert config.mail.outlook.background_sync_enabled is True
    assert config.mail.outlook.sync_interval_seconds == 300
    assert config.mail.outlook.sync_limit == 25
    assert config.mail.outlook.sync_max_pages == 1
    assert config.embedding.provider == "fastembed"
    assert config.embedding.index_provider == "sqlite_vec"
    assert config.embedding.model_name == "BAAI/bge-small-zh-v1.5"
    assert config.embedding.default_retrieval_mode == "keyword"
    assert config.reranker.enabled is True
    assert config.reranker.local_files_only is True
    assert config.query_rewrite.max_rewrites == 8
    assert config.query_rewrite.max_parallel_searches == 4
    assert config.agent.orchestrator == "legacy"
    assert config.agent.checkpoint_backend == "sqlite"
    assert config.agent.mock_workflow_agent_enabled is False
    assert config.agent.codex_expert_enabled is False
    assert config.agent.codex_state_home is None
    assert config.agent.codex_home is None
    assert config.agent.codex_permission_mode == "profile"
    assert config.agent.inference_profiles == ()
    assert config.agent.allowed_child_inference_profile_ids == ()


def test_enabled_codex_requires_explicit_absolute_process_and_workspace_paths(tmp_path):
    with pytest.raises(ValidationError, match="codex_binary_path"):
        AgentConfig(codex_expert_enabled=True)
    with pytest.raises(ValidationError, match="codex_workspace_base"):
        AgentConfig(codex_expert_enabled=True, codex_binary_path=tmp_path / "codex")
    configured = AgentConfig(
        codex_expert_enabled=True,
        codex_binary_path=tmp_path / "codex",
        codex_workspace_base=tmp_path / "leases",
    )
    assert configured.codex_expert_enabled


def test_codex_state_home_requires_absolute_path_when_configured(tmp_path):
    with pytest.raises(ValidationError, match="codex_state_home must be an absolute path"):
        AgentConfig(codex_state_home=Path("relative/codex-state"))
    configured = AgentConfig(codex_state_home=tmp_path / "codex-state")
    assert configured.codex_state_home == tmp_path / "codex-state"


def test_codex_home_requires_absolute_path_when_configured(tmp_path):
    with pytest.raises(ValidationError, match="codex_home must be an absolute path"):
        AgentConfig(codex_home=Path("relative/codex-profile"))
    configured = AgentConfig(codex_home=tmp_path / "codex-profile")
    assert configured.codex_home == tmp_path / "codex-profile"


def test_codex_permission_mode_can_select_legacy_compatibility():
    assert AgentConfig(codex_permission_mode="legacy").codex_permission_mode == "legacy"
    with pytest.raises(ValidationError):
        AgentConfig(codex_permission_mode="unrestricted")


def test_local_config_can_disable_default_reranking(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text("[reranker]\nenabled = false\n", encoding="utf-8")

    assert load_local_config(config_path).reranker.enabled is False


def test_query_rewrite_budget_can_be_configured(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        "[query_rewrite]\nmax_rewrites = 5\nmax_total_chars = 1500\n"
        "max_parallel_searches = 3\nmax_total_candidates = 150\n",
        encoding="utf-8",
    )
    config = load_local_config(config_path)
    assert config.query_rewrite.max_rewrites == 5
    assert config.query_rewrite.max_total_chars == 1500
    assert config.query_rewrite.max_parallel_searches == 3
    assert config.query_rewrite.max_total_candidates == 150


def test_child_inference_profile_requires_server_allowlist_and_supported_effort():
    profile = AgentInferenceProfile(
        profile_id="careful",
        client_name="primary",
        model="reasoner-v2",
        reasoning_effort="high",
    )
    llm = LLMProviderConfig(
        default_client="primary",
        clients=[
            LLMClientConfig(
                name="primary",
                default_model="reasoner-v2",
                available_models=["reasoner-v2"],
                supports_reasoning_effort=True,
            )
        ],
    )
    config = LocalAppConfig(
        llm=llm,
        agent=AgentConfig(
            inference_profiles=(profile,),
            allowed_child_inference_profile_ids=("careful",),
        ),
    )
    assert config.agent.inference_profiles == (profile,)

    with pytest.raises(ValidationError, match="reasoning effort is unsupported"):
        LocalAppConfig(
            llm=LLMProviderConfig(
                default_client="primary",
                clients=[
                    LLMClientConfig(
                        name="primary",
                        default_model="reasoner-v2",
                        available_models=["reasoner-v2"],
                    )
                ],
            ),
            agent=AgentConfig(
                inference_profiles=(profile,),
                allowed_child_inference_profile_ids=("careful",),
            ),
        )


def test_child_inference_profile_allowlist_cannot_name_unconfigured_profile():
    with pytest.raises(ValidationError, match="must be configured"):
        AgentConfig(allowed_child_inference_profile_ids=("unknown",))


def test_mock_workflow_requires_explicit_local_opt_in(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text("[agent]\nmock_workflow_agent_enabled = true\n", encoding="utf-8")

    assert load_local_config(config_path).agent.mock_workflow_agent_enabled is True


def test_settings_default_runtime_data_directory_is_isolated_from_fixtures(monkeypatch):
    monkeypatch.delenv("LKA_DATA_DIR", raising=False)

    settings = Settings(_env_file=None)

    assert settings.data_dir == Path("data/runtime")


def test_local_config_loads_provider_mail_and_embedding_sections(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[llm]
provider = "openai"
api_key_env = "TEST_OPENAI_API_KEY"
model = "gpt-test"

[mail.outlook]
enabled = true
client_id_env = "TEST_MS_GRAPH_CLIENT_ID"
tenant_id = "consumers"
scopes = ["User.Read", "Mail.Read", "offline_access"]
download_attachment_content = false
startup_sync_enabled = false
background_sync_enabled = true
sync_interval_seconds = 120
sync_limit = 15
sync_max_pages = 3

[mail.imap]
enabled = true
host = "imap.example.com"
username = "user@example.com"
password_env = "TEST_MAIL_PASSWORD"

[embedding]
provider = "local_bge"
model_name = "BAAI/bge-m3"
device = "cpu"
cache_dir = "./data/test-models"
batch_size = 8
normalize_embeddings = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "llm-secret")
    monkeypatch.setenv("TEST_MS_GRAPH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEST_MAIL_PASSWORD", "mail-secret")

    config = load_local_config(config_path)

    assert config.llm.provider == "openai"
    assert config.llm.resolved_api_key() == "llm-secret"
    assert config.mail.outlook.enabled is True
    assert config.mail.outlook.resolved_client_id() == "client-id"
    assert config.mail.outlook.download_attachment_content is False
    assert config.mail.outlook.startup_sync_enabled is False
    assert config.mail.outlook.background_sync_enabled is True
    assert config.mail.outlook.sync_interval_seconds == 120
    assert config.mail.outlook.sync_limit == 15
    assert config.mail.outlook.sync_max_pages == 3
    assert config.mail.imap.enabled is True
    assert config.mail.imap.resolved_password() == "mail-secret"
    assert config.embedding.device == "cpu"
    assert config.embedding.cache_dir == Path("data/test-models")


def test_llm_api_key_can_be_loaded_from_dotenv(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[llm]
provider = "packyapi"
api_key_env = "PACKY_API_KEY"
model = "deepseek-v4-flash"
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text('PACKY_API_KEY="dotenv-secret"\n', encoding="utf-8")
    monkeypatch.delenv("PACKY_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    config = load_local_config(config_path)

    assert config.llm.resolved_api_key() == "dotenv-secret"


def test_llm_config_loads_named_clients(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[llm]
default_client = "packyapi"
fallback_client = "mock"
default_response_mode = "json"
max_attempts = 3

[[llm.clients]]
name = "packyapi"
provider = "openai_compatible"
base_url = "https://www.packyapi.ai/v1"
api_key_env = "PACKY_API_KEY"
default_model = "deepseek-v4-flash"
available_models = ["deepseek-v4-flash", "gpt-4.1-mini"]
supports_stream = true
supports_json_mode = false

[[llm.clients]]
name = "mock"
provider = "mock"
default_model = "mock"
available_models = ["mock"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PACKY_API_KEY", "secret")

    config = load_local_config(config_path)

    assert config.llm.default_client == "packyapi"
    assert config.llm.fallback_client == "mock"
    assert config.llm.max_attempts == 3
    assert [client.name for client in config.llm.clients] == ["packyapi", "mock"]
    assert config.llm.clients[0].resolved_api_key() == "secret"
    assert config.llm.clients[0].available_models == [
        "deepseek-v4-flash",
        "gpt-4.1-mini",
    ]


def test_outlook_client_id_can_be_loaded_directly_from_config(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[mail.outlook]
enabled = true
client_id = "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"
""",
        encoding="utf-8",
    )

    config = load_local_config(config_path)

    assert config.mail.outlook.resolved_client_id() == "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"


def test_outlook_client_id_env_accepts_literal_guid_for_local_config(tmp_path):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[mail.outlook]
enabled = true
client_id_env = "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"
""",
        encoding="utf-8",
    )

    config = load_local_config(config_path)

    assert config.mail.outlook.resolved_client_id() == "dd5c654c-9e3c-41fc-9e14-15088af6c8bf"


def test_settings_loads_config_from_configured_path(tmp_path, monkeypatch):
    config_path = tmp_path / "local.toml"
    config_path.write_text(
        """
[embedding]
provider = "local_bge"
model_name = "BAAI/bge-m3"
device = "auto"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config_path))

    settings = Settings()
    config = settings.load_local_config()

    assert config.embedding.provider == "local_bge"
    assert config.embedding.model_name == "BAAI/bge-m3"
