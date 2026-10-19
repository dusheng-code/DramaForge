"""职责：Markdown 组装与渲染工具（剧本、报告、交付包），不判定内容对错。
输入：标题、段落、表格行、任意 markdown 片段。
输出：可直接落盘的 markdown 字符串；可选 html（mistune）。

副作用：无（纯字符串处理）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def h(level: int, text: str) -> str:
    """生成标题行。无副作用。"""
    level = max(1, min(6, level))
    return f"{'#' * level} {text}"


def table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """生成 markdown 表格；空行输出占位 `—`。无副作用。"""
    cols = len(header)
    lines = ["| " + " | ".join(str(c) for c in header) + " |"]
    lines.append("| " + " | ".join(":--" for _ in range(cols)) + " |")
    for row in rows:
        cells = ["" if v is None else str(v) for v in list(row)[:cols]]
        cells += [""] * (cols - len(cells))
        lines.append("| " + " | ".join(c.replace("|", r"\|") for c in cells) + " |")
    return "\n".join(lines)


def bullets(items: Iterable[str], marker: str = "- ") -> str:
    """生成列表。无副作用。"""
    return "\n".join(f"{marker}{i}" for i in items)


def section(title: str, body: str, level: int = 2) -> str:
    """标题 + 正文，末尾保证单空行。无副作用。"""
    return f"{h(level, title)}\n\n{body.strip()}\n"


def join(parts: Iterable[str], sep: str = "\n") -> str:
    """过滤空片段后拼接。无副作用。"""
    return sep.join(p for p in (x.strip("\n") for x in parts) if p)


def code_block(text: str, lang: str = "") -> str:
    """代码块围栏。无副作用。"""
    return f"```{lang}\n{text.strip()}\n```"


def render_html(markdown_text: str) -> str:
    """用 mistune 渲染 html（交付包预览用）。无副作用。"""
    import mistune

    md = mistune.create_markdown(escape=False)
    return str(md(markdown_text))
