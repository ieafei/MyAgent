# MyAgent · 当前进展

> 最后更新：2026-11-17
> 文档目的：记录"现在到哪一步了"，方便随时中断、随时接上。

---

## 1. 状态快照

| 项 | 值 |
|---|---|
| 当前阶段 | **M1 地基**（进行中） |
| 已完成文件 | 2 个（`__init__.py`、`types.py`） |
| 可运行能力 | 能构造消息对象；**还不能和模型对话** |
| 测试 | 0 个（测试要等主链路成形后再补） |
| 代码规模 | 约 220 行 |
| 运行环境 | WSL2 Ubuntu 24.04 · Python 3.14.4 · uv 0.13.9 · venv |
| 依赖状态 | 已安装（httpx / rich / jsonschema / python-dotenv / pytest / ruff / mypy） |

一句话：**地基打完了第一块砖，chat 能力还没接上。**

---

## 2. 目录现状

```
MyAgent/
├── agentlab/               ← Python 包
│   ├── __init__.py         ← 只声明版本号
│   └── types.py            ← ✅ 数据契约
├── docs/
│   ├── goal-and-design.md  ← 目标与实现方式
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
| `Message` | 角色 + 内容块；提供 `system/user/assistant/tool/tool_results` 构造器与 `text/tool_calls/tool_result_blocks` 视图方法 |
| `ToolSpec` | 给模型看的工具说明书，三字段等于 OpenAI 的 `function` 对象 |
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
  都是协议原样；一条工具结果 = 一条 `role="tool"` 消息（`Message.tool_results` 返回元组）
- 缓存命中——`Usage.cached_input_tokens` 单独记，因为价格差一个数量级；
  它**包含在** `input_tokens` 内，`uncached_input_tokens` 是全价部分

---

## 4. 尚未开始

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | `llm/client.py` 模型客户端 | ⬜ 下一步 |
| M2 | `tools/` 工具系统 + 文件工具 | ⬜ |
| M3 | `loop.py` 主循环 | ⬜ |
| M4 | `context.py` / `policy.py` / `session.py` / `cli.py` | ⬜ |
| M5 | 测试、评测、可观测性 | ⬜ |

---

## 5. 下一步：`agentlab/client.py`

**只做一件事**：把消息列表发给模型，拿回一个 `Completion`。

验收标准（全部满足才算完成）：

- [ ] 从环境变量读 `AGENTLAB_BASE_URL` / `AGENTLAB_API_KEY` / `AGENTLAB_MODEL`
- [ ] 提供 `chat(messages) -> Completion`
- [ ] **非流式**（流式留到下一步，先让最简单的版本跑通）
- [ ] 错误分类：401 鉴权失败 / 429 限流 / 5xx 服务端 / 网络错误，各自可区分
- [ ] 函数内部**不打印任何东西**（渲染是 cli 的事）
- [ ] 无 API key 时用假响应验证逻辑，配好 key 后跑一次真实对话

预计 60-80 行。

---

## 6. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-17 | 初次建立项目骨架（types/events/llm/tools/loop + 58 个测试） |
| 2026-11-17 | **整体推倒重来**：一次性生成太多文件，无法逐个理解；改为"一次一个文件、每个文件现场验证" |
| 2026-11-17 | 目录结构三次调整，最终定为 `MyAgent/agentlab/` |
| 2026-11-17 | 重写 `types.py`，import 与对象构造验证通过 |

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
