import pytest

from app.core.llm.errors import LLMClientError, LLMResponseParseError
from app.core.llm.responses import ResponsesStreamDecoder, parse_response, responses_payload


def test_payload_converts_messages_options_and_tools():
    payload = responses_payload(
        {
            "model": "m",
            "temperature": 0.2,
            "stream": True,
            "max_tokens": 80,
            "reasoning_effort": "high",
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "system", "content": "more rules"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,x", "detail": "low"},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "f"}},
        }
    )
    assert payload == {
        "model": "m",
        "temperature": 0.2,
        "stream": True,
        "store": False,
        "max_output_tokens": 80,
        "reasoning": {"effort": "high"},
        "instructions": "rules\n\nmore rules",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,x",
                        "detail": "low",
                    },
                ],
            }
        ],
        "text": {"format": {"type": "json_object"}},
        "tools": [
            {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}
        ],
        "tool_choice": {"type": "function", "name": "f"},
    }


def test_payload_rejects_audio():
    with pytest.raises(LLMClientError, match="audio input"):
        responses_payload({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]})


def test_parse_response_text_function_call_usage_and_finish_reason():
    content, calls, usage, finish = parse_response(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "ok"},
                        {"type": "refusal", "refusal": "no"},
                    ],
                },
                {"type": "function_call", "call_id": "c", "name": "f", "arguments": '{"x":1}'},
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
    )
    assert content == "okno"
    assert calls[0].id == "c" and calls[0].arguments == {"x": 1}
    assert usage == {"input_tokens": 3, "output_tokens": 4}
    assert finish == "tool_calls"


def test_parse_response_rejects_missing_output_and_failed_response():
    with pytest.raises(LLMResponseParseError):
        parse_response({"status": "completed"})
    with pytest.raises(LLMClientError, match="bad"):
        parse_response({"status": "failed", "error": {"message": "bad"}})


def test_stream_decoder_does_not_duplicate_finalized_tool_arguments():
    decoder = ResponsesStreamDecoder()
    added = decoder.decode(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "id": "i", "call_id": "c", "name": "f"},
        }
    )
    delta = decoder.decode(
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"x":1}'}
    )
    done = decoder.decode(
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"x":1}'}
    )
    complete = decoder.decode(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "function_call", "call_id": "c", "name": "f", "arguments": '{"x":1}'}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )
    assert added["choices"][0]["delta"]["tool_calls"][0]["id"] == "c"
    assert delta["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert done == {"choices": []}
    assert complete["choices"][0]["delta"] == {}
    assert complete["choices"][0]["finish_reason"] == "tool_calls"
    assert complete["usage"] == {"input_tokens": 1, "output_tokens": 2}


def test_stream_decoder_fallback_text_and_incomplete_terminal():
    decoder = ResponsesStreamDecoder()
    fallback = decoder.decode(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "final"}]}
                ],
            },
        }
    )
    assert fallback["choices"][0]["delta"]["content"] == "final"
    incomplete = ResponsesStreamDecoder().decode(
        {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        }
    )
    assert incomplete["choices"][0]["finish_reason"] == "length"
    assert incomplete["status"] == "incomplete"


def test_stream_decoder_propagates_provider_errors_and_ignores_lifecycle():
    decoder = ResponsesStreamDecoder()
    assert decoder.decode({"type": "response.created"}) == {"choices": []}
    assert decoder.decode(
        {"type": "response.failed", "response": {"error": {"message": "oops"}}}
    ) == {"error": {"message": "oops"}}


def test_stream_decoder_preserves_top_level_error_diagnostics():
    assert ResponsesStreamDecoder().decode({
        "type": "error", "code": "rate_limit_exceeded", "message": "Try later",
        "param": None, "sequence_number": 2,
    }) == {"error": {
        "code": "rate_limit_exceeded", "message": "Try later", "param": None,
        "sequence_number": 2,
    }}


def test_stream_decoder_done_event_emits_arguments_missing_from_deltas():
    decoder = ResponsesStreamDecoder()
    decoder.decode(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "c", "name": "f"},
        }
    )
    delta = decoder.decode(
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"x":1}'}
    )
    assert delta["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'


def test_stream_decoder_terminal_supplements_partial_text_and_some_tool_calls():
    decoder = ResponsesStreamDecoder()
    decoder.decode({"type": "response.output_text.delta", "delta": "hel"})
    decoder.decode(
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": "first", "name": "one"},
        }
    )
    decoder.decode(
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"a":'}
    )
    terminal = decoder.decode(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                    {
                        "type": "function_call",
                        "call_id": "first",
                        "name": "one",
                        "arguments": '{"a":1}',
                    },
                    {
                        "type": "function_call",
                        "call_id": "second",
                        "name": "two",
                        "arguments": '{"b":2}',
                    },
                ],
            },
        }
    )
    delta = terminal["choices"][0]["delta"]
    assert delta["content"] == "lo"
    assert delta["tool_calls"] == [
        {"index": 0, "id": "", "type": "function", "function": {"name": "", "arguments": "1}"}},
        {
            "index": 1,
            "id": "second",
            "type": "function",
            "function": {"name": "two", "arguments": '{"b":2}'},
        },
    ]
