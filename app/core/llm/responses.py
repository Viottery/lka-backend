"""Codec helpers for the OpenAI Responses API wire format."""

from __future__ import annotations

import json
from typing import Any

from app.core.llm.errors import LLMClientError, LLMProviderStreamError, LLMResponseParseError
from app.core.llm.models import LLMToolCall


def responses_payload(chat_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert the generic chat-completions request shape to Responses input."""
    result: dict[str, Any] = {
        "model": chat_payload.get("model"),
        "input": [],
        "temperature": chat_payload.get("temperature", 0),
        "stream": chat_payload.get("stream", False),
        "store": False,
    }
    if "max_tokens" in chat_payload:
        result["max_output_tokens"] = chat_payload["max_tokens"]
    if chat_payload.get("reasoning_effort") is not None:
        result["reasoning"] = {"effort": chat_payload["reasoning_effort"]}
    messages = chat_payload.get("messages", [])
    if not isinstance(messages, list):
        raise LLMClientError("Responses API messages must be a list.")
    instructions: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise LLMClientError("Responses API message must be an object.")
        role, content = message.get("role"), message.get("content", "")
        if role == "system":
            instructions.append(content if isinstance(content, str) else _text_content(content))
            continue
        if role == "tool":
            result["input"].append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": content,
                }
            )
            continue
        if role not in {"user", "assistant"}:
            raise LLMClientError(f"Unsupported Responses API message role: {role!r}.")
        result["input"].append({"role": role, "content": _content(content)})
    if instructions:
        result["instructions"] = "\n\n".join(instructions)
    if "response_format" in chat_payload:
        fmt = chat_payload["response_format"]
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            result["text"] = {"format": {"type": "json_object"}}
        elif isinstance(fmt, dict):
            result["text"] = {"format": fmt}
    tools = chat_payload.get("tools")
    if isinstance(tools, list):
        result["tools"] = []
        for item in tools:
            fn = item.get("function", {}) if isinstance(item, dict) else {}
            if not isinstance(fn, dict):
                continue
            definition = {
                "type": "function",
                "name": fn.get("name"),
                "parameters": fn.get("parameters", {}),
            }
            if fn.get("description"):
                definition["description"] = fn["description"]
            if fn.get("strict") is not None:
                definition["strict"] = fn["strict"]
            result["tools"].append(definition)
    if "tool_choice" in chat_payload:
        choice = chat_payload["tool_choice"]
        if isinstance(choice, dict) and isinstance(choice.get("function"), dict):
            choice = {"type": "function", "name": choice["function"].get("name")}
        result["tool_choice"] = choice
    return result


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return (
        "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if isinstance(content, list)
        else ""
    )


def _content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    converted = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            converted.append({"type": "input_text", "text": block.get("text", "")})
        elif block.get("type") == "image_url":
            image = block.get("image_url") if isinstance(block.get("image_url"), dict) else {}
            image_input = {"type": "input_image", "image_url": image.get("url")}
            if image.get("detail") is not None:
                image_input["detail"] = image["detail"]
            converted.append(image_input)
        elif block.get("type") in {"input_audio", "audio", "audio_url"}:
            raise LLMClientError("Responses API codec does not support audio input.")
    return converted


def parse_response(
    payload: dict[str, Any],
) -> tuple[str, list[LLMToolCall], dict[str, Any], str | None]:
    if not isinstance(payload, dict):
        raise LLMResponseParseError("Responses API returned a non-object response.")
    if payload.get("error") or payload.get("status") == "failed":
        error = payload.get("error") or {}
        error = error if isinstance(error, dict) else {"message": str(error)}
        raise LLMProviderStreamError(
            message=error.get("message") or "Responses API response failed.",
            error_event={"type": "response.failed", "error": error},
        )
    status = payload.get("status")
    if status is not None and status not in {"completed", "incomplete", "failed"}:
        raise LLMResponseParseError(f"Responses API returned unexpected status: {status!r}.")
    content_parts, calls = [], []
    output = payload.get("output")
    if not isinstance(output, list):
        raise LLMResponseParseError("Responses API response did not include output.")
    for item in output:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "message":
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text":
                    content_parts.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    content_parts.append(part.get("refusal", ""))
        elif typ == "function_call":
            raw = item.get("arguments", "")
            try:
                args = json.loads(raw) if isinstance(raw, str) and raw else {}
            except json.JSONDecodeError:
                args = None
            calls.append(
                LLMToolCall(
                    id=item.get("call_id") or item.get("id"),
                    name=item.get("name", ""),
                    arguments=args if isinstance(args, dict) else None,
                    raw_arguments=raw if isinstance(raw, str) else "",
                )
            )
    details = payload.get("incomplete_details")
    finish = (
        "length"
        if status == "incomplete" or isinstance(details, dict)
        else ("tool_calls" if calls else "stop")
    )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return "".join(content_parts), calls, usage, finish


class ResponsesStreamDecoder:
    """Translate Responses SSE event objects into chat-completions chunks."""

    def __init__(self) -> None:
        self._tools: dict[int, dict[str, str]] = {}
        self._output_index: dict[int, int] = {}
        self._text = ""

    def decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        typ = payload.get("type")
        if typ in {"error", "response.failed"}:
            err = payload.get("error") or (payload.get("response") or {}).get("error")
            if err is None and typ == "error":
                err = {key: value for key, value in payload.items() if key != "type"}
            err = err or {}
            return {"error": err if isinstance(err, dict) else {"message": str(err)}}
        if typ == "response.output_text.delta":
            delta = payload.get("delta", "")
            self._text += delta if isinstance(delta, str) else ""
            return self._chunk({"content": delta if isinstance(delta, str) else ""})
        if typ == "response.output_item.added":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                index = self._index(payload, item)
                self._tools[index] = {
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": "",
                }
                return self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": self._tools[index]["id"],
                                "type": "function",
                                "function": {"name": self._tools[index]["name"], "arguments": ""},
                            }
                        ]
                    }
                )
        if typ == "response.function_call_arguments.delta":
            index = self._index(payload)
            part = self._tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            delta = payload.get("delta", "")
            part["arguments"] += delta if isinstance(delta, str) else ""
            return self._chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": delta},
                        }
                    ]
                }
            )
        if typ == "response.function_call_arguments.done":
            index = self._index(payload)
            part = self._tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            final = payload.get("arguments", "")
            suffix = self._missing_suffix(part["arguments"], final)
            part["arguments"] += suffix
            if suffix:
                return self._tool_delta(index, suffix)
            return {"choices": []}
        if typ in {"response.completed", "response.incomplete"}:
            response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
            parsed_text, calls, usage, _finish = parse_response(response)
            delta: dict[str, Any] = {}
            text_suffix = self._missing_suffix(self._text, parsed_text)
            if text_suffix:
                delta["content"] = text_suffix
                self._text += text_suffix
            recovered = []
            for call in calls:
                index = next(
                    (i for i, part in self._tools.items() if call.id and part["id"] == call.id),
                    None,
                )
                if index is None:
                    index = len(self._tools)
                    while index in self._tools:
                        index += 1
                    self._tools[index] = {"id": call.id or "", "name": call.name, "arguments": ""}
                    recovered.append(
                        {
                            "index": index,
                            "id": call.id or "",
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.raw_arguments},
                        }
                    )
                    self._tools[index]["arguments"] = call.raw_arguments
                else:
                    part = self._tools[index]
                    suffix = self._missing_suffix(part["arguments"], call.raw_arguments)
                    if suffix:
                        recovered.append(
                            {
                                "index": index,
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": suffix},
                            }
                        )
                        part["arguments"] += suffix
            if recovered:
                delta["tool_calls"] = recovered
            reason = (
                "length" if typ == "response.incomplete" else ("tool_calls" if calls else "stop")
            )
            return {
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                "usage": usage,
                "status": response.get(
                    "status", "incomplete" if typ == "response.incomplete" else "completed"
                ),
            }
        return {"choices": []}

    def _index(self, payload: dict[str, Any], item: dict[str, Any] | None = None) -> int:
        oi = payload.get("output_index")
        if isinstance(oi, int):
            return self._output_index.setdefault(oi, len(self._output_index))
        item = item or {}
        call_id = payload.get("item_id") or item.get("id") or item.get("call_id")
        for idx, part in self._tools.items():
            if call_id and part["id"] == call_id:
                return idx
        return len(self._tools)

    @staticmethod
    def _missing_suffix(current: str, final: Any) -> str:
        if not isinstance(final, str) or not final.startswith(current):
            return ""
        return final[len(current) :]

    @staticmethod
    def _tool_delta(index: int, arguments: str) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": arguments},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }

    @staticmethod
    def _chunk(delta: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
