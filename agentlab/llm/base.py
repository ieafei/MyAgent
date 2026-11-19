"""文件 2：模型客户端的契约。

只解决一个问题：主循环该怎么跟"随便哪个模型"打交道。

三件小事：

1. 错误有类型。401 和 429 的处置方式相反：一个必须停下来告诉人，
   一个等两秒再来。用 except Exception 一把抓，等于把两种情况合并成
   "出错了"，重试器只能瞎猜。
2. 客户端有统一形状。主循环只认 LLMClient，不认 DeepSeek / OpenAI / vLLM。
   换厂商 = 换一个实现类，主循环一行不动。
3. 错误信息要可读。服务端 5xx 可能回一整页 HTML，直接塞进日志会淹掉终端。

它不解决的问题（故意的）：

- 不碰网络、不拼 JSON —— 那是 openai_compat.py
- 不做流式、不做重试 —— 各有各的文件，等真需要时再加
- 不校验模型返回的字段 —— 那是适配器在解析时顺手做的事
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from agentlab.types import Completion, Message, ToolDefinition

# ---------------------------------------------------------------------------
# 错误分类
#
# 每个类只回答两个问题：
#   "这错该不该重试"（retryable）—— 给重试器看
#   "人该看到什么"（__str__）     —— 给终端用户看
# ---------------------------------------------------------------------------

_BODY_LIMIT = 300
"""错误响应体截断长度。5xx 时常回一整页 HTML，不截断会把日志淹掉。"""


class LLMError(Exception):
    """所有模型调用失败的根。

    retryable 是类属性而不是实例属性：错误种类决定可重试性，
    不该出现"同一个 401 有时能重试"这种状态。
    """

    retryable: bool = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(LLMError):
    """401 / 403。key 错了或过期了，重试一百次也是错的。"""


class BadRequestError(LLMError):
    """400 / 404 / 422。请求本身有问题（模型名不存在、消息格式不对）。

    重试没用，而且**责任在我们**：通常是适配器翻译错了。
    """


class RateLimitError(LLMError):
    """429。等一会儿再来，服务端可能直接告诉我们等多久（Retry-After）。"""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class ServerError(LLMError):
    """5xx。对方的问题，通常几十秒内自愈。"""

    retryable = True


class NetworkError(LLMError):
    """连不上、DNS 失败、读超时。"""

    retryable = True


class ResponseFormatError(LLMError):
    """HTTP 200，但 body 不是我们认识的形状。

    最容易忽略的一类：它看起来"成功了"，不进任何 except 分支，
    然后在下游某个字段访问处炸掉，报一个和真因毫无关系的 AttributeError。
    """


def _short(body: str) -> str:
    """压成一行并截断，避免把整页 HTML 打进终端。"""
    flat = " ".join(body.split())
    return flat if len(flat) <= _BODY_LIMIT else flat[:_BODY_LIMIT] + "…"


def error_from_status(
    status: int,
    body: str = "",
    *,
    retry_after: float | None = None,
) -> LLMError:
    """把 HTTP 状态码翻译成上面的异常。

    集中在这里的原因：分类一旦散落到各个适配器，就一定会出现
    "这个适配器把 429 当致命错误、那个当服务端错误"的情况。
    """
    detail = _short(body)
    suffix = f" | {detail}" if detail else ""

    if status in (401, 403):
        return AuthError(
            f"鉴权失败({status})：检查 AGENTLAB_API_KEY 是否正确、是否过期{suffix}",
            status=status,
        )
    if status == 429:
        return RateLimitError(f"被限流(429)：稍后重试{suffix}", retry_after=retry_after)
    if status in (400, 404, 422):
        return BadRequestError(f"请求被拒绝({status})：{suffix}", status=status)
    if status >= 500:
        return ServerError(f"服务端错误({status})：稍后重试{suffix}", status=status)
    return LLMError(f"未预期的响应({status}){suffix}", status=status)


# ---------------------------------------------------------------------------
# 客户端契约
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """一个"把消息列表变成 Completion"的东西。

    刻意做成 ABC 而不是 Protocol：想要的是显式继承，让
    `class OpenAIClient(LLMClient)` 一眼可见，也让漏实现方法在
    实例化时就报错，而不是等到第一次调用。

    刻意只有一个方法：接口越窄，假客户端越好写，主循环越好测。
    """

    @property
    @abstractmethod
    def model(self) -> str:
        """实际使用的模型名。日志、token 账本、Completion.model 都要它。"""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
    ) -> Completion:
        """发一轮对话，拿回完整结果。

        实现方必须保证：成功返回 Completion，失败抛 LLMError 的子类，
        **永远不返回 None、永远不打印任何东西**（渲染是 cli 的事）。
        """


__all__ = [
    "AuthError",
    "BadRequestError",
    "LLMClient",
    "LLMError",
    "NetworkError",
    "RateLimitError",
    "ResponseFormatError",
    "ServerError",
    "error_from_status",
]
