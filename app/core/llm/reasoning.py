"""Deterministic resolution of semantic reasoning levels to provider controls."""

from __future__ import annotations

from typing import Any

from app.core.llm.errors import LLMClientError
from app.core.llm.models import LLMReasoningEffort, LLMRequest, ReasoningLevel
from app.core.local_config import LLMProviderConfig

REASONING_MAPPING_VERSION = "reasoning-v1"
_REASONING_LEVELS = {"off", "low", "high", "max"}


def reasoning_capability(
    config: LLMProviderConfig, client_name: str | None, model: str,
) -> dict[str, Any]:
    """Describe declared controls for this exact configured client and model."""
    client = next((item for item in config.client_configs() if item.name == client_name), None)
    protocol = "openai_chat"
    if client is None:
        return _capability(False, False, {}, "unconfigured", protocol)

    exact = client.model_reasoning_overrides.get(model)
    if exact is not None:
        effort_map = dict(exact.effort_map)
        return _capability(
            bool(effort_map) or exact.supports_off,
            exact.supports_off or "off" in effort_map,
            effort_map,
            "model_config",
            "deepseek_chat" if client.thinking_control == "deepseek" else protocol,
        )
    if model != client.default_model:
        return _capability(False, False, {}, "unconfigured_model", protocol)

    effort_map = dict(client.reasoning_effort_map)
    if client.thinking_control == "deepseek":
        # DeepSeek Chat defines these controls; activation still comes from an
        # explicit client setting and is bounded to its configured default model.
        effort_map = {"low": "low", "high": "high", "max": "max", **effort_map}
        return _capability(True, True, effort_map, "client_config", "deepseek_chat")
    if not effort_map and client.supports_reasoning_effort:
        # Compatibility for the old client-wide boolean. The highest declared
        # native level is high, so max is an explicitly visible alias.
        effort_map = {"low": "low", "high": "high", "max": "high"}
    supports_off = client.supports_reasoning_off or "off" in effort_map
    controlled = bool(effort_map or supports_off)
    source = "client_config" if controlled else "uncontrolled"
    return _capability(controlled, supports_off, effort_map, source, protocol)


def _capability(controlled, supports_off, effort_map, source, protocol):
    return {
        "controlled": bool(controlled),
        "supports_off": bool(supports_off),
        "effort_map": dict(effort_map),
        "source": source,
        "protocol": protocol,
    }


def _request_capability(config: LLMProviderConfig, request: LLMRequest) -> tuple[dict[str, Any], str]:
    snapshot = request.metadata.get("reasoning_capability_snapshot")
    if not isinstance(snapshot, dict) or (
        snapshot.get("client_name") != request.client_name
        or snapshot.get("model") != request.model
    ):
        return reasoning_capability(config, request.client_name, request.model or ""), REASONING_MAPPING_VERSION

    capability = snapshot.get("capability")
    effort_map = capability.get("effort_map") if isinstance(capability, dict) else None
    valid = (
        snapshot.get("version") == REASONING_MAPPING_VERSION
        and isinstance(capability, dict)
        and isinstance(capability.get("controlled"), bool)
        and isinstance(capability.get("supports_off"), bool)
        and isinstance(effort_map, dict)
        and set(effort_map).issubset(_REASONING_LEVELS)
        and all(isinstance(value, str) and value for value in effort_map.values())
        and isinstance(capability.get("source"), str)
        and capability.get("source")
        and capability.get("protocol") in {"openai_chat", "deepseek_chat"}
        and capability["controlled"] == bool(effort_map or capability["supports_off"])
    )
    if not valid:
        exc = LLMClientError(
            f"Invalid reasoning capability snapshot for client {request.client_name} and model {request.model}.",
        )
        exc.metadata = {"reasoning": {
            "requested": request.reasoning_level.value,
            "effective": "unsupported",
            "alias": False,
            "provider_params": {},
            "source": request.metadata.get("reasoning_source", "snapshot"),
            "policy": request.metadata.get("reasoning_policy", "user"),
            "status": "snapshot_invalid",
            "client_name": request.client_name,
            "model": request.model,
            "capability_version": snapshot.get("version"),
        }}
        raise exc
    return dict(capability), snapshot["version"]


def validate_reasoning_level(
    config: LLMProviderConfig, client_name: str | None, model: str,
    level: ReasoningLevel | str,
) -> dict[str, Any]:
    """Validate a user-selected level against declared model controls."""
    normalized = ReasoningLevel(level)
    capability = reasoning_capability(config, client_name, model)
    if normalized is ReasoningLevel.OFF and not capability["supports_off"]:
        raise _unsupported_level_error(normalized, client_name, model, capability)
    if (
        normalized is not ReasoningLevel.OFF
        and capability["controlled"]
        and normalized.value not in capability["effort_map"]
    ):
        raise _unsupported_level_error(normalized, client_name, model, capability)
    if normalized in {ReasoningLevel.HIGH, ReasoningLevel.MAX} and not capability["controlled"]:
        raise _unsupported_level_error(normalized, client_name, model, capability)
    return capability


def _unsupported_level_error(level, client_name, model, capability) -> LLMClientError:
    return LLMClientError(
        f"Reasoning level '{level.value}' is unsupported for client {client_name} and model {model}.",
    )


def resolve_reasoning_request(
    config: LLMProviderConfig, request: LLMRequest,
) -> tuple[LLMRequest, dict[str, Any] | None]:
    """Apply a semantic level and trusted purpose policy to a transport request."""
    if request.reasoning_level is None:
        return request, None

    requested = ReasoningLevel(request.reasoning_level)
    policy = request.metadata.get("reasoning_policy", "user")
    if policy not in {"user", "utility", "internal", "safety"}:
        raise LLMClientError(f"Unknown reasoning policy: {policy}")
    capability, capability_version = _request_capability(config, request)

    # Recovery and legacy callers may deliberately supply raw provider controls
    # alongside the frozen semantic choice. Keep those controls authoritative.
    # Explicit disabling is a recovery override. Enabling is complementary on
    # DeepSeek and must still carry the semantic effort selected below.
    if request.thinking_enabled is False:
        if capability["supports_off"]:
            params = (
                {"thinking": {"type": "disabled"}}
                if capability["protocol"] == "deepseek_chat"
                else {"reasoning_effort": capability["effort_map"].get("off", "none")}
            )
            resolution = {
                "requested": requested.value,
                "effective": "off",
                "alias": requested is not ReasoningLevel.OFF,
                "provider_params": params,
                "source": request.metadata.get("reasoning_source", "legacy_override"),
                "policy": policy,
                "status": "legacy_override",
                "client_name": request.client_name,
                "model": request.model,
                "capability": capability,
                "capability_version": capability_version,
            }
            return request.model_copy(update={
                "reasoning_effort": params.get("reasoning_effort"),
                "thinking_enabled": (
                    False if capability["protocol"] == "deepseek_chat" else None
                ),
                "metadata": {**request.metadata, "reasoning": resolution},
            }), resolution

        if policy in {"utility", "internal", "safety"}:
            native = capability["effort_map"].get("low")
            if native is not None:
                params = {"reasoning_effort": native}
                if capability["protocol"] == "deepseek_chat":
                    params["thinking"] = {"type": "enabled"}
                effective = _effective_level_for_native(native, capability["effort_map"])
                resolution = {
                    "requested": requested.value,
                    "effective": effective,
                    "alias": effective != requested.value,
                    "provider_params": params,
                    "source": request.metadata.get("reasoning_source", "legacy_override"),
                    "policy": policy,
                    "status": "fallback",
                    "client_name": request.client_name,
                    "model": request.model,
                    "capability": capability,
                    "capability_version": capability_version,
                }
                return request.model_copy(update={
                    "reasoning_effort": native,
                    "thinking_enabled": (
                        True if capability["protocol"] == "deepseek_chat" else None
                    ),
                    "metadata": {**request.metadata, "reasoning": resolution},
                }), resolution

        exc = _unsupported_level_error(ReasoningLevel.OFF, request.client_name, request.model, capability)
        exc.metadata = {"reasoning": _failure_record(
            request, requested, ReasoningLevel.OFF, policy, capability, capability_version,
        )}
        raise exc

    if request.reasoning_effort is not None and requested is not ReasoningLevel.OFF:
        params: dict[str, Any] = {}
        if request.thinking_enabled is not None:
            params["thinking"] = {
                "type": "enabled" if request.thinking_enabled else "disabled",
            }
        native_effort = reasoning_effort_value(request.reasoning_effort)
        if native_effort is not None:
            params["reasoning_effort"] = native_effort
        effective = (
            "off" if request.thinking_enabled is False
            else ("legacy_raw" if native_effort is not None or request.thinking_enabled else "unsupported")
        )
        resolution = {
            "requested": requested.value,
            "effective": effective,
            "provider_params": params,
            "source": request.metadata.get("reasoning_source", "legacy_override"),
            "policy": policy,
            "status": "legacy_override",
            "client_name": request.client_name,
            "model": request.model,
            "capability": capability,
            "capability_version": capability_version,
        }
        updates = {
            "metadata": {**request.metadata, "reasoning": resolution},
        }
        return request.model_copy(update=updates), resolution

    if policy in {"internal", "safety"}:
        desired = ReasoningLevel.LOW
    elif policy == "utility":
        desired = ReasoningLevel.OFF
    else:
        desired = requested

    effort_map = capability["effort_map"]
    params: dict[str, Any] = {}
    effective: str | None = None
    status = "applied"
    thinking_control = capability["protocol"] == "deepseek_chat"

    if desired is ReasoningLevel.OFF:
        if not capability["supports_off"]:
            if policy == "user":
                try:
                    raise _unsupported_level_error(desired, request.client_name, request.model, capability)
                except LLMClientError as exc:
                    exc.metadata = {"reasoning": {
                        "requested": requested.value,
                        "effective": "unsupported",
                        "provider_params": {},
                        "source": request.metadata.get("reasoning_source", capability["source"]),
                        "policy": policy,
                        "status": "unsupported",
                        "client_name": request.client_name,
                        "model": request.model,
                        "capability": capability,
                        "capability_version": capability_version,
                    }}
                    raise
            desired = ReasoningLevel.LOW
            status = "fallback"
        elif capability["controlled"]:
            effective = "off"
            if thinking_control:
                params["thinking"] = {"type": "disabled"}
            elif "off" in effort_map:
                params["reasoning_effort"] = effort_map["off"]
            else:
                params["reasoning_effort"] = "none"
        else:
            effective = "unsupported"
            status = "uncontrolled"
    if desired is not ReasoningLevel.OFF:
        native = effort_map.get(desired.value)
        if native is None:
            if not capability["controlled"]:
                if policy == "user" and desired in {ReasoningLevel.HIGH, ReasoningLevel.MAX}:
                    exc = _unsupported_level_error(desired, request.client_name, request.model, capability)
                    exc.metadata = {"reasoning": _failure_record(request, requested, desired, policy, capability, capability_version)}
                    raise exc
                status = "uncontrolled"
                effective = "unsupported"
            else:
                # Utility and internal calls may fall back to a declared low
                # setting; user-selected levels must never become provider default.
                native = effort_map.get("low") if policy in {"utility", "internal", "safety"} else None
                if native is None:
                    exc = _unsupported_level_error(desired, request.client_name, request.model, capability)
                    exc.metadata = {"reasoning": _failure_record(request, requested, desired, policy, capability, capability_version)}
                    raise exc
                else:
                    desired = ReasoningLevel.LOW
                    effective = desired.value
                    status = "fallback"
        else:
            effective = _effective_level_for_native(native, effort_map)
        if native is not None:
            params["reasoning_effort"] = native
            if thinking_control:
                params["thinking"] = {"type": "enabled"}

    if thinking_control and params.get("thinking", {}).get("type") == "disabled":
        params.pop("reasoning_effort", None)
    update: dict[str, Any] = {
        "reasoning_effort": params.get("reasoning_effort"),
        "thinking_enabled": (
            params.get("thinking", {}).get("type") == "enabled"
            if thinking_control and "thinking" in params else None
        ),
    }
    resolution = {
        "requested": requested.value,
        "effective": effective,
        "alias": effective not in {None, requested.value},
        "provider_params": params,
        "source": request.metadata.get("reasoning_source", capability["source"]),
        "policy": policy,
        "status": status,
        "client_name": request.client_name,
        "model": request.model,
        "capability": capability,
        "capability_version": capability_version,
    }
    metadata = {**request.metadata, "reasoning": resolution}
    return request.model_copy(update={**update, "metadata": metadata}), resolution


def _failure_record(request, requested, desired, policy, capability, capability_version):
    return {
        "requested": requested.value,
        "effective": "unsupported",
        "alias": False,
        "provider_params": {},
        "source": request.metadata.get("reasoning_source", capability["source"]),
        "policy": policy,
        "status": "unsupported",
        "client_name": request.client_name,
        "model": request.model,
        "capability": capability,
        "capability_version": capability_version,
    }


def _effective_level_for_native(native: str, effort_map: dict[str, str]) -> str:
    if native in effort_map and effort_map[native] == native:
        return native
    aliases = [level for level in ("low", "high", "max") if effort_map.get(level) == native]
    return aliases[0] if aliases else native


def reasoning_effort_value(value: LLMReasoningEffort | str | None) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, LLMReasoningEffort) else str(value)
