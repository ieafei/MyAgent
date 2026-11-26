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
from dataclasses import dataclass

from .llm.base import LLMClient
from .tools.registry import ToolRegistry
from .types import Message, StopReason, Usage

log = logging.getLogger("agentlab.loop")

_THINK_LIMIT = 300
"""日志里思考的截断长度。全文在 Completion.reasoning，日志只要够定位。"""


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

    async def run(self, user_input: str) -> AgentResult:
        messages = self._start(user_input)
        # 工具清单在整个任务期间不变，循环外取一次即可；
        # 每轮都调 definitions() 会白白重建一遍 Schema。
        tools = self.registry.definitions()
        total_usage = Usage()
        last_reasoning: str | None = None
        last_model = ""

        for step in range(1, self.max_steps + 1):
            started = time.perf_counter()
            completion = await self.client.complete(messages, tools=tools)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            total_usage = total_usage + completion.usage
            last_reasoning = completion.reasoning
            last_model = completion.model

            tool_calls = completion.message.tool_calls()
            if completion.reasoning:
                log.info("[think] step=%d %s", step, _clip(completion.reasoning))
            log.info(
                "[loop] step=%d stop=%s calls=%d in=%d out=%d cached=%d %dms",
                step,
                completion.stop_reason,
                len(tool_calls),
                completion.usage.input_tokens,
                completion.usage.output_tokens,
                completion.usage.cached_input_tokens,
                elapsed_ms,
            )

            # 先入历史再判断：收工时 messages 里也带着最后一条 assistant，
            # 否则轨迹缺尾巴，回放时看不出来模型最后说了什么。
            messages.append(completion.message)

            if not tool_calls:
                return AgentResult(
                    last_llm_text=completion.message.text(),
                    messages=tuple(messages),
                    usage=total_usage,
                    steps=step,
                    stop_reason=completion.stop_reason,
                    model=last_model,
                    reasoning=last_reasoning,
                )

            tool_results = await self.registry.execute_all(tool_calls)
            messages.extend(Message.from_tool_results(tool_results))

        raise StepLimitError(self.max_steps, messages[-1] if messages else None)

    def _start(self, user_input: str) -> list[Message]:
        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message.system(self.system_prompt))
        messages.append(Message.user(user_input))
        return messages


def _clip(text: str, limit: int = _THINK_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


__all__ = ["Agent", "AgentResult", "StepLimitError"]
