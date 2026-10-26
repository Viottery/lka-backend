import hashlib
import json

from app.core.context_manifest import build_context_manifest
from app.core.prompt_tokens import PromptTokenCounter


def test_manifest_redacts_values_and_hostile_keys():
    hostile = "SECRET path=/private/session-123 query=needle"
    payload = {
        "session_context_window": {
            "summary": "secret summary", "summary_metadata": {"method": "atomic_records"},
            "recent_messages": [{"role": "user", "seq": 4, "content": hostile}],
            "recall": {"selected_count": 3, "omitted_count": 2},
            "secret-key-" + hostile: hostile,
        },
        "evil-key-" + hostile: {"nested": hostile},
    }
    user_prompt = json.dumps(payload)
    manifest = build_context_manifest(system_prompt="system secret", user_prompt=user_prompt,
        tools=[], token_counter=PromptTokenCounter(), budget_metadata={"input_budget": 1000,
        "unsafe_key_" + hostile: hostile})
    encoded = json.dumps(manifest)
    assert hostile not in encoded
    assert "secret-key" not in encoded and "evil-key" not in encoded
    assert manifest["budget"] == {"input_budget": 1000}
    section = manifest["sections"]["session_context_window"]
    assert section["recent_messages"] == {"count": 1, "roles": ["user"], "min_seq": 4, "max_seq": 4}
    assert section["summary"] == {"kind": "atomic_records", "bytes": len("secret summary")}
    assert section["recall_counts"] == {"selected": 3, "omitted": 2}


def test_manifest_facts_and_hashes_change_with_fitted_prompt():
    before = '{"session_context_window":{"recent_messages":[{"role":"user","seq":1,"content":"a"}]},"memory":[1,2,3]}'
    after = '{"session_context_window":{"recent_messages":[{"role":"user","seq":2,"content":"b"}]},"memory":[1]}'
    counter = PromptTokenCounter()
    first = build_context_manifest(system_prompt="s", user_prompt=before, tools=[], token_counter=counter,
        budget_metadata={"input_budget": 500})
    second = build_context_manifest(system_prompt="s", user_prompt=after, tools=[], token_counter=counter,
        budget_metadata={"input_budget": 500})
    assert first["request_tokens"] != second["request_tokens"]
    assert first["user_prompt_sha256"] == hashlib.sha256(before.encode()).hexdigest()
    assert second["user_prompt_sha256"] == hashlib.sha256(after.encode()).hexdigest()
    assert first["sections"]["memory"]["bytes"] > second["sections"]["memory"]["bytes"]
    assert first["sections"]["session_context_window"]["recent_messages"]["min_seq"] == 1
    assert second["sections"]["session_context_window"]["recent_messages"]["max_seq"] == 2


def test_manifest_unknown_top_level_keys_collapse_and_sections_are_bounded():
    payload = {f"arbitrary_{i}": "payload" for i in range(40)}
    manifest = build_context_manifest(system_prompt="", user_prompt=json.dumps(payload),
                                      token_counter=lambda value: len(value))
    assert len(manifest["sections"]) <= 16
    assert set(manifest["sections"]) == {"other"}
    assert manifest["request_count_method"] == "provided_counter"


def test_manifest_uses_supplied_request_count_and_filters_untrusted_labels():
    payload = {"session_context_window": {"summary": "x",
        "summary_metadata": {"method": "hostile /private/path"},
        "recent_messages": []}}

    class Counter:
        def count_text(self, value):
            assert value not in {"s", json.dumps(payload)}
            return len(value)

    manifest = build_context_manifest(system_prompt="s", user_prompt=json.dumps(payload),
        token_counter=Counter(), request_count=type("Count", (), {
            "count": 77, "method": "fit_count", "conservative": False,
            "approximate": True})(),
        budget_metadata={"input_budget": 100, "fit_attempt": "hostile budget text"})
    assert manifest["request_tokens"] == 77
    assert manifest["request_count_method"] == "fit_count"
    assert manifest["request_count_approximate"] is True
    assert manifest["sections"]["session_context_window"]["summary"]["kind"] == "unknown"
    assert manifest["budget"] == {"input_budget": 100}
