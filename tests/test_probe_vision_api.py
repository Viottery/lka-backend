from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.probe_vision_api import (
    QUESTION,
    case_passed,
    collect_stream,
    image_request,
    main,
    make_case,
    score,
)


def test_probe_default_never_dispatches(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["probe_vision_api.py"])
    monkeypatch.setattr("scripts.probe_vision_api.run_probe", lambda *args: (_ for _ in ()).throw(AssertionError("must not run")))
    assert main() == 0
    assert "No requests sent" in capsys.readouterr().out


def test_randomized_visual_truth_is_not_in_text_prompt(tmp_path):
    path = tmp_path / "case.png"
    truth = make_case(path)
    assert truth["code"] not in QUESTION
    assert truth["total"] == sum(truth["row_values"])
    with Image.open(path) as image:
        assert image.size == (760, 640)
    request = image_request(path, client_name="test", model="visual", thinking=False)
    assert request.max_output_tokens == 512
    assert request.messages[0].content[0].text == QUESTION
    assert request.messages[0].has_images
    assert request.thinking_enabled is False


def test_probe_requires_all_fields_not_just_json_acceptance():
    truth = {"code": "LKA-ABC", "red_squares": 3, "blue_circles": 1, "row_values": [4, 7, 2], "total": 13}
    assert score(json.dumps(truth), truth)["passed"]
    assert not score(json.dumps({**truth, "total": 12}), truth)["passed"]
    assert not score("I can see a table.", truth)["passed"]


@pytest.mark.parametrize("metadata", [
    {"status": "incomplete", "partial": True}, {"finish_reason": "length"},
    {"finish_reason": "max_tokens"},
])
def test_correct_json_does_not_mask_an_incomplete_stream(metadata):
    expected = {"code": "LKA-TEST"}

    class Service:
        async def stream(self, request):
            yield SimpleNamespace(event_type="llm_completed", content_snapshot=json.dumps(expected), metadata=metadata)

    row = asyncio.run(collect_stream(Service(), None))
    row["score"] = score(row["content"], expected)
    assert row["score"]["passed"]
    assert not case_passed(row)
