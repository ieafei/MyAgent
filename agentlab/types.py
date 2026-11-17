"""文件 1：数据结构。

这是整个项目的**地基**，一行业务逻辑都没有，只有"东西长什么样"。

为什么第一个文件是它，而不是"先写个能跑的脚本"？
因为 agent 的 bug 有八成出在数据结构上：
工具结果和调用对不上号、历史里塞了半个 JSON、重试时把消息改脏了。
把这些形状先钉死，后面每个模块都只是"填空"，出问题也能立刻定位到类型。

三条设计原则（后面每个文件都会遵守）：

1. 零依赖。不 import httpx、不碰文件系统、不认识任何模型厂商。
   于是测试可以纯内存构造对象，不需要起服务、不需要 API key。
2. 不可变。frozen dataclass + tuple。历史是"只追加"的，
   一旦某条消息进了历史，就绝不允许有人回头改它。
3. 厂商中立。内部只认三种内容块。OpenAI 用 role="tool" 传结果这种细节，
   留给适配器去翻译，不许泄漏到这里。
"""

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 两个"枚举"
#
# 用 Literal 而不是 enum.Enum：这些值最终要原样进 JSON、日志、数据库。
# Literal 对 str 天然兼容，不需要到处写 .value，序列化时也不会冒出
# "StopReason.TOOL_USE" 这种噪音。
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant"]

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]
"""模型为什么停下来。这是主循环的**判断依据**，不是日志。

- end_turn  : 正常说完，这一轮可以结束
- tool_use  : 要求调用工具，主循环必须继续跑（agent 的心跳）
- max_tokens: 输出被截断，需要重试或要求模型继续
- refusal   : 模型拒绝回答，不该无脑重试
"""


# ---------------------------------------------------------------------------
# 内容块：一条消息里可以装什么
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    """一段纯文本。"""

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """模型发出的"我要调用某个工具"。

    id 由模型厂商生成（OpenAI 是 call_xxx），必须**原样回传**。
    模型靠这个 id 把"结果"和"请求"配对；写错或漏掉，
    轻则被服务端拒绝，重则模型把 A 的结果当成 B 的，直接产生幻觉。
    """

    id: str
    name: str
    input: dict[str, object]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """工具执行结果，回填给模型。

    is_error 不是"给人看的错误报告"，而是给模型的**控制信号**：
    它决定模型看到的是"这是结果"还是"你搞砸了，换条路走"。
    """

    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """一条消息 = 一个角色 + 若干内容块。

    为什么不是 Message(role, text) 这种简单结构？
    因为 assistant 的一步里可以**同时**有解释和多个工具调用：

        assistant(TextBlock("我先看看目录"),
                  ToolUseBlock(id="c1", name="list_files", input={"path": "."}),
                  ToolUseBlock(id="c2", name="read_file",  input={"path": "a.py"}))

    并行工具调用是 agent 效率的核心。用字符串模型你只能把它序列化回去再正则抠，
    那是维护地狱的起点。
    """

    role: Role
    content: tuple[ContentBlock, ...] = ()

    # -- 构造器：覆盖 95% 的调用场景 --------------------------------------

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls(role="system", content=(TextBlock(text),))

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls(role="user", content=(TextBlock(text),))

    @classmethod
    def assistant(cls, *blocks: ContentBlock) -> "Message":
        return cls(role="assistant", content=blocks)

    @classmethod
    def tool_results(cls, blocks: tuple[ToolResultBlock, ...]) -> "Message":
        """工具结果统一包成一条 user 消息。

        这是 Anthropic 的形态；OpenAI 要求拆成多条 role="tool"。
        内部统一成这种形态，历史最可读，拆的工作交给适配器。
        """
        return cls(role="user", content=blocks)

    # -- 视图方法：让调用方不用自己 isinstance 判断 ------------------------

    def text(self) -> str:
        """拼接所有文本块。日志、标题、兜底展示都用它。"""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        """这一步要执行哪些工具调用 —— 主循环靠它决定"继续还是收工"。"""
        return tuple(b for b in self.content if isinstance(b, ToolUseBlock))

    def tool_result_blocks(self) -> tuple[ToolResultBlock, ...]:
        """读取消息里的工具结果。

        注意这个方法**不能**叫 tool_results：那会和上面的 classmethod 同名，
        Python 只保留后定义的那个，于是 Message.tool_results([...]) 会拿一个
        tuple 去调实例方法，报 "tuple object has no attribute 'content'"。
        （这个坑我们上一版真踩过，所以名字取得长一点。）
        """
        return tuple(b for b in self.content if isinstance(b, ToolResultBlock))


# ---------------------------------------------------------------------------
# 工具说明书
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """给模型看的工具说明书。

    parameters 必须是 JSON Schema —— 这是所有厂商共同支持的唯一方言。
    我们不解析它，只透传；但执行前要用它校验参数（下一个功能的事）。
    """

    name: str
    description: str
    parameters: dict[str, object]


# ---------------------------------------------------------------------------
# 一次模型调用的账本和结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """token 账本。生产环境必须记账：成本、上下文水位、缓存命中全靠它。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class Completion:
    """一次模型调用（非流式视角）的完整结果。

    主循环只认它，不关心它是流式拼出来的还是整包返回的。
    这样以后加重试、加缓存、换成假客户端做测试，主循环一行都不用改。
    """

    message: Message
    stop_reason: StopReason
    usage: Usage = Usage()
    model: str = ""


__all__ = [
    "Completion",
    "ContentBlock",
    "Message",
    "Role",
    "StopReason",
    "TextBlock",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "Usage",
]
