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


def test_explicit_proxy_tokenizer_scales_and_pads_request_once(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel(vocab={"[UNK]": 0, "s": 1, "u": 2}, unk_token="[UNK]")
    )
    path = tmp_path / "proxy.json"
    tokenizer.save(str(path))
    counter = PromptTokenCounter(
        fallback_tokenizer_json_path=path,
        fallback_multiplier=1.5,
        fallback_extra_tokens=11,
    )
    result = counter.count_request("s", "u", [{"name": "t"}])
    assert result.count == 5 + 11 + 8 + 8
    assert result.method == "proxy_tokenizer_estimate"
    assert result.conservative is False
    assert result.approximate is True
    assert counter.count_text("s").count == 2  # ceil(1 * 1.5), without request padding
    assert counter.count_text("s").method == "proxy_tokenizer_estimate"


def test_exact_tokenizer_takes_priority_over_proxy(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel(vocab={"[UNK]": 0, "s": 1}, unk_token="[UNK]")
    )
    exact = tmp_path / "exact.json"
    proxy = tmp_path / "proxy.json"
    tokenizer.save(str(exact))
    tokenizer.save(str(proxy))
    result = PromptTokenCounter(
        exact, fallback_tokenizer_json_path=proxy, fallback_extra_tokens=999
    ).count_text("s")
    assert result.count == 1
    assert result.method == "local_tokenizers_json"
    assert result.approximate is False


def test_invalid_proxy_fails_closed_to_utf8_counter(tmp_path):
    path = tmp_path / "invalid-proxy.json"
    path.write_text("invalid", encoding="utf-8")
    result = PromptTokenCounter(fallback_tokenizer_json_path=path).count_text("你好")
    assert result.count == len("你好".encode())
    assert result.method == "utf8_byte_upper_bound"
    assert result.approximate is False
