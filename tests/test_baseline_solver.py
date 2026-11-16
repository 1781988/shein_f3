"""贪心基准与主方案的确定性、复跑记录一致、480 步、五条硬约束零冲突、
诊断白名单与全 480 小时非零格去向。"""
from __future__ import annotations

import sys
import time
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.baseline import fcfs_baseline  # noqa: E402
from src.config import Config  # noqa: E402
from src.diagnose import diagnose, validate_diagnosis  # noqa: E402
from src.domain import AllocationRecord, Solution  # noqa: E402
from src.ledger import SupplyLedger  # noqa: E402
from src.loader import load_all  # noqa: E402
from src.precision import parse_time  # noqa: E402
from src.scoring import dominates, objective_vector, pareto_filter  # noqa: E402
from src.solver import RollingSolver  # noqa: E402
from tests.fixtures.make_fixtures import tiny_dataset  # noqa: E402


def _record_lines(solution: Solution):
    """把 Solution 转成逐行可比对的元组序列（只取外部可见字段）。"""
    return [
        (
            r.bill_no, r.channel_id, r.warehouse_id, r.hour_label,
            r.pickup_time_str, r.volume,
        )
        for r in solution.records
    ]


def _audit(solution: Solution, dataset) -> dict:
    """独立于求解器的五条硬约束复核。"""
    ledger = SupplyLedger(dataset, Config.load())
    ledger.release_all()
    per_bill = defaultdict(int)
    per_cell = defaultdict(int)
    warehouses = defaultdict(set)
    violations = {cid: 0 for cid in ("C1", "C2", "C3", "C4", "C5")}
    for record in solution.records:
        per_bill[record.bill_no] += record.volume
        cell = ledger.cells.get(record.cell_key)
        if cell is None:
            violations["C1"] += 1
            continue
        per_cell[record.cell_key] += record.volume
        warehouses[record.bill_no].add(record.warehouse_id)
        waybill = solution.waybill_state[record.bill_no]
        if int(cell.level) > int(waybill.level):
            violations["C4"] += 1
        pickup = parse_time(record.pickup_time_str)
        if cell.available_at > waybill.latest_pickup:
            violations["C5"] += 1
        if not (waybill.earliest_pickup <= pickup <= waybill.latest_pickup
                and pickup >= cell.available_at):
            violations["C5"] += 1
    for key, volume in per_cell.items():
        if volume > ledger.cells[key].original_volume or volume <= 0:
            violations["C1"] += 1
    for bill_no, volume in per_bill.items():
        if volume > solution.waybill_state[bill_no].plan_volume:
            violations["C2"] += 1
    for bill_no, whs in warehouses.items():
        if len(whs) > 1:
            violations["C3"] += 1
    return violations


class TestSolverDeterminism(unittest.TestCase):
    """同配置跑两次逐行相同。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)
        cls.scenario = cls.cfg.active_scenarios()[0]

    def test_two_runs_identical(self) -> None:
        first = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        second = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        self.assertEqual(_record_lines(first), _record_lines(second))
        self.assertEqual(first.diagnostics.allocated_volume,
                         second.diagnostics.allocated_volume)
        self.assertEqual(first.diagnostics.construction_conflicts,
                         second.diagnostics.construction_conflicts)

    def test_records_sorted_and_unique(self) -> None:
        solution = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        # 输出记录按 (提单号, 产出小时, 渠道ID, 子仓ID, 体积) 排序
        keys = [r.sort_key() for r in solution.records]
        self.assertEqual(keys, sorted(keys))
        # 同一 (提单号, 渠道ID, 子仓ID, 产出小时) 至多一行
        merged_keys = [
            (r.bill_no, r.channel_id, r.warehouse_id, r.hour_label)
            for r in solution.records
        ]
        self.assertEqual(len(merged_keys), len(set(merged_keys)))

    def test_480_steps(self) -> None:
        solution = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        diag = solution.diagnostics
        self.assertEqual(diag.steps, cfg.HOURS)
        self.assertEqual(diag.released_cells, diag.total_nonzero_cells)
        self.assertEqual(diag.reassign_steps, diag.event_steps)
        self.assertGreater(diag.incremental_steps, 0)
        self.assertGreater(diag.event_steps, 0)
        self.assertTrue(diag.disposition_covered)
        self.assertEqual(
            diag.allocated_volume + diag.unallocated_volume,
            self.dataset.total_supply_volume,
        )

    def test_all_cells_have_destination(self) -> None:
        solution = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        diag = solution.diagnostics
        total = sum(row["nonzero_cells"] for row in diag.hour_disposition)
        self.assertEqual(total, diag.total_nonzero_cells)
        for row in diag.hour_disposition:
            self.assertTrue(row["released"])
            self.assertEqual(
                row["nonzero_cells"],
                row["allocated_cells"] + row["unallocated_cells"],
            )
        self.assertEqual(
            sum(row["unallocated_cells"] for row in diag.hour_disposition),
            len(diag.unallocated_cells),
        )

    def test_construction_conflicts_zero(self) -> None:
        solution = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        self.assertEqual(solution.diagnostics.conflict_total(), 0)
        self.assertEqual(solution.diagnostics.construction_samples, [])
        self.assertEqual(_audit(solution, self.dataset),
                         {cid: 0 for cid in ("C1", "C2", "C3", "C4", "C5")})


class TestTinySolver(unittest.TestCase):
    """极小合成数据集上的 480 步与硬约束（快速回归）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = tiny_dataset()
        cls.scenario = cls.cfg.active_scenarios()[0]

    def test_tiny_full_run(self) -> None:
        solution = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        self.assertEqual(solution.diagnostics.steps, cfg.HOURS)
        self.assertEqual(solution.diagnostics.conflict_total(), 0)
        self.assertTrue(solution.diagnostics.disposition_covered)
        self.assertGreater(len(solution.records), 0)
        self.assertEqual(_audit(solution, self.dataset),
                         {cid: 0 for cid in ("C1", "C2", "C3", "C4", "C5")})

    def test_tiny_deterministic(self) -> None:
        a = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        b = RollingSolver(self.cfg, self.dataset).solve(self.scenario)
        self.assertEqual(_record_lines(a), _record_lines(b))


class TestBaseline(unittest.TestCase):
    """贪心基准独立实现、不调用主求解器、确定性、零冲突。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)

    def test_baseline_does_not_use_solver(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "baseline.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("RollingSolver", source)
        self.assertNotIn("from .solver", source)
        self.assertIn("def fcfs_baseline", source)

    def test_baseline_runs_and_deterministic(self) -> None:
        a = fcfs_baseline(self.cfg, self.dataset)
        b = fcfs_baseline(self.cfg, self.dataset)
        self.assertEqual(_record_lines(a), _record_lines(b))
        self.assertGreater(len(a.records), 0)
        self.assertEqual(a.diagnostics.conflict_total(), 0)
        self.assertEqual(_audit(a, self.dataset),
                         {cid: 0 for cid in ("C1", "C2", "C3", "C4", "C5")})

    def test_baseline_differs_from_main(self) -> None:
        main = RollingSolver(self.cfg, self.dataset).solve(
            self.cfg.active_scenarios()[0]
        )
        base = fcfs_baseline(self.cfg, self.dataset)
        self.assertNotEqual(_record_lines(main), _record_lines(base))
        self.assertNotEqual(main.diagnostics.allocated_volume,
                            base.diagnostics.allocated_volume)

    def test_tiny_baseline(self) -> None:
        dataset = tiny_dataset()
        base = fcfs_baseline(self.cfg, dataset)
        self.assertEqual(base.diagnostics.conflict_total(), 0)
        self.assertEqual(_audit(base, dataset),
                         {cid: 0 for cid in ("C1", "C2", "C3", "C4", "C5")})


class TestScenariosAndPareto(unittest.TestCase):
    """三组场景都跑通、优先级顺序不同、近似 Pareto 筛选。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)

    def test_three_scenarios_all_run(self) -> None:
        signatures = []
        for scenario in self.cfg.active_scenarios():
            solution = RollingSolver(self.cfg, self.dataset).solve(scenario)
            self.assertEqual(solution.diagnostics.conflict_total(), 0)
            self.assertEqual(solution.diagnostics.steps, cfg.HOURS)
            signatures.append(_record_lines(solution))
        self.assertEqual(len(signatures), 3)
        distinct = {tuple(map(str, sig)) for sig in signatures}
        self.assertGreaterEqual(len(distinct), 2,
                                "不同优先级顺序应产生不同解")

    def test_pareto_filter(self) -> None:
        points = [
            ("A", (0.9, 0.8, 0.7, 0.6, 0.5)),
            ("B", (0.9, 0.8, 0.7, 0.6, 0.5)),   # 与 A 并列，互不相支配
            ("C", (0.8, 0.7, 0.6, 0.5, 0.4)),   # 被 A 全面支配
            ("D", (0.95, 0.8, 0.7, 0.5, 0.5)),  # 与 A 互有胜负
        ]
        self.assertEqual(pareto_filter(points), ["A", "B", "D"])
        self.assertTrue(dominates((1.0, 1.0), (1.0, 0.9)))
        self.assertFalse(dominates((1.0, 1.0), (1.0, 1.0)))

    def test_objective_vector_signs(self) -> None:
        from src.domain import Metrics

        metrics = Metrics(
            fill_rate=0.8, exact_match_rate=0.5, backlog_rate=0.1,
            primary_injection_rate=0.4, cost=200.0,
        )
        self.assertEqual(
            objective_vector(metrics), (0.8, 0.5, -0.1, 0.4, -200.0)
        )


class TestDiagnosis(unittest.TestCase):
    """诊断原因只出现白名单、搜不到六类被排除规则。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)
        cls.solution = RollingSolver(cls.cfg, cls.dataset).solve(
            cls.cfg.active_scenarios()[0]
        )
        cls.records = diagnose(cls.solution, cls.dataset, cls.cfg)

    def test_only_whitelist_reasons(self) -> None:
        allowed = set(self.cfg.allowed_reasons())
        for record in self.records:
            self.assertIn(record.primary_reason, allowed)
            for reason in record.reason_chain.split(" -> "):
                self.assertIn(reason, allowed)
        validate_diagnosis(self.records, self.cfg)

    def test_no_excluded_rules_anywhere(self) -> None:
        text = "\n".join(
            " ".join([r.bill_no, r.primary_reason, r.reason_chain, r.evidence])
            for r in self.records
        )
        for keyword in self.cfg.excluded_rule_keywords():
            self.assertNotIn(keyword, text)
        # 六类被排除规则的原名逐一搜一遍
        for rule in ("限重", "耗材上限", "计费重", "集装板尺寸", "清关资质", "可混配组合"):
            self.assertNotIn(rule, text)
        # 源码层面也不实现这六类规则
        source = (Path(__file__).resolve().parents[1] / "src" / "diagnose.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("限重", source)

    def test_gap_matches_state(self) -> None:
        for record in self.records:
            waybill = self.solution.waybill_state[record.bill_no]
            self.assertEqual(record.gap_volume, waybill.remaining())
            self.assertGreater(record.gap_volume, 0)

    def test_diagnosis_deterministic(self) -> None:
        again = diagnose(self.solution, self.dataset, self.cfg)
        self.assertEqual(
            [(r.bill_no, r.gap_volume, r.primary_reason, r.reason_chain)
             for r in self.records],
            [(r.bill_no, r.gap_volume, r.primary_reason, r.reason_chain)
             for r in again],
        )


if __name__ == "__main__":
    unittest.main()
