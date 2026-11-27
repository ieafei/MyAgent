"""文件 10：终端客户端。

前面九个文件合起来已经能完成任务，但没有东西把它交到人手里——这个文件就是那个入口。

它自己不当会话，也不当 agent。状态按寿命分三层（goal-and-design 4.14），本文件只活在
**进程层**：读一行、把任务交给一次 run、把结果画出来。会话内容归 `session.py`，run 的
状态归每次新建的 `Agent`，两者都不在这里驻留。所以既不是"一个常驻 agent"，也不是
"进程 = 会话"——进程只是这两者的宿主。

它只做三件事，不做别的：

1. **装配**（`_options` / `_client` / `_ask`）：读环境、建客户端、注册工具、建 `Agent`；
2. **对话**（`_interactive_loop` / `_run`）：REPL（读一行 → 执行 → 打印，再读下一行）
   循环，持有 `Session` 句柄，不持有消息本身；
3. **展示**（`_stream_handler` / `_render`）：过程看事件流（第几步、要调什么工具、
   成了没有），结果看 `AgentResult`（回答 + 账）。

**不做的事**：不路由、不鉴权、不做 Web。本地单用户，一个进程一个 REPL（goal-and-design 1.4）；
落盘推迟，`Session` 现在只在内存里，但这不改变"历史归会话层所有"这个归属。
审批也不在这里：`shell` 工具还没写，等它出现时由工具自己 `await approve(...)`，
而不是在入口或注册表上加钩子（3.14 的教训）。

**边跑边说，但只说结构，不说 token。** 过程靠 `events.py` 的四个事件实时画出来；
模型的正文仍然整包返回，所以回答是一次性打出来的 Markdown，不是逐字冒。逐 token
流式要动 `LLMClient.complete` 的返回类型，是另一件事，别混在这里做。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from .events import (
    AssistantReplied,
    Event,
    EventHandler,
    StepStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from .llm.base import AuthError, LLMError
from .llm.openai_compat import DEFAULT_BASE_URL, OpenAIClient
from .loop import Agent, AgentResult, StepLimitError
from .session import Session
from .tools.builtin.fs import make_file_tools
from .tools.registry import ToolRegistry
from .types import Message

log = logging.getLogger("agentlab.cli")

SYSTEM_PROMPT = """你是运行在用户本机终端里的 agent，工作区根目录是 {root}。

你能用的工具只有文件操作：列目录、读、按模式搜索、写、精确替换。**没有 shell，没有网络。**
所以别承诺"我帮你跑一下测试"——跑不了就直说。

先看清楚再动手：不确定路径就先 list_files / grep 确认，不要凭猜改文件。
改完用一两句话说明改了什么；失败就如实说失败，不要编造结果。

只做用户要求的事：**不要顺手新建记忆、笔记、TODO 这类文件**，也不要改与任务无关的文件。
真要落盘，先问一句。"""

_HELP = """:q / exit      退出（历史不保存）
:reset           清空本轮会话的历史
:help            显示这份说明
Ctrl-C           中断当前任务，不退出（历史保持中断前的状态）"""

_KEY_HINT = "（检查 .env 里的 AGENTLAB_API_KEY；没有就 cp .env.example .env）"

SESSION_ID = "local"
"""单会话的固定 id。落盘后由 `--session` / `--resume` 决定，现在只是个占位符——
但会话从第一天起就有 id，落盘时才不用回头改 `_interactive_loop` 的形状。"""


@dataclass(frozen=True, slots=True)
class _Options:
    """一次进程启动后不再变化的东西。放一起是为了传参时不用排七个位置参数。"""

    root: Path
    system: str
    max_steps: int
    model: str | None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """console script 的入口：`agentlab` 或 `agentlab "一条任务"`。

    同步函数：参数解析、日志、`.env` 都是"进程级"的事，不需要事件循环参与。
    """
    args = _parse_args(argv)
    # 加载 .env 是入口的职责，库不该改调用方的环境（见 OpenAIClient.from_env 的注释）。
    load_dotenv(".env")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",  # 日志行自己带 [loop]/[think] 前缀，不必再叠一层格式
    )

    console = Console()
    options = _Options(
        # 不改进程 cwd：用户在哪个目录敲命令，和 agent 被授权动哪个目录，是两件事。
        root=Path(args.root).expanduser().resolve(),
        system=args.system or SYSTEM_PROMPT.format(root=Path(args.root).expanduser().resolve()),
        max_steps=args.max_steps,
        model=args.model,
    )

    if not options.root.is_dir():
        console.print(f"[red]工作区不存在或不是目录[/]：{options.root}")
        return 2

    try:
        if args.prompt:
            # 单发模式也是一次 run：同样给一个会话对象，形状与 REPL 里一致。
            _run(console, options, args.prompt, Session(id=SESSION_ID))
            return 0
        _interactive_loop(console, options)
    except KeyboardInterrupt:
        # 只在等输入时才会走到这里（跑任务时的 Ctrl-C 由 _run 内部处理）。
        console.print()
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agentlab",
        description="本地 agent：给它一句话，它自己看文件、改东西。",
    )
    parser.add_argument("prompt", nargs="?", help="一条任务；不给就进交互模式")
    parser.add_argument(
        "-C", "--root", default=".", help="工作区根目录，文件工具越界即拒（默认当前目录）"
    )
    parser.add_argument("--system", default=None, help="自定义 system prompt（覆盖默认）")
    parser.add_argument("--max-steps", type=int, default=10, help="单任务最多几轮（默认 10）")
    parser.add_argument("--model", default=None, help="覆盖 AGENTLAB_MODEL")
    parser.add_argument("-v", "--verbose", action="store_true", help="把 [loop]/[think] 日志打出来")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 一次任务
# ---------------------------------------------------------------------------


async def _ask(
    text: str,
    history: Sequence[Message],
    options: _Options,
    on_event: EventHandler | None = None,
) -> AgentResult:
    """跑一次任务：临时建客户端和注册表，跑完关掉。

    为什么每次重建而不是"进程启动时建一次"：`httpx.AsyncClient` 绑定 event loop，
    而这个入口用 `asyncio.run()` 跑任务——每次调用都是一个新循环。
    要么把整个 REPL 塞进一个循环里（输入就得走线程），要么每次重建（代价是重连一次 TCP）。
    选后者：一行 `asyncio.run` 换来 Ctrl-C 干净中断，值。
    """
    client = _client(options.model)
    registry = ToolRegistry()
    for tool in make_file_tools(options.root):
        registry.register(tool)
    agent = Agent(
        client=client,
        registry=registry,
        system_prompt=options.system,
        max_steps=options.max_steps,
    )
    try:
        return await agent.run(text, history, on_event=on_event)
    finally:
        await client.aclose()


def _client(model: str | None) -> OpenAIClient:
    """`--model` 只是临时覆盖，不写回环境变量：环境是用户的环境。"""
    if model is None:
        return OpenAIClient.from_env()
    api_key = os.environ.get("AGENTLAB_API_KEY", "")
    if not api_key:
        # 抛 AuthError 而不是 LLMError：走 `_run` 的鉴权分支，用户能拿到"去配 key"的提示，
        # 而不是一句干巴巴的 `LLMError：缺少 ...`。
        raise AuthError("缺少 AGENTLAB_API_KEY")
    return OpenAIClient(
        api_key=api_key,
        model=model,
        base_url=os.environ.get("AGENTLAB_BASE_URL", DEFAULT_BASE_URL),
    )


# ---------------------------------------------------------------------------
# 交互
# ---------------------------------------------------------------------------


def _interactive_loop(console: Console, options: _Options) -> None:
    """REPL：读一行（Read）→ 跑一次 run（Eval）→ 打印（Print）→ 回到读，直到 EOF/退出。

    这里唯一的会话状态是一个 `Session` 句柄，消息本身归会话层（4.14 R2）：
    UI 只在"这一轮说给谁听"的意义上引用它，不解释、不裁剪、不改写内容。
    句柄现在只在内存，退出即丢——落盘推迟，不影响所有权归属。
    """
    # 显示 `--model` 或环境里的 AGENTLAB_MODEL；两者都没有时 from_env() 会自己报错，不必猜。
    model = options.model or os.environ.get("AGENTLAB_MODEL") or "（未设置模型）"
    console.print(
        f"[bold]agentlab[/] [dim]·[/] {model} "
        f"[dim]·[/] 工作区 {options.root} [dim]·[/] 最多 {options.max_steps} 步"
    )
    console.print("[dim]输入任务，:help 看命令，:q 退出[/]\n")

    session = Session(id=SESSION_ID)
    while True:
        try:
            line = console.input("[bold cyan]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/]")
            return

        if not line:
            continue
        if line in {":q", ":quit", "exit", "quit"}:
            return
        if line == ":reset":
            # 换一个新对象，而不是让 UI 去 clear 会话内部的 list：所有权在会话层。
            session = Session(id=SESSION_ID)
            console.print("[dim]历史已清空[/]")
            continue
        if line in {":h", ":help", "?"}:
            console.print(_HELP)
            continue

        # 传的是句柄。失败时 `_run` 不追加任何东西，会话自然停在上一轮。
        _run(console, options, line, session)


def _run(
    console: Console,
    options: _Options,
    text: str,
    session: Session,
) -> None:
    """跑一条任务并把结果画出来。**异常在这里消化掉**，REPL 才能活下去。"""
    started = time.perf_counter()
    try:
        # 订阅者在 `_run` 里建：它需要 console，而 `_ask` 不该知道终端存在。
        result = asyncio.run(_ask(text, session.messages, options, _stream_handler(console)))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/] [dim]历史保持中断前的状态[/]")
        return
    except StepLimitError as exc:
        console.print(f"[red]步数用尽[/]：{str(exc).strip()}[dim]（--max-steps 可调）[/]")
        return
    except AuthError as exc:
        # 放在 LLMError 之前：这一类的自救动作是"去配 key"，和其他错误完全不同。
        console.print(f"[red]鉴权失败[/]：{exc}")
        if "AGENTLAB_API_KEY" not in str(exc):  # 适配器自己已经说了缺 key 就别复读
            console.print(f"[dim]{_KEY_HINT}[/]")
        return
    except LLMError as exc:
        # 类名比 message 有用：RateLimitError 等一分钟、NetworkError 查网络，动作不同。
        console.print(f"[red]{type(exc).__name__}[/]：{exc}")
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    # 切出本轮新增的消息：最后一条 user 之后的部分。不能用长度切——`Agent.run` 入场
    # 会把前缀压短，返回的 messages 比传进去的少几行。
    last_user = max(i for i, m in enumerate(result.messages) if m.role == "user")
    fresh = result.messages[last_user:]
    # 只追加原始消息，**不写回压缩结果**（4.14 R1）：压掉的是"这轮模型看到了什么"，
    # 而会话要留的是"双方交换过什么"。下一次 run 的入口会重新按预算压一遍。
    session.append(*fresh)
    _render(console, result, latency_ms)


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def _stream_handler(console: Console) -> EventHandler:
    """把事件流画成终端上的进度行：每步一行标题，每个工具两行（要做什么 / 结果）。

    闭包持有 `console`，因为订阅者只在这一屏里活着；事件本身不知道终端存在，
    `loop` 更不知道——它只管把 `Event` 交给回调。

    为什么不画 spinner：那需要一个能后台重绘的对象（rich 的 `Live`），而事件回调
    要求立即返回，两者凑一起要多管一层生命周期。一行"第 N 步"已经能说明
    "请求发出去了、没卡死"，够用。
    """

    def on_event(event: Event) -> None:
        if isinstance(event, StepStarted):
            console.print(f"[dim]· 第 {event.step} 步[/]")
        elif isinstance(event, AssistantReplied):
            # 只有"要动手"那一轮的说明才在这里打。收尾轮的回答由 `_render` 用
            # Markdown 重画一遍，先打一次就成了重复。
            if event.text.strip() and event.stop_reason != "end_turn":
                console.print(f"  [dim]{escape(_clip(event.text, 120))}[/]")
        elif isinstance(event, ToolCallStarted):
            # escape：参数是模型给的任意文本，里面一个 [ 就会被 rich 当成标记吃掉。
            console.print(
                f"  [dim]↳[/] {escape(event.name)} "
                f"[dim]{escape(_clip(event.arguments, 70))}[/]"
            )
        elif isinstance(event, ToolCallFinished):
            mark = "[red]✗[/]" if event.is_error else "[green]✓[/]"
            console.print(f"    {mark} {escape(event.name)} [dim]· {event.chars} 字符[/]")

    return on_event


def _render(console: Console, result: AgentResult, latency_ms: int) -> None:
    """收尾：回答 + 账。

    顺序是刻意的：人先要答案，再关心花了多少钱。轨迹已经不在这里打了——过程
    在跑的时候就由 `_stream_handler` 一行行画完，重打一遍只会让第 5 步之后的屏幕没法看。
    """
    text = result.last_llm_text.strip()
    console.print()
    if text:
        console.print(Markdown(text))
    else:
        console.print("[dim]（这一轮模型没有给出最终回答）[/]")

    console.print()
    console.print(
        f"[dim]{result.steps} 步 · in {result.usage.input_tokens} / "
        f"out {result.usage.output_tokens} · 缓存 {result.usage.cached_input_tokens} · "
        f"{latency_ms}ms · {result.model or '未知模型'}[/]"
    )


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    raise SystemExit(main())
