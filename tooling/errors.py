"""工具层的错误码与异常契约。

工具调用失败分三类，调用方必须能区分：

- 调用方错误（参数非法、工具不存在）：不重试、不计熔断；
- 依赖故障（超时、handler 异常、熔断打开）：只读工具有限重试，计入熔断；
- 协议/限额问题（tool_use 格式非法、调用次数超限）：终止或降级，不执行工具。

错误码会原样写进 `tool_result.error.code`，因此保持稳定、全小写。
"""
from __future__ import annotations

from enum import Enum


class ToolErrorCode(str, Enum):
    """工具调用的稳定错误码；新增值只追加，不改名。"""

    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    TOOL_PROTOCOL_UNSUPPORTED = "tool_protocol_unsupported"

    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_SEMANTIC = "invalid_semantic"
    NEEDS_MORE_INFO = "needs_more_info"
    INVALID_RESULT = "invalid_result"

    TOOL_TIMEOUT = "tool_timeout"
    TOOL_DEADLINE_EXCEEDED = "tool_deadline_exceeded"
    TOOL_HANDLER_ERROR = "tool_handler_error"
    TOOL_CIRCUIT_OPEN = "tool_circuit_open"

    RESULT_TRUNCATED = "result_truncated"
    TOO_MANY_TOOL_CALLS = "too_many_tool_calls"
    DUPLICATE_TOOL_CALL = "duplicate_tool_call"
    TOOL_ANSWER_FAILED = "tool_answer_failed"


class ToolError(Exception):
    """工具层异常基类。

    `retryable` 只描述依赖是否能再试一次，不代表编排层一定会重试；
    参数错误、未知工具等调用方错误的 `retryable` 固定为 False。
    """

    code: ToolErrorCode = ToolErrorCode.TOOL_HANDLER_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ToolErrorCode | None = None,
        retryable: bool | None = None,
    ) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message 必须是非空字符串")
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.retryable = self.code in {
            ToolErrorCode.TOOL_TIMEOUT,
            ToolErrorCode.TOOL_DEADLINE_EXCEEDED,
            ToolErrorCode.TOOL_HANDLER_ERROR,
        } if retryable is None else bool(retryable)


class ToolValidationError(ToolError):
    """JSON Schema 参数校验失败。

    T2 会把 `jsonschema.ValidationError` 转成这个异常；
    `path`/`validator` 用于生成模型可读的 repair hint。
    """

    code = ToolErrorCode.INVALID_ARGUMENTS

    def __init__(
        self,
        message: str,
        *,
        path: str = "参数",
        validator: str | None = None,
    ) -> None:
        super().__init__(message, code=ToolErrorCode.INVALID_ARGUMENTS, retryable=False)
        self.path = path
        self.validator = validator
