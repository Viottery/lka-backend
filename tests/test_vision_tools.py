import hashlib
import json

import pytest
from PIL import Image

from app.core.llm.errors import LLMProviderHTTPError
from app.core.llm.models import LLMResponse
from app.core.llm.registry import LLMClientRegistry
from app.core.llm.service import LLMService
from app.core.local_config import LLMProviderConfig
from app.core.tools import ToolContext, ToolInvocation
from app.tool_packages.filesystem import FileAccessPolicy
from app.tool_packages.vision import InspectImageTool
from tests.test_llm_vision import client


@pytest.fixture
def setup(tmp_path):
    image = tmp_path / "page.png"
    Image.new("RGB", (40, 50), "white").save(image)
    c = client(supports_vision=True)
    registry = LLMClientRegistry()
    registry.register_client(c)
    service = LLMService(config=LLMProviderConfig(default_client="test"), registry=registry)
    tool = InspectImageTool(FileAccessPolicy.from_workspace_roots([tmp_path]), service)
    return tool, c, image


def invoke(tool, image, approved=True, context=None, **extra):
    invocation = ToolInvocation(invocation_id="image", tool=tool.spec, session_id="session", context_id="context", input={"path": str(image), "question": "Read heading", **extra})
    return tool.invoke(invocation=invocation, context=context or ToolContext(session_id="session", safety_review_approved=approved))


def test_success_sanitized_provenance_and_usage(setup):
    tool, c, image = setup
    async def complete(request):
        assert request.messages[1].has_images
        return LLMResponse(provider="test", status="completed", content="Heading", prompt_summary="image", usage={"prompt_tokens": 110, "completion_tokens": 5, "total_tokens": 115})
    c.complete = complete
    result = invoke(tool, image, expected_sha256=hashlib.sha256(image.read_bytes()).hexdigest())
    assert result.status == "completed"
    assert result.output["llm_call"]["total_token_count"] == 115
    assert result.output["llm_call"]["metadata"]["reasoning"]["requested"] == "low"
    assert result.output["llm_call"]["metadata"]["reasoning"]["source"] == "default"
    assert result.output["provenance"]["width"] == 40
    assert "base64" not in result.model_dump_json()


@pytest.mark.parametrize("extra,approved,fragment", [({}, False, "safety review"), ({"expected_sha256": "0" * 64}, True, "does not match"), ({"question": ""}, True, "question")])
def test_preflight(setup, extra, approved, fragment):
    tool, _c, image = setup
    result = invoke(tool, image, approved=approved, **extra)
    assert result.status == "failed"
    assert fragment in result.error


def test_outside_path_denied(setup, tmp_path):
    tool, _c, _image = setup
    result = invoke(tool, tmp_path.parent / "external.png")
    assert result.status == "failed"
    assert "not approved" in result.error


def test_unsupported_and_pixel_bound(setup):
    tool, c, image = setup
    c.supports_vision = False
    assert "supports_vision" in invoke(tool, image).error
    Image.new("RGB", (2001, 2000)).save(image)
    assert "pixel limit" in invoke(tool, image).error


def test_provider_error_never_echoes_image_data(setup):
    tool, c, image = setup
    async def fail(request):
        raise LLMProviderHTTPError(status_code=400, message="data:image/png;base64,SECRET", error_category="invalid_request")
    c.complete = fail
    result = invoke(tool, image)
    assert result.status == "failed"
    assert result.output["llm_call"]["error_category"] == "invalid_request"
    assert result.output["llm_call"]["metadata"]["reasoning"]["requested"] == "low"
    assert result.output["llm_call"]["metadata"]["reasoning"]["status"] == "uncontrolled"
    assert "SECRET" not in json.dumps(result.model_dump())


def test_legacy_run_context_without_reasoning_keeps_provider_default(setup):
    tool, c, image = setup
    captured = []

    async def complete(request):
        captured.append(request)
        return LLMResponse(provider="test", status="completed", content="Heading", prompt_summary="image")

    c.complete = complete
    context = ToolContext(session_id="session", safety_review_approved=True,
                          llm_reasoning_source="legacy_run")
    result = invoke(tool, image, context=context)

    assert result.status == "completed"
    assert captured[0].reasoning_level is None
    assert result.output["llm_call"]["metadata"]["reasoning"]["source"] == "legacy_run"


def test_frozen_child_scope_denied_even_with_approval(setup):
    from app.core.context_driver import ToolView
    tool, _c, image = setup
    invocation = ToolInvocation(invocation_id="child", tool=tool.spec, session_id="session", context_id="context", input={"path": str(image), "question": "Read"})
    context = ToolContext(session_id="session", safety_review_approved=True, approved_paths=[str(image)],
                          tool_view=ToolView(snapshot_id="snapshot", child_run_id="child", side_effect_level="read", allowed_paths=(str(image.parent / "other"),)))
    result = tool.invoke(invocation=invocation, context=context)
    assert result.status == "failed"
    assert "scope" in result.error.lower()


def test_unsupported_file_and_corruption(setup):
    tool, _c, image = setup
    Image.new("RGB", (5, 5)).save(image, format="GIF")
    assert "PNG" in invoke(tool, image).error
    image.write_bytes(b"bad image")
    assert invoke(tool, image).status == "failed"


def test_missing_service_fails_cleanly(tmp_path):
    tool = InspectImageTool(FileAccessPolicy.from_workspace_roots([tmp_path]), None)
    result = invoke(tool, tmp_path / "image.png")
    assert result.status == "failed"
    assert "unavailable" in result.error


@pytest.mark.parametrize("status,finish_reason,partial", [
    ("completed", "length", False), ("completed", "max_tokens", False),
    ("completed", "stop", True), ("failed", None, False),
])
def test_incomplete_replies_are_not_successful_readings(setup, status, finish_reason, partial):
    tool, c, image = setup

    async def complete(request):
        return LLMResponse(provider="test", status=status, content="Partial evidence", prompt_summary="image",
                           finish_reason=finish_reason, partial=partial)

    c.complete = complete
    result = invoke(tool, image)
    assert result.status == "failed"
    assert result.output["content"] == "Partial evidence"
    assert "incomplete" in result.error
    assert result.output["llm_call"]["partial"] == (partial or finish_reason in {"length", "max_tokens"})


@pytest.mark.parametrize("limit", [None, 128, 512, 4096])
def test_output_limit_is_explicit_and_bounded(setup, limit):
    tool, c, image = setup

    async def complete(request):
        assert request.max_output_tokens == (1024 if limit is None else limit)
        return LLMResponse(provider="test", status="completed", content="Done", prompt_summary="image")

    c.complete = complete
    extra = {} if limit is None else {"max_output_tokens": limit}
    assert invoke(tool, image, **extra).status == "completed"


@pytest.mark.parametrize("limit", [True, 0, 127, 4097, "512"])
def test_invalid_output_limit_fails_before_model_dispatch(setup, limit):
    tool, c, image = setup

    async def complete(request):
        raise AssertionError("Invalid bounds must not dispatch")

    c.complete = complete
    result = invoke(tool, image, max_output_tokens=limit)
    assert result.status == "failed" and "max_output_tokens" in result.error
