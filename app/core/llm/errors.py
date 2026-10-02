"""LLM error types shared by clients and the Agent Harness."""

from __future__ import annotations

from typing import Any


class LLMClientError(RuntimeError):
    """Raised when a configured LLM provider cannot complete a request."""


class LLMContextCapacityError(LLMClientError):
    def __init__(self, message: str, *, error_category: str):
        super().__init__(message)
        self.error_category = error_category


class LLMProviderHTTPError(LLMClientError):
    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        retry_after: str | None = None,
        headers: dict[str, str] | None = None,
        error_body: str | dict[str, Any] | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_error_param: str | None = None,
        provider_error_event_id: str | None = None,
        error_category: str | None = None,
        is_retriable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.headers = headers or {}
        self.error_body = error_body
        self.provider_error_type = provider_error_type
        self.provider_error_code = provider_error_code
        self.provider_error_param = provider_error_param
        self.provider_error_event_id = provider_error_event_id
        self.error_category = error_category
        self.is_retriable = is_retriable


class LLMProviderStreamError(LLMClientError):
    def __init__(
        self,
        *,
        message: str,
        error_event: dict[str, Any],
        partial_content: str = "",
        headers: dict[str, str] | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_event = error_event
        self.partial_content = partial_content
        self.headers = headers or {}
        self.provider_request_id = provider_request_id


class LLMRateLimitError(LLMProviderHTTPError):
    """Raised when a provider rejects a request due rate limiting."""


class LLMAuthenticationError(LLMProviderHTTPError):
    """Raised when a provider rejects a request due authentication or authorization."""


class LLMNetworkError(LLMClientError):
    """Raised when the provider cannot be reached."""


class LLMTimeoutError(LLMClientError):
    """Raised when the provider request times out."""


class LLMResponseParseError(LLMClientError):
    """Raised when the provider response is not valid or expected JSON."""
