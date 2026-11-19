"""文件 4：工具协议。

工具系统只有三件事：

    ToolDefinition   告诉模型"我能干什么"——声明，不能执行
    Tool             真正干活的类
    ToolResult       干完之后交回来的话

其余的事——参数校验、超时、兜异常、查表——**不在这里**，在 registry.py。
所以这个文件不 import 网络、不 import 文件系统、不知道 agent 存在，
测它只需要构造普通对象。

两条贯穿全文件的取舍：

1. 工具失败是**数据**，不是异常。run 正常返回 ToolResult(is_error=True)，
   模型读到"文件不存在"会自己换条路；抛异常穿透主循环，等于剥夺它自救的机会。
   真正执行不下去（比如实现有 bug）时才抛 ToolError，由调用方兜住。

2. 结果一律是**字符串**。它最终要进 prompt，模型只读文本；结构化返回值
   进 prompt 前总得序列化一次，那不如在工具里就做完——顺便逼着我们想清楚
   "这个对象打印出来，模型能看懂吗"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from agentlab.types import ToolDefinition, ToolResultBlock

# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具执行的结果。

    content 是给模型看的文本，is_error 决定它是"成功"还是"失败"，
    truncated 是给我们自己看的（UI 上标一句"结果已截断"，别让人以为文件就这么长）。
    三者分开存，因为回填给模型时只用前两个，第三个纯属可观测性。
    """

    content: str
    is_error: bool = False
    truncated: bool = False

    @classmethod
    def ok(cls, content: str) -> ToolResult:
        return cls(content=content)

    @classmethod
    def fail(cls, content: str) -> ToolResult:
        """失败的写法。注意它仍是**正常返回**，不是抛异常。"""
        return cls(content=content, is_error=True)

    def to_block(self, tool_call_id: str) -> ToolResultBlock:
        """转成回填用的内容块。

        tool_call_id 必须来自模型给的那个调用 id：配错 id 时所有数据结构都合法，
        只有模型会拿着 A 的结果去推理 B，日志里极难发现。
        """
        return ToolResultBlock(
            tool_call_id=tool_call_id,
            content=self.content,
            is_error=self.is_error,
        )


class ToolError(Exception):
    """工具能解释清楚的失败，消息会原样交给模型。

    与"随便抛个异常"的区别在于**责任归属**：

    - ToolError：意料之中的坏输入，比如路径越界、目标不存在。模型看了能改主意。
    - 其他异常：我们的 bug。模型看了没用，应该进日志、带 traceback。
    """


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


class Tool(ABC):
    """一个可被模型调用的工具。

    子类只需要三样：name / description / parameters，外加 run。
    parameters 就是 JSON Schema，**原样**发给模型、原样拿去校验参数，
    两处共用同一份，定义与实际行为不会漂。

    description 不是注释，是 prompt 的一部分：模型靠它决定调不调、怎么调。
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, object] = {}

    @property
    def definition(self) -> ToolDefinition:
        """给模型看的声明。registry 注册时拿它建表，发给模型前拿它编码。"""
        if not self.name or not self.parameters:
            # 作者级错误，注册时就该炸，而不是等模型点名调到它才发现。
            raise ValueError(
                f"{type(self).__name__} 缺 name 或 parameters："
                "parameters 至少要 {'type': 'object', 'properties': {}}"
            )
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    @abstractmethod
    async def run(self, args: Mapping[str, object]) -> ToolResult:
        """执行一次调用。

        收 Mapping 而不是 **kwargs：参数来自 JSON，键名可以是任意字符串，
        而且校验是针对整个 dict 做的——拆成关键字参数后，错误信息就说不清
        是哪个键出了问题。

        args 进来前已经过 schema 校验，但"类型对"不等于"语义成立"：
        path 是字符串，但那个文件可能不存在。这类判断在 run 里做，用
        ToolResult.fail 返回。
        """


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS = 24_000
"""单个工具结果的字符上限。约 6k token，给历史留出余量。"""

_MARKER = "\n… [中间省略 {omitted} 字符] …\n"


def truncate_output(
    text: str,
    limit: int = DEFAULT_MAX_CHARS,
    *,
    head_ratio: float = 0.5,
) -> tuple[str, bool]:
    """超长输出砍中间，返回 (文本, 是否截断)。

    头尾都留，因为两边都可能有决定性信息：命令报错的结论在最后几行，
    文件开头的 import 和定义决定整体结构。砍中间丢的最少。

    head_ratio 让调用方按内容类型调比例：bash 的 traceback 结论在末尾，
    可以只给头 0.25；grep 的命中在前面，给头 0.8。默认对半。

    切口对齐到行边界，宁可少给几十个字符，也不让模型看到半行——
    半行会让它以为结构就到这（`def foo(x):` 后面跟着 `    ret`），
    据此去改文件必然匹配不上。

    这只是**兜底**：真正的解法是工具自带分页（read_file 的 offset/limit），
    让长输出一开始就不该整段塞进来，截断只是最后一道闸。
    """
    if not 0.0 < head_ratio < 1.0:
        raise ValueError(f"head_ratio 必须在 (0, 1) 之间，收到 {head_ratio!r}")
    if limit <= 0 or len(text) <= limit:
        return text, False

    head = int(limit * head_ratio)
    tail = limit - head

    # 头段退到最近的行首，尾段进到最近的行首；找不到换行（超长单行）才硬切。
    head_cut = text.rfind("\n", 0, head)
    if head_cut == -1:
        head_cut = head
    tail_cut = text.find("\n", len(text) - tail)
    tail_cut = len(text) - tail if tail_cut == -1 else tail_cut + 1

    omitted = tail_cut - head_cut
    marker = _MARKER.format(omitted=omitted)
    # 返回长度 ≈ limit + 标记长度：标记不占正文预算。
    return text[:head_cut] + marker + text[tail_cut:], True


__all__ = [
    "DEFAULT_MAX_CHARS",
    "Tool",
    "ToolError",
    "ToolResult",
    "truncate_output",
]
