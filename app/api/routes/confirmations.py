from fastapi import APIRouter, Request

from app.api.schemas import ConfirmationDecisionRequest, ConfirmationResponse

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


@router.post("/{confirmation_id}", response_model=ConfirmationResponse)
def respond_confirmation(
    confirmation_id: str,
    payload: ConfirmationDecisionRequest,
    request: Request,
) -> ConfirmationResponse:
    result = request.app.state.runtime.confirm(confirmation_id, payload.decision)
    return ConfirmationResponse(**result)

