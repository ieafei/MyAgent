"""文件 7：agent 主循环。

前面六个文件各管一段：`types.py` 定数据、`llm/` 管说话、`tools/` 管做事。
这个文件把它们串起来，回答唯一的那个问题——**模型要求调工具之后，然后呢？**

一轮循环只做四件事：

1. 把当前完整对话和工具清单发给模型（`complete`）；
2. 看回来的是不是 `tool_calls`：不是就收工，是就继续；
3. 把这一轮的调用交给注册表并行执行（`execute_all`）；
4. 把结果按 `tool_call_id` 配成 `role=tool` 消息追加进对话，回到第 1 步。

循环不产出新数据，只产出"新的消息历史"——模型的每一次判断都基于它，
所以 `AgentResult.messages` 是完整的轨迹，排查时看它比看日志更准。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from .context import DEFAULT_BUDGET_TOKENS, fit
from .events import (
    AssistantReplied,
    Event,
    EventHandler,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from .llm.base import LLMClient
from .tools.registry import ToolRegistry
from .types import Message, StopReason, Usage

log = logging.getLogger("agentlab.loop")

_THINK_LIMIT = 300
"""日志里思考的截断长度。全文在 Completion.reasoning，日志只要够定位。"""

_TOOLS_LIMIT = 120
"""日志里工具名列表的截断长度。并行调 10 个工具也不会把一行撑爆。"""


class StepLimitError(RuntimeError):
    """步数用完而模型还在要工具。

    宁可报错也不静默返回半成品：调用方看到异常才知道"任务没完成"。
    真正要防的不是模型笨，而是它在两个工具之间来回试探、每轮都以为有进展——
    这正是账单失控的典型形状。
    """

    def __init__(self, steps: int, last_message: Message | None = None) -> None:
        super().__init__(f"达到最大步数 {steps}，模型仍在调用工具")
        self.steps = steps
        self.last_message = last_message


@dataclass(frozen=True, slots=True)
class AgentResult:
    """一次 agent 任务的完整结果。

    last_llm_text 是最后一轮模型的自然语言回答（可能为空，比如它只调了工具就结束）；
    messages 是完整轨迹，含所有 tool 结果，回放/排查都靠它；
    usage 是**所有轮次之和**，而不是最后一轮——账要算全。
    """

    last_llm_text: str
    messages: tuple[Message, ...]
    usage: Usage
    steps: int
    stop_reason: StopReason
    model: str = ""
    reasoning: str | None = None
    """最后一轮模型的思考。中间各轮的思考在日志 [think] 里，这里只留收尾那条。"""


@dataclass(slots=True)
class Agent:
    """把 client 和 registry 编成"能自主完成任务"的东西。

    它自己不碰 HTTP、不碰文件系统：调模型走 `client`，执行工具走 `registry`。
    唯一属于它的逻辑就是**流程**：什么时候继续、什么时候收工、步数够不够。
    """

    client: LLMClient
    registry: ToolRegistry
    system_prompt: str | None = None
    max_steps: int = 10
    max_context_tokens: int = DEFAULT_BUDGET_TOKENS

    async def run(
        self,
        user_input: str,
        history: Sequence[Message] = (),
        on_event: EventHandler | None = None,
    ) -> AgentResult:
        """跑一次任务。`history` 是之前几轮的消息，`None`/空表示从零开始。

        `on_event` 是过程中的**通知**（见 `events.py`）：不传就一点额外开销都没有，
        传了也不改变任何返回值——`AgentResult` 仍然是唯一的权威结果，事件只是让人
        在等待期间看得见进度。
        """
        # 发给模型前先按预算压一遍：历史来自 session，可能已经很长，
        # 而一次任务内部还会继续长（每轮的工具结果都追加），不在入口压就会打爆窗口。
        fitted = fit(history, max_tokens=self.max_context_tokens)
        if fitted.changed:
            log.info(
                "[ctx] %d→%d tokens（压缩 %d 条工具结果，丢弃 %d 条消息）",
                fitted.before_tokens,
                fitted.after_tokens,
                fitted.squeezed,
                fitted.dropped,
            )
        messages = self._start(user_input, fitted.messages)
        # 工具清单在整个任务期间不变，循环外取一次即可；
        # 每轮都调 definitions() 会白白重建一遍 Schema。
        tools = self.registry.definitions()
        total_usage = Usage()
        last_reasoning: str | None = None
        last_model = ""

        for step in range(1, self.max_steps + 1):
            # 在 `complete()` 之前发：这一行的意思就是"请求已经发出去了"。
            # 放在之后发，UI 就要等模型回来才知道"刚才在等"——等于没通知。
            _emit(on_event, StepStarted(step=step))
            started = time.perf_counter()
            completion = await self.client.complete(messages, tools=tools)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            total_usage = total_usage + completion.usage
            last_reasoning = completion.reasoning
            last_model = completion.model

            tool_calls = completion.message.tool_calls()
            if completion.reasoning:
                log.info("[think] step=%d %s", step, _clip(completion.reasoning))
            # 工具轮连工具名一起打：排查"它到底调了什么"时不用回头翻轨迹。
            # 收工轮没有工具，打 "-" 占位，各行列数一致才好扫。
            tool_names = ",".join(call.name for call in tool_calls) or "-"
            log.info(
                "[loop] step=%d stop=%s tools=%s in=%d out=%d cached=%d %dms",
                step,
                completion.stop_reason,
                _clip(tool_names, _TOOLS_LIMIT),
                completion.usage.input_tokens,
                completion.usage.output_tokens,
                completion.usage.cached_input_tokens,
                elapsed_ms,
            )

            # 先入历史再判断：收工时 messages 里也带着最后一条 assistant，
            # 否则轨迹缺尾巴，回放时看不出来模型最后说了什么。
            messages.append(completion.message)
            reply_text = completion.message.text()

            # 事件放在"已入历史"之后发：UI 看到的顺序必须和会话里记的顺序一致，
            # 将来用事件重放排查时才不会出现"屏幕上先有、历史里后到"。
            _emit(
                on_event,
                AssistantReplied(
                    step=step,
                    text=reply_text,
                    stop_reason=completion.stop_reason,
                    usage=completion.usage,
                    model=completion.model,
                    elapsed_ms=elapsed_ms,
                    reasoning=completion.reasoning,
                ),
            )

            if not tool_calls:
                return AgentResult(
                    last_llm_text=reply_text,
                    messages=tuple(messages),
                    usage=total_usage,
                    steps=step,
                    stop_reason=completion.stop_reason,
                    model=last_model,
                    reasoning=last_reasoning,
                )

            # 先整批发"要调什么"，再执行，再逐条发结果：执行是并行的，
            # 开始事件却必须在动手前发完——否则 UI 的顺序会和真实开始时间对不上。
            for call in tool_calls:
                _emit(
                    on_event,
                    ToolCallStarted(
                        step=step,
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                    ),
                )
            tool_results = await self.registry.execute_all(tool_calls)
            names = {call.id: call.name for call in tool_calls}
            for block in tool_results:
                _emit(
                    on_event,
                    ToolCallFinished(
                        step=step,
                        call_id=block.tool_call_id,
                        name=names.get(block.tool_call_id, "?"),
                        chars=len(block.content),
                        is_error=block.is_error,
                    ),
                )
            messages.extend(Message.from_tool_results(tool_results))

        raise StepLimitError(self.max_steps, messages[-1] if messages else None)

    def _start(self, user_input: str, history: Sequence[Message] = ()) -> list[Message]:
        """组装本轮入场时的消息：历史 + （必要时）system + 本轮 user。

        `list(history)` 先拷一份：调用方传进来的可能是 session 里的 list，
        直接 append 会改到别人的状态。
        """
        messages = list(history)
        # 历史里已经带 system 就不再插：续聊时 system 由第一轮定，之后忽略新的。
        if self.system_prompt and not any(m.role == "system" for m in messages):
            messages.insert(0, Message.system(self.system_prompt))
        messages.append(Message.user(user_input))
        return messages


def _clip(text: str, limit: int = _THINK_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _emit(on_event: EventHandler | None, event: Event) -> None:
    """把事件交给订阅者；订阅者抛错只记日志，不打断任务。

    为什么吞异常：画不出来是 UI 的事，任务本身已经跑完、钱已经花了，不能因为一行
    print 失败就丢掉结果。吞掉但记日志，至少不是静默——这也是本仓库对"回调"的一贯
    态度：回调可以坏，主流程不能跟着坏。
    """
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # noqa: BLE001 - 订阅者的任何错都不该冒泡进主循环
        log.warning("[event] 订阅者抛错，已忽略（任务继续）", exc_info=True)


__all__ = ["Agent", "AgentResult", "StepLimitError"]
