"""文件 6：内置文件工具。

五个工具，共用同一套规矩：

    read_file    按行分页读
    write_file   整文件写（新建/覆盖）
    edit_file    精确替换，且必须唯一命中
    list_files   看目录结构
    grep         按正则找

它们共享一个前提：**工作区**。任何路径 `resolve()` 之后必须仍在根目录内，
越界直接抛 `ToolError`。这条检查只写一处（`_Workspace.resolve`）——
每个工具各写一份，迟早会漏一个，而漏掉的那个就是 shell。

**这里没有权限判断，是刻意的。** 越界检查是功能约束（工具必须能回答"这个路径
是不是在工作区里"），不等于"该不该让模型读"。谁能读、谁能写，取决于部署形态：
CLI 里用户就在键盘前，`.env` 该不该读由他自己决定；服务端多会话才需要硬隔离。
上一版把黑名单折进 `_Workspace`，结果是"一个路径匹配函数被摆成了 agent 的策略中枢"——
现在撤掉，等 CLI 的审批回路（`await approve(...)`）落地后一起重新设计。

这个文件独有的两个义务（纯逻辑模块没有这两个约束）：

1. **阻塞 IO 丢进 `asyncio.to_thread`。** registry 会用 gather 并发调工具，
   直接在事件循环里 `read_bytes()` 会让并发退化成串行。
2. **结果自带分页。** read_file 有 offset/limit，grep 有 max_matches。
   目标是日常使用中碰不到 registry 的截断闸门——截断只是最后一道保险。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path

from agentlab.tools.base import Tool, ToolError, ToolResult

IGNORE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
    }
)
"""遍历时跳过的目录：依赖、缓存、版本库内部。

不跳过的话，grep 一次会扫进几万个 .venv 文件，既慢又把真正的命中淹没在噪音里。
"""

MAX_FILE_BYTES = 2_000_000
"""单个文本文件上限（约 2 MB）。超过就请模型换策略：先 grep，再 offset/limit 局部读。"""

MAX_READ_LINES = 2000
MAX_LIST_ENTRIES = 200
MAX_GREP_MATCHES = 50
MAX_SCAN_FILES = 2000
"""四个上限分别管：一次读的行数、列目录条目、grep 命中数、grep 扫描文件数。

有上限的结果必须告诉模型"被截断了、下一步怎么缩小"，否则它会把"只看到 50 条"
当成"只有 50 条"。
"""


class _Workspace:
    """一整套工具共用的根目录与路径检查。

    单独一个类而不是自由函数，是因为"根目录"要跟着工具实例走：
    同一个进程里可以同时开两个工作区（两个会话），互不影响。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"工作区根目录不存在或不是目录：{self.root}")

    def display(self, path: Path) -> str:
        """给模型看的路径：工作区内一律转成相对路径。

        绝对路径更长，也会暴露本机目录结构；模型只用相对路径就够了。
        工作区外的路径原样返回——正常流程到不了这里（resolve() 已拦越界），
        而越界报错里确实会出现工作区根，那是排查越界必需的信息。
        """
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:  # 理论上到不了：resolve() 已经拦过越界
            return str(path)

    def resolve(self, raw: str) -> Path:
        """相对路径按工作区解析；解析后必须仍在工作区内，否则 ToolError。

        `resolve()` 会跟穿符号链接，所以"根目录里放一个指向 /etc/passwd 的软链接"
        也会在这里被拦住——只比较字符串前缀拦不住这种。

        这是**功能边界**，不是权限边界：它只回答"这个路径指向的文件在不在工作区里"，
        不回答"该不该让模型碰"。后者见文件开头的说明。
        """
        if not raw.strip():
            raise ToolError('path 不能为空；指工作区根目录请用 "."')
        candidate = Path(raw).expanduser()
        full = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        try:
            full.relative_to(self.root)
        except ValueError:
            raise ToolError(
                f"路径越界：{raw!r} 解析后是 {full}，不在工作区 {self.root} 内。"
                "请改用相对工作区的路径，例如 agentlab/types.py。"
            ) from None
        return full

    def files(self, base: Path, pattern: str | None) -> list[Path]:
        """base 之下符合 pattern 的文件：已跳过忽略目录、已排序、有上限。

        排序是为了输出稳定：`rglob` 的顺序依赖文件系统，同样的搜索两次跑出不同顺序，
        模型会以为工作区变了。
        """
        if base.is_file():
            return [base]
        found = [
            path
            for path in base.rglob(pattern or "*")
            if path.is_file()
            and not any(part in IGNORE_DIRS for part in path.relative_to(self.root).parts)
        ]
        return sorted(found)[:MAX_SCAN_FILES]


def _get_str_arg(args: Mapping[str, object], key: str, default: str | None = None) -> str:
    """取字符串参数。

    参数进来前 registry 已按 JSON Schema 校验过，所以这里的分支正常永不触发；
    留着是因为 Tool.run 也可能被直接调用（测试），且显式收窄类型能省掉 cast。
    """
    value = args.get(key, default)
    if not isinstance(value, str):
        raise ToolError(f"参数 {key} 缺失或不是字符串")
    return value


def _get_int_arg(args: Mapping[str, object], key: str, default: int) -> int:
    value = args.get(key, default)
    # bool 是 int 的子类，True 会被当成 1；显式排掉，免得 offset=True 静默生效。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {key} 缺失或不是整数")
    return value


def _read_text(path: Path, workspace: _Workspace) -> str:
    """阻塞读：存在性 / 目录 / 大小 / 二进制 / 编码，五道检查后再解码。"""
    shown = workspace.display(path)
    if not path.exists():
        raise ToolError(f"文件不存在：{shown}")
    if path.is_dir():
        raise ToolError(f"{shown} 是目录；看目录内容请用 list_files")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(
            f"{shown} 太大（{len(data):,} 字节，上限 {MAX_FILE_BYTES:,}）。"
            "先用 grep 定位，再用 read_file 的 offset/limit 局部读。"
        )
    if b"\x00" in data[:4096]:
        raise ToolError(f"{shown} 看起来是二进制文件（前 4KB 含 NUL 字节）；read_file 只读文本")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            f"{shown} 不是 UTF-8 文本（第 {exc.start} 字节解码失败）。"
            "如果它是 GBK 之类的编码，需要先转换。"
        ) from None


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _WorkspaceTool(Tool):
    """持有工作区的工具基类：子类只管自己的参数和行为。"""

    def __init__(self, workspace: _Workspace) -> None:
        self._ws = workspace


class ReadFile(_WorkspaceTool):
    name = "read_file"
    description = (
        "读取工作区内的文本文件，按行分页。修改文件之前先用它看清原文："
        "结果头部会标出读到的行号范围与全文行数，超大文件用 offset/limit 分段读。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的路径，如 agentlab/types.py"},
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "从第几行开始读（1 表示文件开头）",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5000,
                "description": "最多读多少行，默认 2000",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        path = self._ws.resolve(_get_str_arg(args, "path"))
        offset = _get_int_arg(args, "offset", 1)
        limit = _get_int_arg(args, "limit", MAX_READ_LINES)

        text = await asyncio.to_thread(_read_text, path, self._ws)
        lines = text.splitlines()
        total = len(lines)
        shown = self._ws.display(path)

        if total == 0:
            return ToolResult.ok(f"{shown} 是空文件（0 字节）")
        if offset > total:
            raise ToolError(
                f"{shown} 只有 {total} 行，offset={offset} 超出末尾；"
                f"要看结尾请用 offset={max(1, total - limit + 1)}"
            )

        chunk = lines[offset - 1 : offset - 1 + limit]
        end = offset - 1 + len(chunk)

        # 头部是分页的路标：没有它，模型不知道自己在哪一段，也不知道还有没有下文。
        header = f"{shown}  第 {offset}–{end} 行 / 共 {total} 行"
        tips: list[str] = []
        if offset > 1:
            tips.append(f"前面还有 {offset - 1} 行")
        if end < total:
            tips.append(f"用 offset={end + 1} 继续")
        if tips:
            header += "（" + "；".join(tips) + "）"
        return ToolResult.ok(header + "\n" + "\n".join(chunk))


class WriteFile(_WorkspaceTool):
    name = "write_file"
    description = (
        "新建或整体覆盖一个文本文件，父目录不存在会自动创建。"
        "只改文件里的一小段请用 edit_file——整文件重写容易把没读到的内容冲掉。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的路径"},
            "content": {"type": "string", "description": "文件的完整新内容（不是追加）"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        path = self._ws.resolve(_get_str_arg(args, "path"))
        content = _get_str_arg(args, "content")
        shown = self._ws.display(path)

        if path.is_dir():
            raise ToolError(f"{shown} 是目录，不能当文件写")
        existed = path.exists()
        old_bytes = path.stat().st_size if existed else 0

        await asyncio.to_thread(_write_text, path, content)

        action = "已覆盖" if existed else "已新建"
        detail = f"，原来 {old_bytes:,} 字节" if existed else ""
        return ToolResult.ok(
            f"{action} {shown}（{len(content.splitlines())} 行，{len(content)} 字符{detail}）"
        )


class EditFile(_WorkspaceTool):
    name = "edit_file"
    description = (
        "把文件里的 old_text 精确替换成 new_text，old_text 必须在文件里唯一出现。"
        "必须与原文逐字符一致（含缩进与空行）；找不到或匹配多处都会失败并说明原因。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的路径"},
            "old_text": {"type": "string", "description": "要被替换的原文，必须唯一命中"},
            "new_text": {"type": "string", "description": "替换成什么；允许空字符串（即删除）"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        path = self._ws.resolve(_get_str_arg(args, "path"))
        old = _get_str_arg(args, "old_text")
        new = _get_str_arg(args, "new_text")
        shown = self._ws.display(path)

        if not old:
            raise ToolError("old_text 不能为空；要整体替换请用 write_file")
        if old == new:
            raise ToolError("old_text 与 new_text 完全一样，这次调用不会产生任何改动")

        text = await asyncio.to_thread(_read_text, path, self._ws)
        hits = text.count(old)
        if hits == 0:
            raise ToolError(
                f"在 {shown} 里没找到 old_text。请先用 read_file 确认原文："
                "缩进、空行、行尾空格、引号必须完全一致。"
            )
        if hits > 1:
            raise ToolError(
                f"old_text 在 {shown} 里出现了 {hits} 次，无法确定改哪一处。"
                "请把它扩大到能唯一定位，例如带上相邻的一两行。"
            )

        line_no = text[: text.index(old)].count("\n") + 1
        updated = text.replace(old, new, 1)
        await asyncio.to_thread(_write_text, path, updated)
        return ToolResult.ok(
            f"已修改 {shown}（从第 {line_no} 行起替换 1 处；"
            f"文件现在 {len(updated.splitlines())} 行）"
        )


class ListFiles(_WorkspaceTool):
    name = "list_files"
    description = (
        "列出工作区内某个目录的文件与子目录（跳过 .git/.venv 等），"
        "用来先看清项目结构再决定读哪个文件。目录名以 / 结尾。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的目录，默认 ."},
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": "展开几层，默认 2",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        base = self._ws.resolve(_get_str_arg(args, "path", "."))
        depth = _get_int_arg(args, "depth", 2)
        shown = self._ws.display(base)

        if base.is_file():
            raise ToolError(f"{shown} 是文件；看单个文件请用 read_file")
        if not base.exists():
            raise ToolError(f"目录不存在：{shown}")

        entries, capped = await asyncio.to_thread(self._walk, base, depth)
        if not entries:
            return ToolResult.ok(f"{shown} 是空目录（已跳过 .git/.venv 等）")

        summary = f"共 {len(entries)} 项"
        if capped:
            summary += f"（已到上限 {MAX_LIST_ENTRIES}，用更具体的 path 缩小）"
        return ToolResult.ok("\n".join(entries) + "\n" + summary)

    def _walk(self, base: Path, depth: int) -> tuple[list[str], bool]:
        """深度优先、最多 depth 层。返回 (条目, 是否被上限截断)。

        必须是深度优先：`src/` 的下一行就该是 `src/app.py`，
        而不是把所有浅层文件列完、再回头补子目录内容——那样的输出读起来像乱序。
        """
        entries: list[str] = []

        def collect(directory: Path, level: int) -> bool:
            """递归一层；返回 True 表示触到条目上限，需要一路往回收。"""
            # 目录在前、同类按名字排：输出稳定，模型才能对比两次列目录的差异。
            try:
                children = sorted(
                    directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
                )
            except PermissionError:
                # 别人的私有目录（/tmp/systemd-private-*、~/.cache 里的 0700）
                # 不该让整条 list_files 失败：跳过一个目录，比列不出其余 99 个有用。
                return False
            for child in children:
                rel = child.relative_to(self._ws.root).as_posix()
                if child.is_dir():
                    if child.name in IGNORE_DIRS:
                        continue
                    entries.append(rel + "/")
                    if level + 1 < depth and collect(child, level + 1):
                        return True
                else:
                    entries.append(rel)
                if len(entries) >= MAX_LIST_ENTRIES:
                    return True
            return False

        try:
            truncated = collect(base, 0)
        except OSError as exc:
            raise ToolError(
                f"读目录失败：{self._ws.display(base)}（{exc.strerror}）"
            ) from None
        return entries, truncated


class Grep(_WorkspaceTool):
    name = "grep"
    description = (
        "在工作区内按正则搜索文本，返回 文件:行号: 内容。比逐个 read_file 快得多。"
        "没有命中时不要立刻断言不存在：先确认 pattern 的转义和 glob 是否正确。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则，例如 def\\s+\\w+"},
            "path": {"type": "string", "description": "搜索起点（目录或文件），默认 ."},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "最多返回多少条命中，默认 50",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        pattern = _get_str_arg(args, "pattern")
        base = self._ws.resolve(_get_str_arg(args, "path", "."))
        glob = _get_str_arg(args, "glob", "") or None
        max_matches = _get_int_arg(args, "max_matches", MAX_GREP_MATCHES)

        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(
                f"正则不合法：{exc}。要搜字面量里的特殊字符，请先转义（\\. \\[ \\]）。"
            ) from None
        if not base.exists():
            raise ToolError(f"路径不存在：{self._ws.display(base)}")

        hits, scanned, capped = await asyncio.to_thread(
            self._search, base, regex, glob, max_matches
        )
        if not hits:
            scope = f"glob={glob!r}" if glob else "全部文本文件"
            return ToolResult.ok(
                f"没有匹配：pattern={pattern!r}（{scope}，扫描 {scanned} 个文件）"
            )

        body = "\n".join(hits)
        tail = ""
        if capped:
            tail = f"\n（已到上限 {max_matches} 条，可能还有更多：加上 glob 或收紧 pattern）"
        return ToolResult.ok(f"匹配 {len(hits)} 处：\n{body}{tail}")

    def _search(
        self,
        base: Path,
        regex: re.Pattern[str],
        glob: str | None,
        max_matches: int,
    ) -> tuple[list[str], int, bool]:
        """返回 (命中行, 扫描文件数, 是否触顶)。二进制与超大文件直接跳过。"""
        hits: list[str] = []
        files = self._ws.files(base, glob)
        for path in files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > MAX_FILE_BYTES or b"\x00" in data[:4096]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    snippet = line.strip()[:200]  # 压缩过的单行文件可能长达几万字符
                    hits.append(f"{self._ws.display(path)}:{lineno}: {snippet}")
                    if len(hits) >= max_matches:
                        return hits, len(files), True
        return hits, len(files), False


def make_file_tools(root: str | Path = ".") -> list[Tool]:
    """建一整套文件工具，共用同一个工作区根。

    返回 list[Tool] 而不是五个独立变量：调用方拿到就统一往 registry 里注册，
    不用记"这批工具有哪几个"。
    """
    workspace = _Workspace(root)
    return [
        ReadFile(workspace),
        WriteFile(workspace),
        EditFile(workspace),
        ListFiles(workspace),
        Grep(workspace),
    ]


__all__ = [
    "IGNORE_DIRS",
    "EditFile",
    "Grep",
    "ListFiles",
    "ReadFile",
    "WriteFile",
    "make_file_tools",
]
