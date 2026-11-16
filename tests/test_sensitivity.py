"""等级映射、多层级取最高、按行取值、兼容与完全匹配的单测。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.sensitivity import (  # noqa: E402
    SensitivityLevel,
    build_row_level_index,
    is_compatible,
    is_exact_match,
    level_name,
    parse_level,
    split_tokens,
    unknown_tokens,
)


class TestLevelMapping(unittest.TestCase):
    """映射表：提单写法 / 货源写法 / 备注写法。"""

    def test_puhuo_is_sensitive_general(self) -> None:
        """备注里的「普货」＝等级「敏感普货」，不是「无（普货）」。"""
        self.assertEqual(parse_level("普货"), SensitivityLevel.SENSITIVE_GENERAL)
        self.assertEqual(int(parse_level("普货")), cfg.LEVEL_SENSITIVE_GENERAL)
        self.assertNotEqual(parse_level("普货"), parse_level("无（普货）"))

    def test_all_writings(self) -> None:
        self.assertEqual(parse_level("锂电产品"), SensitivityLevel.LITHIUM)
        self.assertEqual(parse_level("锂电"), SensitivityLevel.LITHIUM)
        self.assertEqual(parse_level("干电产品"), SensitivityLevel.DRY)
        self.assertEqual(parse_level("干电"), SensitivityLevel.DRY)
        self.assertEqual(parse_level("敏感普货"), SensitivityLevel.SENSITIVE_GENERAL)
        self.assertEqual(parse_level("无"), SensitivityLevel.NONE)
        self.assertEqual(parse_level("无（普货）"), SensitivityLevel.NONE)
        self.assertEqual(parse_level("无(普货)"), SensitivityLevel.NONE)
        self.assertEqual(parse_level(""), SensitivityLevel.NONE)
        self.assertEqual(parse_level(None), SensitivityLevel.NONE)

    def test_multi_level_takes_highest(self) -> None:
        raw = "无（普货）,敏感普货,干电产品,锂电产品"
        self.assertEqual(parse_level(raw), SensitivityLevel.LITHIUM)
        self.assertEqual(split_tokens("敏感普货，干电产品"), ["敏感普货", "干电产品"])
        self.assertEqual(parse_level("敏感普货,锂电产品"), SensitivityLevel.LITHIUM)
        self.assertEqual(parse_level("无（普货）,敏感普货"), SensitivityLevel.SENSITIVE_GENERAL)

    def test_unknown_token(self) -> None:
        self.assertEqual(unknown_tokens("敏感普货,未知等级"), ["未知等级"])
        self.assertEqual(parse_level("未知等级"), SensitivityLevel.NONE)
        self.assertEqual(level_name(SensitivityLevel.DRY), "干电")


class TestRowLevel(unittest.TestCase):
    """敏感品按「渠道 × 子仓」这一行取值，不按渠道 ID 建对照表。"""

    def test_same_channel_different_warehouse(self) -> None:
        rows = [
            SimpleNamespace(channel_id="C1", warehouse_id="W1", sensitive_raw="锂电产品"),
            SimpleNamespace(channel_id="C1", warehouse_id="W2", sensitive_raw="无"),
            SimpleNamespace(channel_id="C2", warehouse_id="W1", sensitive_raw="普货"),
        ]
        index = build_row_level_index(rows)
        self.assertEqual(index[("C1", "W1")], SensitivityLevel.LITHIUM)
        self.assertEqual(index[("C1", "W2")], SensitivityLevel.NONE)
        self.assertEqual(index[("C2", "W1")], SensitivityLevel.SENSITIVE_GENERAL)
        self.assertEqual(len(index), 3)

    def test_first_occurrence_wins_on_duplicate_key(self) -> None:
        rows = [
            SimpleNamespace(channel_id="C1", warehouse_id="W1", sensitive_raw="锂电产品"),
            SimpleNamespace(channel_id="C1", warehouse_id="W1", sensitive_raw="无"),
        ]
        index = build_row_level_index(rows)
        self.assertEqual(index[("C1", "W1")], SensitivityLevel.LITHIUM)


class TestCompatibility(unittest.TestCase):
    """兼容 = 货等级 ≤ 提单等级；完全匹配 = 相等。"""

    def test_compatible(self) -> None:
        lvl = parse_level("无（普货）,敏感普货,干电产品,锂电产品")
        self.assertTrue(is_compatible(SensitivityLevel.LITHIUM, lvl))
        self.assertTrue(is_compatible(SensitivityLevel.DRY, lvl))
        self.assertTrue(is_compatible(SensitivityLevel.NONE, lvl))
        general = parse_level("敏感普货")
        self.assertFalse(is_compatible(SensitivityLevel.LITHIUM, general))
        self.assertTrue(is_compatible(SensitivityLevel.NONE, general))

    def test_exact_match_is_soft(self) -> None:
        self.assertTrue(is_exact_match(SensitivityLevel.DRY, SensitivityLevel.DRY))
        self.assertFalse(is_exact_match(SensitivityLevel.NONE, SensitivityLevel.DRY))
        # 完全匹配是目标，不是硬约束：兼容但不等仍应可配
        self.assertTrue(is_compatible(SensitivityLevel.NONE, SensitivityLevel.DRY))
        self.assertFalse(is_exact_match(SensitivityLevel.NONE, SensitivityLevel.DRY))

    def test_accept_objects_with_level(self) -> None:
        wb = SimpleNamespace(level=SensitivityLevel.LITHIUM)
        cargo = SimpleNamespace(level=SensitivityLevel.DRY)
        self.assertTrue(is_compatible(cargo, wb))
        self.assertFalse(is_exact_match(cargo, wb))


if __name__ == "__main__":
    unittest.main()
