from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from app.api.routes.session_inputs import SubmitSessionInput, submit_input


def _request(submit):
    runtime = SimpleNamespace(session_inputs=SimpleNamespace(submit=submit))
    request = Request({
        "type": "http",
        "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
    })
    return request


def test_route_delegates_full_payload_and_maps_selection_validation_error():
    payload = SubmitSessionInput(
        client_message_id="message",
        user_input="hello",
        attachment_ids=["att_" + "a" * 32],
        replace_run_id="run-1",
        safety_review_mode="manual",
        llm={
            "response_mode": "stream",
            "reasoning_level": "high",
            "answer_client_name": "answer-client",
            "answer_model": "answer-model",
        },
    )
    calls = []

    def accepted(session_id, **kwargs):
        calls.append((session_id, kwargs))
        return {"accepted": True}

    assert submit_input("session", payload, _request(accepted)) == {"accepted": True}
    assert calls == [("session", payload.model_dump())]

    def invalid_selection(_session_id, **_kwargs):
        raise ValueError("unsupported reasoning selection")

    with pytest.raises(HTTPException) as error:
        submit_input("session", payload, _request(invalid_selection))
    assert error.value.status_code == 422
    assert error.value.detail == "unsupported reasoning selection"


def test_write_admission_refusal_is_explicit_and_never_mistaken_for_acceptance():
    import json

    from app.storage.keyed_write_admission import KeyedWriteAdmissionError

    def full(*_args, **_kwargs):
        raise KeyedWriteAdmissionError('timeout')

    response = submit_input('session', SubmitSessionInput(
        client_message_id='stable-id', user_input='hello', attachment_ids=[],
    ), _request(full))
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert json.loads(response.body) == {'detail': 'session_input_write_admission_timeout',
                                         'accepted': False, 'retry_safe': True}
