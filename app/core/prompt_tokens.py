"""Provider-neutral prompt size estimates for chat completion requests.

When configured with a local ``tokenizers`` JSON file, this uses that tokenizer
for content. Without one, the result is a conservative UTF-8 byte upper bound
plus bounded chat/tool envelope overhead, not a claim about provider token use.
No tokenizer files are downloaded by this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A count with enough metadata to interpret its estimation method."""

    count: int
    method: str
    conservative: bool


class PromptTokenCounter:
    """Count system/user prompt content and serialized tool definitions.

    A configured path must name a valid local Hugging Face ``tokenizers`` JSON
    file. The optional ``tokenizers`` library is imported only in that mode.
    """

    _CHAT_ENVELOPE_TOKENS = 8
    _PER_TOOL_ENVELOPE_TOKENS = 8
    _MAX_TOOL_ENVELOPE_TOKENS = 4096

    def __init__(self, tokenizer_json_path: Path | None = None) -> None:
        self._tokenizer = None
        if tokenizer_json_path is None:
            return
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
            self._tokenizer = Tokenizer.from_file(str(path))
        except Exception as exc:
            raise ValueError(f"Invalid tokenizer JSON file: {path}") from exc

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

        if self._tokenizer is not None:
            content_count = sum(len(self._tokenizer.encode(chunk).ids) for chunk in chunks)
            return TokenCount(
                count=content_count + envelope,
                method="local_tokenizers_json",
                conservative=False,
            )

        byte_count = sum(len(chunk.encode("utf-8")) for chunk in chunks)
        return TokenCount(
            count=byte_count + envelope,
            method="utf8_byte_upper_bound",
            conservative=True,
        )

    def count_text(self, text: str) -> TokenCount:
        """Count plain text without a chat envelope (for session precompaction)."""
        if self._tokenizer is not None:
            return TokenCount(
                count=len(self._tokenizer.encode(text).ids),
                method="local_tokenizers_json",
                conservative=False,
            )
        return TokenCount(
            count=len(text.encode("utf-8")),
            method="utf8_byte_upper_bound",
            conservative=True,
        )

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
