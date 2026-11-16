"""七项指标可从 allocation.csv 复算；口径说明非空；未配提单货量口径正确。"""
from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.config import Config  # noqa: E402
from src.loader import load_all  # noqa: E402
from src.metrics import (  # noqa: E402
    INDICATOR_NAMES,
    compute,
    compute_from_rows,
    metric_values,
)
from src.solver import RollingSolver  # noqa: E402
from tests.fixtures.make_fixtures import tiny_dataset  # noqa: E402


def _csv_rows(path: Path):
    """读 CSV 返回 (表头, 行字典列表)。"""
    with path.open("r", encoding=cfg.CSV_ENCODING, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = [
            {header[i]: row[i] for i in range(min(len(header), len(row)))}
            for row in reader if row
        ]
    return list(header), rows


class TestMetricsFromCsv(unittest.TestCase):
    """内存计算与从 CSV 复算必须逐项一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)
        cls.solution = RollingSolver(cls.cfg, cls.dataset).solve(
            cls.cfg.active_scenarios()[0]
        )

    def test_compute_and_recompute_match(self) -> None:
        main, auxiliary = compute(self.solution, self.dataset, self.cfg)
        rows = [
            {
                cfg.ALLOCATION_COLUMNS[0]: r.bill_no,
                cfg.ALLOCATION_COLUMNS[1]: r.channel_id,
                cfg.ALLOCATION_COLUMNS[2]: r.warehouse_id,
                cfg.ALLOCATION_COLUMNS[3]: r.hour_label,
                cfg.ALLOCATION_COLUMNS[4]: r.pickup_time_str,
                cfg.ALLOCATION_COLUMNS[5]: (
                    f"{r.volume // cfg.VOLUME_UNIT}."
                    f"{r.volume % cfg.VOLUME_UNIT:0{cfg.VOLUME_DECIMALS}d}"
                ),
            }
            for r in self.solution.records
        ]
        recomputed, aux2 = compute_from_rows(rows, self.dataset, self.cfg)
        self.assertEqual(metric_values(main, self.cfg),
                         metric_values(recomputed, self.cfg))
        self.assertEqual(auxiliary["a_record_count"], aux2["a_record_count"])

    def test_seven_indicators_and_notes(self) -> None:
        metrics, _ = compute(self.solution, self.dataset, self.cfg)
        values = metric_values(metrics, self.cfg)
        self.assertEqual(tuple(values.keys()), INDICATOR_NAMES)
        for name in INDICATOR_NAMES:
            self.assertTrue(values[name], f"{name} 数值为空")
            self.assertTrue(metrics.formula_notes.get(name, "").strip(),
                            f"{name} 口径说明为空")

    def test_unallocated_is_supply_side_not_bill_gap(self) -> None:
        """未配提单货量是供给侧剩余，S1 全量配出时为 0，但虚仓仍 > 0。"""
        metrics, auxiliary = compute(self.solution, self.dataset, self.cfg)
        self.assertEqual(metrics.unallocated_volume, 0)
        self.assertEqual(
            auxiliary["sum_a_volume"], self.dataset.eval_range_supply_volume
        )
        self.assertGreater(metrics.virtual_warehouse_count, 0)
        note = metrics.formula_notes["未配提单货量"]
        self.assertIn("供给侧剩余", note)
        self.assertIn("不是提单缺口", note)

    def test_rates_in_unit_interval(self) -> None:
        metrics, _ = compute(self.solution, self.dataset, self.cfg)
        for name in ("fill_rate", "exact_match_rate", "backlog_rate",
                     "primary_injection_rate"):
            value = getattr(metrics, name)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertGreaterEqual(metrics.cost, 0.0)


class TestMetricsOnDisk(unittest.TestCase):
    """若已跑过全流程，则从磁盘 CSV 复算并与 indicators.csv 比对。"""

    def test_disk_indicators_if_present(self) -> None:
        cfg_obj = Config.load()
        alloc = cfg_obj.results_path() / "allocation.csv"
        indicators = cfg_obj.results_path() / "indicators.csv"
        if not (alloc.is_file() and indicators.is_file()):
            self.skipTest("尚未运行 python run.py，跳过磁盘结果复算")
        header, rows = _csv_rows(indicators)
        self.assertEqual(header, list(cfg.INDICATORS_COLUMNS))
        self.assertEqual(len(rows), len(INDICATOR_NAMES))
        published = {r.get("指标", ""): r.get("数值", "") for r in rows}
        dataset = load_all(cfg_obj)
        _, alloc_rows = _csv_rows(alloc)
        recomputed, _ = compute_from_rows(alloc_rows, dataset, cfg_obj)
        self.assertEqual(published, metric_values(recomputed, cfg_obj))


class TestMetricsTiny(unittest.TestCase):
    """极小合成数据集也能算出七项指标，不除零、不抛异常。"""

    def test_tiny(self) -> None:
        cfg_obj = Config.load()
        dataset = tiny_dataset()
        solution = RollingSolver(cfg_obj, dataset).solve(
            cfg_obj.active_scenarios()[0]
        )
        metrics, auxiliary = compute(solution, dataset, cfg_obj)
        self.assertEqual(len(metric_values(metrics, cfg_obj)), len(INDICATOR_NAMES))
        self.assertGreaterEqual(metrics.virtual_warehouse_count, 0)
        self.assertGreaterEqual(auxiliary["a_record_count"], 0)


if __name__ == "__main__":
    unittest.main()
