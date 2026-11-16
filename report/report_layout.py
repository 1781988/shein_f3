"""报告章节与分页控制：前 2 页固定为《作品说明单》，一般不超过 10 页。

``build_chapters`` 把 docs/ 里的正文拆成章节，并注入 report_data.json 的
真实数值；``MAX_PAGES`` 与 ``COVER_PAGES`` 供 build_pdf 校验页数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

MAX_PAGES: int = 10
COVER_PAGES: int = 2


@dataclass
class Chapter:
    """一页报告：标题 + 若干段落（段落内换行由 PDF 渲染器处理）。"""

    title: str = ""
    paragraphs: List[str] = field(default_factory=list)


def _flatten(text: str) -> List[str]:
    """把 Markdown 正文转成可排版的纯文本段落（去掉标记符号，保留内容）。"""
    paragraphs: List[str] = []
    buffer: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        if line.startswith("```"):
            continue
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            line = line.lstrip("> ").strip()
        # 表格行转成短句
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            line = "；".join(c for c in cells if c)
        line = line.replace("**", "").replace("`", "").replace("- ", "", 1)
        buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer))
    return [p for p in paragraphs if p]


def _split_document(path: Path, chapter_titles: List[str]) -> List[Chapter]:
    """按二级标题把一份 Markdown 拆成章节；标题数量不足时补齐空章节。"""
    if not path.is_file():
        return [Chapter(title=title, paragraphs=["（内容源缺失）"]) for title in chapter_titles]
    text = path.read_text(encoding="utf-8")
    parts: Dict[str, List[str]] = {title: [] for title in chapter_titles}
    current = chapter_titles[0] if chapter_titles else ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            heading = line[3:].strip()
            # 用章节标题前缀匹配，匹配不上就归到当前章节
            for title in chapter_titles:
                if title.split(" ")[0] in heading or heading.startswith(title.split(" ")[0]):
                    current = title
                    break
            continue
        parts.setdefault(current, []).append(raw)
    return [
        Chapter(title=title, paragraphs=_flatten("\n".join(parts.get(title, []))))
        for title in chapter_titles
    ]


def _chapter(title: str, lines: List[str]) -> Chapter:
    """构造一个章节，段落已是可直接排版的字符串。"""
    return Chapter(title=title, paragraphs=list(lines))


def build_chapters(
    docs_dir: Path, report_data: Mapping[str, Any]
) -> List[Chapter]:
    """按「说明单 1、说明单 2、问题与建模、口径与假设、算法、审计、实验、
    复现、局限」组织 9 个章节（渲染后一般 9 页，不超过 10 页）。"""
    cover = _flatten(
        (docs_dir / "11_作品说明单.md").read_text(encoding="utf-8")
        if (docs_dir / "11_作品说明单.md").is_file() else "《作品说明单》缺失"
    )
    half = max(1, len(cover) // 2)
    cover_1 = _chapter("作品说明单（一）", cover[:half])
    cover_2 = _chapter("作品说明单（二）", cover[half:])

    main_titles = [
        "一 问题与建模",
        "二 口径与假设",
        "三 算法与实现",
        "四 约束逐条审计",
        "五 实验结果与规模曲线",
        "六 如何复现",
        "七 局限与风险",
    ]
    body = _split_document(docs_dir / "10_方案报告.md", main_titles)

    # 注入真实数值，避免正文与结果文件口径漂移
    metrics = report_data.get("七项指标", {})
    if metrics:
        lines = ["七项指标实测值（详见 results/indicators.csv）："]
        for name, item in metrics.items():
            lines.append(f"{name}：{item.get('数值', '')}；{item.get('口径说明', '')}")
        body[3] = _chapter("四 约束逐条审计", body[3].paragraphs + lines)
    audit = report_data.get("约束审计", {})
    if audit:
        lines = [f"独立审计全部通过={audit.get('全部通过')}，冲突总数={audit.get('冲突总数')}。"]
        for finding in audit.get("逐条", []):
            lines.append(
                f"{finding.get('约束')}：检查 {finding.get('检查数')} 行，"
                f"冲突 {finding.get('冲突数')} 条。"
            )
        body[3] = _chapter("四 约束逐条审计", body[3].paragraphs + lines)
    scenarios = report_data.get("场景对比", [])
    if scenarios:
        lines = ["三组场景七项指标对比："]
        for row in scenarios:
            lines.append(
                f"{row.get('场景')}：配满率 {row.get('提单配满率')}，"
                f"时效积压率 {row.get('时效积压率')}，成本 {row.get('成本')} 元。"
            )
        body[4] = _chapter("五 实验结果与规模曲线", body[4].paragraphs + lines)
    pareto = report_data.get("近似Pareto", {})
    if pareto:
        body[4].paragraphs.append(
            f"非支配采样点：{'、'.join(pareto.get('non_dominated', []))}。"
            f"{pareto.get('note', '')}"
        )

    chapters = [cover_1, cover_2] + body
    # 页数上限：章节本身不超过 MAX_PAGES，每章渲染时独占一页
    if len(chapters) > MAX_PAGES:
        chapters = chapters[:MAX_PAGES]
    return chapters


def html_chapters(chapters: List[Chapter]) -> str:
    """把章节转成自带 @page / page-break-after 的可打印 HTML 片段。"""
    parts: List[str] = []
    for chapter in chapters:
        parts.append('<section class="page">')
        parts.append(f"<h1>{chapter.title}</h1>")
        for paragraph in chapter.paragraphs:
            parts.append(f"<p>{paragraph}</p>")
        parts.append("</section>")
    return "\n".join(parts)


__all__ = ["Chapter", "MAX_PAGES", "COVER_PAGES", "build_chapters", "html_chapters"]
