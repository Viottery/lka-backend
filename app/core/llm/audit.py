"""LLM call audit records and provider error classification."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field

from app.core.llm.errors import LLMTimeoutError

DEBUG_HEADER_NAMES = [
    "x-request-id",
    "openai-organization",
    "openai-processing-ms",
    "openai-version",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-limit-project-tokens",
    "x-ratelimit-remaining-project-tokens",
    "x-ratelimit-reset-project-tokens",
    "retry-after",
]


class LLMCallRecord(BaseModel):
    llm_call_id: str
    run_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    stage: str
    client_name: str
    provider: str
    model: str
    response_mode: str
    status: str
    started_at: str
    completed_at: str | None = None
    failed_at: str | None = None
    duration_ms: int | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    retry_after: str | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    provider_error_param: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    is_retriable: bool | None = None
    finish_reason: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    total_token_count: int | None = None
    content_length: int | None = None
    prompt_summary: str | None = None
    partial: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_llm_call_id(*parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"llm_call_{digest}"


def normalize_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else []
    normalized: dict[str, str] = {}
    for key, value in items:
        normalized[str(key).lower()] = str(value)
    return normalized


def audit_headers(headers: Any) -> dict[str, str]:
    normalized = normalize_headers(headers)
    return {
        key: value
        for key, value in normalized.items()
        if key in DEBUG_HEADER_NAMES
    }


def provider_request_id_from_headers(headers: Any) -> str | None:
    return audit_headers(headers).get("x-request-id")


def retry_after_from_headers(headers: Any) -> str | None:
    return audit_headers(headers).get("retry-after")


def parse_error_body(error_body: str | bytes | dict[str, Any] | None) -> dict[str, Any]:
    if error_body is None:
        return {}
    if isinstance(error_body, bytes):
        error_body = error_body.decode("utf-8", errors="replace")
    if isinstance(error_body, str):
        try:
            payload = json.loads(error_body)
        except json.JSONDecodeError:
            return {"raw_body": error_body}
    elif isinstance(error_body, dict):
        payload = error_body
    else:
        return {"raw_body": str(error_body)}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return {
            "type": error.get("type"),
            "code": error.get("code"),
            "message": error.get("message"),
            "param": error.get("param"),
            "event_id": error.get("event_id"),
            "body": payload,
        }
    return {"body": payload}


def classify_provider_error(
    *,
    http_status: int | None,
    provider_error_type: str | None = None,
    provider_error_code: str | None = None,
) -> tuple[str, bool]:
    if http_status is not None:
        category = _category_from_http_status(http_status)
        if category is not None:
            return category

    provider_marker = " ".join(
        marker
        for marker in [provider_error_type, provider_error_code]
        if isinstance(marker, str)
    ).lower()
    if "invalid_request" in provider_marker:
        return "bad_request", False
    if "authentication" in provider_marker or "invalid_api_key" in provider_marker:
        return "authentication_failed", False
    if "permission" in provider_marker:
        return "permission_denied", False
    if "rate" in provider_marker and "limit" in provider_marker:
        return "rate_limited", True
    if "server" in provider_marker or "internal" in provider_marker:
        return "provider_internal_error", True
    if "timeout" in provider_marker:
        return "timeout", True
    return "unknown_provider_error", False


def classify_openai_sdk_exception(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, LLMTimeoutError):
        return "timeout", True
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return classify_provider_error(http_status=status_code)
    mapping: dict[str, tuple[str, bool]] = {
        "BadRequestError": ("bad_request", False),
        "AuthenticationError": ("authentication_failed", False),
        "PermissionDeniedError": ("permission_denied", False),
        "NotFoundError": ("not_found", False),
        "ConflictError": ("conflict", True),
        "UnprocessableEntityError": ("unprocessable_entity", False),
        "RateLimitError": ("rate_limited", True),
        "InternalServerError": ("provider_internal_error", True),
        "APIConnectionError": ("network_failed", True),
        "APITimeoutError": ("timeout", True),
        "APIResponseValidationError": ("response_validation_failed", False),
        "LengthFinishReasonError": ("output_length_limit", False),
        "ContentFilterFinishReasonError": ("content_filter", False),
        "OAuthError": ("oauth_failed", False),
        "SubjectTokenProviderError": ("oauth_subject_token_failed", False),
        "InvalidWebhookSignatureError": ("invalid_webhook_signature", False),
        "WebSocketConnectionClosedError": ("websocket_closed", True),
        "WebSocketQueueFullError": ("websocket_queue_full", True),
        "APIError": ("provider_error", False),
        "OpenAIError": ("provider_error", False),
    }
    return mapping.get(name, ("unknown_provider_error", False))


def stream_error_category(error_type: str | None) -> tuple[str, bool]:
    marker = (error_type or "").lower()
    if marker == "invalid_request_error":
        return "bad_request", False
    if "authentication" in marker:
        return "authentication_failed", False
    if "permission" in marker:
        return "permission_denied", False
    if "rate_limit" in marker or "rate" in marker:
        return "rate_limited", True
    if "parse" in marker or "json" in marker:
        return "response_parse_failed", False
    if "server" in marker:
        return "provider_internal_error", True
    return "provider_stream_error", False


def usage_token_counts(usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    usage = usage or {}
    input_count = _int_or_none(
        usage.get("prompt_tokens")
        if "prompt_tokens" in usage
        else usage.get("input_tokens")
    )
    output_count = _int_or_none(
        usage.get("completion_tokens")
        if "completion_tokens" in usage
        else usage.get("output_tokens")
    )
    total_count = _int_or_none(usage.get("total_tokens"))
    return input_count, output_count, total_count


def prompt_metadata(*, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    return {
        "prompt": {
            "message_count": 2,
            "system_prompt_length": len(system_prompt),
            "user_prompt_length": len(user_prompt),
        }
    }


def build_stream_error_call_record(
    *,
    llm_call_id: str,
    stage: str,
    started_at: str,
    duration_ms: int | None,
    client_name: str,
    provider: str,
    model: str,
    response_mode: str,
    error_event: dict[str, Any],
    partial_content: str = "",
    provider_request_id: str | None = None,
    headers: dict[str, str] | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> LLMCallRecord:
    error = error_event.get("error") if isinstance(error_event.get("error"), dict) else {}
    category, retriable = stream_error_category(
        str(error.get("type") or error_event.get("type") or "")
    )
    return LLMCallRecord(
        llm_call_id=llm_call_id,
        run_id=run_id,
        trace_id=trace_id,
        session_id=session_id,
        stage=stage,
        client_name=client_name,
        provider=provider,
        model=model,
        response_mode=response_mode,
        status="partial_failed" if partial_content else "failed",
        started_at=started_at,
        failed_at=now_iso(),
        duration_ms=duration_ms,
        provider_request_id=provider_request_id,
        provider_error_type=_str_or_none(error.get("type")),
        provider_error_code=_str_or_none(error.get("code")),
        provider_error_param=_str_or_none(error.get("param")),
        error_category=category,
        error_message=_str_or_none(error.get("message")),
        is_retriable=retriable,
        content_length=len(partial_content),
        partial=bool(partial_content),
        metadata={
            "event_id": error_event.get("event_id") or error.get("event_id"),
            "sequence_number": error_event.get("sequence_number"),
            "partial_content_length": len(partial_content),
            "headers": headers or {},
        },
    )


def _category_from_http_status(status_code: int) -> tuple[str, bool] | None:
    if status_code == 400:
        return "bad_request", False
    if status_code == 401:
        return "authentication_failed", False
    if status_code == 403:
        return "permission_denied", False
    if status_code == 404:
        return "not_found", False
    if status_code == 408:
        return "timeout", True
    if status_code == 409:
        return "conflict", True
    if status_code == 422:
        return "unprocessable_entity", False
    if status_code == 429:
        return "rate_limited", True
    if status_code >= 500:
        return "provider_internal_error", True
    if 400 <= status_code < 500:
        return "provider_http_error", False
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
