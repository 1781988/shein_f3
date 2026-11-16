"""整数化、四位小数、0.0005 立方米容差、时间串解析的单测。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.precision import (  # noqa: E402
    format_cost,
    format_hour,
    format_time,
    hour_column_name,
    int_to_vol_str,
    int_to_vol_float,
    is_bad_volume,
    parse_hour_column_left,
    parse_time,
    vol_to_int,
    within_volume_tolerance,
)


class TestVolumePrecision(unittest.TestCase):
    """体积整数化与格式化。"""

    def test_vol_to_int_exact(self) -> None:
        self.assertEqual(vol_to_int(8.6993), 86993)
        self.assertEqual(vol_to_int("0.0136"), 136)
        self.assertEqual(vol_to_int("26.8"), 268000)
        self.assertEqual(vol_to_int(0), 0)
        self.assertEqual(vol_to_int(None), 0)
        self.assertEqual(vol_to_int(""), 0)
        self.assertEqual(vol_to_int(" 1.2345 "), 12345)

    def test_int_to_vol_str(self) -> None:
        self.assertEqual(int_to_vol_str(86993), "8.6993")
        self.assertEqual(int_to_vol_str(0), "0.0000")
        self.assertEqual(int_to_vol_str(5), "0.0005")
        self.assertEqual(int_to_vol_str(268000), "26.8000")
        self.assertAlmostEqual(int_to_vol_float(86993), 8.6993, places=8)

    def test_roundtrip(self) -> None:
        for raw in ("0.0001", "1.5215", "8.6993", "13.0000", "0.0050"):
            self.assertEqual(int_to_vol_str(vol_to_int(raw)), raw)

    def test_tolerance(self) -> None:
        # 0.0005 立方米 = 5 个整数单位
        self.assertEqual(cfg.VOLUME_TOLERANCE_INT, 5)
        self.assertTrue(within_volume_tolerance(100000, 100005))
        self.assertFalse(within_volume_tolerance(100000, 100006))
        self.assertTrue(within_volume_tolerance(100000, 99995))

    def test_bad_volumes(self) -> None:
        self.assertIsNotNone(is_bad_volume(-1))
        self.assertIsNotNone(is_bad_volume(float("nan")))
        self.assertIsNotNone(is_bad_volume(float("inf")))
        self.assertIsNotNone(is_bad_volume("abc"))
        self.assertIsNotNone(is_bad_volume("-0.1"))
        self.assertIsNone(is_bad_volume(None))
        self.assertIsNone(is_bad_volume(0))
        self.assertIsNone(is_bad_volume("1.2345"))


class TestTimePrecision(unittest.TestCase):
    """时间串严格解析与产出小时列名。"""

    def test_parse_time_strict(self) -> None:
        dt = parse_time("2026-10-05 10:06:00")
        self.assertEqual(dt, datetime(2026, 10, 5, 10, 6, 0))
        self.assertEqual(format_time(dt), "2026-10-05 10:06:00")
        with self.assertRaises(ValueError):
            parse_time("2026/10/05 10:06:00")
        with self.assertRaises(ValueError):
            parse_time("2026-10-05")
        with self.assertRaises(ValueError):
            parse_time(None)

    def test_hour_column_left(self) -> None:
        left = parse_hour_column_left("2026-10-05 08:00-09:00")
        self.assertEqual(format_hour(left), "2026-10-05 08:00")
        self.assertEqual(hour_column_name(left), "2026-10-05 08:00-09:00")
        # 23:00 的右端是 24:00，不是 00:00
        last = parse_hour_column_left("2026-10-20 23:00-24:00")
        self.assertEqual(hour_column_name(last), "2026-10-20 23:00-24:00")

    def test_cost_format(self) -> None:
        self.assertEqual(format_cost(12.345), "12.35")
        self.assertEqual(format_cost(0), "0.00")


if __name__ == "__main__":
    unittest.main()
