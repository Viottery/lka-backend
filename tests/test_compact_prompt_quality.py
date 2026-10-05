"""Only encode constructed payloads compactly; never reinterpret raw JSON text.

Identity projections isolate serialization from existing evidence/view policies.
No provider is dispatched, and no production budget is changed by these fixtures.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from app.core import agent_turn as turn
from app.core import prompt_budget
from app.core.llm.models import LLMToolDefinition
from app.core.prompt_budget import PromptBudgeter
from app.core.prompt_tokens import PromptTokenCounter
from app.core.sessions import SessionRecentMessage
from app.core.tools import ToolResult

TEXT = '原文🙂\n  indentation\t"quoted": {"duplicate":1,"duplicate":2,"value":NaN} \\ path'
SCOPE = {"view_id": "frozen-view", "side_effect_level": "read", "full_workspace_authority": False,
    "allowed_tool_names": ["fixture.read"], "allowed_source_ids": ["source-1"],
    "allowed_paths": ["/workspace/原文"], "expires_at": "2026-10-06T00:00:00Z"}
VALUES = {"text": TEXT, "empty": "", "null": None, "bools": [False, True],
    "numbers": [0, -12.5, 9007199254740993], "punctuation": "$12.50 / 1/2 / v3.14"}
CACHE = {"artifact_id": "artifact-1", "sha256": "a" * 64, "next_offset": 406,
    "returned_bytes": 200, "total_bytes": 606, "has_more": True,
    "continuation_refs": [{"artifact_id": "artifact-1", "offset": 406, "path": "/rows/0"}]}
STAGES = ("route", "decision", "native_decision", "decision_repair", "safety_review",
    "tool_result_check", "answer", "context_answer", "context_summarize")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture
def fixture_loop(tmp_path, monkeypatch):
    loop = turn.AgentTurnLoop(session_service=None, tool_executor=None,
        llm_client=SimpleNamespace(supports_function_calling=False), log_dir=tmp_path)
    captures = []

    def complete(**kwargs):
        captures.append(kwargs)

    monkeypatch.setattr(loop, "_complete_text_with_retry", complete)
    monkeypatch.setattr(loop, "_complete_answer_with_recovery", complete)
    monkeypatch.setattr(loop, "_complete_control_generation", lambda **kw: (complete(**kw), None))
    monkeypatch.setattr(loop, "_context_window_for_llm", lambda value: value)
    monkeypatch.setattr(loop, "_route_context", lambda value: value)
    monkeypatch.setattr(loop, "_catalog_for_prompt", lambda value, **kwargs: value)
    monkeypatch.setattr(loop, "_observations_within_prompt_budget", lambda value, **kwargs: value)
    monkeypatch.setattr(loop, "_observations_for_answer_prompt", lambda value: value)
    monkeypatch.setattr(loop, "_route_locally", lambda value: {"selected_package": None})
    monkeypatch.setattr(loop, "_child_budget_for_prompt", lambda: {
        "max_tokens": 32768, "remaining_tokens": 16000, "remaining_llm_calls": 5,
        "finish_output_reserve_tokens": 1536, "prompt_overhead_tokens": 80})
    monkeypatch.setattr(loop, "_plan_patch_contract_for_prompt", lambda: {
        "plan_id": "canonical-plan", "expected_revision": 4, "replan_required": True,
        "eligible_failed_step_ids": ["failed-step"], "scope": SCOPE})
    monkeypatch.setattr(turn, "_current_output_contract", lambda: "Preserve facts and unknowns.\n" + TEXT)
    return loop, captures


def inputs():
    return {"user_input": TEXT, "route": {"selected_package": "fixture", "hint": TEXT},
        "context_window": {"summary": TEXT, "values": copy.deepcopy(VALUES), "authority": copy.deepcopy(SCOPE)},
        "package_catalog": [{"name": "fixture", "description": TEXT}],
        "expanded_package_names": ["fixture"], "expanded_tools": [{
            "name": "fixture.read", "description": TEXT, "read_only": True,
            "requires_confirmation": False, "side_effects": "none",
            "origin_constraints": {"scope": SCOPE},
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}},
                "required": ["id"], "additionalProperties": False}}],
        "observations": [{"tool_name": "fixture.read", "result": {"status": "completed", "output": VALUES},
            "_result_cache": copy.deepcopy(CACHE)}], "llm_events": []}


def generate(loop, stage, data):
    if stage == "route":
        loop._route(**{k: data[k] for k in ("user_input", "context_window", "package_catalog", "llm_events")},
            decision_events=[])
    elif stage == "decision":
        loop._decide_next_action_once(**data, plan_patch_repair={"patch_id": "same-id", "raw": TEXT})
    elif stage == "native_decision":
        loop._decide_next_action_with_native_tools(**data, completed_tool_calls=[],
            tools=[LLMToolDefinition(name="fixture_read", description=TEXT,
                parameters=data["expanded_tools"][0]["input_schema"])], actions={},
            plan_patch_repair={"patch_id": "same-id", "raw": TEXT})
    elif stage == "decision_repair":
        loop._repair_malformed_decision_output(**{k: data[k] for k in
            ("user_input", "route", "expanded_tools", "observations", "llm_events")}, raw_output=TEXT)
    elif stage == "safety_review":
        payload = {"review_id": "review-1", "tool_read_only": False, "scope": SCOPE, "values": VALUES}
        review = SimpleNamespace(review_id="review-1", tool_name="fixture.read", model_dump=lambda **kw: payload)
        manager = SimpleNamespace(decide_safety_review=lambda **kw: review,
            attach_safety_review_llm_output=lambda **kw: review)
        token = turn._turn_run_manager.set(manager)
        try:
            loop._decide_safety_review_with_llm(review, llm_events=[])
        finally:
            turn._turn_run_manager.reset(token)
    elif stage == "tool_result_check":
        loop._local_tool_feedback = lambda **kw: {"status": "failed", "message": TEXT, "scope": SCOPE}
        loop._check_tool_result(user_input=TEXT, tool_package="fixture", decision={"tool_input": VALUES},
            tool_result=ToolResult(invocation_id="invocation-1", tool_name="fixture.read", status="failed",
                output={"scope": SCOPE, "values": VALUES, "_result_cache": CACHE}, execution_started=False),
            llm_events=[])
    elif stage == "answer":
        loop._answer_with_llm(**{k: data[k] for k in
            ("user_input", "route", "context_window", "observations", "llm_events")},
            final_decision={"action": "final_answer", "reason": TEXT})
    elif stage == "context_answer":
        loop._answer_from_context_with_llm(**{k: data[k] for k in
            ("user_input", "route", "context_window", "llm_events")})
    else:
        message = SessionRecentMessage(role="user", content=TEXT, created_at="2026-10-05T00:00:00Z",
            trace_id="retained-trace")
        loop._summarize_context_window(summary=TEXT, messages_to_summarize=[message],
            retained_recent_messages=[message], token_budget=32768, llm_events=[])


@pytest.mark.parametrize("stage", STAGES)
def test_generated_stage_is_compact_without_changing_values_authority_or_cache_refs(fixture_loop, stage):
    loop, captures = fixture_loop
    data = inputs()
    before = copy.deepcopy(data)
    generate(loop, stage, data)
    assert len(captures) == 1
    raw = captures[0]["user_prompt"]
    visible = json.loads(raw)
    if "user_input" in visible:
        assert visible["user_input"] == TEXT
    if "session_context_window" in visible:
        assert visible["session_context_window"] == data["context_window"]
    if "observations" in visible:
        assert visible["observations"] == data["observations"]
    if "expanded_tools" in visible:
        assert visible["expanded_tools"] == loop._tools_for_prompt(data["expanded_tools"])
    if "child_budget" in visible:
        assert visible["child_budget"]["max_tokens"] == 32768
        assert visible["plan_patch_contract"]["expected_revision"] == 4
        assert visible["plan_patch_contract"]["scope"] == SCOPE
        assert visible["plan_patch_repair"] == {"patch_id": "same-id", "raw": TEXT}
    if stage == "native_decision":
        assert captures[0]["tools"][0].parameters == data["expanded_tools"][0]["input_schema"]
    if stage == "safety_review":
        assert visible["review"] == {"review_id": "review-1", "tool_read_only": False,
            "scope": SCOPE, "values": VALUES}
    if stage == "tool_result_check":
        assert visible["tool_result"]["execution_started"] is False
        assert visible["tool_result"]["output"] == {"scope": SCOPE, "values": VALUES, "_result_cache": CACHE}
    if stage == "decision_repair":
        assert visible["malformed_output"] == TEXT
    if stage == "context_summarize":
        assert visible["existing_summary"] == TEXT and visible["token_budget"] == 32768
        assert visible["retained_recent_messages"] == visible["messages_to_summarize"]
        assert visible["retained_recent_messages"][0]["content"] == TEXT
    assert data == before
    assert raw == compact(visible), f"{stage} reintroduced JSON formatting overhead"


def test_shared_serializer_preserves_nested_strings_scalars_and_order_without_mutation():
    serializer = getattr(prompt_budget, "serialize_prompt_payload", None)
    assert callable(serializer), "constructed payloads need one shared compact serializer"
    value = {"operation": {"type": "no_op"}, "assistant_message": TEXT,
        "authority": SCOPE, "_result_cache": CACHE, "values": VALUES}
    before = copy.deepcopy(value)
    encoded = serializer(value)
    assert encoded == compact(value) and json.loads(encoded) == before and value == before
    assert list(json.loads(encoded)) == list(value)


def test_budget_trim_recount_remains_compact_and_keeps_latest_refs_and_authority():
    data = inputs()
    latest = data["observations"][0]
    payload = {"user_input": TEXT, "scope": SCOPE, "values": VALUES,
        "session_context_window": data["context_window"],
        "observations": [{"old": "x" * 6000, "_result_cache": {"artifact_id": "old-artifact"}}, latest]}
    before = copy.deepcopy(payload)
    counter = PromptTokenCounter()
    wire = compact(payload)
    fitted = PromptBudgeter(counter).fit(system_prompt="frozen system", user_prompt=wire,
        input_limit=counter.count_request("frozen system", wire).count - 3000,
        output_reserve_tokens=1536)
    visible = json.loads(fitted.user_prompt)
    assert payload == before and visible["observations"] == [latest]
    for key in ("user_input", "scope", "values", "session_context_window"):
        assert visible[key] == payload[key]
    assert fitted.omitted == {"older_observations": 1, "observation_artifact_ids": ["old-artifact"]}
    assert fitted.output_reserve_tokens == 1536
    assert fitted.user_prompt == compact(visible), "trim recount must not restore pretty whitespace"


def test_child_answer_reserve_and_answer_dispatch_use_same_compact_encoding(fixture_loop, monkeypatch):
    loop, captures = fixture_loop
    fitted_calls = []
    counter = PromptTokenCounter()

    def fit(**kwargs):
        result = PromptBudgeter(counter).fit(system_prompt=kwargs["system_prompt"],
            user_prompt=kwargs["user_prompt"], tools=kwargs.get("tools"), input_limit=100_000,
            output_reserve_tokens=kwargs["max_output_tokens"])
        fitted_calls.append((kwargs, result))
        return result

    monkeypatch.setattr(loop, "_budget_llm_prompt", fit)
    data = inputs()
    control = {"user_input": TEXT, "route_context": data["route"],
        "session_context_window": data["context_window"]}
    estimate = loop._child_answer_input_estimate(user_prompt=compact(control),
        observations=data["observations"], fallback=100_000)
    forecast = json.loads(fitted_calls[0][0]["user_prompt"])
    generate(loop, "answer", data)
    answer = json.loads(captures[0]["user_prompt"])
    for key in ("user_input", "route_context", "session_context_window", "observations", "output_contract"):
        assert forecast[key] == answer[key]
    expected = counter.count_request("", compact(forecast)).count + turn.CHILD_FINISH_SYSTEM_RESERVE_TOKENS * 4
    assert estimate == expected, "reserve must count the same compact encoding, not a pretty clone"
    assert fitted_calls[0][0]["user_prompt"] == compact(forecast)
    assert captures[0]["user_prompt"] == compact(answer)


@pytest.mark.parametrize("raw", [
    '{"duplicate":1,"duplicate":2}', '{"value":NaN}', '{"value":Infinity}',
    ' {"literal": "keep  spacing\\ninside"} ', 'not JSON at all',
])
def test_under_limit_arbitrary_input_is_not_parsed_or_reconstructed(raw):
    fitted = PromptBudgeter(PromptTokenCounter()).fit(system_prompt="", user_prompt=raw, input_limit=1000)
    assert fitted.user_prompt == raw and fitted.omitted == {}


def test_private_log_json_block_stays_pretty(fixture_loop):
    loop, _ = fixture_loop
    block = loop._json_block({"values": VALUES, "authority": SCOPE})
    assert block == "```json\n" + json.dumps({"values": VALUES, "authority": SCOPE},
        ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
