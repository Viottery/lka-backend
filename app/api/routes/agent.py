from fastapi import APIRouter, Request

from app.api.schemas import AgentTurnRequest, AgentTurnResponse

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/turn", response_model=AgentTurnResponse)
def run_agent_turn(payload: AgentTurnRequest, request: Request) -> AgentTurnResponse:
    result = request.app.state.runtime.run_agent_turn(
        session_id=payload.session_id,
        user_input=payload.user_input,
    )
    return AgentTurnResponse(**result.model_dump())
