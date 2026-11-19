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
from agentlab.llm.openai_compat import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OpenAIClient,
    decode_response,
    encode_messages,
    encode_tools,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "AuthError",
    "BadRequestError",
    "LLMClient",
    "LLMError",
    "NetworkError",
    "OpenAIClient",
    "RateLimitError",
    "ResponseFormatError",
    "ServerError",
    "error_from_status",
    "decode_response",
    "encode_messages",
    "encode_tools",
]
