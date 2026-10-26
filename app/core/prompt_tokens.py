"""Provider-neutral prompt size estimates for chat completion requests.

When configured with a local ``tokenizers`` JSON file, this uses that tokenizer
for content. An explicitly configured fallback tokenizer is scaled and padded
as an approximate estimate; it is not a guaranteed upper bound. Otherwise the
counter uses UTF-8 byte counting. No tokenizer files are downloaded here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A count with enough metadata to interpret its estimation method."""

    count: int
    method: str
    conservative: bool
    approximate: bool = False


class PromptTokenCounter:
    """Count system/user prompt content and serialized tool definitions.

    A configured path must name a valid local Hugging Face ``tokenizers`` JSON
    file. The optional ``tokenizers`` library is imported only in that mode.
    """

    _CHAT_ENVELOPE_TOKENS = 8
    _PER_TOOL_ENVELOPE_TOKENS = 8
    _MAX_TOOL_ENVELOPE_TOKENS = 4096

    def __init__(
        self,
        tokenizer_json_path: Path | None = None,
        *,
        fallback_tokenizer_json_path: Path | None = None,
        fallback_multiplier: float = 1.5,
        fallback_extra_tokens: int = 2048,
    ) -> None:
        self._tokenizer = None
        self._fallback = None
        self._fallback_multiplier = fallback_multiplier
        self._fallback_extra_tokens = fallback_extra_tokens
        if tokenizer_json_path is not None:
            self._tokenizer = self._load_tokenizer(tokenizer_json_path)
        elif fallback_tokenizer_json_path is not None:
            try:
                self._fallback = self._load_tokenizer(fallback_tokenizer_json_path)
            except (FileNotFoundError, RuntimeError, ValueError):
                # A broken opt-in fallback must not make prompt fitting fail open.
                self._fallback = None

    @staticmethod
    def _load_tokenizer(tokenizer_json_path: Path):
        path = Path(tokenizer_json_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Tokenizer JSON file does not exist: {path}")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError(
                "A local tokenizer JSON was configured, but the 'tokenizers' package is not installed."
            ) from exc
        try:
            return Tokenizer.from_file(str(path))
        except Exception as exc:
            raise ValueError(f"Invalid tokenizer JSON file: {path}") from exc

    def _content_count(self, chunks: list[str], *, include_fallback_floor: bool = False) -> TokenCount:
        if self._tokenizer is not None:
            return TokenCount(sum(len(self._tokenizer.encode(chunk).ids) for chunk in chunks),
                              "local_tokenizers_json", False)
        if self._fallback is not None:
            raw = sum(len(self._fallback.encode(chunk).ids) for chunk in chunks)
            return TokenCount(
                math.ceil(raw * self._fallback_multiplier)
                + (self._fallback_extra_tokens if include_fallback_floor else 0),
                "proxy_tokenizer_estimate", False, True,
            )
        return TokenCount(sum(len(chunk.encode("utf-8")) for chunk in chunks),
                          "utf8_byte_upper_bound", True)

    def count_request(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any] | None = None,
    ) -> TokenCount:
        """Estimate request tokens, including serialized schemas for all tools."""
        serialized_tools = [self._serialize_tool(tool) for tool in (tools or [])]
        chunks = [system_prompt, user_prompt, *serialized_tools]
        envelope = min(
            self._MAX_TOOL_ENVELOPE_TOKENS,
            self._CHAT_ENVELOPE_TOKENS
            + len(serialized_tools) * self._PER_TOOL_ENVELOPE_TOKENS,
        )

        content = self._content_count(chunks, include_fallback_floor=True)
        return TokenCount(content.count + envelope, content.method, content.conservative, content.approximate)

    def count_text(self, text: str) -> TokenCount:
        """Count plain text without a chat envelope (for session precompaction)."""
        return self._content_count([text])

    @property
    def request_padding_tokens(self) -> int:
        """One padding reservation for callers that count separate message blocks."""
        return self._content_count([], include_fallback_floor=True).count

    @staticmethod
    def _serialize_tool(tool: Any) -> str:
        """Serialize Pydantic definitions and JSON-compatible tool objects."""
        if hasattr(tool, "model_dump"):
            value = tool.model_dump(mode="json")
        elif hasattr(tool, "dict") and callable(tool.dict):
            value = tool.dict()
        else:
            value = tool
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Tool definition is not JSON serializable: {type(tool).__name__}") from exc
