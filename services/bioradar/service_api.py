"""The endpoints a professional service calls on the radar: permission before each of its model requests,
and the outcome after. They are not the people's API: the caller is a service, identified by a token of
its own (BIORADAR_MODEL_ATTEMPT_TOKEN, never the login credential), and the endpoint is the one the
service has in its environment - no request can name another. See research/attempts.py for what is decided
and recorded."""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import attempts

router = APIRouter(prefix="/internal/v1/model-attempts")


def calling_service(request: Request):
    token = settings().model_attempt_token
    if not token:
        raise HTTPException(
            503, "BIORADAR_MODEL_ATTEMPT_TOKEN is not set: no service may send a model request"
        )
    given = request.headers.get("authorization", "")
    if not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "a service token is needed")


def refusal(error: attempts.Refused, contract: str):
    return JSONResponse(
        {"contract_version": contract, "valid": False, "recorded": False, "reason_code": error.code},
        status_code=error.status,
    )


@router.post("/authorize", dependencies=[Depends(calling_service)])
def authorize(body: Annotated[Any, Body()]):
    try:
        return attempts.authorize(Fabric(settings()), body)
    except attempts.Refused as error:
        return refusal(error, attempts.AUTHORIZE_V1)


@router.post("/{attempt_id}/outcome", dependencies=[Depends(calling_service)])
def outcome(attempt_id: str, body: Annotated[Any, Body()]):
    try:
        return attempts.settle(attempt_id, body)
    except attempts.Refused as error:
        return refusal(error, attempts.OUTCOME_V1)
