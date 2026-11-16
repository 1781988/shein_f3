"""边界场景与报错方式：第五节 20 类样例按策略处理并产出 data_issues.csv。"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.cli import check_output_conflict  # noqa: E402
from src.config import Config  # noqa: E402
from src.errors import (  # noqa: E402
    ConfigError,
    DataValidationError,
    OutputConflictError,
    SchemaError,
)
from src.loader import load_all, sha256_file  # noqa: E402
from src.time_axis import TimeAxis  # noqa: E402
from src.validate import check_integerization, validate_dataset, write_data_issues  # noqa: E402
from tests.fixtures.make_fixtures import (  # noqa: E402
    config_for,
    make_all,
)


class TestSyntheticEdgeCases(unittest.TestCase):
    """合成样例：覆盖空表、缺列、非法时间、负/NaN/非数值、重复、全零、备注等。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="f3_fixtures_")
        cls.root = Path(cls._tmp.name)
        cls.paths = make_all(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _load(self, name: str):
        return load_all(config_for(self.paths[name]))

    def test_valid_fixture_loads(self) -> None:
        dataset = self._load("valid")
        self.assertEqual(len(dataset.supply_rows), 3)
        self.assertEqual(len(dataset.waybills), 4)
        self.assertEqual(dataset.notes_meta.rule_count, cfg.NOTES_RULE_COUNT)
        self.assertTrue(dataset.source_hashes["before"] == dataset.source_hashes["after"])

    def test_empty_wide_raises_schema(self) -> None:
        with self.assertRaises(SchemaError):
            self._load("empty_wide")

    def test_missing_column_raises_schema(self) -> None:
        with self.assertRaises(SchemaError) as ctx:
            self._load("missing_column")
        self.assertIn("缺失列名", str(ctx.exception))

    def test_bad_notes_raises_schema(self) -> None:
        with self.assertRaises(SchemaError):
            self._load("bad_notes")

    def test_missing_notes_sheet_raises_schema(self) -> None:
        with self.assertRaises(SchemaError):
            self._load("missing_notes_sheet")

    def test_negative_volume_raises_validation(self) -> None:
        with self.assertRaises(DataValidationError) as ctx:
            self._load("negative_volume")
        self.assertIn("体积不能为负", str(ctx.exception))

    def test_nan_volume_raises_validation(self) -> None:
        with self.assertRaises(DataValidationError):
            self._load("nan_volume")

    def test_non_numeric_volume_raises_validation(self) -> None:
        with self.assertRaises(DataValidationError):
            self._load("non_numeric_volume")

    def test_bad_time_skips_row_and_records_issue(self) -> None:
        dataset = self._load("bad_time")
        self.assertEqual(dataset.skipped_waybills, 1)
        self.assertTrue(any("时间串解析失败" in i.action for i in dataset.issues))

    def test_time_failure_over_limit_raises(self) -> None:
        with self.assertRaises(DataValidationError) as ctx:
            self._load("time_failure_over_limit")
        self.assertIn("失败率", str(ctx.exception))

    def test_duplicate_bill_records_issue(self) -> None:
        dataset = self._load("duplicate_bill")
        self.assertEqual(dataset.duplicate_bill_count, 1)
        self.assertTrue(any("重复提单号" in i.action for i in dataset.issues))

    def test_all_zero_no_nonzero_cells(self) -> None:
        dataset = self._load("all_zero")
        self.assertEqual(dataset.nonzero_cells, 0)
        self.assertEqual(dataset.total_supply_volume, 0)

    def test_empty_density_counted(self) -> None:
        dataset = self._load("empty_density")
        self.assertEqual(dataset.empty_density_count, 3)

    def test_bad_hour_column_raises_schema(self) -> None:
        with self.assertRaises(SchemaError) as ctx:
            self._load("bad_hour_column")
        self.assertIn("小时列名", str(ctx.exception))

    def test_shj_and_abnormal_waybills(self) -> None:
        dataset = self._load("valid")
        self.assertEqual(dataset.shj_bill_count, 1)
        self.assertEqual(dataset.abnormal_count, 1)
        abnormal = [w for w in dataset.waybills if w.is_abnormal][0]
        self.assertEqual(
            abnormal.latest_pickup - abnormal.earliest_pickup,
            __import__("datetime").timedelta(hours=cfg.ABNORMAL_FALLBACK_HOURS),
        )

    def test_existing_csv_conflict(self) -> None:
        directory = self.paths["existing_csv"]
        cfg_obj = config_for(directory, strict_no_overwrite=True)
        cfg_obj.outputs["dir"] = str(directory / "results")
        with self.assertRaises(OutputConflictError):
            check_output_conflict(cfg_obj)
        cfg_obj.strict_no_overwrite = False
        check_output_conflict(cfg_obj)


class TestDataIssuesCsv(unittest.TestCase):
    """data_issues.csv 四列、UTF-8 无 BOM、固定换行。"""

    def test_write_and_columns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f3_issues_") as tmp:
            target = Path(tmp) / "results" / "data_issues.csv"
            from src.domain import DataIssue

            issues = [
                DataIssue("数值", "宽表!第2行", "1.2345", "忽略合计体积列"),
                DataIssue("info", "提单明细", "10 张", "正常处理"),
            ]
            write_data_issues(issues, target)
            raw = target.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", raw)
            with target.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], list(cfg.DATA_ISSUES_COLUMNS))
            self.assertEqual(len(rows), 3)


class TestRealDataFacts(unittest.TestCase):
    """真实数据只读核对（不构成需求，只锁住实现口径）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config.load()
        cls.dataset = load_all(cls.cfg)
        validate_dataset(cls.dataset, cls.cfg)

    def test_hashes_unchanged(self) -> None:
        before = self.dataset.source_hashes["before"]
        after = self.dataset.source_hashes["after"]
        self.assertEqual(before, after)
        for name, digest in before.items():
            self.assertEqual(sha256_file(self.cfg.data_path() / name), digest)

    def test_480_columns_parse(self) -> None:
        axis: TimeAxis = self.dataset.axis
        self.assertEqual(len(self.dataset.hour_column_names), cfg.HOURS)
        self.assertEqual(axis.verify_columns(self.dataset.hour_column_names), [])
        for i, name in enumerate(self.dataset.hour_column_names):
            self.assertEqual(axis.hour_index_of_column(name), i)
        self.assertEqual(len(axis.eval_hour_indices()), cfg.EVAL_HOURS)

    def test_integerization_sum_matches_four_decimals(self) -> None:
        for row in self.dataset.supply_rows:
            self.assertEqual(sum(row.hourly_volumes), row.exact_sum_int)
        total, in_eval = check_integerization(self.dataset)
        self.assertEqual(total, self.dataset.total_supply_volume)
        self.assertEqual(in_eval, self.dataset.eval_range_supply_volume)
        # 与资料包核对值一致：全 20 天 35239.6992 立方米，评估范围 26200.284 立方米
        self.assertAlmostEqual(total / cfg.VOLUME_UNIT, 35239.6992, places=6)
        self.assertAlmostEqual(in_eval / cfg.VOLUME_UNIT, 26200.284, places=6)

    def test_known_data_facts(self) -> None:
        self.assertEqual(len(self.dataset.supply_rows), 301)
        self.assertEqual(len(self.dataset.waybills), 2652)
        self.assertEqual(len(self.dataset.densities), 6020)
        self.assertEqual(len(self.dataset.cost_rates.by_port), 11)
        self.assertEqual(self.dataset.nonzero_cells, 65512)
        self.assertEqual(self.dataset.abnormal_count, 233)
        self.assertEqual(self.dataset.shj_bill_count, 10)
        self.assertEqual(self.dataset.empty_density_count, 1447)
        self.assertEqual(self.dataset.duplicate_bill_count, 0)
        self.assertEqual(self.dataset.max_declared_diff, 50)  # 0.0050 立方米
        self.assertEqual(self.dataset.notes_meta.rule_count, cfg.NOTES_RULE_COUNT)

    def test_eval_range_is_statistical_only(self) -> None:
        self.assertLess(self.dataset.eval_range_supply_volume,
                        self.dataset.total_supply_volume)
        self.assertTrue(any("全 480" in i.action or "评估范围" in i.raw_value
                           for i in self.dataset.issues))


class TestConfigAndExitCodes(unittest.TestCase):
    """配置非法时退出码 2 且提示合法取值。"""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, str(root / "run.py"), *args],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        )

    def test_help_exit_zero(self) -> None:
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--data-dir", proc.stdout)

    def test_invalid_seed_exit_2(self) -> None:
        proc = self._run("--seed", "abc", "--only", "load")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("必须是整数", proc.stdout + proc.stderr)

    def test_unknown_scenario_exit_2(self) -> None:
        proc = self._run("--scenario", "NOPE", "--only", "load")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("合法取值", proc.stdout + proc.stderr)

    def test_invalid_scale_exit_2(self) -> None:
        proc = self._run("--scale", "1.5", "--only", "load")
        self.assertEqual(proc.returncode, 2)

    def test_missing_data_dir_exit_2(self) -> None:
        proc = self._run("--data-dir", "不存在的目录123", "--only", "load")
        self.assertEqual(proc.returncode, 2)

    def test_config_validate_direct(self) -> None:
        with self.assertRaises(ConfigError):
            Config.load(overrides={"scenario": "不存在"})
        with self.assertRaises(ConfigError):
            Config.load(overrides={"scale": 0.0})
        with self.assertRaises(ConfigError):
            Config.load(overrides={"seed": "abc"})

    def test_no_scattered_literals(self) -> None:
        """精度与等级字面量集中在 config.py，不在其他源码里散落。"""
        root = Path(__file__).resolve().parents[1] / "src"
        offenders: list = []
        for path in sorted(root.glob("*.py")):
            if path.name == "config.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "10000" in text or "0.0005" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
