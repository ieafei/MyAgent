"""文件 8：会话（按 session_id 存消息历史）。

解决的问题：`/chat` 每次从空历史开始，第二句"继续改一下"没有上下文。
**不解决**：压缩（`context.py` 的事）、持久化（重启即丢，这版是内存）、鉴权
（任何人都能猜一个 id 接上去，单机自用够了）。

一个会话就是"一串消息 + 一把锁"：

- 锁不是"一个会话只能有一个任务"的产品限制，而是**同一份历史不能被两个任务同时
  改写**的技术保护。两个请求各拿一份历史、各写回一份，后写的覆盖先写的——经典的
  丢写。要"一个会话里并行派生多个任务"，得先把历史分叉（fork 出子会话），不是拆锁。
- 存的是**完整消息**，包括 `assistant(tool_calls)` 和对应的 `tool` 结果。裁剪时
  必须成对丢弃：OpenAI 协议要求每个 `tool_call` 都有配对的 `tool` 消息，只删一半
  下一次请求就 400。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from .types import Message

DEFAULT_MAX_SESSIONS = 100
"""最多留多少个会话，超了淘汰最久没更新的（只淘汰当前空闲的，见 `_drop_one_session`）。"""

DEFAULT_MAX_MESSAGES = 60
"""单个会话最多留多少条消息。条数不是 token 的精确代理，但能把上下文关在笼子里。"""


@dataclass(slots=True)
class Session:
    """一个会话的全部状态。

    没有 `trim()` 之外的逻辑：怎么存由 `SessionStore` 决定，会话自己只管
    "我是哪一串消息"和"现在有没有人在用我"。
    """

    id: str
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def trim(self, limit: int) -> int:
        """裁到 `limit` 条以内，返回丢掉的条数。

        两条规则：

        1. `system` 永远不丢（它在最前，是整场对话的前提）；
        2. 丢 `assistant(tool_calls)` 时，把它后面连续的 `tool` 消息一起丢——留一半
           就是非法历史。
        """
        preserve = 1 if self.messages and self.messages[0].role == "system" else 0
        dropped = 0
        while len(self.messages) > limit and len(self.messages) > preserve:
            head = self.messages[preserve]
            span = 1
            if head.role == "assistant" and head.tool_calls():
                while (
                    preserve + span < len(self.messages)
                    and self.messages[preserve + span].role == "tool"
                ):
                    span += 1
            del self.messages[preserve : preserve + span]
            dropped += span
        return dropped


class SessionStore:
    """内存里的会话表。进程退出即清空，这是本版有意接受的代价。"""

    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions
        self._max_messages = max_messages
        self._lock = asyncio.Lock()

    async def get_session(self, session_id: str) -> Session:
        """取出会话，没有就建（get-or-create）。

        只用全局锁保护"查表 / 建表"这一小段；真正的任务在 `session.lock` 下跑，
        不占着全局锁（否则所有会话被迫串行）。
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                self._make_room()
                session = Session(id=session_id)
                self._sessions[session_id] = session
            return session

    def save_session(self, session: Session, messages: Sequence[Message]) -> int:
        """用一次任务的完整轨迹替换历史并裁剪；返回丢弃条数。

        整体替换而不是追加：`AgentResult.messages` 已经是"旧历史 + 本轮新增"的
        完整版，追加会把旧历史写两遍。
        """
        session.messages = list(messages)
        session.updated_at = time.time()
        return session.trim(self._max_messages)

    def _make_room(self) -> None:
        """腾出位置：从最久没更新的空闲会话删起，直到低于上限。

        全在跑就停手——删掉一个正在跑的会话，它结束时写回的对象已不在表里，等于
        静默丢对话。宁可暂时超限。
        """
        while len(self._sessions) >= self._max_sessions:
            dropped = self._drop_one_session()
            if not dropped:
                break

    def _drop_one_session(self) -> bool:
        """丢掉最久没更新的**空闲**会话；返回 False 表示一个都删不了（全在跑）。"""
        idle = [s for s in self._sessions.values() if not s.lock.locked()]
        if not idle:
            return False
        oldest = min(idle, key=lambda s: s.updated_at)
        del self._sessions[oldest.id]
        return True

    def __len__(self) -> int:
        return len(self._sessions)

    def ids(self) -> list[str]:
        return list(self._sessions)


__all__ = ["Session", "SessionStore", "DEFAULT_MAX_MESSAGES", "DEFAULT_MAX_SESSIONS"]
