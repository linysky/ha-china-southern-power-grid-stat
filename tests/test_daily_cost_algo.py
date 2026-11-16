"""每日电费计价纯函数单元测试。

这些函数位于 sensor.py 顶部，不依赖 Home Assistant 运行时；用 AST 抽取后
直接执行，避免为了跑测试而安装整套 homeassistant 包。
运行：python tests/test_daily_cost_algo.py
"""
import ast
import calendar
import datetime
import json
import os
import pathlib
import time
import traceback
import unittest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "custom_components/china_southern_power_grid_stat/sensor.py")


def _load_algo():
    src = SRC.read_text(encoding="utf-8")
    start = src.index("DAILY_HISTORY_YEARS = 3")
    end = src.index("class CSGDailyHistory:")
    tree = ast.parse(src[start:end])
    keep = [n for n in tree.body
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.FunctionDef))]
    mod = ast.Module(body=keep, type_ignores=[])
    g = {"datetime": datetime, "calendar": calendar, "json": json,
         "os": os, "time": time, "traceback": traceback}
    exec(compile(ast.fix_missing_locations(mod), str(SRC), "exec"), g)
    return g


A = _load_algo()


class TierPricingTests(unittest.TestCase):
    def _month(self, ym, kwh_per_day, bill):
        rows = [(f"{ym[:4]}-{ym[4:]}-{d:02d}", kwh_per_day) for d in range(1, 29)]
        return A["_tier_price_month"](ym, rows, bill, None)

    def test_month_sum_equals_bill(self):
        for ym, kwh, bill in [
            ("202608", 8.0, 180.55),    # 夏季，第一档内
            ("202607", 12.0, 320.40),   # 夏季，跨二档
            ("202601", 5.0, 96.10),     # 非夏季，第一档内
            ("202612", 30.0, 700.00),   # 非夏季，跨三档
        ]:
            with self.subTest(ym=ym):
                charges, labels, base, prices = self._month(ym, kwh, bill)
                self.assertAlmostEqual(round(sum(charges), 2), bill, places=2)
                self.assertGreater(base, 0)
                self.assertEqual(len(charges), len(prices))

    def test_cross_tier_labels(self):
        charges, labels, base, prices = self._month("202612", 30.0, 700.00)
        self.assertIn("1-2", [str(x) for x in labels])
        self.assertIn("2-3", [str(x) for x in labels])

    def test_tier_segments(self):
        self.assertEqual(
            A["_tier_segments"](250, 20, (260, 600)), [(1, 10), (2, 10)])
        self.assertEqual(
            A["_tier_segments"](590, 20, (260, 600)), [(2, 10), (3, 10)])

    def test_apportion_cents_no_last_day_spike(self):
        out = A["_apportion_cents"]([1.11] * 30, 33.33)
        self.assertAlmostEqual(round(sum(out), 2), 33.33, places=2)
        self.assertLess(max(out) - min(out), 0.02)

    def test_zero_bill_keeps_kwh(self):
        rows = [(f"2026-02-{d:02d}", 7.2) for d in range(1, 29)]
        charges, labels, base, prices = A["_zero_bill_rows"]("202602", rows)
        self.assertEqual(set(charges), {0.0})
        self.assertEqual(len(labels), 28)

    def test_normalize_ym(self):
        self.assertEqual(A["_normalize_ym"]("2023-04"), "202304")
        self.assertEqual(A["_normalize_ym"]("202304"), "202304")
        self.assertEqual(A["_normalize_ym"]("bad"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
