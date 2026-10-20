"""Budgeted source previews remain recoverable without evicting later evidence."""

import json
from types import SimpleNamespace

import pytest

from app.core.prompt_budget import PromptBudgeter, PromptBudgetExceeded


class CharCounter:
    def count_request(self, system_prompt, user_prompt, tools=None):
        return SimpleNamespace(count=len(system_prompt) + len(user_prompt),
                               method="test_chars", conservative=True)


def source(attachment_id, text):
    return {
        "attachment_id": attachment_id, "sha256": "source-hash", "text": text,
        "start_char": 0, "end_char": len(text), "total_chars": len(text),
        "next_start_char": None, "truncated": False,
        "_resource": {"identity": attachment_id, "read_package": "attachments",
                      "read_tool": "attachments.read",
                      "read_input": {"attachment_id": attachment_id, "start_char": 0}},
    }


def test_multiple_source_previews_fit_and_keep_later_evidence_and_continuations():
    payload = {"user_input": "Compare both documents", "input_sources": [
        source("first", "a" * 8000), source("second", "b" * 5000)],
        "observations": [{"text": "Evidence found on a later page"}]}
    budgeter = PromptBudgeter(CharCounter())
    fitted = budgeter.fit(system_prompt="system", user_prompt=json.dumps(payload), input_limit=2400)
    visible = json.loads(fitted.user_prompt)
    assert fitted.input_tokens <= 2400
    assert visible["observations"] == payload["observations"]
    assert visible["user_input"] == payload["user_input"]
    for original, reduced in zip(payload["input_sources"], visible["input_sources"], strict=True):
        assert reduced["attachment_id"] == original["attachment_id"]
        assert reduced["sha256"] == original["sha256"]
        assert original["text"].startswith(reduced["text"])
        assert reduced["truncated"] is True
        assert reduced["next_start_char"] == reduced["end_char"] == len(reduced["text"])
        assert reduced["read_more"]["input"] == {
            "attachment_id": original["attachment_id"], "start_char": reduced["end_char"]}
    omitted = sum(len(item["text"]) for item in payload["input_sources"]) - sum(
        len(item["text"]) for item in visible["input_sources"])
    assert fitted.omitted["input_source_preview_chars"] == omitted
    # A second fit with less space must keep the earlier omission accounting.
    second = budgeter.fit(system_prompt="system", user_prompt=fitted.user_prompt, input_limit=2000)
    assert second.input_tokens <= 2000
    assert second.omitted["input_source_preview_chars"] > omitted
    assert payload["input_sources"][0]["text"] == "a" * 8000


@pytest.mark.parametrize("resource", [None, {"identity": "missing-read-route"}])
def test_source_without_valid_continuation_fails_instead_of_discarding_text(resource):
    item = source("first", "a" * 8000)
    item["_resource"] = resource
    with pytest.raises(PromptBudgetExceeded):
        PromptBudgeter(CharCounter()).fit(system_prompt="system", input_limit=2000,
                                         user_prompt=json.dumps({"input_sources": [item]}))
