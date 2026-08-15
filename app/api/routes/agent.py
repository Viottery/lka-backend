from fastapi import APIRouter, Request

from app.api.schemas import AgentTurnRequest, AgentTurnResponse
from app.core.llm import LLMResponseMode

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/turn", response_model=AgentTurnResponse)
async def run_agent_turn_endpoint(
    payload: AgentTurnRequest,
    request: Request,
) -> AgentTurnResponse:
    llm_options = payload.llm
    result = await request.app.state.runtime.run_agent_turn_async(
        session_id=payload.session_id,
        user_input=payload.user_input,
        llm_client_name=llm_options.client_name if llm_options else None,
        llm_model=llm_options.model if llm_options else None,
        llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
    )
    return AgentTurnResponse(**result.model_dump())


def run_agent_turn(payload: AgentTurnRequest, request: Request) -> AgentTurnResponse:
    """Synchronous test helper preserving the old direct-call path."""

    llm_options = payload.llm
    result = request.app.state.runtime.run_agent_turn(
        session_id=payload.session_id,
        user_input=payload.user_input,
        llm_client_name=llm_options.client_name if llm_options else None,
        llm_model=llm_options.model if llm_options else None,
        llm_response_mode=llm_options.response_mode if llm_options else LLMResponseMode.TEXT,
    )
    return AgentTurnResponse(**result.model_dump())
