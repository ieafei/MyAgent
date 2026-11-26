"""MyAgent 服务主入口：`python main.py` 起一个 HTTP 服务，用 curl 或 `/docs` 页面验证功能。

为什么要有它（而不是只写断言）：每落地一个新能力（工具注册表、主循环、上下文压缩），
重启进程、换一条请求就能看到真实的完整往返——模型怎么回、花了多少 token、工具有没有被
调用。断言只能证明"没坏"，服务才能证明"能用"。

职责边界（越界就会长出第二个大泥球）：
- 只做装配：读 .env、造 client、挂路由、把异常翻译成 HTTP 状态码；
- 编解码在 llm/openai_compat.py，工具在 tools/，多轮循环在 loop.py。

本次有四个端点：
- GET  /health  服务活着、配置读对没、注册了几个工具
- GET  /tools   已注册工具清单（和发给模型的是同一份）
- POST /chat    一次 agent 任务：内部多轮（调模型 → 执行工具 → 回填 → 再调），
                直到模型给出最终回答或步数用完
- GET  /        浏览器里的对话测试页（frontend/index.html，无构建、无 CDN）
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import agentlab
from agentlab.llm import (
    AuthError,
    BadRequestError,
    LLMError,
    NetworkError,
    OpenAIClient,
    RateLimitError,
)
from agentlab.loop import Agent, StepLimitError
from agentlab.tools.builtin.fs import make_file_tools
from agentlab.tools.registry import ToolRegistry
from agentlab.types import ToolCallBlock, ToolDefinition, Usage

# -- 请求 / 响应模型：这就是给人看的接口文档，写清类型，/docs 自动生成 ---------


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_000, description="用户这句话")
    system: str | None = Field(default=None, description="可选 system prompt，仅本次请求有效")


class ChatResponse(BaseModel):
    """对外契约。

    tool_calls / usage 直接用 agentlab.types 里的 dataclass，不再另立一份镜像：
    同一份数据只写一遍，就不存在"内部加了字段、API 静默少一个"的漂移。
    代价写在明处：内部 dataclass 改字段名 = API 变更。
    """

    text: str
    reasoning: str | None = Field(
        default=None,
        description="模型的思考过程（只有推理模型会返回，如 deepseek-reasoner）",
    )
    tool_calls: list[ToolCallBlock] = Field(
        description="模型最后一轮要求调用的工具；任务中的调用都已执行并把结果回填给模型",
    )
    stop_reason: str
    usage: Usage
    model: str
    latency_ms: int
    steps: int = Field(
        default=1,
        description="本次任务一共调了几轮模型；1 表示一问一答没用到工具",
    )


# -- 日志：一条消息一行业务日志，健康探测不刷屏 ---------------------------------

log = logging.getLogger("agentlab.main")


def _setup_logging() -> None:
    """在 main() 里装 handler，不在 import 时改全局日志配置（被 import 也不该影响调用方）。

    幂等：`--reload` 时 uvicorn 在子进程里只 import 模块、不走 main()，
    子进程的 lifespan 会再调一次；不判断就会挂两个 handler、每行日志吐两遍。
    """
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False  # 别让 root 再吐一遍

    # agentlab 包自己的日志（loop 的 [loop]/[think]）走同一个 handler：
    # 库不配 handler 是对的，但"谁接进服务谁负责配"——这里就是装配处。
    lib_log = logging.getLogger("agentlab")
    if not lib_log.handlers:
        lib_log.addHandler(handler)
        lib_log.setLevel(logging.INFO)
        lib_log.propagate = False


class _QuietHealth(logging.Filter):
    """挡掉 /health 的访问日志。

    前端每 15 秒轮询一次，一天就是 5760 行噪音，把真正的 /chat 记录淹掉。
    uvicorn.access 的 record.args = (client_addr, method, full_path, http_version, status_code)。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        return args[2] != "/health"


def _brief(text: str, limit: int = 40) -> str:
    """压成一行并截断：日志要能一眼扫过去，不是存原文。"""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


# -- 生命周期：client 进程内复用，退出时关闭连接池 -----------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _setup_logging()  # --reload 的子进程只 import 模块、不走 main()
    logging.getLogger("uvicorn.access").addFilter(_QuietHealth())
    # 加载 .env 是"入口"的职责；OpenAIClient.from_env() 只读 os.environ，库不改调用方环境。
    load_dotenv()
    try:
        app.state.client = OpenAIClient.from_env()
    except AuthError as exc:
        print(f"[启动失败] {exc}")
        raise

    # 工作区取启动目录。换目录启动 = 换一个工作区，这是有意的：
    # "在哪个仓库里跑 agent" 该由人启动时的位置决定，不该写死在本文件里。
    workspace = Path.cwd()
    registry = ToolRegistry()
    try:
        for tool in make_file_tools(workspace):
            registry.register(tool)
    except ValueError as exc:  # 重名 / Schema 非法：作者级错误，别带着坏工具启动
        print(f"[启动失败] {exc}")
        raise
    app.state.registry = registry
    log.info(
        "[启动] 工作区=%s 工具=%d 个：%s",
        workspace,
        len(registry),
        ", ".join(registry.names()),
    )

    app.state.started_at = time.monotonic()
    yield
    await app.state.client.aclose()


app = FastAPI(title="MyAgent", version=agentlab.__version__, lifespan=lifespan)


def _client(request: Request) -> OpenAIClient:
    client: OpenAIClient = request.app.state.client
    return client


def _registry(request: Request) -> ToolRegistry:
    """注册表挂在 app.state 上而不是模块全局：同一个进程测试时可以起多份互不干扰。"""
    registry: ToolRegistry = request.app.state.registry
    return registry


def _status_for(exc: LLMError) -> int:
    """把错误分类映射成 HTTP 状态码：调用方能从状态码就看懂是谁的问题。"""
    if isinstance(exc, AuthError):
        return 401
    if isinstance(exc, BadRequestError):
        return 400
    if isinstance(exc, RateLimitError):
        return 429
    if isinstance(exc, NetworkError):
        return 503
    return 502  # ServerError / ResponseFormatError / 兜底


@app.exception_handler(LLMError)
async def _llm_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, LLMError)
    status = _status_for(exc)
    log.warning("[chat] 失败 %s → HTTP %d: %s", type(exc).__name__, status, exc)
    return JSONResponse(
        status_code=status,
        content={"error": {"type": type(exc).__name__, "message": str(exc)}},
    )


@app.exception_handler(StepLimitError)
async def _step_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    """步数用完不是 bug，是保护。明确告诉调用方"任务没做完"，而不是 200 给半成品。"""
    assert isinstance(exc, StepLimitError)
    log.warning("[chat] 步数上限：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"type": "StepLimitError", "message": str(exc)}},
    )


# -- 端点 ---------------------------------------------------------------------


# 单文件、零构建：改完 HTML 直接刷新浏览器，不用 npm，也不用重启服务
# （静态文件由 FileResponse 每次现读，只有改 main.py 才需要重启）。
_INDEX = Path(__file__).resolve().parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@app.get("/health", summary="活着吗、配置读对没")
async def health(request: Request) -> dict[str, object]:
    return {
        "status": "ok",
        "version": agentlab.__version__,
        "model": _client(request).model,
        "tools": len(_registry(request)),
        "uptime_s": round(time.monotonic() - request.app.state.started_at, 1),
    }


@app.get(
    "/tools",
    response_model=list[ToolDefinition],
    summary="已注册的工具清单（模型看到的就是这一份）",
)
async def tools(request: Request) -> list[ToolDefinition]:
    """原样返回 registry 的快照，不加工、不排序。

    `/tools` 存在的意义是"所见即所发"：它和主循环传给 `complete(tools=...)` 的
    是同一个 `definitions()`，所以在这里看到的 Schema 就是模型眼里的 Schema;
    另立一份给调试用的描述，迟早会和真发出去的那份漂移。
    """
    return _registry(request).definitions()


def _log_chat(
    req: ChatRequest,
    answer: str,
    stop_reason: str,
    usage: Usage,
    latency_ms: int,
    steps: int,
) -> None:
    """一次 agent 任务压成一行。中途每轮由 loop 自己打 [loop] / [think]，这里只留摘要。"""
    log.info(
        "[chat] sys=%s q=%r → %r | stop=%s steps=%d in=%d out=%d cached=%d %dms",
        f"{len(req.system)}字" if req.system else "无",
        _brief(req.text),
        _brief(answer),
        stop_reason,
        steps,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        latency_ms,
    )


@app.post("/chat", response_model=ChatResponse, summary="一次 agent 任务（内部多轮）")
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """对外仍是一问一答，内部是完整循环：模型要工具就执行并回填，直到它收工。"""
    agent = Agent(
        client=_client(request),
        registry=_registry(request),
        system_prompt=req.system,
    )
    started = time.perf_counter()
    result = await agent.run(req.text)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # 轨迹最后一条是模型的收尾回答；正常收工时它没有工具调用。
    last_calls = list(result.messages[-1].tool_calls()) if result.messages else []
    _log_chat(
        req,
        result.last_llm_text,
        result.stop_reason,
        result.usage,
        latency_ms,
        result.steps,
    )
    return ChatResponse(
        text=result.last_llm_text,
        reasoning=result.reasoning,
        tool_calls=last_calls,
        stop_reason=result.stop_reason,
        usage=result.usage,
        model=result.model or _client(request).model,
        latency_ms=latency_ms,
        steps=result.steps,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MyAgent HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    args = parser.parse_args()
    _setup_logging()  # 放在 uvicorn.run 之前：uvicorn 的 dictConfig 不会吃掉已装的 handler
    # 模块名必须与文件名一致：uvicorn 是按字符串导入的，改文件名不改这里会 "Could not import module"
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
