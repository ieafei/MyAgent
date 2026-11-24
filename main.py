"""MyAgent 服务主入口：`python main.py` 起一个 HTTP 服务，用 curl 或 `/docs` 页面验证功能。

为什么要有它（而不是只写断言）：每落地一个新能力（工具注册表、主循环、上下文压缩），
重启进程、换一条请求就能看到真实的完整往返——模型怎么回、花了多少 token、工具有没有被
调用。断言只能证明"没坏"，服务才能证明"能用"。

职责边界（越界就会长出第二个大泥球）：
- 只做装配：读 .env、造 client、挂路由、把异常翻译成 HTTP 状态码；
- 编解码在 llm/openai_compat.py，工具在 tools/，多轮在 loop.py（尚未实现）。

本次只有三个端点：
- GET  /health  服务活着、配置读对没
- POST /chat    单轮对话（模型若要求调工具，原样透出，暂不执行）
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
from agentlab.types import Message, ToolCallBlock, Usage

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
    tool_calls: list[ToolCallBlock] = Field(
        description="模型要求调用的工具；arguments 是 JSON 字符串，本端点不解析也不执行",
    )
    stop_reason: str
    usage: Usage
    model: str
    latency_ms: int
    note: str | None = Field(
        default=None,
        description="能力缺口说明，例如出现 tool_calls 但注册表还没实现",
    )


# -- 日志：一条消息一行业务日志，健康探测不刷屏 ---------------------------------

log = logging.getLogger("myagent")


def _setup_logging() -> None:
    """在 main() 里装 handler，不在 import 时改全局日志配置（被 import 也不该影响调用方）。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False  # 别让 root 再吐一遍


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
    logging.getLogger("uvicorn.access").addFilter(_QuietHealth())
    # 加载 .env 是"入口"的职责；OpenAIClient.from_env() 只读 os.environ，库不改调用方环境。
    load_dotenv()
    try:
        app.state.client = OpenAIClient.from_env()
    except AuthError as exc:
        print(f"[启动失败] {exc}")
        raise
    app.state.started_at = time.monotonic()
    yield
    await app.state.client.aclose()


app = FastAPI(title="MyAgent", version=agentlab.__version__, lifespan=lifespan)


def _client(request: Request) -> OpenAIClient:
    client: OpenAIClient = request.app.state.client
    return client


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
        "uptime_s": round(time.monotonic() - request.app.state.started_at, 1),
    }


def _log_chat(
    req: ChatRequest,
    answer: str,
    stop_reason: str,
    usage: Usage,
    latency_ms: int,
    tool_calls: int,
) -> None:
    """一次真实往返压成一行。原文看响应体，这里只要够定位问题：谁问了、答了什么、花了多少。"""
    log.info(
        "[chat] sys=%s q=%r → %r | stop=%s in=%d out=%d cached=%d tools=%d %dms",
        f"{len(req.system)}字" if req.system else "无",
        _brief(req.text),
        _brief(answer),
        stop_reason,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        tool_calls,
        latency_ms,
    )


@app.post("/chat", response_model=ChatResponse, summary="单轮对话")
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    messages: list[Message] = []
    if req.system:
        messages.append(Message.system(req.system))
    messages.append(Message.user(req.text))

    started = time.perf_counter()
    completion = await _client(request).complete(messages)
    latency_ms = int((time.perf_counter() - started) * 1000)

    calls = list(completion.message.tool_calls())
    usage = completion.usage
    _log_chat(req, completion.message.text(), completion.stop_reason, usage, latency_ms, len(calls))
    return ChatResponse(
        text=completion.message.text(),
        tool_calls=calls,
        stop_reason=completion.stop_reason,
        usage=completion.usage,
        model=completion.model or _client(request).model,
        latency_ms=latency_ms,
        note=("模型要求调用工具，但工具注册表还没实现，本次只透出参数" if calls else None),
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
