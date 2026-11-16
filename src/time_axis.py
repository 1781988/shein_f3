"""480 小时轴、产出小时↔列名、可配时刻、可见窗口、当天界定、评估范围判定。

时间约定（对应假设 A2 / B3 / B10 / B12）：
- 小时 i 的左端 = start + i 小时，列名 ``YYYY-MM-DD HH:MM-HH:MM``；
- 该小时的货从左端 + 1 小时（即右端）起可配，``available_at(i)``；
- 决策步 t 释放第 t 小时的货，决策时刻 = available_at(t)；
- 可见窗口 = [当天 00:00:00, 当天+2 自然日 23:59:59]，按最早提货时间判断；
- 全 480 小时货源都参与配载、明细全量输出；货源方数类指标（敏感品完全匹配率、
  时效积压率、主注入点选择率、成本、未配提单货量）只统计产出小时落在
  10-03 00:00 ~ 10-17 23:00 的记录集合 A；配满率与虚仓提单数量是提单侧口径，
  按完整配载结果与全部 2652 张提单统计，不受 A 限制。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import config as cfg
from .precision import format_hour, hour_column_name, parse_hour_column_left


class TimeAxis:
    """固定 480 小时的本地时间轴，无时区换算。"""

    def __init__(
        self,
        start: datetime,
        hours: int,
        eval_start: datetime,
        eval_end_inclusive: datetime,
        window_days: int,
    ) -> None:
        self.start = start
        self.hours = hours
        self.eval_start = eval_start
        self.eval_end_inclusive = eval_end_inclusive
        self.window_days = window_days
        self.end = self.start + timedelta(hours=hours)
        self._labels: List[str] = [
            format_hour(self.left_at(i)) for i in range(hours)
        ]
        self._column_names: List[str] = [
            hour_column_name(self.left_at(i)) for i in range(hours)
        ]
        self._available: List[datetime] = [
            self.left_at(i) + timedelta(hours=1) for i in range(hours)
        ]
        self._label_to_index: Dict[str, int] = {
            label: i for i, label in enumerate(self._labels)
        }
        self._eval_flags: List[bool] = [
            self.eval_start <= self.left_at(i) <= self.eval_end_inclusive
            for i in range(hours)
        ]

    @classmethod
    def from_config(cls, cfg_obj: Any) -> "TimeAxis":
        """按 Config 构造时间轴。"""
        return cls(
            start=datetime.strptime(cfg.AXIS_START_STR, cfg.TIME_FORMAT),
            hours=cfg.HOURS,
            eval_start=cfg_obj.eval_range.start,
            eval_end_inclusive=cfg_obj.eval_range.end_inclusive,
            window_days=cfg_obj.window_days,
        )

    # -- 小时索引 ----------------------------------------------------------
    def left_at(self, i: int) -> datetime:
        """小时 i 的左端时刻。"""
        if i < 0 or i >= self.hours:
            raise ValueError(f"小时索引越界：{i}，合法范围 0..{self.hours - 1}")
        return self.start + timedelta(hours=i)

    def hour_label(self, i: int) -> str:
        """小时 i 的产出小时标签 ``YYYY-MM-DD HH:MM``（无秒）。"""
        if i < 0 or i >= self.hours:
            raise ValueError(f"小时索引越界：{i}，合法范围 0..{self.hours - 1}")
        return self._labels[i]

    def hour_column_name(self, i: int) -> str:
        """小时 i 对应的宽表小时列名。"""
        if i < 0 or i >= self.hours:
            raise ValueError(f"小时索引越界：{i}，合法范围 0..{self.hours - 1}")
        return self._column_names[i]

    def available_at(self, i: int) -> datetime:
        """小时 i 产出货的可配时刻 = 左端 + 1 小时。"""
        if i < 0 or i >= self.hours:
            raise ValueError(f"小时索引越界：{i}，合法范围 0..{self.hours - 1}")
        return self._available[i]

    def decision_time(self, hour_index: int) -> datetime:
        """决策步 t 的当前时刻，等于第 t 小时货的可配时刻。"""
        return self.available_at(hour_index)

    def hour_index_of_column(self, column_name: str) -> int:
        """由宽表小时列名反查小时索引。"""
        left = parse_hour_column_left(column_name)
        delta = left - self.start
        if delta.total_seconds() % 3600 != 0:
            raise ValueError(f"小时列名不是整点左端：{column_name!r}")
        index = int(delta.total_seconds() // 3600)
        if index < 0 or index >= self.hours:
            raise ValueError(
                f"小时列名落在时间轴之外：{column_name!r}，"
                f"时间轴 {self.start} ~ {self.end}"
            )
        return index

    def hour_index_of_label(self, label: str) -> int:
        """由 ``YYYY-MM-DD HH:MM`` 标签反查小时索引。"""
        text = str(label).strip()
        if text not in self._label_to_index:
            raise ValueError(f"产出小时标签不在时间轴内：{label!r}")
        return self._label_to_index[text]

    def all_column_names(self) -> List[str]:
        """返回 480 个宽表小时列名（按时间序）。"""
        return list(self._column_names)

    def all_labels(self) -> List[str]:
        """返回 480 个产出小时标签（按时间序）。"""
        return list(self._labels)

    def verify_columns(self, names: List[str]) -> List[Tuple[int, str]]:
        """核对 480 个小时列名：数量、左端可解析、连续 1 小时、与时间轴一致。

        返回 (索引, 错误描述) 列表，空列表表示全部通过。
        """
        problems: List[Tuple[int, str]] = []
        if len(names) != self.hours:
            problems.append(
                (-1, f"小时列数量 {len(names)}，期望 {self.hours}")
            )
        for i, name in enumerate(names[: self.hours]):
            try:
                left = parse_hour_column_left(name)
            except ValueError as exc:
                problems.append((i, f"列 {name!r} 左端无法解析：{exc}"))
                continue
            expected = self.left_at(i)
            if left != expected:
                problems.append(
                    (i, f"列 {name!r} 左端 {left} 与期望 {expected} 不一致")
                )
        return problems

    # -- 评估范围 ----------------------------------------------------------
    def is_in_eval_range(self, i: int) -> bool:
        """小时 i 的产出小时是否落在 10-03 00:00 ~ 10-17 23:00（含端点）。"""
        if i < 0 or i >= self.hours:
            return False
        return self._eval_flags[i]

    def eval_hour_indices(self) -> List[int]:
        """返回评估范围内的全部小时索引（应为 360 个）。"""
        return [i for i, flag in enumerate(self._eval_flags) if flag]

    # -- 可见窗口 ----------------------------------------------------------
    def day_of(self, dt: datetime) -> date:
        """决策时刻所在自然日。"""
        return dt.date()

    def day_start(self, day: date) -> datetime:
        """自然日 00:00:00。"""
        return datetime.combine(day, time(0, 0, 0))

    def visible_window(
        self, day: date, upper_bound: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """返回 [当天 00:00:00, 当天 + window_days 日 23:59:59]。

        ``upper_bound`` 只在时间轴末步使用：把上界延伸到全部提单的最早提货
        时间，避免时间轴外的提单被静默丢弃（对应第五节「不虚构截断」）。
        """
        start = self.day_start(day)
        end = datetime.combine(
            day + timedelta(days=self.window_days + 1), time(0, 0, 0)
        ) - timedelta(seconds=1)
        if upper_bound is not None and upper_bound > end:
            end = upper_bound
        return (start, end)

    def visible_window_for_step(
        self, hour_index: int, upper_bound: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """决策步 t 的可见窗口，以决策时刻所在自然日为准。"""
        now = self.decision_time(hour_index)
        return self.visible_window(self.day_of(now), upper_bound=upper_bound)

    def clamp_pickup_time(
        self, candidate: datetime, earliest: datetime, latest: datetime
    ) -> datetime:
        """按 B12 构造提货时刻：max(最早提货, 候选) 再夹到不晚于最晚提货。"""
        base = candidate if candidate > earliest else earliest
        return base if base <= latest else latest


__all__ = ["TimeAxis"]
