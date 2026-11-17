import sys
import tempfile
import unittest
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.config import Config  # noqa: E402
from app.errors import ForbiddenError, ValidationError  # noqa: E402
from app.services import analyst_chat_service as acs  # noqa: E402
from app.services.analyst_chat_service import AnalystChatService  # noqa: E402
from tests.test_analyst_dashboard import CSV, FakeDB  # noqa: E402


class FakeLLM:
    """Scripted LLM: returns queued JSON objects in order (plan first, then answer)."""

    def __init__(self, *outs):
        self.outs = list(outs)

    def complete_json(self, system, user, max_tokens=0):
        o = self.outs.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class ChatTests(unittest.TestCase):
    def setUp(self):
        acs._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(CSV, encoding="utf-8")
        self.file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                     "sha256": "abc", "graph_version_number": 3}
        self.wf = {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": None, "selected_blocks": None}

    def tearDown(self):
        self.tmp.cleanup()

    def svc(self, llm=None, final=3):
        wf = dict(self.wf, finalized_version_number=final)
        return AnalystChatService(FakeDB(wf, [self.file]), Config(UPLOAD_DIR=self.tmp.name), llm)

    def ask(self, q, llm=None, **kw):
        return self.svc(llm, **kw).ask(2, "data_analyst", 5, q)

    # ---- rule planner (no LLM configured)
    def test_total_is_computed_from_data(self):
        out = self.ask("What is the total weight?")
        self.assertFalse(out["unanswerable"])
        self.assertEqual(out["table"]["rows"][0][0], 58.0)
        self.assertIn("58", out["answer"])

    def test_top_n_by_measure_with_chart(self):
        out = self.ask("top 2 karigar name by weight")
        self.assertEqual([r[0] for r in out["table"]["rows"]], ["Bhagya", "Laxmi"])  # 30.5 vs 20.0
        self.assertEqual(out["table"]["rows"][0][1], 30.5)
        self.assertEqual(out["chart"]["type"], "bar")

    def test_record_count(self):
        self.assertEqual(self.ask("how many records")["table"]["rows"][0][0], 5)

    def test_unanswerable_gives_suggestions(self):
        out = self.ask("What is the weather tomorrow?")
        self.assertTrue(out["unanswerable"])
        self.assertTrue(out["follow_ups"])

    # ---- LLM planner is untrusted
    def test_llm_plan_with_filter_and_group(self):
        plan = {"table": "a.csv", "filters": [{"column": "Status", "op": "=", "value": "ok"}],
                "group_by": ["Karigar Name"], "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}],
                "order_by": {"by": "w", "desc": True}, "limit": 5}
        out = self.ask("weight of OK items per karigar", FakeLLM(plan, {"answer": "Bhagya has 30.5.", "follow_ups": ["x?"]}))
        rows = {r[0]: r[1] for r in out["table"]["rows"]}
        self.assertEqual(rows, {"Bhagya": 30.5, "Laxmi": 5.0, "Ravi": 7.5})  # the Rework row (15) is filtered out
        self.assertEqual(out["source"], "llm")

    def test_llm_unknown_column_falls_back_to_rules(self):
        bad = {"table": "a.csv", "metrics": [{"fn": "sum", "column": "Salary; DROP TABLE users", "as": "x"}]}
        out = self.ask("total weight", FakeLLM(bad))
        self.assertEqual(out["table"]["rows"][0][0], 58.0)

    def test_sum_on_non_numeric_rejected(self):
        s = self.svc()
        tables = s._tables(s.dash.resolve_model(2, "data_analyst", 5)[0])
        with self.assertRaises(ValidationError):
            s.validate({"table": "a.csv", "metrics": [{"fn": "sum", "column": "Karigar Name"}]}, tables)

    def test_invented_number_in_wording_is_replaced(self):
        plan = {"table": "a.csv", "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}]}
        out = self.ask("total weight", FakeLLM(plan, {"answer": "Total weight is 99999.", "follow_ups": []}))
        self.assertNotIn("99999", out["answer"])
        self.assertIn("58", out["answer"])

    def test_llm_down_still_answers(self):
        from app.llm import LLMError
        out = self.ask("total weight", FakeLLM(LLMError("down")))
        self.assertEqual(out["table"]["rows"][0][0], 58.0)

    def test_llm_unanswerable_passthrough(self):
        out = self.ask("who won the cup", FakeLLM({"unanswerable": "not in the data"}))
        self.assertTrue(out["unanswerable"])

    # ---- access and input
    def test_draft_model_forbidden_for_analyst(self):
        with self.assertRaises(ForbiddenError):
            self.ask("total weight", final=None)

    def test_empty_and_long_questions_rejected(self):
        with self.assertRaises(ValidationError):
            self.ask("   ")
        with self.assertRaises(ValidationError):
            self.ask("x" * 600)

    def test_ranking_never_sorts_by_category_label(self):
        """Regression: LLM said order_by region name, so 'highest' returned the alphabetically last group."""
        plan = {"table": "a.csv", "group_by": ["Karigar Name"], "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}],
                "order_by": {"by": "Karigar Name", "desc": True}, "limit": 1}
        out = self.ask("which karigar has the highest weight", FakeLLM(plan, {"answer": "ok", "follow_ups": []}))
        self.assertEqual(out["table"]["rows"][0][:2], ["Bhagya", 30.5])  # not 'Ravi' (alphabetically last)

    def test_every_suggested_followup_is_answerable(self):
        """Suggestions are generated from the schema, so clicking one must never dead-end."""
        out = self.ask("total weight")
        self.assertTrue(out["follow_ups"])
        for q in out["follow_ups"]:
            self.assertFalse(self.ask(q)["unanswerable"], q)

    def test_llm_invented_filter_is_dropped(self):
        """Regression (real Mistral output): plan filtered Status='OK' though the question never mentions it."""
        plan = {"table": "a.csv", "filters": [{"column": "Status", "op": "=", "value": "OK"}],
                "group_by": ["Karigar Name"], "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}],
                "order_by": {"by": "w", "desc": True}, "limit": 1}
        out = self.ask("who contributes the most overall", FakeLLM(plan))
        self.assertEqual(out["table"]["rows"][0][:2], ["Bhagya", 30.5])
        self.assertTrue(out["notes"])

    def test_direction_comes_from_the_question_not_the_model(self):
        """Regression: model sent desc=true for 'lowest', so the highest group was reported as the lowest."""
        plan = {"table": "a.csv", "group_by": ["Karigar Name"], "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}],
                "order_by": {"by": "w", "desc": True}, "limit": 1}
        out = self.ask("which karigar delivered the lowest weight overall", FakeLLM(plan))
        self.assertEqual(out["table"]["rows"][0][0], "Ravi")  # 7.5 is the smallest total

    def test_which_question_without_grouping_is_refused_not_guessed(self):
        plan = {"table": "a.csv", "metrics": [{"fn": "avg", "column": "Purity", "as": "p"}]}
        out = self.ask("which karigar delivered something odd", FakeLLM(plan))
        self.assertTrue(out["unanswerable"])

    def test_which_has_the_lowest_via_rules(self):
        out = self.ask("which karigar name has the lowest weight")
        self.assertEqual(out["table"]["rows"][0][0], "Ravi")
        self.assertIn("lowest", out["answer"])

    def test_date_group_is_iso_and_chronological(self):
        plan = {"table": "a.csv", "group_by": ["Receipt Date"], "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}],
                "order_by": {"by": "Receipt Date", "desc": True}}
        out = self.ask("weight by date", FakeLLM(plan, {"answer": "ok", "follow_ups": []}))
        self.assertEqual(out["table"]["rows"][0][0], "2026-07-03")
        self.assertEqual(out["chart"]["type"], "line")

    def test_rule_planner_matches_partial_column_names(self):
        self.assertEqual(self.ask("what is the total purity")["table"]["rows"][0][0], 441.1)

    def test_month_bucket(self):
        plan = {"table": "a.csv", "group_by": [{"column": "Receipt Date", "bucket": "month"}],
                "metrics": [{"fn": "sum", "column": "Weight", "as": "w"}]}
        out = self.ask("weight per month", FakeLLM(plan, {"answer": "ok", "follow_ups": []}))
        self.assertEqual([r[0] for r in out["table"]["rows"]], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(out["chart"]["type"], "line")


if __name__ == "__main__":
    unittest.main()
