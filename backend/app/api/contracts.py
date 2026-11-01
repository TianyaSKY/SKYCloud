"""Public REST error contract; provider/MCP wire protocols remain separate."""

from typing import Generic, Literal, NotRequired, TypedDict, TypeVar, cast

from pydantic import BaseModel, ConfigDict
from runtime_common.json_types import JsonValue


class ValidationIssue(BaseModel):
    """A compact, non-sensitive request validation issue."""

    model_config = ConfigDict(extra="forbid")
    loc: list[str | int]
    type: str
    msg: str


class ErrorDetail(BaseModel):
    """The stable error object returned to HTTP clients."""

    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    issues: list[ValidationIssue] | None = None


class ErrorResponse(BaseModel):
    """The public JSON error envelope."""

    model_config = ConfigDict(extra="forbid")
    success: Literal[False] = False
    error: ErrorDetail


class ErrorDetailEnvelope(TypedDict):
    """The JSON-safe error object produced from ErrorDetail."""

    code: str
    message: str
    issues: NotRequired[list[dict[str, JsonValue]]]


class ErrorEnvelope(TypedDict):
    """The JSON-safe public error envelope."""

    success: Literal[False]
    error: ErrorDetailEnvelope


HTTP_ERROR_CODES: dict[int, str] = {
    400: "INVALID_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def error_payload(
    status: int,
    message: str,
    *,
    code: str | None = None,
    issues: list[ValidationIssue] | None = None,
) -> ErrorEnvelope:
    """Build a validated, JSON-safe error envelope."""
    response = ErrorResponse(
        error=ErrorDetail(
            code=code or HTTP_ERROR_CODES.get(status, "HTTP_ERROR"),
            message=message,
            issues=issues,
        ),
    )
    return cast("ErrorEnvelope", response.model_dump(exclude_none=True))


DataT = TypeVar("DataT")


class SuccessResponse(BaseModel, Generic[DataT]):
    """Typed JSON success contract; streams, downloads and 204 have no envelope."""

    success: Literal[True] = True
    data: DataT


class SuccessEnvelope(TypedDict, Generic[DataT]):
    """The JSON-safe success envelope around a typed data value."""

    success: Literal[True]
    data: DataT


def success_payload(data: DataT) -> SuccessEnvelope[DataT]:
    """Wrap data in the public success envelope."""
    return {"success": True, "data": data}


class HealthResponse(BaseModel):
    """The dependency-free process health payload."""

    status: Literal["ok"]
