"""文件 11：一次 run 的过程事件。

`loop.py` 的返回值只在任务**结束**时出现，所以模型思考的那几秒、工具执行的那几毫秒，
调用方两眼一抹黑。这个模块定义"过程中发生了什么"的最小词汇表：loop 每走一步就把
对应事件交给 `on_event` 回调，UI（以及将来的会话日志）从这里取"正在发生什么"。

四条规矩：

1. **事件是通知，不是数据源。** 一条事件只描述"刚刚发生的这一件事"，不携带完整历史。
   所以要工具结果只带"多少字符、成功没有"，不带正文——正文已经进了轨迹
   （`AgentResult.messages`）和会话，再存一份就有两个真相。
2. **能直接进 JSONL。** 字段只有标量（str / int / bool / None）和 `Usage` 这种纯数字的
   frozen dataclass，每条带 `type` 字符串。4.14 R4 要求会话按事件存，届时
   `dataclasses.asdict(event)` 就是一行，之后加新类型不迁移老文件。
3. **接收方不许阻塞。** 回调是同步函数，跑在事件循环上；在里面写文件、发 HTTP，
   整个 run 都停在你那一行。要慢操作，自己 `asyncio.create_task` 挪走。
4. **事件不认 UI。** 这里没有颜色、没有 rich、没有 print。谁订阅谁决定怎么画。

为什么不复用 `logging`：日志是给人扫的文本，字段随时可以改；事件是给程序消费的结构，
字段名一旦落盘就是契约。两者读者不同，混在一起会互相绑死。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .types import StopReason, Usage

# ---------------------------------------------------------------------------
# 四个事件：一轮 step 的生命周期就是它们的顺序
#
#   prompt 发出去                → StepStarted
#   模型回来（可能带 tool_calls）→ AssistantReplied
#   要执行工具                   → ToolCallStarted（每个调用一条）
#   工具回来了                   → ToolCallFinished（每个调用一条）
#
# 没有 RunStarted / RunFinished：调用方自己发起 run、自己拿到返回值，
# 再发两条事件给它，只是在重复它已经知道的事。
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepStarted:
    """第 N 轮请求发出去了。

    存在的唯一理由：让 UI 在模型沉思时有个东西可画。推理模型一步能想 30 秒，
    这 30 秒里屏幕上如果什么都没有，用户会以为程序卡了。
    """

    step: int
    type: Literal["step_start"] = "step_start"


@dataclass(frozen=True, slots=True)
class AssistantReplied:
    """模型对第 N 轮请求的回答。

    收工轮和工具轮共用这一个事件，靠 `stop_reason` 区分：`tool_use` 是"我要动手了"
    （`text` 通常是它打算怎么做），`end_turn` 是最终回答。不拆成两个类型，是因为
    两者字段完全一样，拆开只会让消费者写两个分支再干同一件事。

    `usage` 是**这一轮**的账，不是整个任务的；总账在 `AgentResult.usage`。
    `reasoning` 可能为 None（非推理模型），它只给人看，绝不回传模型。
    """

    step: int
    text: str
    stop_reason: StopReason
    usage: Usage
    model: str
    elapsed_ms: int
    reasoning: str | None = None
    type: Literal["assistant_reply"] = "assistant_reply"


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """模型要求调用某个工具，马上要执行。

    这是"正在干活"最准的信号：参数已经拿到，只是还没跑。长工具（将来的 `shell`）
    靠它在屏幕上挂一个"执行中"，而不是等结果回来才知道开始过。

    `arguments` 是 JSON **字符串**，与 `ToolCallBlock` 一致——不在这里解析，
    解析是注册表的事；UI 想美化就自己 loads。
    """

    step: int
    call_id: str
    name: str
    arguments: str
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """某个工具跑完了。

    只有 `chars`（结果长度）而没有内容：正文已经作为 `role=tool` 消息进了轨迹，
    事件只负责告诉 UI"这条回来了、多大、成不成"。
    `name` 冗余地带上了（协议里 tool 结果只有 `tool_call_id`），因为 UI 拿到一条
    孤立的结果时没有地方去查名字，而事件必须能独立渲染。
    """

    step: int
    call_id: str
    name: str
    chars: int
    is_error: bool
    type: Literal["tool_result"] = "tool_result"


Event = StepStarted | AssistantReplied | ToolCallStarted | ToolCallFinished

EventHandler = Callable[[Event], None]
"""订阅者签名：同步、立即返回、不返回值。

叫 Handler 不叫 Sink / Subscriber：它就是个被直接调用的函数，没有订阅表也没有退订，
名字里不该暗示这两样东西。

不设计成 `async def`：调用方几乎都是"打印一行"，为了它给 loop 里每个事件点加一个
`await` 不值当；真正需要异步的消费者（写日志到网络）自己丢进队列。
"""


__all__ = [
    "AssistantReplied",
    "Event",
    "EventHandler",
    "StepStarted",
    "ToolCallFinished",
    "ToolCallStarted",
]
