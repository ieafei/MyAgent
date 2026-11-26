"""文件 1：数据结构（OpenAI 格式）。

整个 agent 只流动这几种对象，别的都是它们的组合：

    Message     角色 + 内容块        —— 发给模型的东西
    Completion  消息 + 停止原因 + 账本 —— 模型回来的东西

三条硬规矩：

1. 零依赖。只 import 标准库的 dataclasses / typing，不碰网络和文件。
   于是测试可以纯内存构造对象，不需要 API key。
2. 不可变。frozen=True + tuple。历史是只追加的，进了历史就不许回头改。
3. OpenAI 原生。role 用 system/user/assistant/tool 四种，工具调用是
   tool_calls，参数是 JSON 字符串。不另造一套方言，将来接 DeepSeek /
   Kimi / vLLM 时，适配器只剩"发 HTTP + 拼 JSON"这一层。
"""

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 两个枚举
#
# 用 Literal 而不是 enum.Enum：这些值最终要原样进 JSON、日志、数据库。
# Literal 对 str 天然兼容，不需要到处写 .value。
# 代价：运行时完全不校验，只有 mypy 会拦。
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool"]
"""四种角色，与 OpenAI 完全一致。

assistant 可以同时带文本和 tool_calls；tool 是工具结果的回填消息，
一条 tool 消息只对应一个工具调用。
"""

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]
"""模型为什么停下来。这是主循环的**判断依据**，不是日志。

与 OpenAI 的 finish_reason 一一对应，翻译在适配器里做：

    "stop"           → end_turn    说完，收工
    "tool_calls"     → tool_use    要调工具，继续跑（agent 的心跳）
    "length"         → max_tokens  被截断
    "content_filter" → refusal     被拦截，别重试
"""


# ---------------------------------------------------------------------------
# 内容块：一条消息里能装什么
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    """一段纯文本。"""

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    """模型发出的"我要调用某个工具"。

    id 由模型生成（OpenAI 是 call_xxx），必须**原样回传**。
    模型靠它把结果和请求配对；配错 id，它会拿着 A 的结果推理 B，
    而日志里所有数据结构都是合法的，极难查。

    arguments 是 **JSON 字符串**，不是 dict。两个理由：
    1. OpenAI 协议里它就是字符串，保持原样 = 不重复编解码、不丢信息；
    2. 流式返回时它是碎片拼起来的，最终也只能落到一个字符串上。
    dict 由工具注册表在执行前 json.loads 并校验。
    """

    id: str
    name: str
    arguments: str
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """工具执行结果，回填给模型。

    content 是字符串而不是任意对象：这一块最终要进 prompt，模型只能读文本。
    is_error 是给我们自己看的（OpenAI 协议里没有这个字段，适配器会丢弃它，
    或者把错误文本写成模型能读懂的样子）——它决定 UI 是显示成失败还是成功。
    """

    tool_call_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolCallBlock | ToolResultBlock


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """一条消息 = 一个角色 + 若干内容块。

    assistant 的一步里可以同时有解释和多个工具调用：

        Message.assistant(
            TextBlock("我先看看目录"),
            ToolCallBlock(id="c1", name="list_files", arguments='{"path": "."}'),
            ToolCallBlock(id="c2", name="read_file",  arguments='{"path": "a.py"}'),
        )

    并行工具调用是 agent 效率的核心。所以内容是"块"而不是一个字符串。
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
    def tool(cls, block: ToolResultBlock) -> "Message":
        """一条工具结果 = 一条 role="tool" 消息（OpenAI 的形态）。"""
        return cls(role="tool", content=(block,))

    @classmethod
    def from_tool_results(cls, blocks: tuple[ToolResultBlock, ...]) -> tuple["Message", ...]:
        """一批工具结果 → 一批 tool 消息。

        注意返回的是**元组**，因为 OpenAI 要求一个 tool_call 对应一条
        role="tool" 消息，缺一条就 400。批量调用时用：
            history.extend(Message.from_tool_results(blocks))
        这个函数的存在就是为了不让人"算完一个填一个"。

        名字带 from_ 前缀：它是**构造器**，不要和实例方法 tool_results()
        搞混 —— 那个是读一条消息里已有的结果。
        """
        return tuple(cls.tool(b) for b in blocks)

    # -- 视图方法：让调用方不用自己 isinstance 判断 ------------------------

    def text(self) -> str:
        """拼接所有文本块。日志、标题、兜底展示都用它。"""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        """这一步要执行哪些工具调用 —— 主循环靠它决定"继续还是收工"。"""
        return tuple(b for b in self.content if isinstance(b, ToolCallBlock))

    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        """读取消息里的工具结果。

        三个视图与三个内容块一一对应，命名只差 Block 后缀：
            text()          → TextBlock
            tool_calls()    → ToolCallBlock
            tool_results()  → ToolResultBlock
        造消息请用类方法 Message.from_tool_results()。
        """
        return tuple(b for b in self.content if isinstance(b, ToolResultBlock))


# ---------------------------------------------------------------------------
# 工具说明书
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """给模型看的工具说明书。

    叫 Definition 不叫 Spec：它是给模型看的**声明**，本身不能执行；
    将来 tools/base.py 里可执行的工具叫 Tool。定义与实现分开命名，
    读代码时不用猜哪个能 run。

    三个字段正好等于 OpenAI 的 function 对象，适配器直接包一层
    {"type": "function", "function": {...}} 就能发出去。

    parameters 必须是 JSON Schema。我们不解析它，只透传；
    执行前用它校验参数那是工具注册表的事。
    description 不是注释，是 prompt 的一部分，直接影响模型选不选这个工具。
    """

    name: str
    description: str
    parameters: dict[str, object]


# ---------------------------------------------------------------------------
# 一次模型调用的账本和结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """token 账本。

    cached_input_tokens 是 input_tokens 里命中缓存的那部分，
    它包含在 input_tokens 内（OpenAI 的 prompt_tokens_details.cached_tokens
    和 DeepSeek 的 prompt_cache_hit_tokens 都是这个语义）。

    单独记它是因为价格差一个数量级：不分离出来，成本估算会离谱。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """没命中缓存、按全价计费的输入。"""
        return max(0, self.input_tokens - self.cached_input_tokens)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class Completion:
    """一次模型调用的完整结果。

    主循环只认它，不关心它是整包返回的还是流式拼出来的。
    以后加重试、加缓存、换假客户端测试，主循环一行都不用改。

    stop_reason 是独立字段而不是从 message 推出来的：两者不等价。
    没有内容块可能是正常说完，也可能是被截断；有工具调用块也可能是
    截断导致参数不完整。这两条信息在协议里本来是两个字段。
    """

    message: Message
    stop_reason: StopReason
    usage: Usage = Usage()
    model: str = ""
    reasoning: str | None = None
    """模型的思考过程（DeepSeek 的 reasoning_content、部分服务的 reasoning）。

    故意放在 Completion 而不是 Message 里：思考不是对话内容，**不能**回传给
    模型（DeepSeek 对 reasoner 回传 reasoning_content 会直接 400）。它的用途
    只有一个：让人看见模型"为什么这么答"——日志、审计、排查。

    非推理模型的服务不返回这个字段，此时为 None。
    """


__all__ = [
    "Completion",
    "ContentBlock",
    "Message",
    "Role",
    "StopReason",
    "TextBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ToolDefinition",
    "Usage",
]
