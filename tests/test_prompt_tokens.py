from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.core.prompt_tokens import PromptTokenCounter


class _ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict
    strict: bool = False


def test_fallback_counts_chinese_as_utf8_bytes_and_marks_estimate():
    system = "你是助手"
    user = "总结这段中文"
    result = PromptTokenCounter().count_request(system, user)
    assert result.count == len(system.encode("utf-8")) + len(user.encode("utf-8")) + 8
    assert result.method == "utf8_byte_upper_bound"
    assert result.conservative is True


def test_plain_text_count_has_no_chat_envelope():
    result = PromptTokenCounter().count_text("你好")
    assert result.count == len("你好".encode())
    assert result.method == "utf8_byte_upper_bound"


def test_fallback_includes_json_unicode_and_serialized_tool_schema():
    tool = _ToolDefinition(
        name="memory.search",
        description="Search 用户记忆 🧭",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索词"}},
            "required": ["query"],
        },
    )
    serialized = json.dumps(
        tool.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result = PromptTokenCounter().count_request("system", '{"question":"你好"}', [tool])
    expected = (
        len(b"system")
        + len('{"question":"你好"}'.encode())
        + len(serialized.encode("utf-8"))
        + 8
        + 8
    )
    assert result.count == expected
    assert result.conservative is True


def test_fallback_accepts_json_compatible_tool_mappings():
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    count = PromptTokenCounter().count_request("s", "u", [tool])
    assert count.count > len(json.dumps(tool, sort_keys=True).encode())


def test_missing_tokenizer_json_path_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="Tokenizer JSON file does not exist"):
        PromptTokenCounter(tmp_path / "missing.json")


def test_invalid_tokenizer_json_fails_without_download(tmp_path):
    tokenizer_path = tmp_path / "invalid.json"
    tokenizer_path.write_text("not tokenizer json", encoding="utf-8")
    try:
        import tokenizers  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="not installed"):
            PromptTokenCounter(tokenizer_path)
    else:
        with pytest.raises(ValueError, match="Invalid tokenizer JSON"):
            PromptTokenCounter(tokenizer_path)
