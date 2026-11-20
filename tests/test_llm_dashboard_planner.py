"""LLM plans the dashboard (charts, wording); pandas computes every value. Bad LLM output is dropped, never shown."""
import sys
import unittest
from pathlib import Path

import pandas as pd

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.dynamic_kpi_engine import DynamicKpiEngine  # noqa: E402


class FakeLLM:
    def __init__(self, text="", js=None):
        self.text, self.js = text, js

    def complete(self, *a, **k):
        return self.text

    def complete_json(self, *a, **k):
        if self.js is None:
            raise RuntimeError("down")
        return self.js


DF = pd.DataFrame({
    "promo_type": ["TPR", "TPR", "Coupon", "Coupon", "Feature", "None"] * 4,
    "region": ["N", "S"] * 12,
    "date": pd.date_range("2025-01-01", periods=24, freq="15D").strftime("%Y-%m-%d"),
    "net_sales": [100.0 + i for i in range(24)],
    "cogs": [60.0 + i for i in range(24)],
})


class ChartPlanTests(unittest.TestCase):
    def setUp(self):
        self.e = DynamicKpiEngine(None, None, None)
        self.p = self.e.profile_dataset(DF)

    def test_valid_specs_are_computed_by_pandas_not_the_llm(self):
        plan = [{"type": "bar", "dimension": "promo_type", "measure": "net_sales", "agg": "sum", "title": "Net sales by promo", "value": 999999}]
        ch = self.e.execute_chart_plan(DF, plan, self.p)
        self.assertEqual(len(ch), 1)
        want = DF.groupby("promo_type")["net_sales"].sum().sort_values(ascending=False)
        self.assertEqual([d["value"] for d in ch[0]["data"]], [round(v, 2) for v in want.values])
        self.assertEqual(ch[0]["title"], "Net sales by promo")

    def test_line_needs_date_and_is_monthly(self):
        ch = self.e.execute_chart_plan(DF, [{"type": "line", "dimension": "date", "measure": "cogs", "agg": "avg"}], self.p)
        self.assertEqual(len(ch), 1)
        self.assertTrue(ch[0]["data"][0]["label"].startswith("2025-01"))

    def test_invented_or_mismatched_specs_are_dropped(self):
        bad = [
            {"type": "bar", "dimension": "nope", "measure": "net_sales", "agg": "sum"},
            {"type": "bar", "dimension": "promo_type", "measure": "region", "agg": "sum"},
            {"type": "line", "dimension": "promo_type", "measure": "net_sales", "agg": "sum"},
            {"type": "bar", "dimension": "date", "measure": "net_sales", "agg": "sum"},
            {"type": "radar", "dimension": "promo_type", "measure": "net_sales", "agg": "sum"},
            {"type": "bar", "dimension": "promo_type", "measure": "net_sales", "agg": "eval"},
        ]
        self.assertEqual(self.e.execute_chart_plan(DF, bad, self.p), [])

    def test_falls_back_to_templates_without_llm(self):
        self.assertGreaterEqual(len(self.e.generate_dynamic_charts(DF, self.p)), 2)

    def test_llm_charts_used_when_enough_are_valid(self):
        specs = ('[{"type":"bar","dimension":"promo_type","measure":"net_sales","agg":"sum"},'
                 '{"type":"bar","dimension":"region","measure":"cogs","agg":"avg"},'
                 '{"type":"line","dimension":"date","measure":"net_sales","agg":"sum"}]')
        e = DynamicKpiEngine(None, None, FakeLLM(text="Sure:\n" + specs))
        ch = e.generate_dynamic_charts(DF, self.p)
        self.assertEqual(len(ch), 3)
        self.assertTrue(all(c["source"] == "llm_planned" for c in ch))


class SummaryTests(unittest.TestCase):
    KPIS = [{"label": "Total Net Sales", "display": "$2,800.00"}]
    CHARTS = [{"data": [{"label": "TPR", "value": 1000.0}]}]
    PROFILE = {"total_rows": 24, "total_columns": 5}

    def summary(self, js):
        return DynamicKpiEngine(None, None, FakeLLM(js=js)).generate_grounded_summary("Promo", self.KPIS, self.CHARTS, self.PROFILE)

    def test_dict_shaped_summary_is_flattened_to_paragraphs(self):
        out = self.summary({"title": "Promo", "summary": {"Paragraph 1": "Net sales were $2,800.00.", "Paragraph 2": "TPR led with 1000.0."}})
        self.assertEqual(out["source"], "llm_grounded")
        self.assertNotIn("{", out["text"])
        self.assertIn("Net sales were $2,800.00.", out["text"])

    def test_stringified_dict_is_flattened(self):
        out = self.summary({"title": "Promo", "summary": "{'Paragraph 1': 'Net sales were $2,800.00.', 'Paragraph 2': 'Over 24 records.'}"})
        self.assertNotIn("Paragraph", out["text"])
        self.assertNotIn("{", out["text"])

    def test_invented_number_falls_back_to_default(self):
        out = self.summary({"title": "Promo", "summary": "Net sales were $9,999,999.00."})
        self.assertNotEqual(out.get("source"), "llm_grounded")
        self.assertIn("$2,800.00", out["text"])

    def test_llm_down_uses_default(self):
        self.assertIn("2,800.00", self.summary(None)["text"])


if __name__ == "__main__":
    unittest.main()
