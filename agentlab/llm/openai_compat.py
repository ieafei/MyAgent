"""文件 3：OpenAI 兼容适配器。

它实现文件 2 定下的 LLMClient：把 Message 列表变成 OpenAI 的 HTTP 请求，
把回来的 JSON 变成 Completion。DeepSeek / Kimi / 通义兼容模式 / vLLM /
Ollama 都能用，只要它们的接口是 OpenAI 那一套。

结构上刻意分成两半：

    encode_messages / encode_tools / decode_response   ← 编解码纯函数，不联网
    OpenAIClient.complete                              ← 只剩发请求

叫 encode/decode 而不是 to_openai_xxx：源和目标在一句话里都看得见
（我们的 Message ↔ 该协议的 JSON），而且将来 anthropic_compat 里可以复用
同一套动词——两个适配器用同样的词，读代码不用重新学。

因为翻译才是 bug 高发区（arguments 转义、tool_call_id 对不上、content 该是
null 却给了空串），而纯函数用普通 assert 就能测，不需要模拟网络。

不做的三件事：流式、重试、打印。各有各的去处。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

import httpx

from agentlab.llm.base import (
    AuthError,
    BadRequestError,
    LLMClient,
    NetworkError,
    ResponseFormatError,
    error_from_status,
)
from agentlab.types import (
    Completion,
    Message,
    StopReason,
    TextBlock,
    ToolCallBlock,
    ToolDefinition,
    Usage,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# OpenAI 的 finish_reason → 我们的 StopReason。
# 查不到的（新加的、厂商自己编的）走兜底，不抛异常：多一个取值不该让整轮失败。
_FINISH_REASON: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


# ---------------------------------------------------------------------------
# 第一半：编解码（纯函数，可单独测试）
# ---------------------------------------------------------------------------


def encode_messages(messages: Sequence[Message]) -> list[dict[str, object]]:
    """Message 列表 → OpenAI 的 messages 数组。

    四种角色处理方式不同：

    - system / user：拼成一段文本
    - assistant：content 可以为 None（只有工具调用时），另外挂 tool_calls
    - tool：**一个结果一条消息**，靠 tool_call_id 与请求配对
    """
    out: list[dict[str, object]] = []

    for msg in messages:
        if msg.role == "tool":
            # 一条 tool 消息里理论上只有一个结果块；写成循环是为了让手工构造
            # 的多块消息也能正确展开，而不是悄悄丢掉后面几块。
            for block in msg.tool_results():
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_call_id,
                        "content": block.content,
                        # is_error 在 OpenAI 协议里没有对应字段，这里丢弃；
                        # 它是给我们自己的日志和 UI 看的。
                    }
                )
            continue

        if msg.role == "assistant":
            text = msg.text()
            calls = msg.tool_calls()
            if not text and not calls:
                # 提前拦，比让服务端回一个看不懂的 400 强。
                raise BadRequestError("assistant 消息既没有文本也没有工具调用，发出去必被拒")
            item: dict[str, object] = {
                "role": "assistant",
                # 有工具调用、没有文本时必须给 None；给 "" 有些服务会报错。
                "content": text or None,
            }
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            # arguments 本来就是 JSON 字符串，**原样透传**。
                            # 再 json.dumps 一次会变成双层转义，模型收到的参数是
                            # 一个没人能解析的字符串。
                            "arguments": call.arguments or "{}",
                        },
                    }
                    for call in calls
                ]
            out.append(item)
            continue

        out.append({"role": msg.role, "content": msg.text()})

    return out


def encode_tools(definitions: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    """ToolDefinition → OpenAI 的 tools 数组。

    ToolDefinition 的三个字段正好等于 function 对象，所以只是套两层壳。
    parameters 原样透传，不做任何解析。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
            },
        }
        for definition in definitions
    ]


def _parse_usage(raw: object) -> Usage:
    """解析 usage，兼容 OpenAI 和 DeepSeek 的两种缓存字段写法。

    - OpenAI:          prompt_tokens_details.cached_tokens
    - DeepSeek:        prompt_cache_hit_tokens
    两者语义相同：命中缓存的输入 token，**已包含在** prompt_tokens 内。
    """
    if not isinstance(raw, dict):
        return Usage()

    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = raw.get("prompt_cache_hit_tokens")

    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cached_input_tokens=int(cached or 0),
    )


def decode_response(data: dict[str, object], model: str) -> Completion:
    """响应 JSON → Completion。

    这里对"结构不对"要抠得细一点：一个字段缺失就在下游变成别的错误，
    错误信息会指向错误的地方。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        dump = json.dumps(data, ensure_ascii=False)
        raise ResponseFormatError(f"响应里没有 choices：{_preview(dump)}")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ResponseFormatError(f"choices[0] 不是对象：{_preview(str(choice))}")

    raw_message = choice.get("message")
    if not isinstance(raw_message, dict):
        raise ResponseFormatError(
            f"choices[0].message 缺失或不是对象：{_preview(str(raw_message))}"
        )

    blocks: list[TextBlock | ToolCallBlock] = []

    content = raw_message.get("content")
    if isinstance(content, str) and content:
        blocks.append(TextBlock(content))

    # 思考过程只取出来看，不进消息：DeepSeek 的 reasoner 明确拒绝回传
    # reasoning_content（400），放在 Completion 上最安全。
    reasoning: str | None = None
    for key in ("reasoning_content", "reasoning"):
        raw_reasoning = raw_message.get(key)
        if isinstance(raw_reasoning, str) and raw_reasoning:
            reasoning = raw_reasoning
            break

    raw_calls = raw_message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            fn = raw_call.get("function")
            fn = fn if isinstance(fn, dict) else {}
            arguments = fn.get("arguments")
            if isinstance(arguments, dict):
                # 少数服务直接把对象塞进来，转回字符串保持内部统一。
                arguments = json.dumps(arguments, ensure_ascii=False)
            blocks.append(
                ToolCallBlock(
                    id=str(raw_call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=str(arguments or "{}"),
                )
            )

    finish_reason = choice.get("finish_reason")
    stop_reason: StopReason
    if isinstance(finish_reason, str) and finish_reason in _FINISH_REASON:
        stop_reason = _FINISH_REASON[finish_reason]
    else:
        # 兜底：有工具调用就当 tool_use，否则当作正常说完。
        # 只有这个判断是"猜"的，其余都按字段原样走。
        has_call = any(isinstance(b, ToolCallBlock) for b in blocks)
        stop_reason = "tool_use" if has_call else "end_turn"

    return Completion(
        message=Message(role="assistant", content=tuple(blocks)),
        stop_reason=stop_reason,
        usage=_parse_usage(data.get("usage")),
        model=str(data.get("model") or model),
        reasoning=reasoning,
    )


def _preview(text: str, limit: int = 300) -> str:
    """压成一行并截断，避免把整页 HTML / 巨型 JSON 打进终端。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------------------
# 第二半：真正的客户端
# ---------------------------------------------------------------------------


class OpenAIClient(LLMClient):
    """OpenAI 兼容服务的客户端。

    http_client 可以注入：测试时塞一个带 httpx.MockTransport 的实例，
    就能在不联网、不填 key 的情况下跑完整个 complete()。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise AuthError("api_key 不能为空：检查 AGENTLAB_API_KEY")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            # 自己重试会让重试器失去统一入口，这里必须关掉。
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    @classmethod
    def from_env(cls) -> OpenAIClient:
        """从环境变量构造。

        只读 os.environ，**不加载 .env**：改进程环境是入口（cli.py）的事，
        库不该替调用方做这种事。
        """
        api_key = os.environ.get("AGENTLAB_API_KEY", "")
        if not api_key:
            raise AuthError("缺少 AGENTLAB_API_KEY：复制 .env.example 为 .env 并填入 key")
        return cls(
            api_key=api_key,
            base_url=os.environ.get("AGENTLAB_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("AGENTLAB_MODEL", DEFAULT_MODEL),
        )

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
    ) -> Completion:
        """发一轮对话。成功返回 Completion，失败抛 LLMError 的子类。"""
        payload: dict[str, object] = {
            "model": self._model,
            "messages": encode_messages(messages),
            "stream": False,
        }
        if tools:
            # 注意是"有工具才加这个字段"：传空数组有些服务会报 400。
            payload["tools"] = encode_tools(tools)

        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as exc:
            # from exc 保留原始异常：否则堆栈断在 raise 这里，
            # 永远看不出底下是 DNS、TLS 还是读超时。
            raise NetworkError(f"连接 {self._base_url} 失败：{exc}") from exc

        if response.status_code != 200:
            raise error_from_status(
                response.status_code,
                response.text,
                retry_after=_retry_after(response),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ResponseFormatError(f"响应不是合法 JSON：{_preview(response.text)}") from exc
        if not isinstance(data, dict):
            raise ResponseFormatError(f"响应顶层不是对象：{_preview(response.text)}")

        return decode_response(data, self._model)

    async def aclose(self) -> None:
        """释放连接池。只关自己创建的 client，注入的不归我们管。"""
        if self._owns_http:
            await self._http.aclose()


def _retry_after(response: httpx.Response) -> float | None:
    """读 Retry-After 头（秒）。读不到就是 None，让重试器自己估。"""
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "OpenAIClient",
    "decode_response",
    "encode_messages",
    "encode_tools",
]
