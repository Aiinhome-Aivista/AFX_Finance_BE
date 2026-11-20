import sys
import tempfile
import unittest
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.config import Config  # noqa: E402
from app.services import analyst_chat_service as acs  # noqa: E402
from app.services import evidence_service as ev  # noqa: E402
from app.services import executive_synthesizer as synth  # noqa: E402
from app.services import intent_router_service as router  # noqa: E402
from app.services.analyst_chat_service import AnalystChatService  # noqa: E402
from tests.test_analyst_dashboard import CSV, FakeDB  # noqa: E402

TERMS = ["Weight", "Karigar Name", "Bhagya", "Receipt Date"]


class RouterTests(unittest.TestCase):
    def r(self, q):
        return router.route(q, TERMS).intent

    def test_math_questions_are_aggregation(self):
        for q in ["What is the total weight?", "Top 5 karigar by weight", "average purity per karigar", "show margin %",
                  "monthly trend of weight", "lowest purity", "how many records are there"]:
            self.assertEqual(self.r(q), router.AGGREGATION, q)

    def test_why_about_a_figure_is_hybrid(self):
        self.assertEqual(self.r("Why did weight drop last month?"), router.HYBRID)
        self.assertEqual(self.r("Why is Bhagya so high?"), router.HYBRID)
        self.assertEqual(self.r("Summarize the total weight and explain it"), router.HYBRID)

    def test_qualitative_questions_are_insight(self):
        for q in ["Give me an overview of this data", "What does the policy say about rework?", "Summarize the key risks"]:
            self.assertEqual(self.r(q), router.INSIGHT, q)

    def test_math_keywords_can_never_be_insight(self):
        for q in ["total revenue outlook", "top risks by region", "average of the notes"]:
            self.assertNotEqual(self.r(q), router.INSIGHT, q)


class SynthTests(unittest.TestCase):
    def test_drivers_use_exact_shares(self):
        res = {"rows": [["East", 60.0], ["West", 30.0], ["North", 10.0]], "group_columns": ["Region"], "metric_columns": ["sum Sales"],
               "is_time": False, "total_groups": 3}
        spec = {"metrics": [{"fn": "sum"}]}
        d = synth.drivers(res, spec)
        self.assertIn("East: total Sales 60 (60% of the total).", d)
        self.assertTrue(any("6.0x North" in x for x in d))

    def test_time_drivers_report_the_change(self):
        res = {"rows": [["2026-07", 30.0], ["2026-08", 20.0], ["2026-09", 7.0]], "group_columns": ["d"], "metric_columns": ["sum W"],
               "is_time": True, "total_groups": 3}
        d = synth.drivers(res, {"metrics": [{"fn": "sum"}]})
        self.assertIn("2026-09: 7 vs 20 in 2026-08 (-65.0%).", d)

    def test_five_followups_cycle_what_where_why(self):
        acs._CACHE.clear()
        tables = _tables()
        f = synth.follow_ups(tables, None, "")
        self.assertEqual(len(f), 5)
        self.assertEqual(len(set(f)), 5)
        self.assertTrue(f[0].startswith("What") or f[0].startswith("How many"))
        self.assertTrue(any(x.startswith("Why") for x in f))
        self.assertTrue(any(x.startswith(("Top", "Which")) for x in f))
        # every suggestion is something the planner/router can actually answer
        for q in f:
            self.assertIn(router.route(q, TERMS).intent, (router.AGGREGATION, router.HYBRID), q)

    def test_asked_question_is_not_suggested_again(self):
        self.assertNotIn("How many records are there?", synth.follow_ups(_tables(), None, "How many records are there?"))

    def test_narrative_guards(self):
        class L:
            def __init__(self, t): self.t = t
            def complete(self, *a, **k): return self.t
        synth._FAIL_UNTIL = 0.0
        ok = "Weight fell to 7 in September, mostly from Laxmi. The data does not say why."
        args = ("why?", "Weight fell from 20 to 7.", ["Laxmi: -15"], [{"source": "data", "text": "x", "citation": ""}])
        self.assertEqual(synth.narrate(L(ok), *args), ok)
        self.assertIsNone(synth.narrate(L("Weight fell to 7 because demand dropped sharply in the region."), *args))   # invented cause
        self.assertIsNone(synth.narrate(L("Weight fell to 7,500 in September, a notable drop overall."), *args))    # invented number
        docs = [{"source": "document", "text": "Demand dropped in September.", "citation": "memo.pdf p2"}]
        cause = "Weight fell to 7 because demand dropped in September per the memo."
        self.assertEqual(synth.narrate(L(cause), "why?", "Weight fell from 20 to 7.", [], docs), cause)               # cause is documented
        self.assertIsNone(synth.narrate(None, *args))


class ChatFlowTests(unittest.TestCase):
    def setUp(self):
        acs._CACHE.clear()
        synth._FAIL_UNTIL = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(CSV, encoding="utf-8")
        self.file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                     "sha256": "abc", "graph_version_number": 3}
        self.wf = {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": None, "selected_blocks": None}

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, q, llm=None):
        return AnalystChatService(FakeDB(dict(self.wf), [self.file]), Config(UPLOAD_DIR=self.tmp.name), llm).ask(2, "data_analyst", 5, q)

    def test_aggregation_is_exact_and_has_five_followups(self):
        out = self.ask("Top 3 karigar by weight")
        self.assertEqual(out["intent"], router.AGGREGATION)
        self.assertEqual(out["table"]["rows"][0][:2], ["Bhagya", 30.5])
        self.assertTrue(out["drivers"])
        self.assertEqual(len(out["follow_ups"]), 5)

    def test_why_question_computes_the_change_and_its_drivers(self):
        out = self.ask("Why did weight drop in the latest month?")
        self.assertEqual(out["intent"], router.HYBRID)
        self.assertFalse(out["unanswerable"])
        text = " ".join(out["drivers"])
        self.assertIn("2026-09: 7.50 vs 20", text)            # exact month-on-month figures from pandas
        self.assertIn("moved -12.50", text)
        self.assertIn("Karigar Name Laxmi: -20.00", text)   # biggest contributor to the fall
        self.assertIn("Partly offset by Karigar Name Ravi: +7.50", text)
        self.assertEqual(out["source"], "rules")           # no LLM: deterministic, no invented cause
        self.assertNotRegex(out["answer"].lower(), r"because|due to")

    def test_why_question_can_be_filtered_to_a_category(self):
        out = self.ask("Why did weight drop for Laxmi?")
        self.assertEqual(out["spec"]["filters"], [{"column": "Karigar Name", "op": "=", "value": "Laxmi"}])

    def test_overview_is_answered_from_computed_facts_with_a_note(self):
        out = self.ask("Give me an overview of this data")
        self.assertEqual(out["intent"], router.INSIGHT)
        self.assertFalse(out["unanswerable"])
        self.assertTrue(out["evidence"])
        self.assertTrue(any("No documents" in n for n in out["notes"]))

    def test_llm_wording_with_an_invented_cause_is_dropped(self):
        class L:
            def complete(self, *a, **k): return "Weight fell sharply because of a supplier strike in September."
            def complete_json(self, *a, **k): raise RuntimeError("planner down")
        out = self.ask("Why did weight drop in the latest month?", L())
        self.assertEqual(out["source"], "rules")
        self.assertNotIn("strike", out["answer"])

    def test_evidence_retrieval_has_no_documents_dependency(self):
        items = ev.retrieve("weight by karigar", _tables())
        self.assertTrue(items)
        self.assertTrue(all(i["source"] == "data" for i in items))


def _tables():
    import pandas as pd
    from app.services import dataset_profiler as prof
    import io
    df = pd.read_csv(io.StringIO(CSV), dtype=str, keep_default_na=False)
    summary, cols = prof.profile_table("a.csv", 1, df)
    summary["roles"] = {c: v["role"] for c, v in cols.items()}
    return {"a.csv": (summary, pd.DataFrame({c: v["series"] for c, v in cols.items()}))}


if __name__ == "__main__":
    unittest.main()
