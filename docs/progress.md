# MyAgent · 当前进展

> 最后更新：2026-11-18
> 文档目的：记录"现在到哪一步了"，方便随时中断、随时接上。

---

## 1. 状态快照

| 项 | 值 |
|---|---|
| 当前阶段 | **M1 地基**（接近完成） |
| 已完成文件 | 5 个（`agentlab/__init__.py`、`types.py`、`llm/__init__.py`、`llm/base.py`、`llm/openai_compat.py`） |
| 可运行能力 | 能用 `MockTransport` 跑通完整一轮（含并行工具调用 + 工具回填）；**真实请求还没发过** |
| 测试 | 0 个自动化测试（手工验证脚本放 `/tmp`，不进仓库） |
| 代码规模 | 839 行（`wc -l`：types 265 / base 179 / openai_compat 353 / 两个 `__init__` 42 / 包入口 3） |
| 运行环境 | WSL2 Ubuntu 24.04 · Python 3.14.4 · uv 0.13.9 · venv |
| 依赖状态 | 已安装（httpx / rich / jsonschema / python-dotenv / pytest / ruff / mypy） |

一句话：**适配器写完了，只差用真 key 打一次真实的 `POST /chat/completions`。**

---

## 2. 目录现状

```
MyAgent/
├── agentlab/               ← Python 包
│   ├── __init__.py         ← 只声明版本号
│   ├── types.py            ← ✅ 数据契约（OpenAI 格式）
│   └── llm/                ← 模型适配层
│       ├── __init__.py     ← ✅ 导出契约
│       ├── base.py         ← ✅ 错误分类 + LLMClient 接口
│       └── openai_compat.py← ✅ OpenAI 兼容适配器（353 行）
├── docs/
│   ├── goal-and-design.md  ← 目标与实现方式
│   ├── working-agreement.md← 协作约定（怎么写、怎么讲）
│   └── progress.md         ← 本文档
├── .env.example            ← 配置模板
├── .gitignore
├── pyproject.toml          ← 项目名 myagent，包名 agentlab
├── pytest.ini
└── .venv/                  ← 虚拟环境（不入库）
```

---

## 3. 已完成

### 3.1 开发环境

- WSL2 Ubuntu 24.04
- Python 3.14.4（系统自带）+ 独立 venv
- `uv` 管理依赖（系统 Python 没有 pip，用 uv 装的）
- 以 editable 模式安装：`uv pip install -e .`

验证：

```bash
$ .venv/bin/python -V
Python 3.14.4
$ .venv/bin/python -c "import agentlab; print(agentlab.__file__)"
/home/afei/MyAgent/agentlab/__init__.py
```

### 3.2 目录结构定型

经过三次调整，最终确定：

| 方案 | 结果 | 结论 |
|---|---|---|
| `agentlab/src/agentlab/` | 项目根与包同名，且多一层 src | 弃用：名字重复，难读 |
| `agent/src/agentlab/` | 消除重名，但仍多一层 | 弃用：`src` 对个人项目是多余抽象 |
| **`MyAgent/agentlab/`** | **项目名 MyAgent，包名 agentlab** | **采用** |

命名规则：**目录名 = import 名**。`import agentlab` 之所以能工作，是因为有个叫 `agentlab` 的目录，里面有 `__init__.py`。

### 3.3 `types.py`（文件 1 / 约 220 行）

**职责**：定义 agent 在内存里流动的所有数据结构。零依赖、零业务逻辑。

包含：

| 类型 | 作用 |
|---|---|
| `Role` | `system` / `user` / `assistant` / `tool`（与 OpenAI 一致） |
| `StopReason` | `end_turn` / `tool_use` / `max_tokens` / `refusal`，是主循环的**判断依据** |
| `TextBlock` | 一段文本 |
| `ToolCallBlock` | 模型要求调用工具（`id` 是配对凭证；`arguments` 是 JSON **字符串**原文） |
| `ToolResultBlock` | 工具执行结果（`is_error` 给我们自己看，OpenAI 协议无此字段） |
| `Message` | 角色 + 内容块；提供 `system/user/assistant/tool/from_tool_results` 构造器与 `text/tool_calls/tool_results` 视图方法 |
| `ToolDefinition` | 给模型看的工具说明书，三字段等于 OpenAI 的 `function` 对象 |
| `Usage` | token 账本：`input/output/cached_input`，支持 `+` 累加 |
| `Completion` | 一次模型调用的完整结果 |

验证：

```bash
$ .venv/bin/python -c "import agentlab.types as t; print(t.Message.user('hi').role)"
user
```

设计要点（详见 `goal-and-design.md` 第 4 节）：

- 内容块而不是纯字符串——因为一条 assistant 消息里可以同时有解释和多个工具调用
- `frozen=True` + `tuple`——历史只追加，禁止回头改已发出的消息
- **OpenAI 原生**——`role="tool"`、`tool_calls`、`arguments` 用 JSON 字符串，
  都是协议原样；一条工具结果 = 一条 `role="tool"` 消息（`Message.from_tool_results` 返回元组）
- 缓存命中——`Usage.cached_input_tokens` 单独记，因为价格差一个数量级；
  它**包含在** `input_tokens` 内，`uncached_input_tokens` 是全价部分
- 命名——`ToolDefinition` 是**声明**（不能执行），将来 `Tool` 才是实现；两个名字成对，读代码不用猜哪个能跑

### 3.4 `agentlab/llm/base.py`（文件 2 / 约 180 行）

**职责**：定"主循环怎么跟模型说话"的规矩。不碰网络、不拼 JSON。

| 类型 | 作用 |
|---|---|
| `LLMError` | 所有调用失败的根；`retryable` 是类属性，不是实例字段 |
| `AuthError` | 401 / 403，不重试 |
| `BadRequestError` | 400 / 404 / 422，不重试，责任在我们（翻译错了） |
| `RateLimitError` | 429，可重试，带 `retry_after` |
| `ServerError` | 5xx，可重试 |
| `NetworkError` | 连不上 / 超时，可重试 |
| `ResponseFormatError` | HTTP 200 但 body 形状不对，不重试 |
| `error_from_status()` | 状态码 → 异常，集中在一处，避免各适配器分类不一致 |
| `LLMClient` | 抽象基类，只有 `model` + `complete(messages, tools)` |

验证（不联网跑通契约）：

```bash
$ .venv/bin/python -c "from agentlab.llm import LLMClient; LLMClient()"
TypeError: Can't instantiate abstract class LLMClient without an implementation for abstract methods 'complete', 'model'
```

设计要点：

- 错误消息里带处置建议（"检查 AGENTLAB_API_KEY"），光说"鉴权失败"等于把排查推给用户
- 响应体截断 300 字符并压成一行——5xx 常回整页 HTML
- `ResponseFormatError` 专治"HTTP 200 但结构不对"：这种错不进任何 `except`，
  会在下游炸成和真因无关的 `AttributeError`
- 用 `ABC` 不用 `Protocol`：漏实现方法在**实例化时**就报错
- 有它之后，主循环的测试可以全用假客户端，不需要网络

### 3.5 `agentlab/llm/openai_compat.py`（文件 3 / 353 行）

**职责**：实现 `LLMClient`——`Message` 列表 → OpenAI HTTP 请求，响应 JSON → `Completion`。
DeepSeek / Kimi / 通义兼容模式 / vLLM / Ollama 都能用。

结构上刻意分两半：**上半是纯函数（不联网），下半只剩发请求**。因为翻译才是 bug 高发区，纯函数用普通 assert 就能测。

命名约定（2026-11-19 定案，改过一次名）：

- 三个纯函数叫 `encode_messages` / `encode_tools` / `decode_response`，不用 `to_openai_xxx`：源和目标在同一句话里都看得见，且将来 `anthropic_compat` 能复用同一套动词。
- 类型叫 `ToolDefinition` 不叫 `ToolSpec`：它是给模型看的**声明**，不能执行；以后 `tools/base.py` 里能跑的才叫 `Tool`。
- `Completion` **保持不改**：它贴着 OpenAI 协议（`/chat/completions`、`completion_tokens`），改成 `ModelResponse` 反而丢掉这层对齐。

逐功能清单：

| 代码单元 | 行 | 做什么 | 关键取舍 |
|---|---|---|---|
| `_FINISH_REASON` | 49-54 | `finish_reason` → `StopReason` 查表 | 查不到的走兜底、**不抛异常**：厂商新增取值不该让整轮失败 |
| `encode_messages` | 62-120 | 四条角色分支编码（`Message` → JSON） | `tool` 一块一条消息；`assistant` 无内容提前拦；`arguments` 原样透传 |
| `encode_tools` | 123-139 | `ToolDefinition` 套两层壳 | `parameters` 不解析，只透传 |
| `_parse_usage` | 142-161 | token 账本 | 兼容 OpenAI `prompt_tokens_details.cached_tokens` 与 DeepSeek `prompt_cache_hit_tokens` |
| `decode_response` | 164-224 | 响应 JSON → `Completion` | 逐字段校验，缺字段报 `ResponseFormatError` 而不是在下游炸 |
| `_preview` | 227-230 | 压一行 + 截断 300 | 防止一整页 HTML 打进终端 |
| `OpenAIClient.__init__` | 245-264 | 存 key/model/url，建或注入 http | `httpx` 自带重试必须关（`retries=0`）；`_owns_http` 记所有权 |
| `from_env` | 266-280 | 读三个环境变量 | **不加载 `.env`**：改进程环境是 `cli.py` 的事 |
| `complete` | 286-330 | 组装 payload → POST → 分类错误 → 解析 | `tools` 为空时**不加这个字段**；`from exc` 保留原始堆栈 |
| `aclose` | 332-335 | 释放连接池 | 只关自己创建的，注入的不动 |
| `_retry_after` | 338-343 | 读 `Retry-After` 头 | 只认数字秒；HTTP-date 格式读不出，返回 `None` 让重试器自己估 |

几个容易踩的代码点：

```python
# 1) tool 结果：一条块一条消息，is_error 在这里被丢弃（协议没有这个字段）
for block in msg.tool_results():
    out.append({"role": "tool", "tool_call_id": block.tool_call_id,
                "content": block.content})

# 2) assistant：有工具无文本时 content 必须是 None，给 "" 有的服务会 400
item = {"role": "assistant", "content": text or None}

# 3) arguments 本来就是 JSON 字符串，再 dumps 一次 = 双层转义
"arguments": call.arguments or "{}"

# 4) 只有传了工具才加 tools 字段（空数组有的服务报 400）
if tools:
    payload["tools"] = encode_tools(tools)
```

验证（`/tmp/verify_openai_compat.py`，全程 `httpx.MockTransport`，不联网、不填 key）：

```
$ .venv/bin/python /tmp/verify_openai_compat.py

=== 1. encode_messages：文本 + 两个并行工具调用 + 两条工具结果 ===（节选）
{"role": "assistant", "content": "我先看这两个文件",
 "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}}, {...第二个略}]}
{"role": "tool", "tool_call_id": "call_1", "content": "print(1)"}
{"role": "tool", "tool_call_id": "call_2", "content": "Permission denied"}

=== 2. encode_tools ===
[{"type": "function", "function": {"name": "read_file", "description": "读一个文件",
  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]

=== 3. decode_response：finish_reason=tool_calls + DeepSeek 缓存命中 ===
stop_reason = tool_use
text        = '我查一下'
tool_calls  = [('call_x', 'read_file', '{"path":"a.py"}')]
usage       = Usage(input_tokens=1200, output_tokens=30, cached_input_tokens=1024) | total = 1230 | 全价输入 = 176

=== 4. decode_response：没见过的 finish_reason 走兜底（不抛异常） ===
stop_reason = end_turn | usage = Usage(input_tokens=0, output_tokens=0, cached_input_tokens=0)

=== 5. 真报错 A：assistant 既没文本也没工具调用 ===
BadRequestError: assistant 消息既没有文本也没有工具调用，发出去必被拒

=== 6. 真报错 B：HTTP 200 但 body 没有 choices ===
ResponseFormatError: 响应里没有 choices：{"error": "something"}

=== 7. OpenAIClient.complete()：MockTransport，记录发出去的请求 ===
实际请求 URL : https://example.invalid/v1/chat/completions
Authorization: Bearer sk-fake
请求 messages: [{'role': 'user', 'content': '你好'}]
请求带 tools : True
--> Completion: tool_use deepseek-chat Usage(input_tokens=1200, output_tokens=30, cached_input_tokens=1024)
aclose() 后注入的 client 仍可复用: True

AuthError (status=401, retryable=False): 鉴权失败(401)：检查 AGENTLAB_API_KEY 是否正确、是否过期 | {"error":{"message":"Invalid API key"}}

NetworkError: 连接 http://127.0.0.1:9 失败：All connection attempts failed
```

静态检查：`ruff check agentlab` → `All checks passed!`；`python -m mypy agentlab` → `Success: no issues found in 5 source files`。

设计要点：

- **编解码与传输分离**：`encode_messages` / `encode_tools` / `decode_response` 都是纯函数，不联网就能测；`complete()` 只剩"发请求 + 分类错误"。
- **编码错误提前拦**：`assistant` 既无文本也无工具调用时直接抛 `BadRequestError`，而不是等服务端回一个看不懂的 400。
- **`is_error` 在发请求时丢弃**：OpenAI 协议没有这个字段，它只活在我们自己的历史与 UI 里；模型看到的是错误文本。
- **未知 `finish_reason` 兜底是"猜"**：有工具调用判 `tool_use`，否则 `end_turn`。全文件唯一不按字段原样走的地方。
- **不重试、不打印、不流式**：`stream=False` 写死；429 只抛带 `retry_after` 的 `RateLimitError`；渲染归 `cli.py`。
- 诚实记录两处小瑕疵：`_preview` 与 `base._short` 逻辑重复（因 `_short` 是私有）；`_retry_after` 不解析 HTTP-date 格式。

### 3.6 `agentlab/tools/base.py`（文件 4 / 188 行）+ `agentlab/tools/__init__.py`（21 行）

**职责**：只定工具的形状——声明（`ToolDefinition` 由类属性生成）、实现（`Tool.run`）、结果（`ToolResult`）、截断（`truncate_output`）。**校验、超时、兜异常、查表都不在这里**，留给下一个文件 `registry.py`。

不 import 网络、不 import 文件系统、不知道 agent 存在，因此测它只需要普通对象。

逐功能清单：

| 代码单元 | 行 | 做什么 | 关键取舍 |
|---|---|---|---|
| `ToolResult` | 38-66 | 结果三字段：`content` / `is_error` / `truncated` | 失败是**正常返回**而不是异常；`truncated` 只给我们自己看，不进协议 |
| `ToolResult.ok` / `.fail` | 50-56 | 两种构造快捷方式 | 让 `return ToolResult.fail("文件不存在")` 一眼看出是失败分支 |
| `ToolResult.to_block` | 58-66 | → `ToolResultBlock` | `tool_call_id` 必须来自模型给的那个 id；配错时所有结构都合法，只有模型会串味 |
| `ToolError` | 72-80 | 能解释清楚的失败 | 与"我们的 bug"分开：前者消息原样给模型，后者进日志带 traceback |
| `Tool` | 87-127 | 三属性 + `definition` + 抽象 `run` | `definition` 由类属性现算，声明与执行共用同一份 `parameters`，不会漂 |
| `Tool.definition` | 101-114 | 类属性 → `ToolDefinition` | 缺 `name` / `parameters` 在这里就抛 `ValueError`（注册期发现，不是等模型点名） |
| `Tool.run` | 116-127 | `async def run(args: Mapping) -> ToolResult` | 收 Mapping 不收 `**kwargs`：JSON 键名任意，且错误信息要能点名是哪个键 |
| `truncate_output` | 140-180 | 超长砍中间，返回 `(文本, 是否截断)` | 头尾都留；切口**对齐行边界**（否则模型会以为结构到此为止）；`head_ratio` 按工具调比例；仍只是兜底 |

验证（`/tmp/verify_tools_base.py`，纯内存）：

```
=== 1. Tool.definition：给模型看的那份声明 ===
ToolDefinition(name='echo', description='把输入原样返回，用来验证工具协议',
               parameters={'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']})

[{"type": "function", "function": {"name": "echo", "description": "把输入原样返回，用来验证工具协议",
  "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}]

=== 2. run → ToolResult → to_block → tool 消息（is_error 在这里被协议丢掉） ===
成功 : ToolResult(content='hi', is_error=False, truncated=False)
失败 : ToolResult(content='参数类型不对：text 应为 string', is_error=True, truncated=False)
{"role": "assistant", "content": null, "tool_calls": [{"id": "call_1", ...}, {"id": "call_2", ...}]}
{"role": "tool", "tool_call_id": "call_1", "content": "hi"}
{"role": "tool", "tool_call_id": "call_2", "content": "参数类型不对：text 应为 string"}
回填块里 is_error 还在（本地): [False, True]

=== 3. 忘写 parameters：访问 definition 时直接炸，不进请求 ===
ValueError: NoParamsTool 缺 name 或 parameters：parameters 至少要 {'type': 'object', 'properties': {}}

=== 4. 执行不下去抛 ToolError，由调用方兜住 ===
ToolError: 目标路径越界：../../etc/passwd（只能在工作区内操作）

=== 5. truncate_output：短的不动，长的砍中间 ===
短文本: 'print(1)' | 截断 = False
原文长度: 55000
截断 = True | 返回长度: 24000
切口前最后一行: 'line 01089'
切口后第一行  : 'line 03910'
结尾 : ' 04996\nline 04997\nline 04998\nline 04999\n'

=== 6. 自定义上限（limit=100） ===
截断 = True | 返回长度: 108
'line 00000\nline 00001\nline 00002\nline 00003\n… [中间省略 54913 字符] …\nline 04996\nline 04997\nline 04998\nline 04999\n'

=== 7. head_ratio：traceback 场景只留头部 1/4 ===
头段行数 = 22 | 尾段行数 = 69

=== 8. 非法 head_ratio 与超长单行（没有换行可对齐） ===
ValueError: head_ratio 必须在 (0, 1) 之间，收到 1.5
单行 50000 字符 → 截断 = True | 返回: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' … 'AAAAAAAAAAAAAAA'
```

（第 5/6 条与改动前对比：之前切口落在 `line 0` / `04995` 这种半行上，返回长度 24021；现在首尾都是完整行，长度 24000。单行无换行时退化为硬切，见第 8 条。）

静态检查：`ruff check agentlab` → `All checks passed!`（首次跑出 2 个 `UP037`：`"ToolResult"` 不该加引号，因已 `from __future__ import annotations`，已修）；`python -m mypy agentlab` → `Success: no issues found in 7 source files`。

设计要点：

- **声明与实现共用一份 `parameters`**：发给模型的和用来校验的是同一个对象，杜绝"文档写 A、代码查 B"。
- **结果只收字符串**：它最终要进 prompt，模型只读文本；结构化值进 prompt 前总得序列化，那就在工具里做完，顺带逼着想清楚"这个对象打印出来模型能看懂吗"。
- **失败是数据（4.4 的落地）**：`ToolResult(is_error=True)` 正常返回，模型读到"文件不存在"会自己换路；只有实现层面走不下去才抛 `ToolError`。
- **截断砍中间，但不切半行**：头尾都有决定性信息（报错结论在末尾、import/定义在开头）。切口对齐行边界——半行会让模型以为结构到此为止，据此 `edit_file` 必然匹配不上。实测 55000 字符 → 24000，首行 `line 01089`、末行 `line 03910` 都完整。
- **`head_ratio` 留出按内容调的门**：默认 0.5；`bash` 的 traceback 结论在末尾可给 0.25，`grep` 命中在前面可给 0.8。现在没人传非默认值，参数先摆好。
- **截断只是兜底，不是解法**：真正的解法是工具自带分页（`read_file` 的 offset/limit），让长输出一开始就不整段塞进来。已列为 `registry.py` / `builtin/fs.py` 的验收项。
- **不做的**：schema 校验、超时、重试、注册表——全在 `registry.py`；`truncate_output` 只被它调用，且它不知道自己在截什么（不按工具语义做启发式）。

---

## 4. 尚未开始

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | `llm/openai_compat.py` 真实 HTTP 适配器 | ✅ 代码完成，待真实请求验证 |
| M2 | `tools/` 工具系统 + 文件工具 | 🟡 `base.py` ✅（协议 / 结果 / 截断），待 `registry.py` |
| M3 | `loop.py` 主循环 | ⬜ |
| M4 | `context.py` / `policy.py` / `session.py` / `cli.py` | ⬜ |
| M5 | 测试、评测、可观测性 | ⬜ |

---

## 5. 下一步：M2 第二个文件 `agentlab/tools/registry.py`

M1 的真实请求暂缓（需要真 key，不阻塞后续开发）；`tools/base.py` 已完成，见 3.6。

`registry.py` 要做五道守门：**查表**（模型点名的工具不存在）→ **JSON Schema 校验**（4.5）→ **超时** → **兜异常**（`ToolError` 原样给模型，其他异常加前缀进日志）→ **截断回填**（`truncate_output` + `ToolResult.to_block`）。之后是 `tools/builtin/fs.py`。

截断讨论留下的两条验收项，别忘：

- **截断后要给出可执行的下一步**：回填文本附"共 N 字符，可用 `read_file` 的 offset 继续"，全文落临时文件、路径交给模型。只说"被截断了"等于让模型束手无策。
- **让截断尽量不发生**：`read_file` 自带 `offset` / `limit`，`grep` 自带命中上限。目标是日常使用中 `truncate_output` 几乎不触发，它只做最后一道闸。

### M1 欠账（有真 key 时补）

`openai_compat.py` 的验收清单，8 项里 8 项已有真实证据，但证据全部来自 `MockTransport`——**还没打过真正的网络**：

- [x] 从环境变量读 `AGENTLAB_BASE_URL` / `AGENTLAB_API_KEY` / `AGENTLAB_MODEL`（缺 key 与填 key 两条路径都跑过，见 3.5）
- [x] `Message` → OpenAI `messages` 数组（含 `role="tool"`、并行 `tool_calls`）
- [x] `ToolDefinition` → `tools` 数组
- [x] `usage` 解析出 `cached_input_tokens`（DeepSeek 写法已验证）
- [x] `finish_reason` → `StopReason` 映射，未知值有兜底
- [x] **非流式**
- [x] 错误按 `base.py` 分类抛（401 → `AuthError`、连不上 → `NetworkError` 都实测过）
- [x] 函数内部不打印任何东西

只剩一件事：**用真 key 发一轮**，确认真实服务端接受这套 payload（尤其 `arguments` 透传和 `content: null`）：

```bash
cp .env.example .env        # 填入 AGENTLAB_API_KEY
.venv/bin/python -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from agentlab.llm import OpenAIClient
from agentlab.types import Message

async def main():
    c = OpenAIClient.from_env()
    r = await c.complete([Message.user('用一句话说明你是谁')])
    print(r.stop_reason, r.usage)
    print(r.message.text())
    await c.aclose()

asyncio.run(main())
"
```

通过之后 M1 收尾。M2 已开工：`tools/base.py` ✅（3.6），下一个是 `registry.py`。仍然一次一个文件。

> 文档冲突已消除：本文此前把 M1 标为"⬜ 下一步"，与 `openai_compat.py` 已存在的事实不符；本次一并同步。

---

## 6. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-17 | 初次建立项目骨架（types/events/llm/tools/loop + 58 个测试） |
| 2026-11-17 | **整体推倒重来**：一次性生成太多文件，无法逐个理解；改为"一次一个文件、每个文件现场验证" |
| 2026-11-17 | 目录结构三次调整，最终定为 `MyAgent/agentlab/` |
| 2026-11-17 | 重写 `types.py`，import 与对象构造验证通过 |
| 2026-11-18 | `types.py` 按 **OpenAI 协议**重塑：`Role` 增加 `tool`；`ToolUseBlock` → `ToolCallBlock`、`input` → `arguments`（JSON 字符串）；工具结果改为一条结果一条消息；`Usage` 增加 `cached_input_tokens` |
| 2026-11-18 | 新增 `agentlab/llm/`：`base.py` 定错误分类与 `LLMClient` 契约，`__init__.py` 导出 |
| 2026-11-18 | 新增 `docs/working-agreement.md`：把"怎么讲新文件、怎么控复杂度"写成固定约定 |
| 2026-11-18 | 提交 `a239380`（types + llm 契约 + 文档同步） |
| 2026-11-19 | 新增 `agentlab/llm/openai_compat.py`（353 行）：三个翻译纯函数 + `OpenAIClient`；全程 `MockTransport` 验证，未联网 |
| 2026-11-19 | `working-agreement.md` 第 2 节改写为"逐文件 → 逐功能 → 逐代码"；第 9 节补一条反馈信号 |
| 2026-11-19 | 命名调整：`ToolSpec` → `ToolDefinition`；`to_openai_messages`/`to_openai_tools`/`parse_completion` → `encode_messages`/`encode_tools`/`decode_response`；`Completion` 与 `complete()` 保持不动（贴协议）。改完重跑验证，脚本/ruff/mypy 全绿 |
| 2026-11-19 | 新增 `agentlab/tools/base.py`（166 行）与 `agentlab/tools/__init__.py`（21 行）：`Tool` 协议、`ToolResult`、`ToolError`、`truncate_output`。验证脚本、ruff（修掉 2 处 UP037）、mypy 全绿 |
| 2026-11-19 | `truncate_output` 由"按字符硬切"改为"切口对齐行边界 + `head_ratio` 可调"（188 行）：半行会让模型误判结构、`edit_file` 匹配失败。重新验证 8 项、ruff、mypy 全绿；截断的分页解法列为后续验收项 |

### 2026-11-18 的两条调整

1. **协议方向**：从"厂商中立（Anthropic 形态）"改为**对齐 OpenAI**。理由是实际要接的是
   DeepSeek / Kimi / vLLM 这类 OpenAI 兼容服务，内部少一层翻译就少一类 bug。
   代价：以后接 Anthropic 需要反过来写适配器。
2. **讲解方式**：第一版按行号平铺讲 `types.py`，被判定"过于复杂"。改为
   "我刚写完递给你"的口吻——先说这文件干什么，再逐个说决定和代价。
   规则写进 `docs/working-agreement.md`，避免下次重犯。

### 为什么推倒重来

第一版在一次会话里生成了 12 个源文件、2100 行代码、58 个测试。**能跑，但读不懂**——文件之间互相依赖，改动一处要同时理解五处。

于是清空源码，从 `types.py` 重新开始，规则是：**一个文件讲透再写下一个，每个文件必须现场跑出真实结果。**

---

## 7. 已知问题

| 问题 | 影响 | 处理 |
|---|---|---|
| 无自动化测试 | 重构时缺少回归保护 | M5 补；当前每个文件用手工脚本验证 |
| Python 3.14 较新 | 个别第三方库可能没有适配 | 目前依赖均正常 |
| 开发机文件工具异常 | DSH 的文件编辑工具无法覆盖 WSL 路径下的已有文件，只能用脚本改写 | 环境问题，与项目无关；不影响 GitHub 上传 |
| `.venv/bin/mypy` 入口脚本损坏 | 直接执行报 `bad interpreter`（shebang 指向 `/home/afei/agentlab/.venv`） | 改用 `.venv/bin/python -m mypy`；重建 venv 时一并修 |

---

## 8. 如何运行

```bash
git clone <repo-url> MyAgent
cd MyAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"       # 或 uv pip install -e ".[dev]"

cp .env.example .env          # 填入 API Key

python -c "import agentlab; print(agentlab.__version__)"
```

> 注：当前还没有可执行的 CLI。`cli.py` 完成后才能直接 `agentlab "你的任务"`。
