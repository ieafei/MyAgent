# MyAgent

> 不用任何 agent 框架，从空目录开始、逐文件手写的生产级 agent。

**状态：早期开发中（M4 进行中）。`agentlab` 命令已经能用：进 REPL 或带一句话跑一次，模型要工具就并行执行、结果回填、再问一轮直到给出最终回答，下一句话还接得上上一句。执行期间边跑边说：第几步、要调什么工具、成了没有（`on_event`）。`shell` 与审批在 M5。**

---

## 这是什么

目标是做一个能在**本机真实干活**的命令行 agent：给它一句自然语言，它自己看文件、跑命令、查资料、改东西、验证结果。**编程只是其中一类任务。**

```
$ agentlab "把 agentlab/ 里所有 print 换成 logging，然后跑测试"
  ↳ read_file   agentlab/client.py
  ↳ edit_file   agentlab/client.py
  ↳ shell       pytest -q
  ↳ 读失败输出 → 再改 → 通过

$ agentlab "把 ~/Downloads 里这个月的发票合并成一个 PDF，金额汇总进 expenses.csv"
  ↳ shell       pdftotext 发票_001.pdf -
  ↳ shell       qpdf --empty --pages *.pdf -- 发票_2026-11.pdf
  ↳ write_file  expenses.csv
  ↳ 3 张金额对不上 → 报告给你，不猜
```

它是**本地单用户**的工具，不是服务：一个进程一个 REPL，会话与 agent 分层（会话跨 run 累积，agent 每次任务重建），危险动作当场问你。这个前提决定了安全模型（能审的命令就不用写死的黑名单），所以形态不是口味问题。

## 为什么再造一个

现成的 agent 框架很多。这个项目的重点不是功能更强，而是**每一行都看得懂**：

- **不依赖任何 agent 框架**（LangChain / LlamaIndex / LangGraph / AutoGen 一律不用）
- **没有黑盒**：prompt 拼装、工具调用解析、上下文压缩、权限校验、失败重试，全部自己写
- **每个文件都能单独跑通、单独验证**，不是一次性生成一大堆然后祈祷它能跑

## 架构

依赖方向严格向下，下层不知道上层存在。

```
┌────────────────────────────────────────────────┐
│cli.py   REPL · 流式渲染 · 审批交互             │
├────────────────────────────────────────────────┤
│loop.py  调模型 → 执行工具 → 回填 → 再循环      │
├────────────────────────────────────────────────┤
│context.py  历史与压缩  session.py  会话        │
├────────────────────────────────────────────────┤
│llm/     模型适配器     tools/  注册表与工具    │
├────────────────────────────────────────────────┤
│types.py  数据契约      events.py  事件流       │
└────────────────────────────────────────────────┘
```

## 进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 数据契约、模型客户端 | ✅ 真实请求验证通过 |
| M2 | 工具系统 + 文件工具 | ✅ 5 个文件工具 + 注册表（六道守门） |
| M3 | 主循环 | ✅ `loop.py`，多步工具调用已闭环 |
| M4 | 会话、上下文压缩、CLI、事件流 | ✅ `session.py`、`context.py`、`cli.py`、`events.py`（边跑边说）；权限层做完又撤了，见 [3.14](docs/progress.md) |
| M5 | 泛化：`shell` + 多根授权工作区 + `http` | ⬜ 不只写代码，见 [1.1](docs/goal-and-design.md) |
| M6 | 测试、评测、可观测性 | ⬜ |

已完成：`types.py`（数据契约，OpenAI 格式）、`llm/`（错误分类 + OpenAI 兼容适配器）、
`tools/`（工具协议、注册表、5 个文件工具）、`loop.py`（agent 主循环）、
`session.py`（会话存储）、`context.py`（上下文预算）、`cli.py`（终端客户端）。
详见 [docs/progress.md](docs/progress.md)。

## 快速开始

```bash
git clone <repo-url> MyAgent
cd MyAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # 填入 AGENTLAB_API_KEY
```

装好后 `agentlab` 就在 PATH 里：

```bash
agentlab                                    # 交互模式：一个进程一个会话，历史在内存
agentlab "把 README 前 3 行概括一下"          # 一次性模式：跑完退出
agentlab -C ~/Downloads "整理这个月的发票"     # 换工作区根（不改你的 cwd）
agentlab --max-steps 20 -v "重构 fs.py"       # 放宽步数 + 打开 [loop]/[think] 日志
```

实际跑起来是这样（`agentlab "读一下 README.md 的前 3 行，用一句话概括"`）：

```
README.md 前 3 行是：# MyAgent、空行，以及一句引用语……

  ↳ read_file {"path": "README.md", "limit": 3} → 成功 · 94 字符

2 步 · in 2450 / out 119 · 缓存 2048 · 1434ms · deepseek-flash
```

交互里 `:help` 看命令、`:reset` 清历史、`:q` 退出、Ctrl-C 只中断当前任务。
stdout 只有回答、轨迹、一行汇总；日志（`[loop]` / `[think]`）走 stderr。

## 文档

- [目标与实现方式](docs/goal-and-design.md) —— 定位、架构、10 条设计决策、里程碑
- [当前进展](docs/progress.md) —— 到哪一步了、下一步做什么、变更记录
- [协作约定](docs/working-agreement.md) —— 怎么讲新生成的文件、怎么控复杂度、提交规范

## 技术栈

Python 3.11+ · httpx · jsonschema · rich · uv

## License

待定。
