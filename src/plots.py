"""规模曲线：matplotlib 画 PNG；缺失或被占用时降级成静态 SVG 或纯数据表。

降级状态如实记录到 figures/curve_status.json，主流程不因此失败。
坐标轴与标题只用「立方米」或 m3，不用黑体缺字形的上标字符。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from . import config as cfg
from .config import Config
from .domain import ScaleRow

CURVE_KEYS: Dict[str, str] = {
    "fill_rate": "配满率",
    "backlog_rate": "时效积压率",
    "cost": "成本（元）",
    "elapsed_seconds": "耗时（秒）",
}


def _series(rows: Sequence[ScaleRow], method: str) -> List[ScaleRow]:
    """取某方法、按规模升序的序列（列表本身已按规模顺序生成，这里显式排序）。"""
    return sorted(
        [row for row in rows if row.method == method],
        key=lambda row: row.scale,
    )


def _svg_line_chart(
    rows: Sequence[ScaleRow], attribute: str, title: str, width: int = 640,
    height: int = 360,
) -> str:
    """纯字符串生成静态 SVG 折线图，不依赖任何第三方包。"""
    main = _series(rows, "主方案")
    base = _series(rows, "贪心基准")
    all_values = [getattr(row, attribute) for row in list(main) + list(base)]
    low = min(all_values) if all_values else 0.0
    high = max(all_values) if all_values else 1.0
    span = high - low if high != low else 1.0
    pad_left, pad_right, pad_top, pad_bottom = 70, 24, 44, 46
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    def xy(index: int, count: int, value: float) -> tuple:
        x = pad_left + (plot_w * index / max(1, count - 1))
        y = pad_top + plot_h * (1.0 - (value - low) / span)
        return (x, y)

    def polyline(points: Sequence[ScaleRow], color: str) -> str:
        coords = " ".join(
            f"{xy(i, len(points), getattr(row, attribute))[0]:.1f},"
            f"{xy(i, len(points), getattr(row, attribute))[1]:.1f}"
            for i, row in enumerate(points)
        )
        dots = "".join(
            f'<circle cx="{xy(i, len(points), getattr(row, attribute))[0]:.1f}" '
            f'cy="{xy(i, len(points), getattr(row, attribute))[1]:.1f}" r="3" '
            f'fill="{color}"/>'
            for i, row in enumerate(points)
        )
        return f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>{dots}'

    scales = [row.scale for row in main] or [row.scale for row in base]
    x_labels = "".join(
        f'<text x="{xy(i, len(scales), low)[0]:.1f}" y="{height - 12}" '
        f'font-size="11" text-anchor="middle">{scale:.2f}</text>'
        for i, scale in enumerate(scales)
    )
    grid = "".join(
        f'<line x1="{pad_left}" y1="{pad_top + plot_h * k / 4:.1f}" '
        f'x2="{width - pad_right}" y2="{pad_top + plot_h * k / 4:.1f}" '
        f'stroke="#e5e7eb" stroke-width="1"/>'
        f'<text x="{pad_left - 8}" y="{pad_top + plot_h * k / 4 + 4:.1f}" '
        f'font-size="10" text-anchor="end">{low + span * (1 - k / 4):.4f}</text>'
        for k in range(5)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<text x="{width // 2}" y="24" font-size="14" text-anchor="middle">{title}</text>'
        f'{grid}{x_labels}'
        f'{polyline(main, "#2563eb")}{polyline(base, "#dc2626")}'
        f'<rect x="{pad_left}" y="{height - 34}" width="12" height="4" fill="#2563eb"/>'
        f'<text x="{pad_left + 18}" y="{height - 30}" font-size="11">主方案</text>'
        f'<rect x="{pad_left + 90}" y="{height - 34}" width="12" height="4" fill="#dc2626"/>'
        f'<text x="{pad_left + 108}" y="{height - 30}" font-size="11">贪心基准</text>'
        f'<text x="{pad_left}" y="{height - 2}" font-size="10">实例规模</text>'
        f'</svg>'
    )


def plot_scale_curves(
    rows: Sequence[ScaleRow], outdir: Path, cfg_obj: Config = None
) -> List[str]:
    """画四条规模曲线，返回生成的文件路径列表；状态写 curve_status.json。

    matplotlib 可用时生成 PNG；否则每张图降级成静态 SVG；
    连 SVG 都写不出时只保留数据表（由调用方写 scale_experiments.csv）。
    """
    target = Path(outdir)
    target.mkdir(parents=True, exist_ok=True)
    produced: List[str] = []
    status: Dict[str, Any] = {
        "backend": "none",
        "generated": [],
        "reason": "",
        "fallback_chain": ["matplotlib PNG", "静态 SVG", "纯数据表"],
    }

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        matplotlib.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        backend = "matplotlib"
    except Exception as exc:  # noqa: BLE001 - 可选依赖，如实降级
        plt = None
        backend = "svg"
        status["reason"] = f"matplotlib 不可用：{exc!r}"
    status["backend"] = backend

    for attribute, title in CURVE_KEYS.items():
        stem = f"scale_{attribute}"
        if plt is not None:
            try:
                fig, ax = plt.subplots(figsize=(6.4, 3.6))
                for method, color in (("主方案", "#2563eb"), ("贪心基准", "#dc2626")):
                    series = _series(rows, method)
                    ax.plot(
                        [row.scale for row in series],
                        [getattr(row, attribute) for row in series],
                        marker="o", color=color, label=method,
                    )
                ax.set_title(title)
                ax.set_xlabel("实例规模")
                ax.set_ylabel(title)
                ax.grid(True, alpha=0.3)
                ax.legend()
                png = target / f"{stem}.png"
                fig.tight_layout()
                fig.savefig(png, dpi=120)
                plt.close(fig)
                produced.append(str(png))
                continue
            except Exception as exc:  # noqa: BLE001 - 单图失败继续走 SVG
                status["reason"] = f"PNG 生成失败改用 SVG：{exc!r}"
                status["backend"] = "svg"
        # SVG 降级
        try:
            svg = target / f"{stem}.svg"
            svg.write_text(
                _svg_line_chart(rows, attribute, title), encoding=cfg.CSV_ENCODING
            )
            produced.append(str(svg))
        except OSError as exc:
            status["reason"] = f"SVG 也写不出，只剩数据表：{exc!r}"
            status["backend"] = "table_only"

    status["generated"] = [Path(p).name for p in produced]
    status["generated_count"] = len(produced)
    status_file = target / "curve_status.json"
    with status_file.open("w", encoding=cfg.CSV_ENCODING) as handle:
        json.dump(status, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    produced.append(str(status_file))
    return produced


__all__ = ["plot_scale_curves", "CURVE_KEYS"]
