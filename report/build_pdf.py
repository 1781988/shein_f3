"""方案报告 PDF 生成：fpdf2 + 中文字体，三级降级，状态如实写进 manifest。

字体优先用已实测可用的 ``C:\\Windows\\Fonts\\simhei.ttf``（纯 TTF，
不走 .ttc 集合解析）。正文单位一律写「立方米」或 m3，不带上标字符，
避免黑体缺字形告警。

三级链：
1. fpdf2 与中文字体都可用 → 写 results/方案报告.pdf，记录页数与前 2 页说明单；
2. 否则写 results/方案报告.html，自带打印分页 CSS，manifest 明确写「PDF 未自动生成」；
3. 连 HTML 都写不出 → 只保留 docs/10_方案报告.md 内容源，manifest 记录失败原因。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src import config as cfg
from report.report_layout import (
    COVER_PAGES,
    MAX_PAGES,
    build_chapters,
    html_chapters,
)

FONT_CANDIDATES: List[str] = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
]


def _safe_text(text: str) -> str:
    """把报告正文里可能导致黑体缺字形的字符替换掉，并压掉控制字符。

    实测 simhei.ttf 缺上标 ³、数学减号 U+2212、小于等于号 U+2264 等字形，
    fpdf2 会打印 missing glyphs 告警；这里统一换成黑体覆盖得到的 ASCII 写法。
    """
    replacements = {
        "m³": "m3", "³": "3",
        "\u2212": "-", "\u2264": "<=", "\u2265": ">=",
        "\u00d7": "x", "\u2192": "->", "\u2190": "<-",
        "\u2208": "in", "\u2209": "not in", "\u2211": "sum",
        "\u2229": "and", "\u222a": "or", "\u2200": "for all",
        "\u2203": "exists", "\u00ac": "not ", "\u221e": "inf",
        "\u2113": "l", "\u2206": "delta",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.replace("\t", " ").replace("\r", "")


def find_font() -> Optional[str]:
    """按候选顺序找一个存在的中文字体；找不到返回 None，不假装可用。"""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _render_pdf(
    chapters: List[Any], font_path: str, output: Path
) -> Dict[str, Any]:
    """用 fpdf2 逐章分页渲染，返回真实页数等信息。"""
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_font("CJK", "", font_path)
    for chapter in chapters:
        pdf.add_page()
        pdf.set_font("CJK", size=15)
        pdf.multi_cell(0, 9, _safe_text(chapter.title))
        pdf.ln(3)
        pdf.set_font("CJK", size=9.5)
        for paragraph in chapter.paragraphs:
            pdf.multi_cell(0, 5.6, _safe_text(paragraph))
            pdf.ln(1.2)
    pages = pdf.page_no()
    pdf.output(str(output))
    return {
        "status": "generated",
        "path": str(output),
        "pages": pages,
        "font": Path(font_path).name,
        "engine": "fpdf2",
        "front_pages_are_cover": pages >= COVER_PAGES,
        "within_page_limit": pages <= MAX_PAGES,
        "bytes": output.stat().st_size,
    }


def _render_html(
    chapters: List[Any], output: Path, reason: str
) -> Dict[str, Any]:
    """二级降级：写自带 @page / page-break-after 的可打印 HTML。"""
    body = html_chapters(chapters)
    html = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>方案报告</title><style>"
        "@page{size:A4;margin:16mm}"
        "body{font-family:'Microsoft YaHei','SimHei',sans-serif;font-size:10pt;line-height:1.6}"
        "h1{font-size:15pt;border-bottom:1px solid #ccc;padding-bottom:4px}"
        ".page{page-break-after:always}"
        "</style></head><body>"
        f"<p>提示：PDF 未自动生成（{reason}）。用浏览器打开本页并「打印为 PDF」即可。</p>"
        + body + "</body></html>"
    )
    output.write_text(html, encoding=cfg.CSV_ENCODING)
    return {
        "status": "generated",
        "path": str(output),
        "reason": reason,
        "note": "PDF 未自动生成；用浏览器打印为 PDF",
        "print_css": "@page + page-break-after",
    }


def build(
    cfg_obj: Any,
    report_data: Mapping[str, Any],
    docs_dir: Path,
    results_dir: Path,
) -> Dict[str, Any]:
    """三级降级生成报告，返回 manifest 的 report 段（如实记录每级状态）。"""
    results = Path(results_dir)
    results.mkdir(parents=True, exist_ok=True)
    chapters = build_chapters(Path(docs_dir), report_data)
    pdf_path = results / "方案报告.pdf"
    html_path = results / "方案报告.html"
    result: Dict[str, Any] = {
        "chapters": len(chapters),
        "cover_pages": COVER_PAGES,
        "max_pages": MAX_PAGES,
        "markdown_source": "docs/10_方案报告.md",
        "pdf": {"status": "not_attempted"},
        "html": {"status": "not_attempted"},
        "markdown": {"status": "available", "path": "docs/10_方案报告.md"},
    }

    font = find_font()
    pdf_reason = ""
    if font is None:
        pdf_reason = "系统中文字体未找到"
        result["pdf"] = {"status": "not_generated", "reason": pdf_reason}
    else:
        try:
            result["pdf"] = _render_pdf(chapters, font, pdf_path)
        except Exception as exc:  # noqa: BLE001 - PDF 生成期失败走降级
            pdf_reason = repr(exc)
            result["pdf"] = {"status": "not_generated", "reason": pdf_reason}
            if pdf_path.exists():
                try:
                    pdf_path.unlink()
                except OSError:
                    pass

    if result["pdf"]["status"] != "generated":
        try:
            result["html"] = _render_html(chapters, html_path, pdf_reason or "PDF 未生成")
        except Exception as exc:  # noqa: BLE001 - 三级降级
            result["html"] = {"status": "failed", "reason": repr(exc)}
            result["fallback"] = "仅保留 docs/10_方案报告.md 内容源"
    else:
        result["html"] = {
            "status": "not_needed",
            "note": "PDF 已生成，未走 HTML 降级；如需可打印版可另行生成",
        }

    with (results / "build_manifest.json").open("w", encoding=cfg.CSV_ENCODING) as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return result


__all__ = ["build", "find_font", "FONT_CANDIDATES"]
