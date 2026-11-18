"""模型适配层：把各家厂商的协议翻译成 agentlab.types 里的统一形状。"""

from agentlab.llm.base import (
    AuthError,
    BadRequestError,
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
    ResponseFormatError,
    ServerError,
    error_from_status,
)

__all__ = [
    "AuthError",
    "BadRequestError",
    "LLMClient",
    "LLMError",
    "NetworkError",
    "RateLimitError",
    "ResponseFormatError",
    "ServerError",
    "error_from_status",
]
