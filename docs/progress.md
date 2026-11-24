# MyAgent · 当前进展

> 最后更新：2026-11-23
> 文档目的：记录"现在到哪一步了"，方便随时中断、随时接上。

---

## 1. 状态快照

| 项 | 值 |
|---|---|
| 当前阶段 | **M2 进行中**：工具协议 ✅ → 注册表 ✅，下一步内置文件工具 |
| 已完成文件 | `agentlab/` 下 10 个（`__init__`、`types`、`llm/` 3 个、`tools/` 3 个、`tools/builtin/` 2 个）+ 根目录 `main.py`（服务入口）+ `frontend/index.html`（对话测试页） |
| 可运行能力 | `python main.py` 起 HTTP 服务：`/health` 报配置，`/chat` 单轮对话实测 1.2s 拿到真实回答，`/docs` 点按钮可用，**`/` 打开网页版对话测试台**；`ToolRegistry` 可在内存里完整跑通一次工具调用（尚无端点、也还没有真实工具） |
| 测试 | 无 pytest 用例（4 个集成测试已按决定删除）；验证方式改为"重启服务 + 网页/curl"，纯逻辑文件用 `/tmp` 脚本跑真实输出 |
| 代码规模 | 源码约 1800 行（types 269 / llm 575 / tools 435 / builtin 518 / 包入口 3）+ `main.py` 240 行 + `frontend/index.html` 13.7 KB |
| 运行环境 | WSL2 Ubuntu 24.04 · Python 3.14.4 · uv 0.13.9 · venv |
| 依赖状态 | 已安装（httpx / rich / jsonschema / python-dotenv / fastapi / uvicorn / ruff / mypy / types-jsonschema；`pytest` 仍在 dev extras 里，仓库暂无测试文件） |

一句话：**工具从"模型说"到"真执行"的五道守门已经落地（`registry.py`），下一步写 `tools/builtin/fs.py` 给出第一批能真跑的工具，再接 `loop.py` 让 `/chat` 闭环。**

---

## 2. 目录现状

```
MyAgent/
├── main.py              ← ✅ 服务主入口（175 行）：/ + /health + /chat
├── frontend/
│   └── index.html          ← ✅ 对话测试台（13.2 KB，单文件、零构建、无 CDN）
├── agentlab/               ← Python 包
│   ├── __init__.py         ← 只声明版本号
│   ├── types.py            ← ✅ 数据契约（OpenAI 格式）
│   ├── llm/                ← 模型适配层
│   │   ├── __init__.py     ← ✅ 导出契约
│   │   ├── base.py         ← ✅ 错误分类 + LLMClient 接口
│   │   └── openai_compat.py← ✅ OpenAI 兼容适配器（357 行）
│   └── tools/              ← 工具层
│       ├── __init__.py     ← ✅ 只导出协议层
│       ├── base.py         ← ✅ Tool / ToolResult / 截断（188 行）
│       ├── registry.py     ← ✅ 注册表：查表/校验/超时/兜异常/截断（226 行）
│       └── builtin/        ← ✅ 内置工具
│           ├── __init__.py ← ✅ 不 re-export（工具必须显式构造）
│           └── fs.py       ← ✅ 5 个文件工具 + 工作区越界检查（509 行）
├── docs/
│   ├── goal-and-design.md  ← 目标与实现方式
│   ├── working-agreement.md← 协作约定（怎么写、怎么讲）
│   └── progress.md         ← 本文档
├── .env.example            ← 配置模板（`.env` 是本地真实配置，不入库）
├── .gitignore
├── pyproject.toml          ← 项目名 myagent，包名 agentlab
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

### 3.7 `main.py`（服务主入口，175 行）

一句话职责：**把库装配成一个一直开着的服务**，让"新增一个能力 → 重启 → 发一条请求"成为验证方式。断言只能证明"没坏"，服务才能证明"能用"：模型怎么答、花了多少 token、工具有没有真被调用，全都看得见。

本次只有两个端点：

| 端点 | 作用 |
|---|---|
| `GET /health` | 活着吗？`version` / `model` / `uptime_s` |
| `POST /chat` | 单轮对话，返回 `text` / `tool_calls` / `stop_reason` / `usage` / `latency_ms` |

真实输出（`python main.py --port 8123` 之后）：

```
$ curl -s 127.0.0.1:8123/health
{"status":"ok","version":"0.1.0","model":"deepseek-chat","uptime_s":3.1}

$ curl -s 127.0.0.1:8123/chat -H 'content-type: application/json' \
       -d '{"text":"用一句话说明你是谁"}'
{"text":"我是DeepSeek，由深度求索公司创造的AI助手……","tool_calls":[],"stop_reason":"end_turn",
 "usage":{"input_tokens":8,"output_tokens":29,"cached_input_tokens":0},
 "model":"deepseek-flash","latency_ms":1215,"note":null}

$ curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8123/chat \
       -H 'content-type: application/json' -d '{}'
422        ← pydantic 直接挡掉缺 text 的请求，不浪费一次模型调用
```

设计要点：

- **只做装配**：读 `.env`、造 client、挂路由、把异常翻成状态码。编解码在 `llm/`、工具在 `tools/`、多轮在 `loop.py`，都不进这里。
- **`load_dotenv()` 放在入口**：库只读 `os.environ` 这条边界不破——改进程环境是入口的职责。
- **client 进程内复用**：`lifespan` 里建一次、退出时 `aclose()`。请求级新建会把连接池的复用全丢掉，每次都重新 TLS 握手。
- **异常翻译成状态码**：`AuthError→401`、`BadRequestError→400`、`RateLimitError→429`、`NetworkError→503`、其余 `502`，body 统一 `{"error": {"type", "message"}}`。调用方看状态码就知道是谁的问题。
- **`arguments` 原样透出**：它是 JSON 字符串，解析与校验是注册表的职责，路由不越界。
- **`note` 字段坦白缺口**：模型要求调工具而注册表还没实现时，明确写"本次只透出参数"；接上 registry 后它自然变 `null`。
- **`model` 两侧可能不一致**：`/health` 报的是环境变量里的 `deepseek-chat`，`/chat` 报的是服务端回显的 `deepseek-flash`。以回显为准，别以为配错了。
- **`--reload` 需要 `watchfiles`**：当前只装了纯 `uvicorn`，要用它得先 `uv pip install watchfiles`；否则手动 Ctrl-C 重启。

### 3.8 `frontend/index.html`（对话测试台，13.2 KB）

一句话职责：**让人用鼠标就能把 `/chat` 的每条返回都看全**，不用背 curl 参数。

单文件、零构建、不引 CDN（离线可用），`GET /` 由 `main.py` 的 `FileResponse` 直接吐出来。改 HTML 只需刷新浏览器，不用重启服务；只有改 `main.py` 才需要重启。

界面上刻意暴露的信息（都是排查时真正会看的）：

| 位置 | 显示什么 | 为什么值得看 |
|---|---|---|
| 顶栏胶囊 | `/health` 的 model / version / uptime，15 秒轮询 | 服务挂了立刻变红，不用去翻终端 |
| 顶栏统计 | 本会话累计 input / output tokens | 多轮试错的成本一眼可见 |
| 每条回复下方 | `stop_reason` · 服务端 `latency_ms` · in/out · 浏览器测的 `rtt` | `latency_ms` 与服务端一致；`rtt` 比它大的是网络+渲染开销 |
| `cached N`（黄色） | DeepSeek 上下文缓存命中的 token 数 | 为后面做成本控制留观测点 |
| 工具调用块 | 模型要求调用的工具名、id、`arguments` 原文 | `arguments` 故意不解析——注册表没实现前，看原文才知道模型想干什么 |
| 失败气泡 | `HTTP 状态码 + error.type + message` | 401/404/422/503 的区分直接体现在颜色和文案上 |
| `note` 黄条 | 后端声明的能力缺口 | 例如"要求调工具但注册表还没实现" |

几个体验细节：Enter 发送、Shift+Enter 换行（中文输入法组合中不误发）、输入框随内容长高、请求中禁用发送并显示三个点、system prompt 存在 `localStorage` 里免重输、"清空"重置会话与统计。

验证：`curl -s -o /dev/null -w '%{http_code}' localhost:8000/` → `200`；`node --check` 检查内联 JS 无语法错误；`/docs` 不受影响（307→`/docs`，仍 200）。

### 3.9 `agentlab/tools/registry.py`（文件 5 / 226 行）

一句话职责：**把"模型说了一句 tool_call"变成"一条可以回填给模型的 tool 结果"**，中间六道守门（下表把"兜异常"拆成两行说）：

| 守门 | 失败时怎么处理 | 为什么这样 |
|---|---|---|
| 查表 | 失败结果，附当前可用工具清单 | 模型点错名字是常事，给它清单下一轮就能改 |
| 解析 `arguments` | 失败结果，带 `JSONDecodeError` 的 msg + 位置 | 原串截 200 字符回显，模型才知道自己哪儿写错了 |
| JSON Schema 校验 | 失败结果，只报**第一处** | 一次改一个更容易改对；五条错误一起塞回去小模型会连改错三处 |
| 超时 | 失败结果（`asyncio.wait_for`） | 工具卡死不能把主循环一起拖住 |
| `ToolError` | 消息**原样**给模型 | 意料之中的坏输入，模型看了能换路 |
| 其他异常 | 日志带 traceback，回填"内部错误" | 那是我们的 bug，堆栈给模型没用 |
| 截断 | 正常结果 + `truncated=True` + 一句可执行的下一步 | 只说"被截断"模型会重试同一个调用 |

注册期就炸的三件事：重名、缺 `name`/`parameters`、`parameters` 不是合法 JSON Schema
（`Draft202012Validator.check_schema` 在注册时跑一遍，Schema 只编译一次）。

公开面只有四个方法：`register` / `definitions()` / `execute(call)` / `execute_all(calls)`，
外加 `names()`、`__len__`、`__contains__`。`execute` 用 `call.id` 原样配对，
`execute_all` 用 `asyncio.gather` 并行且保序。

验证（`/tmp/verify_registry.py`，5 个假工具，全内存、不联网；`timeout_s=0.2`、`max_chars=2000`）：

```
=== 2. 注册期就炸 ===
Echo     → ValueError: 工具名重复：'echo'，已被 Echo 占用
NoParams → ValueError: NoParams 缺 name 或 parameters：parameters 至少要 {'type': 'object', 'properties': {}}
BadSchema→ ValueError: badschema 的 parameters 不是合法 JSON Schema：'nonsense' is not valid under any of the given schemas

=== 3. 查表失败 ===
没有名为 'read_file' 的工具。当前可用：echo、slow、faulty、strict、big    (is_error=True)

=== 4/5. 解析与校验 ===
arguments 不是合法 JSON（Illegal trailing comma before end of object，位置 12）：{"text": "a",}
参数不合法：(根) → 'text' is a required property
参数不合法：text → 1 is not of type 'string'
参数不合法：n → 99 is greater than the maximum of 10

=== 6/7. 正常执行与 ToolError ===
tool_call_id='call_abc' content='echo: hi' is_error=False
目标路径越界：../../etc/passwd（只能在工作区内操作）            (is_error=True)

=== 8. 超时 ===
slow 执行超时（超过 0.2 秒），已放弃本次调用。请缩小范围或换个方式再试。 | 实际耗时 0.20s

=== 9. 未预期异常 ===
ERROR myagent.tools: 工具 faulty 抛出未预期异常（这是实现问题，不是模型的问题）
RuntimeError: 数据库连接串拼错了          ← traceback 只进日志
回填：faulty 内部错误，已记录日志。请换一种方式继续，不要重复同样的调用。

=== 10/11. 截断与并行 ===
长度 = 1788 | ≤ 2000 = True | is_error = False
结尾：…[结果已截断] 原文 52000 字符，这里只保留了头尾共 1684 字符。请改用该工具的分页参数（例如 read_file 的 offset / limit、grep 的匹配上限）缩小范围后再调用一次。
耗时 0.000s（三个调用共享 0.2s 超时预算）→ c1: n=1 / c2: n=2 / c3: 参数不合法
```

静态检查：`ruff check agentlab` → `All checks passed!`；`python -m mypy agentlab` → `Success: no issues found in 8 source files`
（为此新增 dev 依赖 `types-jsonschema`：jsonschema 本体不带 `py.typed`，不给存根就得为它关掉 strict）。

设计要点：

- **失败一律是数据**：前五道守门全部返回 `ToolResult(is_error=True)`，不抛异常。参数写错、工具不存在都是模型能自己纠正的事。
- **校验用同一份 `parameters`**：发给模型的和用来校验的是同一个对象（`Tool.definition` 现算），文档与行为不会漂。
- **编译一次，调用多次**：`Draft202012Validator` 在 `register` 时建好存着，调用时不再解析 Schema。
- **三类失败分开写**：`ToolError`（模型的锅）/ 超时（环境的锅）/ 其他异常（我们的锅），前两者不写日志、后者写 traceback。混成一个 `except Exception` 会让日志里全是模型写错参数。
- **`max_chars` 管整块文本**：截断前先让出 300 字符给提示语和截断标记，否则"截到 24000"实际回填 24300。行对齐本身还有几十字符误差，见 `truncate_output`。
- **诚实记录**：`execute_all` 目前是 `gather` 全并发，没有并发上限——一轮里几十个调用时会同时开几十个协程，等 `fs.py` 落地后按需加信号量。

### 3.10 `agentlab/tools/builtin/fs.py`（文件 6 / 509 行）

一句话：**五个能真碰磁盘的工具，共用一份工作区检查。**

| 工具 | 干什么 | 关键约束 |
|---|---|---|
| `read_file` | 按行读 | `offset`/`limit`；头部标「第 N–M 行 / 共 K 行」+「用 offset=X 继续」 |
| `write_file` | 整体写 | 父目录自动创建；回报"已新建/已覆盖 + 原字节数" |
| `edit_file` | 精确替换 | 必须唯一命中：0 次、多次都失败，并说清怎么补 |
| `list_files` | 看目录 | 跳过 `.git/.venv` 等；深度优先；上限 200 条 |
| `grep` | 正则搜索 | 命中上限 50、扫描上限 2000 文件；跳过二进制与 >2 MB |

工作区（`_Workspace`）是所有工具的唯一前提：

- 构造时 `root.resolve()`；每个路径 `resolve()` 后用 `relative_to(root)` 判断越界。
- 因为 `resolve()` 会跟穿符号链接，**根目录里放一个指向 `/etc/passwd` 的软链接也会被拦住**（实测见下），只比字符串前缀拦不住这种。
- 报错与结果里给**相对路径**（`display()`），短，也不泄漏本机目录布局。
- `files()` 统一做三件事：跳过 `IGNORE_DIRS`、排序（`rglob` 顺序依赖文件系统，两次跑出不同顺序会误导模型）、限量。

两个实现要点：

- **阻塞 IO 全走 `asyncio.to_thread`**：registry 用 `gather` 并发调工具，直接在事件循环里 `read_bytes()` 会把并发变回串行。
- **分页在工具层做**：`read_file` 的 offset/limit、`grep` 的 max_matches 是主手段，registry 的 `truncate_output` 只做最后一道闸。

真实验证（`/tmp/verify_fs.py`，20 组用例，`ToolRegistry(timeout_s=5, max_chars=3000)`）：

```
--- 8. 路径越界 ../../etc/passwd ---
ERR 路径越界：'../../etc/passwd' 解析后是 /etc/passwd，不在工作区 /tmp/fsdemo 内。请改用相对工作区的路径，例如 agentlab/types.py。
--- 10. 软链接逃逸（根内 evil.txt → /etc/passwd）---
ERR 路径越界：'evil.txt' 解析后是 /etc/passwd，不在工作区 /tmp/fsdemo 内。
--- 3. 分页 ---
OK  src/app.py  第 7–10 行 / 共 10 行（前面还有 6 行）
--- 7. 二进制 ---
ERR binary.bin 看起来是二进制文件（前 4KB 含 NUL 字节）；read_file 只读文本
--- 13/14/15. edit_file ---
OK  已修改 src/app.py（从第 6 行起替换 1 处；文件现在 10 行）
ERR 在 src/app.py 里没找到 old_text。请先用 read_file 确认原文：缩进、空行、行尾空格、引号必须完全一致。
ERR old_text 在 src/app.py 里出现了 2 次，无法确定改哪一处。请把它扩大到能唯一定位，例如带上相邻的一两行。
--- 16/19. grep ---
OK  匹配 3 处：src/app.py:3: def greet... / :6: def twice(x: int)... / :9: def twice_again...
OK  匹配 2 处（max_matches=2）…（已到上限 2 条，可能还有更多：加上 glob 或收紧 pattern）
--- 20. read_file big.txt limit=2000 → registry 闸门 ---
总长度 = 2822（≤ 3000）结尾：[结果已截断] 原文 20048 字符，这里只保留了头尾共 2718 字符。请改用 offset/limit …
--- 1. list_files（.venv 被跳过，深度优先）---
src/ → src/app.py → big.txt → binary.bin → evil.txt → README.md   共 6 项
```

`list_files` 第一版是广度优先，输出把 `src/app.py` 排到了最后（像乱序），改成递归深度优先后目录紧跟自己的内容。

已知取舍（都没有瞒着）：

- **不做原子写**：`write_text` 直接覆盖，写到一半断电会留半截文件。等真有并发写需求再上临时文件 + `os.replace`。
- **`edit_file` 是字符串替换**，不做 diff/patch：简单、可预测，代价是模型必须逐字符给对原文。
- **不读 `.gitignore`**：只跳过固定的 `IGNORE_DIRS`。要跟 `.gitignore` 得引入解析器或 `git check-ignore`，现在不值得。
- **只搜 UTF-8**：GBK 文件会被 grep 静默跳过（read_file 会报编码错）。

---

## 4. 尚未开始

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | `llm/openai_compat.py` 真实 HTTP 适配器 | ✅ 真实请求已验证（经 `/chat` 实测） |
| M2 | `tools/` 工具系统 + 文件工具 | 🟢 `base.py` ✅、`registry.py` ✅、`builtin/fs.py` ✅，待接进 `main.py` |
| M3 | `loop.py` 主循环 | ⬜ |
| M4 | `context.py` / `policy.py` / `session.py` / `cli.py` | ⬜ |
| M5 | 测试、评测、可观测性 | ⬜ |

---

## 5. 下一步：把工具接进服务（改 `main.py`）

`tools/base.py`（3.6）、`registry.py`（3.9）、`builtin/fs.py`（3.10）都已完成，但这套东西现在**只有 `/tmp` 脚本能碰到**——服务里还没有任何工具。按 2026-11-20 的约定，下一步必须让它通过服务可见：

1. `lifespan` 里建 `ToolRegistry()`，用 `make_file_tools(Path.cwd())` 注册五个工具，挂到 `app.state.tools`。
2. 新增 `GET /tools`：返回 `registry.definitions()`，用 `BaseModel` + `Field(description=...)` 写好响应模型，保证能在 `/docs` 里点出来。
3. 服务重启后用 curl 打 `/tools`，把**请求与响应原文**贴进讲解——这是约定第 4 条要求的"看见完整往返"。

这一步先不碰 `/chat`：`/chat` 要真正闭环还得有 `loop.py`（多轮 tool_use 循环），那是再下一步。届时 3.7 里 `note="注册表还没实现"` 那句可以删掉。

工具侧还留了两个已知口子，等接线时一并处理或明确推迟：

- `execute_all` 是 `gather` 全并发、无上限（3.9 已记录）。接入前先确认单轮工具调用数量，必要时加信号量。
- `write_file` 非原子写（3.10 已记录）。默认工作区是 `Path.cwd()`，先只在可信目录跑。

### M1 验收清单（8 项全部有真实证据）

证据来源：2026-11-20 的 4 个集成测试（真实 DeepSeek 服务），不再只来自 `MockTransport`。

- [x] 从环境变量读 `AGENTLAB_BASE_URL` / `AGENTLAB_API_KEY` / `AGENTLAB_MODEL`（缺 key 与填 key 两条路径都跑过，见 3.5）
- [x] `Message` → OpenAI `messages` 数组（含 `role="tool"`、并行 `tool_calls`）
- [x] `ToolDefinition` → `tools` 数组
- [x] `usage` 解析出 `cached_input_tokens`（DeepSeek 写法已验证）
- [x] `finish_reason` → `StopReason` 映射，未知值有兜底
- [x] **非流式**
- [x] 错误按 `base.py` 分类抛（401 → `AuthError`、连不上 → `NetworkError` 都实测过）
- [x] 函数内部不打印任何东西

**2026-11-20 已收尾**：先由 4 个集成用例打真网络验证（已按决定删除），再由 `main.py` 的 `/chat` 端点复验——`stop_reason=end_turn`、`usage` 齐备、`latency_ms=1215`（见 3.7）。`arguments` 透传与 `role="tool"` 回填两条，等 `registry.py` 接上后在服务里再走一遍。

M2 进展：`tools/base.py` ✅（3.6）→ `registry.py` ✅（3.9）→ `builtin/fs.py` ✅（3.10），下一个是改 `main.py` 把工具接进服务。仍然一次一个文件。

> 文档同步：本文此前把 M1 标为"待真实请求验证"，与集成测试已全绿的事实不符；本次一并修正。

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
| 2026-11-20 | 新增 `tests/integration/`（conftest 32 行 + 4 个用例 109 行）：只测跨真实边界的契约；默认 `-m "not integration"` 不跑，缺 key 自动 skip。真 key 首跑 `4 passed in 2.29s`，**M1 收尾** |
| 2026-11-20 | 配置踩坑：根目录 `pytest.ini` 优先级高于 `pyproject.toml`，写进 pyproject 的 `markers`/`addopts` 完全不生效。配置集中到 `pytest.ini`，pyproject 里的 pytest 段落整段删除 |
| 2026-11-20 | 口径调整：集成测试**默认就跑**（删除 `addopts = -m "not integration"`），marker 只用于 `-m "not integration"` 选择性排除——几分钱不值得换来“真实边界永远没人测” |
| 2026-11-20 | **验证方式改为服务化**：删除 `tests/` 与 `pytest.ini`（`testpaths` 指向已删目录会报错）。理由：需要的是"改完重启、发请求看真实往返"，不是断言 |
| 2026-11-20 | 新增根目录 `main.py`（175 行）：FastAPI + uvicorn，`/health` + `/chat`。实测 `/chat` 1.2s 返回真实回答、缺 `text` 返回 422；依赖新增 `fastapi`、`uvicorn` |
| 2026-11-20 | 修 venv 搬迁残留：`.venv` 是从 `/home/afei/agentlab/.venv` 整体搬来的，`activate` 与 18 个 console script 里写死了旧路径——`source` 后 PATH 前端是**不存在的目录**，`python` 落到 conda base（`ModuleNotFoundError: uvicorn`），提示符却照常显示 `(.venv)`。批量改回新路径后 `which python` → `.venv/bin/python`，`mypy`/`ruff` 也可直接执行 |
| 2026-11-23 | 新增 `frontend/index.html`（13.2 KB）与 `GET /`：网页版对话测试台。单文件、零构建、无 CDN；展示 `stop_reason` / `latency_ms` / in-out tokens / 缓存命中 / `tool_calls` 原文 / 错误码，顶栏轮询 `/health`。`main.py` 增到 187 行 |
| 2026-11-23 | `frontend/index.html` 加内联 SVG favicon（data URI），消掉浏览器自动请求 `/favicon.ico` 产生的 404 噪音 |
| 2026-11-23 | **日志分诊**：`/chat` 每次往返打一行业务摘要（system 字数、问答各截 40 字、`stop_reason`、in/out/cached、工具数、耗时），失败时记 `ExceptionName → HTTP 码 + 原因`；`/health` 的访问日志用 `uvicorn.access` 上的 filter 挡掉（前端 15s 轮询 ≈ 每天 5760 行噪音）。实测 3 次 `/health` 零输出、`/chat` 两行（摘要 + 访问行） |
| 2026-11-23 | **前端目录改名**：`web/` → `frontend/`。服务入口一度改成 `backend.py`，当天又改回 `main.py`（用户决定）——文件名的价值不如"少一次迁移"，且它和 `agentlab/` 包平级，叫什么都只是入口。踩到的坑留作经验：`uvicorn.run("...:app")` 与 `_INDEX` 里的目录名都是字符串，改文件名必须同步改它们，否则前者报 `Could not import module`、后者让 `/` 返回 500 |
| 2026-11-23 | **删掉 `UsageOut` / `ToolCallOut` 两个镜像类**（`main.py` 187→175 行）：`ChatResponse` 的 `usage` / `tool_calls` 直接用 `types.py` 的 `Usage` / `ToolCallBlock`，pydantic 会为 dataclass 生成 schema，`/docs` 不受影响。理由：同一份数据写两遍必然漂移（`ToolCallOut` 已经静默丢了 `type` 字段）。代价明确接受：内部 dataclass 改字段名 = API 变更 |
| 2026-11-23 | 新增 `agentlab/tools/registry.py`（226 行）：`ToolRegistry` 六道守门（查表 / 解析 / Schema 校验 / 超时 / 兜异常 / 截断），`execute` 单发、`execute_all` 并行保序。`/tmp/verify_registry.py` 11 组用例全绿；ruff、mypy 通过（新增 dev 依赖 `types-jsonschema`） |
| 2026-11-23 | 新增 `agentlab/tools/builtin/`（`__init__.py` 9 行 + `fs.py` 504 行）：`read_file` / `write_file` / `edit_file` / `list_files` / `grep`，共用 `_Workspace` 根目录与越界检查（含软链接逃逸）。`/tmp/verify_fs.py` 20 组用例全绿；ruff、mypy 通过。首版 `list_files` 广度优先导致输出乱序，改为递归深度优先 |
| 2026-11-24 | `fs.py` 可读性整理（504→509 行）：`_arg_str` / `_arg_int` → `_get_str_arg` / `_get_int_arg`；`_walk` 内部 `visit` → `collect`，返回值统一成"返回 True 表示触到上限"，调用处 `complete = visit(...)` 改为 `truncated = collect(...)`；`display()` 注释改为如实说明——正常结果只给相对路径，但越界报错里仍会带出工作区根 |

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
| 没有自动化测试 | 重构时缺回归保护，改坏不会立刻报警 | 刻意如此（M5 再补）；当前每步都现场跑真实请求验证 |
| 服务只支持单轮 | 模型给 `tool_calls` 时不会执行，只透出参数并写明 `note` | `registry.py` 接上后可执行，`loop.py` 接上后开 `/agent` |
| 每次验证都花一次真实调用 | 调试时频繁重启 + curl 会累积费用 | 已确认接受；请求都极短，成本以分计 |
| Python 3.14 较新 | 个别第三方库可能没有适配 | 目前依赖均正常 |
| 开发机文件工具异常 | DSH 的文件编辑工具无法覆盖 WSL 路径下的已有文件，只能用脚本改写 | 环境问题，与项目无关；不影响 GitHub 上传 |

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

# 起服务（默认 127.0.0.1:8000）
python main.py

# 另一个终端：验证
curl -s 127.0.0.1:8000/health
curl -s 127.0.0.1:8000/chat -H 'content-type: application/json' \
     -d '{"text":"用一句话说明你是谁"}'
```

浏览器里三个入口：

- <http://127.0.0.1:8000/> —— 对话测试台（推荐）：发消息、看 token/延迟、看错误
- <http://127.0.0.1:8000/docs> —— 自动生成的接口文档，可点按钮发请求
- <http://127.0.0.1:8000/health> —— 纯文本状态

curl 只在"要写进脚本复现"时才用，日常点网页更快。

> 注：当前还没有可执行的 CLI。`cli.py` 完成后才能直接 `agentlab "你的任务"`。
