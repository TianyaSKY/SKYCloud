"""Public REST error contract; provider/MCP wire protocols remain separate."""
from typing import Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    loc: list[str | int]
    type: str
    msg: str


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    message: str
    issues: list[ValidationIssue] | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    success: Literal[False] = False
    error: ErrorDetail


HTTP_ERROR_CODES = {400:'INVALID_REQUEST',401:'AUTHENTICATION_REQUIRED',403:'PERMISSION_DENIED',
    404:'NOT_FOUND',405:'METHOD_NOT_ALLOWED',409:'CONFLICT',413:'PAYLOAD_TOO_LARGE',
    422:'VALIDATION_ERROR',429:'RATE_LIMITED',500:'INTERNAL_ERROR',503:'SERVICE_UNAVAILABLE'}


def error_payload(status, message, *, code=None, issues=None):
    return ErrorResponse(error=ErrorDetail(code=code or HTTP_ERROR_CODES.get(status,'HTTP_ERROR'),
        message=message,issues=issues)).model_dump(exclude_none=True)


DataT = TypeVar('DataT')


class SuccessResponse(BaseModel, Generic[DataT]):
    """Typed JSON success contract; streams, downloads and 204 have no envelope."""
    success: Literal[True] = True
    data: DataT


def success_payload(data):
    return {"success": True, "data": data}


class HealthResponse(BaseModel):
    status: Literal["ok"]
