"""文件 5：工具注册表。

base.py 只定了工具的**形状**，这个文件补上**从"模型说"到"真执行"之间的六道守门**：

    1. 查表     模型点名的工具存在吗？不存在就把可用清单还给它。
    2. 解析     arguments 是 JSON 字符串，先 json.loads；空串当无参数。
    3. 校验     用工具自己的 parameters（同一份 JSON Schema）验参数。
    4. 超时     工具可能卡死，超时当失败返回，而不是把主循环一起拖住。
    5. 兜异常   ToolError 原样给模型；其他异常进日志（那是我们的 bug）。
    6. 截断     超长结果砍中间，并告诉模型下一步怎么做（不是只说"被截断"）。

前五道失败**都不抛异常**，一律转成 ToolResult(is_error=True) 回给模型：
参数写错是模型能自己改的事，抛异常穿透主循环等于剥夺它自救的机会。

这个文件不 import 具体工具，也不知道有哪些工具 —— 它只认 Tool 协议。
于是它可以用一个假工具（echo）完整测通，不需要文件系统、不需要网络。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentlab.tools.base import (
    DEFAULT_MAX_CHARS,
    Tool,
    ToolError,
    ToolResult,
    truncate_output,
)
from agentlab.types import ToolCallBlock, ToolDefinition, ToolResultBlock

log = logging.getLogger("agentlab.tools")

DEFAULT_TIMEOUT_S = 30.0
"""单次工具调用的默认上限。读文件、跑命令都够用；真卡死就说明这次调用本身有问题。"""

_TRUNCATED_HINT = (
    "\n\n[结果已截断] 原文 {total} 字符，这里只保留了头尾共 {kept} 字符。"
    "请改用该工具的分页参数（例如 read_file 的 offset / limit、grep 的匹配上限）"
    "缩小范围后再调用一次。"
)
"""截断时必须给出可执行的下一步。

只说"结果被截断了"，模型只会换个说法重试同一个调用，白白多花一轮 token。
"""

_HINT_RESERVE = 300
"""给提示语和截断标记预留的字符预算。

max_chars 管的是"回填给模型的这一块总共多长"，不是"正文多长"：
不让出这份额度，说好截到 24000 实际回填 24300，预算就成了摆设。
提示语实测约 100 字符，截断标记约 30，留 300 有余量；
上限只有几百字符时按一半让，别把正文压成只剩一两个字。
行对齐本身还会带来几十字符误差，见 truncate_output。
"""


class ToolRegistry:
    """工具名 → 工具实例 的表，外加执行一次调用的全过程。

    只收 name 唯一、parameters 合法的工具：这两件事都是作者级错误，
    在注册时（服务启动时）就该炸，而不是等模型点名调到它才发现。
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正数，收到 {timeout_s!r}")
        self._timeout_s = timeout_s
        self._max_chars = max_chars
        self._tools: dict[str, Tool] = {}
        # 校验器跟着工具一起建：Schema 只在注册时编译一次，调用时不再重复解析。
        self._validators: dict[str, Draft202012Validator] = {}

    # -- 注册 ---------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """登记一个工具。重名、缺 name/parameters、Schema 非法都在这里失败。"""
        definition = tool.definition  # 缺 name / parameters 时在属性里抛 ValueError
        name = definition.name
        if name in self._tools:
            owner = type(self._tools[name]).__name__
            raise ValueError(f"工具名重复：{name!r}，已被 {owner} 占用")

        try:
            Draft202012Validator.check_schema(definition.parameters)
        except SchemaError as exc:
            raise ValueError(
                f"{name} 的 parameters 不是合法 JSON Schema：{exc.message}"
            ) from exc

        self._tools[name] = tool
        self._validators[name] = Draft202012Validator(definition.parameters)

    # -- 给模型看的声明 -----------------------------------------------------

    def definitions(self) -> list[ToolDefinition]:
        """发给模型的 tools 数组。主循环每次调用都拿这份快照。"""
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # -- 执行 ---------------------------------------------------------------

    async def execute(self, call: ToolCallBlock) -> ToolResultBlock:
        """执行一次工具调用，返回可以直接回填给模型的内容块。

        tool_call_id 用的就是 call.id —— 配对凭证必须原样传回，
        否则所有数据结构都合法，只有模型会拿着 A 的结果去推理 B。
        """
        return (await self._run(call.name, call.arguments)).to_block(call.id)

    async def execute_all(self, calls: Sequence[ToolCallBlock]) -> tuple[ToolResultBlock, ...]:
        """并行执行同一轮里的多个调用。

        OpenAI 允许一条 assistant 消息带多个 tool_calls，串行跑等于把延迟相加。
        返回顺序与传入顺序一致（asyncio.gather 保序），配对仍靠 id。
        """
        if not calls:
            return ()
        return tuple(await asyncio.gather(*(self.execute(call) for call in calls)))

    async def _run(self, name: str, raw_args: str) -> ToolResult:
        # 1) 查表：模型偶尔会凭空点一个不存在的工具，把可用清单给它，它下一轮就能改。
        tool = self._tools.get(name)
        if tool is None:
            available = "、".join(self._tools) or "（当前没有注册任何工具）"
            return ToolResult.fail(f"没有名为 {name!r} 的工具。当前可用：{available}")

        # 2) 解析：arguments 是 JSON 字符串；部分服务在无参数时回空串，等价于 {}。
        args, problem = _parse_arguments(raw_args)
        if problem is not None:
            return ToolResult.fail(problem)

        # 3) 校验：用工具自己的 parameters，和发给模型的是同一份，不会漂。
        problem = self._validate(name, args)
        if problem is not None:
            return ToolResult.fail(problem)

        # 4) 执行 + 超时 + 5) 兜异常：三类结果分开，责任归属才看得清。
        try:
            result = await asyncio.wait_for(tool.run(args), timeout=self._timeout_s)
        except ToolError as exc:
            # 意料之中的坏输入，消息本身就能指导模型换条路。
            return ToolResult.fail(str(exc))
        except TimeoutError:
            return ToolResult.fail(
                f"{name} 执行超时（超过 {self._timeout_s:g} 秒），已放弃本次调用。"
                "请缩小范围或换个方式再试。"
            )
        except Exception:
            # 我们的 bug：模型看了堆栈也没用，日志里留 traceback，回填一句能继续的话。
            log.exception("工具 %s 抛出未预期异常（这是实现问题，不是模型的问题）", name)
            return ToolResult.fail(
                f"{name} 内部错误，已记录日志。请换一种方式继续，不要重复同样的调用。"
            )

        # 6) 截断：仍是正常结果，只是可能不完整；truncated 供 UI 标一句。
        return _fit(result, self._max_chars)

    def _validate(self, name: str, args: Mapping[str, object]) -> str | None:
        """返回第一处错误的说明，全通过则返回 None。

        只报第一处：一次改一个，模型更容易改对；把五条错误一起塞回去，
        小模型往往会连着改错三处。
        """
        validator = self._validators[name]
        errors = sorted(validator.iter_errors(dict(args)), key=_error_sort_key)
        if not errors:
            return None
        first = errors[0]
        where = ".".join(str(p) for p in first.absolute_path) or "(根)"
        return f"参数不合法：{where} → {first.message}"


def _error_sort_key(error: object) -> str:
    """让"报第一处"稳定：按参数路径排序，而不是依赖 schema 内部的遍历顺序。"""
    task = getattr(error, "absolute_path", ())
    return ".".join(str(p) for p in task)


def _parse_arguments(raw: str) -> tuple[dict[str, object], str | None]:
    """JSON 字符串 → dict。失败时返回 (空 dict, 原因)，由调用方转成失败结果。"""
    if not raw.strip():
        return {}, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"arguments 不是合法 JSON（{exc.msg}，位置 {exc.pos}）：{raw[:200]}"
    if not isinstance(value, dict):
        return {}, f"arguments 必须是 JSON 对象，收到 {type(value).__name__}：{raw[:200]}"
    return value, None


def _fit(result: ToolResult, max_chars: int) -> ToolResult:
    """超长结果砍中间，并附一句"下一步怎么做"。

    正文额度先扣掉提示语预算：max_chars 管的是整块回填文本，
    否则"截断到 24000"实际回填 24300，预算就成了摆设。
    """
    reserve = min(_HINT_RESERVE, max_chars // 2)
    text, truncated = truncate_output(result.content, max(1, max_chars - reserve))
    if not truncated:
        return result
    hint = _TRUNCATED_HINT.format(total=len(result.content), kept=len(text))
    return ToolResult(content=text + hint, is_error=result.is_error, truncated=True)


__all__ = ["DEFAULT_TIMEOUT_S", "ToolRegistry"]
