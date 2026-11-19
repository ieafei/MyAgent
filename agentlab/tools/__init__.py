"""工具层：协议（base）、注册表（registry）、内置工具（builtin/）。

这里只导出协议层。registry 与内置工具由调用方按需直接 import——
否则 `import agentlab.tools` 会把所有内置工具连带拖进来。
"""

from agentlab.tools.base import (
    DEFAULT_MAX_CHARS,
    Tool,
    ToolError,
    ToolResult,
    truncate_output,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "Tool",
    "ToolError",
    "ToolResult",
    "truncate_output",
]
