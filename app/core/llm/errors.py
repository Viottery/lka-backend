"""LLM error types shared by clients and the Agent Harness."""

from __future__ import annotations


class LLMClientError(RuntimeError):
    """Raised when a configured LLM provider cannot complete a request."""


class LLMProviderHTTPError(LLMClientError):
    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


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
