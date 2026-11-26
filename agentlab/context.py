"""文件 9：上下文预算。

`session.py` 按**条数**裁，解决的是"历史不会无限长"；这个文件按 **token** 裁，
解决的是"这一发会不会撑爆窗口、撑爆了先牺牲谁"。

三档手段，从轻到重：

1. 超预算 → 先**压缩早期工具结果的内容**：`tool_call_id` 原样保留（配对不能断），
   只把长文本换成一个短摘要；
2. 还超 → 从最早的开始**成对丢弃**（`assistant(tool_calls)` 和它的 `tool` 消息一起走）；
3. 永远保留：`system`（对话前提）和最近 `keep_recent` 条（当前任务现场）。

为什么先压工具结果而不是先丢消息：一条 `read_file` 可以是三万字符，一条 `user`
通常几十字。丢一条 tool 结果，模型会忘了自己查过什么，于是再查一遍；把内容压成
"读过 X，原文 N 字符"，它至少知道自己查过。

**这是闸门，不是账本**：没有 tokenizer，按 2 字符 ≈ 1 token 粗估（中文偏准、英文偏保守），
误差三成以上很正常。它的职责是"别把 200k 窗口打爆"，不是精确计费。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .types import ContentBlock, Message, ToolCallBlock, ToolResultBlock

DEFAULT_BUDGET_TOKENS = 24_000
"""单次请求的输入预算。给输出和工具往返留出余量，24k 约等于 4.8 万字符。"""

DEFAULT_KEEP_RECENT = 8
"""最近这么多条消息不压缩、不丢弃：它们是当前任务的现场，压了模型就断片。"""

DEFAULT_TOOL_KEEP_CHARS = 300
"""工具结果被压缩时，开头保留的字符数；超出部分换成"原文 N 字符"的说明。"""

_PER_MESSAGE_OVERHEAD = 4
"""每条消息的结构开销（role、分隔符），粗估用。"""


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 token 数：按 2 字符 1 token。"""
    return len(text) // 2 + 1


def estimate_messages(messages: Sequence[Message]) -> int:
    """粗估一组消息的 token 总数。"""
    total = 0
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD
        for block in message.content:
            total += estimate_tokens(_block_text(block))
    return total


def _block_text(block: ContentBlock) -> str:
    """取一个内容块里真正进 prompt 的文本。"""
    if isinstance(block, ToolResultBlock):
        return block.content
    if isinstance(block, ToolCallBlock):
        return block.name + block.arguments
    return block.text


@dataclass(frozen=True, slots=True)
class FitResult:
    """一次预算适配的结果。`changed` 为 False 时输入原样返回。"""

    messages: list[Message]
    before_tokens: int
    after_tokens: int
    squeezed: int
    """被压缩内容（不是被丢弃）的工具结果条数。"""
    dropped: int
    """被整条丢弃的消息条数。"""

    @property
    def changed(self) -> bool:
        return self.squeezed > 0 or self.dropped > 0


def fit(
    messages: Sequence[Message],
    *,
    max_tokens: int = DEFAULT_BUDGET_TOKENS,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    tool_keep_chars: int = DEFAULT_TOOL_KEEP_CHARS,
) -> FitResult:
    """把历史塞进 `max_tokens` 预算内。

    不修改入参：返回的永远是新的 list（消息本身不可变，可以安全共享）。
    实在塞不下就停在预算之上——和 `session.trim()` 一样，宁可超，不做破坏性的裁剪。
    """
    before_tokens = estimate_messages(messages)
    if before_tokens <= max_tokens:
        return FitResult(list(messages), before_tokens, before_tokens, 0, 0)

    out = list(messages)
    # system 永远在头上，最后 keep_recent 条是现场；head 到 squeezable_end 之间才是可牺牲的。
    head = 1 if out and out[0].role == "system" else 0
    squeezable_end = max(head, len(out) - keep_recent)

    squeezed = 0
    for i in range(head, squeezable_end):
        compressed = _squeeze_tool_message(out[i], tool_keep_chars)
        if compressed is not None:
            out[i] = compressed
            squeezed += 1

    dropped = 0
    # 每次只从 head 删，并用"删完还剩多少"重新判断，而不是把边界跟着左移——
    # 跟着移会在消息很少时把保留区一起吃掉，连 system 都保不住。
    while estimate_messages(out) > max_tokens and len(out) - head > keep_recent:
        span = _pair_span(out, head)
        if len(out) - head - span < keep_recent:
            break
        del out[head : head + span]
        dropped += span

    return FitResult(out, before_tokens, estimate_messages(out), squeezed, dropped)


def _squeeze_tool_message(message: Message, keep_chars: int) -> Message | None:
    """压缩一条 tool 消息里的工具结果；没有可压的就返回 None。"""
    if message.role != "tool":
        return None
    blocks = tuple(_squeeze_block(b, keep_chars) for b in message.content)
    if blocks == message.content:
        return None
    return replace(message, content=blocks)


def _squeeze_block(block: ContentBlock, keep_chars: int) -> ContentBlock:
    """把一个工具结果块截成"开头 + 原长度说明"。"""
    if not isinstance(block, ToolResultBlock) or len(block.content) <= keep_chars:
        return block
    note = f"…[已压缩：原文 {len(block.content)} 字符]"
    return replace(block, content=block.content[:keep_chars] + note)


def _pair_span(messages: Sequence[Message], index: int) -> int:
    """从 `index` 开始，这一条连同它必须一起走的兄弟，一共几条。

    `assistant(tool_calls)` 后面必须紧跟它的 `tool` 消息，只删一半就是非法历史。
    """
    if messages[index].role != "assistant" or not messages[index].tool_calls():
        return 1
    span = 1
    while index + span < len(messages) and messages[index + span].role == "tool":
        span += 1
    return span


__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_TOOL_KEEP_CHARS",
    "FitResult",
    "estimate_messages",
    "estimate_tokens",
    "fit",
]
