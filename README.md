# MyAgent

> 不用任何 agent 框架，从空目录开始、逐文件手写的生产级 agent。

**状态：早期开发中（M3 已收尾）。现在可以起一个 HTTP 服务，`/chat` 是一次真正的 agent 任务：模型要工具就执行、结果回填、再问一轮，直到它给出最终回答。还没有跨请求的会话。**

---

## 这是什么

目标是做一个能真正干活的命令行 agent：给它一句自然语言，它自己读文件、改代码、跑命令、看报错、再改，直到任务完成。

```
$ agentlab "把 agentlab/ 里所有 print 换成 logging，然后跑测试"
  ↳ read_file   agentlab/client.py
  ↳ edit_file   agentlab/client.py
  ↳ bash        pytest -q
  ↳ 读失败输出 → 再改 → 通过
```

## 为什么再造一个

现成的 agent 框架很多。这个项目的重点不是功能更强，而是**每一行都看得懂**：

- **不依赖任何 agent 框架**（LangChain / LlamaIndex / LangGraph / AutoGen 一律不用）
- **没有黑盒**：prompt 拼装、工具调用解析、上下文压缩、权限校验、失败重试，全部自己写
- **每个文件都能单独跑通、单独验证**，不是一次性生成一大堆然后祈祷它能跑

## 架构

依赖方向严格向下，下层不知道上层存在。

```
┌──────────────────────────────────────────────┐
│ cli.py       REPL、流式渲染、审批交互          │
├──────────────────────────────────────────────┤
│ loop.py      调模型 → 执行工具 → 回填 → 再循环 │
├──────────────┬──────────────┬────────────────┤
│ context.py   │ policy.py    │ session.py     │
│ 历史与压缩    │ 权限与路径守卫 │ 持久化与续跑   │
├──────────────┴──────┬───────┴────────────────┤
│ llm/  模型适配器     │ tools/  工具注册表与工具 │
├─────────────────────┴────────────────────────┤
│ types.py + events.py   数据契约 + 事件流      │
└──────────────────────────────────────────────┘
```

## 进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 数据契约、模型客户端 | ✅ 真实请求验证通过 |
| M2 | 工具系统 + 文件工具 | ✅ 5 个文件工具，注册表已接进服务 |
| M3 | 主循环 | ✅ `loop.py`，`/chat` 已闭环（多步工具调用） |
| M4 | 上下文压缩、权限、持久化、CLI | ⬜ 下一步 |
| M5 | 测试、评测、可观测性 | ⬜ |

已完成：`types.py`（数据契约，OpenAI 格式）、`llm/`（错误分类 + OpenAI 兼容适配器）、
`tools/`（工具协议、注册表、5 个文件工具）、`loop.py`（agent 主循环）、`main.py`（HTTP 服务入口）。
详见 [docs/progress.md](docs/progress.md)。

## 快速开始

```bash
git clone <repo-url> MyAgent
cd MyAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # 填入 AGENTLAB_API_KEY

python main.py            # 起服务，默认 127.0.0.1:8000
```

然后浏览器打开 <http://127.0.0.1:8000/> —— 网页版对话测试台，发消息即可看到回复、
token 用量、延迟与错误码；<http://127.0.0.1:8000/docs> 是自动生成的接口文档。

命令行验证也行：

```bash
curl -s 127.0.0.1:8000/chat -H 'content-type: application/json' \
     -d '{"text":"用一句话说明你是谁"}'

# 让它用工具：它会自己读文件，再把内容总结给你
curl -s 127.0.0.1:8000/chat -H 'content-type: application/json' \
     -d '{"text":"读一下 README.md 的前 5 行，用一句话概括"}'
```

## 文档

- [目标与实现方式](docs/goal-and-design.md) —— 定位、架构、10 条设计决策、里程碑
- [当前进展](docs/progress.md) —— 到哪一步了、下一步做什么、变更记录
- [协作约定](docs/working-agreement.md) —— 怎么讲新生成的文件、怎么控复杂度、提交规范

## 技术栈

Python 3.11+ · httpx · jsonschema · rich · FastAPI · uvicorn · uv

## License

待定。
