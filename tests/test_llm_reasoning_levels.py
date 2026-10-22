from __future__ import annotations

import asyncio
import json

import pytest

from app.core.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ReasoningLevel,
    build_llm_service,
)
from app.core.llm.errors import LLMClientError
from app.core.local_config import LLMClientConfig, LLMProviderConfig, LLMReasoningConfig


def _config(client: LLMClientConfig) -> LLMProviderConfig:
    client.api_key_env = "TEST_REASONING_KEY"
    return LLMProviderConfig(provider="openai_compatible", default_client=client.name, clients=[client])


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("TEST_REASONING_KEY", "test-only")


def _request(level, **metadata) -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="hello")],
        prompt_summary="reasoning test",
        reasoning_level=level,
        metadata=metadata,
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("level", "thinking", "effort"),
    [
        (ReasoningLevel.OFF, {"type": "disabled"}, None),
        (ReasoningLevel.LOW, {"type": "enabled"}, "low"),
        (ReasoningLevel.HIGH, {"type": "enabled"}, "high"),
        (ReasoningLevel.MAX, {"type": "enabled"}, "max"),
    ],
)
def test_deepseek_semantic_levels_resolve_and_serialize(level, thinking, effort, stream):
    cfg = _config(LLMClientConfig(
        name="deep", default_model="deepseek-chat", thinking_control="deepseek",
    ))
    service = build_llm_service(cfg)
    request = _request(level)
    resolved, audit = __import__("app.core.llm.reasoning", fromlist=["resolve_reasoning_request"]).resolve_reasoning_request(
        cfg, request.model_copy(update={"client_name": "deep", "model": "deepseek-chat"}),
    )
    wire = json.loads(service.registry.get("deep")._build_request(resolved, stream=stream).data)
    assert wire["thinking"] == thinking
    assert wire.get("reasoning_effort") == effort
    assert audit["requested"] == level.value
    assert audit["effective"] == level.value


def test_generic_exact_model_mapping_supports_none_and_xhigh_without_leaking():
    cfg = _config(LLMClientConfig(
        name="generic", default_model="base", available_models=["base", "reasoner"],
        model_reasoning_overrides={
            "reasoner": LLMReasoningConfig(
                effort_map={"off": "none", "low": "low", "high": "high", "max": "xhigh"},
            ),
        },
    ))
    service = build_llm_service(cfg)
    cap = service.reasoning_capability(client_name="generic", model="reasoner")
    assert cap["supports_off"]
    assert cap["effort_map"]["max"] == "xhigh"
    assert not service.reasoning_capability(client_name="generic", model="base")["controlled"]
    with pytest.raises(LLMClientError, match="does not support reasoning_effort"):
        service.registry.get("generic")._build_request(
            LLMRequest(
                messages=[LLMMessage(role="user", content="hello")],
                prompt_summary="unsupported raw model effort",
                model="base",
                reasoning_effort="high",
            ),
            stream=False,
        )
    for level, native in ((ReasoningLevel.OFF, "none"), (ReasoningLevel.MAX, "xhigh")):
        request = _request(level, reasoning_source="user_setting").model_copy(
            update={"client_name": "generic", "model": "reasoner"},
        )
        resolved = __import__("app.core.llm.reasoning", fromlist=["resolve_reasoning_request"]).resolve_reasoning_request(cfg, request)[0]
        payload = json.loads(service.registry.get("generic")._build_request(resolved, stream=False).data)
        assert payload["reasoning_effort"] == native
        assert resolved.metadata["reasoning"]["source"] == "user_setting"


def test_unsupported_off_fails_with_reasoning_metadata_and_unknown_model_degrades():
    cfg = _config(LLMClientConfig(name="plain", default_model="plain-model"))
    service = build_llm_service(cfg)
    with pytest.raises(LLMClientError, match="off.*unsupported") as caught:
        asyncio.run(service.complete(_request(ReasoningLevel.OFF)))
    assert caught.value.metadata["reasoning"]["status"] == "unsupported"

    from app.core.llm.reasoning import resolve_reasoning_request

    request = _request(ReasoningLevel.LOW).model_copy(update={"client_name": "plain", "model": "other"})
    resolved, audit = resolve_reasoning_request(cfg, request)
    assert resolved.reasoning_effort is None
    assert audit["effective"] == "unsupported"
    assert audit["status"] == "uncontrolled"


@pytest.mark.parametrize("policy", ["internal", "safety"])
def test_internal_and_safety_policy_always_resolve_to_low(policy):
    cfg = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={
            "low": "low", "high": "high", "max": "xhigh",
        },
    ))
    from app.core.llm.reasoning import resolve_reasoning_request

    request = _request(ReasoningLevel.MAX, reasoning_policy=policy).model_copy(
        update={"client_name": "generic", "model": "reasoner"},
    )
    resolved, audit = resolve_reasoning_request(cfg, request)
    assert resolved.reasoning_effort == "low"
    assert audit["requested"] == "max"
    assert audit["effective"] == "low"
    assert audit["policy"] == policy


def test_utility_uses_off_when_supported_and_low_fallback_otherwise():
    from app.core.llm.reasoning import resolve_reasoning_request

    cfg = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={"off": "none", "low": "low"},
    ))
    request = _request(ReasoningLevel.HIGH, reasoning_policy="utility").model_copy(
        update={"client_name": "generic", "model": "reasoner"},
    )
    resolved, audit = resolve_reasoning_request(cfg, request)
    assert resolved.reasoning_effort == "none"
    assert audit["effective"] == "off"

    cfg2 = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={"low": "low"},
    ))
    resolved, audit = resolve_reasoning_request(cfg2, request)
    assert resolved.reasoning_effort == "low"
    assert audit["status"] == "fallback"


def test_explicit_recovery_control_overrides_semantic_level_and_records_both(monkeypatch):
    cfg = _config(LLMClientConfig(
        name="deep", default_model="deepseek-chat", thinking_control="deepseek",
    ))
    service = build_llm_service(cfg)
    from app.core.llm.reasoning import resolve_reasoning_request

    request = _request(ReasoningLevel.LOW, reasoning_source="recovery").model_copy(
        update={"client_name": "deep", "model": "deepseek-chat", "thinking_enabled": False},
    )
    resolved, audit = resolve_reasoning_request(cfg, request)
    payload = json.loads(service.registry.get("deep")._build_request(resolved, stream=False).data)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert audit["requested"] == "low"
    assert audit["effective"] == "off"
    assert audit["source"] == "recovery"


def test_explicit_deepseek_enable_complements_semantic_effort_and_cannot_reenable_off():
    cfg = _config(LLMClientConfig(
        name="deep", default_model="deepseek-chat", thinking_control="deepseek",
    ))
    service = build_llm_service(cfg)
    from app.core.llm.reasoning import resolve_reasoning_request

    base = {"client_name": "deep", "model": "deepseek-chat", "thinking_enabled": True}
    low = _request(ReasoningLevel.LOW).model_copy(update=base)
    resolved, audit = resolve_reasoning_request(cfg, low)
    payload = json.loads(service.registry.get("deep")._build_request(resolved, stream=False).data)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert audit["effective"] == "low"

    off = _request(ReasoningLevel.OFF).model_copy(update={**base, "reasoning_effort": "high"})
    resolved, audit = resolve_reasoning_request(cfg, off)
    payload = json.loads(service.registry.get("deep")._build_request(resolved, stream=False).data)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert audit["effective"] == "off"


def test_declared_missing_level_fails_instead_of_using_provider_default_and_max_alias_is_audited():
    cfg = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={"low": "low", "high": "high"},
    ))
    service = build_llm_service(cfg)
    with pytest.raises(LLMClientError, match="max.*unsupported") as caught:
        asyncio.run(service.complete(_request(ReasoningLevel.MAX)))
    assert caught.value.metadata["reasoning"]["status"] == "unsupported"

    alias_cfg = _config(LLMClientConfig(
        name="legacy", default_model="reasoner", supports_reasoning_effort=True,
    ))
    from app.core.llm.reasoning import resolve_reasoning_request

    request = _request(ReasoningLevel.MAX).model_copy(
        update={"client_name": "legacy", "model": "reasoner"},
    )
    resolved, audit = resolve_reasoning_request(alias_cfg, request)
    assert resolved.reasoning_effort == "high"
    assert audit["requested"] == "max"
    assert audit["effective"] == "high"
    assert audit["alias"] is True
    assert audit["provider_params"]["reasoning_effort"] == "high"


def test_unknown_model_rejects_explicit_high_but_keeps_low_uncontrolled():
    cfg = _config(LLMClientConfig(name="plain", default_model="plain-model"))
    from app.core.llm.reasoning import resolve_reasoning_request

    unknown = {"client_name": "plain", "model": "other"}
    with pytest.raises(LLMClientError, match="high.*unsupported"):
        resolve_reasoning_request(cfg, _request(ReasoningLevel.HIGH).model_copy(update=unknown))


def test_matching_capability_snapshot_freezes_mapping_across_config_change():
    cfg = _config(LLMClientConfig(
        name="generic", default_model="base", available_models=["base", "reasoner"],
        model_reasoning_overrides={
            "reasoner": LLMReasoningConfig(
                effort_map={"low": "low", "high": "high", "max": "xhigh"},
            ),
        },
    ))
    service = build_llm_service(cfg)
    capability = service.reasoning_capability(client_name="generic", model="reasoner")
    snapshot = {
        "client_name": "generic",
        "model": "reasoner",
        "version": "reasoning-v1",
        "capability": capability,
    }
    cfg.clients[0].model_reasoning_overrides["reasoner"] = LLMReasoningConfig(
        effort_map={"low": "low", "high": "high", "max": "high"},
    )
    request = _request(ReasoningLevel.MAX, reasoning_capability_snapshot=snapshot).model_copy(
        update={"client_name": "generic", "model": "reasoner"},
    )
    from app.core.llm.reasoning import resolve_reasoning_request

    resolved, audit = resolve_reasoning_request(cfg, request)
    payload = json.loads(service.registry.get("generic")._build_request(resolved, stream=False).data)
    assert payload["reasoning_effort"] == "xhigh"
    assert audit["capability_version"] == "reasoning-v1"
    assert audit["capability"]["effort_map"]["max"] == "xhigh"


def test_invalid_matching_capability_snapshot_fails_closed():
    cfg = _config(LLMClientConfig(name="plain", default_model="plain-model"))
    request = _request(
        ReasoningLevel.LOW,
        reasoning_capability_snapshot={
            "client_name": "plain",
            "model": "plain-model",
            "version": "future-version",
            "capability": {
                "controlled": False,
                "supports_off": False,
                "effort_map": {},
                "source": "snapshot",
                "protocol": "openai_chat",
            },
        },
    ).model_copy(update={"client_name": "plain", "model": "plain-model"})
    from app.core.llm.reasoning import resolve_reasoning_request

    with pytest.raises(LLMClientError, match="Invalid reasoning capability snapshot") as caught:
        resolve_reasoning_request(cfg, request)
    assert caught.value.metadata["reasoning"]["status"] == "snapshot_invalid"


def test_recovery_disable_respects_exact_model_off_capability():
    cfg = _config(LLMClientConfig(
        name="deep", default_model="deepseek-chat", available_models=["deepseek-chat", "limited-chat"],
        thinking_control="deepseek",
        model_reasoning_overrides={
            "limited-chat": LLMReasoningConfig(effort_map={"low": "low"}),
        },
    ))
    from app.core.llm.reasoning import resolve_reasoning_request

    identity = {"client_name": "deep", "model": "limited-chat", "thinking_enabled": False}
    user_request = _request(
        ReasoningLevel.LOW, reasoning_source="recovery",
    ).model_copy(update=identity)
    with pytest.raises(LLMClientError, match="off.*unsupported") as caught:
        resolve_reasoning_request(cfg, user_request)
    assert caught.value.metadata["reasoning"]["effective"] == "unsupported"

    internal_request = _request(
        ReasoningLevel.MAX, reasoning_source="recovery", reasoning_policy="internal",
    ).model_copy(update=identity)
    resolved, audit = resolve_reasoning_request(cfg, internal_request)
    payload = json.loads(build_llm_service(cfg).registry.get("deep")._build_request(resolved, stream=False).data)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert audit["effective"] == "low"
    assert audit["status"] == "fallback"


def test_legacy_single_client_effort_capability_reaches_transport():
    cfg = LLMProviderConfig(
        provider="openai_compatible", api_key_env="OPENAI_API_KEY",
        supports_reasoning_effort=True, model="legacy-model",
    )
    client = cfg.client_configs()[0]
    assert client.supports_reasoning_effort
    assert client.default_model == "legacy-model"


def test_service_attaches_reasoning_metadata_to_complete_and_stream(monkeypatch):
    cfg = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={"low": "low"},
    ))
    service = build_llm_service(cfg)
    client = service.registry.get("generic")
    request = _request(ReasoningLevel.LOW)

    async def complete(captured):
        return LLMResponse(provider="generic", status="completed", content="ok", prompt_summary=captured.prompt_summary)

    monkeypatch.setattr(client, "complete", complete)
    response = asyncio.run(service.complete(request))
    assert response.metadata["reasoning"]["effective"] == "low"

    async def stream(captured):
        yield LLMStreamEvent(
            event_type="llm_completed", stage="answer", client_name="generic",
            provider="generic", model="reasoner",
        )

    monkeypatch.setattr(client, "stream", stream)

    async def collect():
        return [event async for event in service.stream(request)]

    events = asyncio.run(collect())
    assert events[-1].metadata["reasoning"]["effective"] == "low"


def test_service_attaches_reasoning_metadata_to_provider_failures(monkeypatch):
    cfg = _config(LLMClientConfig(
        name="generic", default_model="reasoner", reasoning_effort_map={"low": "low"},
    ))
    service = build_llm_service(cfg)
    client = service.registry.get("generic")

    async def fail(_request):
        raise LLMClientError("provider rejected request")

    monkeypatch.setattr(client, "complete", fail)
    with pytest.raises(LLMClientError) as caught:
        asyncio.run(service.complete(_request(ReasoningLevel.LOW)))
    assert caught.value.metadata["reasoning"]["requested"] == "low"
    assert caught.value.metadata["reasoning"]["effective"] == "low"
