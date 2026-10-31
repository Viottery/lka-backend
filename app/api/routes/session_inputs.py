"""Local user acceptance and inspection of durable session input queues."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.routes.memories import require_local_memory_control
from app.core.session_inputs import SessionInputConflict
from app.domains.session_attachments import SessionAttachmentError
from app.storage.keyed_write_admission import KeyedWriteAdmissionError

router = APIRouter(prefix='/sessions/{session_id}/inputs', tags=['session inputs'],
                   dependencies=[Depends(require_local_memory_control)])


class InputLLMOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    response_mode: Literal['stream', 'text'] = 'text'
    reasoning_level: Literal['off', 'low', 'high', 'max'] | None = None
    answer_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    answer_model: str | None = Field(default=None, min_length=1, max_length=200)


class SubmitSessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_message_id: str = Field(min_length=1, max_length=128)
    user_input: str = Field(max_length=100000)
    attachment_ids: list[str] = Field(max_length=4)
    replace_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    safety_review_mode: Literal['skip', 'llm', 'manual'] | None = None
    llm: InputLLMOptions | None = None


class ResumeSessionInputs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_id: str | None = Field(default=None, min_length=1, max_length=128)
    answer_client_name: str | None = Field(default=None, min_length=1, max_length=200)
    answer_model: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def require_recovery_target(self):
        if self.input_id is None and self.model_fields_set & {'answer_client_name', 'answer_model'}:
            raise ValueError('Answer model recovery requires input_id')
        return self


def _call(request, operation, session_id, **kwargs):
    try:
        return getattr(request.app.state.runtime.session_inputs, operation)(session_id, **kwargs)
    except KeyedWriteAdmissionError as exc:
        return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content={
            "detail": "session_input_write_admission_"+exc.reason,
            "accepted": False, "retry_safe": True,
        })
    except KeyError as exc:
        raise HTTPException(404, 'session_or_attachment_not_found') from exc
    except SessionInputConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionAttachmentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('', status_code=202)
def submit_input(session_id: str, payload: SubmitSessionInput, request: Request):
    return _call(request, 'submit', session_id, **payload.model_dump())


@router.get('')
def list_inputs(session_id: str, request: Request):
    return _call(request, 'snapshot', session_id)


@router.post('/resume')
def resume_inputs(session_id: str, request: Request, payload: ResumeSessionInputs | None = None):
    return _call(request, 'resume', session_id, **(payload.model_dump() if payload else {}))
