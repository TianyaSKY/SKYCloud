"""公共 REST 错误响应契约; 第三方服务商与 MCP 传输协议保持独立."""

from typing import Generic, Literal, NotRequired, TypedDict, TypeVar, cast

from pydantic import BaseModel, ConfigDict
from runtime_common.json_types import JsonValue


class ValidationIssue(BaseModel):
    """精简且不包含敏感信息的请求校验问题明细."""

    model_config = ConfigDict(extra="forbid")
    loc: list[str | int]
    type: str
    msg: str


class ErrorDetail(BaseModel):
    """返回给 HTTP 客户端的稳定错误对象."""

    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    issues: list[ValidationIssue] | None = None


class ErrorResponse(BaseModel):
    """公开的 JSON 错误信封响应模型."""

    model_config = ConfigDict(extra="forbid")
    success: Literal[False] = False
    error: ErrorDetail


class ErrorDetailEnvelope(TypedDict):
    """由 ErrorDetail 生成的 JSON 安全错误字典对象."""

    code: str
    message: str
    issues: NotRequired[list[dict[str, JsonValue]]]


class ErrorEnvelope(TypedDict):
    """公开的 JSON 安全错误信封字典."""

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
    """构建经过校验的 JSON 安全错误信封."""
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
    """类型化 JSON 成功契约; 数据流、文件下载和 204 响应不使用此信封."""

    success: Literal[True] = True
    data: DataT


class SuccessEnvelope(TypedDict, Generic[DataT]):
    """包装了类型化数据值的 JSON 安全成功信封."""

    success: Literal[True]
    data: DataT


def success_payload(data: DataT) -> SuccessEnvelope[DataT]:
    """将数据包装在公开的成功信封中."""
    return {"success": True, "data": data}


class HealthResponse(BaseModel):
    """无外部依赖的进程健康检查响应载荷."""

    status: Literal["ok"]
