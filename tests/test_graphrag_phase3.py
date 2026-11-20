import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

import pandas as pd  # noqa: E402

from app.config import Config  # noqa: E402
from app.services import analyst_chat_service as acs  # noqa: E402
from app.services import dataset_profiler as prof  # noqa: E402
from app.services import executive_synthesizer as synth  # noqa: E402
from app.services import kgraph_builder_service as kb  # noqa: E402
from app.services import kgraph_verifier as ver  # noqa: E402
from app.services.kgraph_review_service import MetricReviewService  # noqa: E402
from app.services.analyst_chat_service import AnalystChatService  # noqa: E402
from tests.test_analyst_dashboard import FakeDB  # noqa: E402

SALES = (
    "Region,Country,City,Order Date,Gross Sales,COGS,Discount,Quantity\n"
    "East,India,Kolkata,2026-01-05,1000,600,50,10\n"
    "East,India,Kolkata,2026-01-20,500,300,20,5\n"
    "East,India,Patna,2026-02-03,800,500,40,8\n"
    "West,India,Mumbai,2026-02-14,1200,800,60,12\n"
    "West,India,Pune,2026-03-01,600,300,30,6\n"
    "North,Nepal,Kathmandu,2026-03-10,400,250,10,4\n"
)


def typed(csv, name="a.csv"):
    df = pd.read_csv(io.StringIO(csv), dtype=str, keep_default_na=False)
    summary, cols = prof.profile_table(name, 1, df)
    summary["roles"] = {c: v["role"] for c, v in cols.items()}
    return name, (summary, pd.DataFrame({c: v["series"] for c, v in cols.items()}))


def tables_of(*pairs):
    return dict(pairs)


class VerifierTests(unittest.TestCase):
    def test_join_is_accepted_only_when_values_really_match(self):
        a = typed("Region,Sales\nEast,1\nWest,2\nNorth,3\nSouth,4\n", "sales.csv")
        b = typed("Region,Target\nEast,10\nWest,20\nNorth,30\nSouth,40\nEast,11\n", "targets.csv")
        c = typed("Zone,Target\nAlpha,1\nBeta,2\nGamma,3\n", "other.csv")
        t = tables_of(a, b, c)
        ev, why = ver.verify_edge(t, {"from_table": "sales.csv", "from_column": "Region", "to_table": "targets.csv", "to_column": "Region"})
        self.assertIsNone(why)
        self.assertEqual(ev["overlap"], 1.0)
        self.assertEqual(ev["cardinality"], "one-to-many")
        _, why = ver.verify_edge(t, {"from_table": "sales.csv", "from_column": "Region", "to_table": "other.csv", "to_column": "Zone"})
        self.assertIn("0% of values match", why)                       # a plausible-looking but false join is rejected
        _, why = ver.verify_edge(t, {"from_table": "sales.csv", "from_column": "Region", "to_table": "sales.csv", "to_column": "Region"})
        self.assertIn("different tables", why)
        _, why = ver.verify_edge(t, {"from_table": "sales.csv", "from_column": "Nope", "to_table": "targets.csv", "to_column": "Region"})
        self.assertIn("unknown column", why)

    def test_hierarchy_is_discovered_from_the_data_and_bad_ones_rejected(self):
        t = tables_of(typed(SALES))
        found = ver.discover_hierarchies(t)
        self.assertEqual(found, [{"table": "a.csv", "levels": ["Country", "Region", "City"]}])
        ev, why = ver.verify_hierarchy(t, "a.csv", ["Country", "Region", "City"])
        self.assertIsNone(why)
        self.assertEqual(ev["steps"][0]["consistency"], 1.0)
        _, why = ver.verify_hierarchy(t, "a.csv", ["City", "Country"])   # reversed: a country is not inside a city
        self.assertIn("more distinct values", why)

    def test_inconsistent_hierarchy_is_rejected(self):
        # Kolkata appears under two regions: City does not belong to a single Region
        bad = typed("Region,City\nEast,Kolkata\nWest,Kolkata\nEast,Patna\nWest,Pune\nNorth,Delhi\nNorth,Agra\nSouth,Chennai\nSouth,Madurai\n")
        _, why = ver.verify_hierarchy(tables_of(bad), "a.csv", ["Region", "City"])
        self.assertIn("does not belong to a single Region", why)

    def test_formula_evaluator_is_restricted_arithmetic_only(self):
        v = {"A": 10.0, "B": 4.0}
        self.assertEqual(ver.evaluate("([A] - [B]) / [A]", v), 0.6)
        for bad in ["__import__('os').system('echo x')", "[A] ** 2", "open('x')", "[A] if 1 else [B]", "[A] and [B]", "[A] / ([B] - [B])", "[Z] + 1", "1 +"]:
            with self.assertRaises(ver.FormulaError, msg=bad):
                ver.evaluate(bad, v)

    def test_metric_needs_real_numeric_columns(self):
        t = tables_of(typed(SALES))
        ok, _ = ver.verify_metric(t, {"name": "Margin", "formula": "([Gross Sales] - [COGS]) / [Gross Sales]"})
        self.assertAlmostEqual(ok["value"], (4500 - 2750) / 4500, places=5)
        for f in ["[Gross Sales] / [Missing]", "[Region] / [COGS]", "[Gross Sales] / ([COGS] - [COGS])", "5 + 5"]:
            m, why = ver.verify_metric(t, {"name": "X", "formula": f})
            self.assertIsNone(m, f)
            self.assertTrue(why)

    def test_percent_columns_are_averaged_not_summed_in_formulas(self):
        t = tables_of(typed("Region,Fee_pct,Sales\nA,10,100\nB,20,200\nC,30,300\n"))
        ok, _ = ver.verify_metric(t, {"name": "Fee amount", "formula": "[Fee_pct] * 100"})
        self.assertEqual(ok["value"], 2000.0)                              # mean(10,20,30) * 100, not 60 * 100

    def test_ambiguous_synonyms_are_dropped(self):
        t = tables_of(typed(SALES))
        kept, rej = ver.verify_synonyms(t, [{"term": "sales", "column": "Gross Sales"}, {"term": "cost", "column": "COGS"},
                                            {"term": "cost", "column": "Discount"}, {"term": "ghost", "column": "Nope"}])
        self.assertEqual([k["term"] for k in kept], ["sales"])
        self.assertEqual(len(rej), 2)


class FakeArango:
    def __init__(self, fail=False):
        self.fail, self.calls, self.docs = fail, [], {}

    def ensure_collection(self, name, is_edge=False):
        if self.fail:
            raise ConnectionError("arango down")
        self.calls.append(("ensure", name, is_edge))

    def query(self, aql, bind=None):
        if self.fail:
            raise ConnectionError("arango down")
        self.calls.append(("query", aql, bind))
        if aql.startswith("FOR d IN data_kg_meta FILTER d._key"):
            d = self.docs.get(("data_kg_meta", bind["k"]))
            return [d] if d else []
        return []

    def import_documents(self, coll, docs, overwrite=True):
        if self.fail:
            raise ConnectionError("arango down")
        self.calls.append(("import", coll, len(docs)))
        for d in docs:
            self.docs[(coll, d["_key"])] = d
        return len(docs)


class GraphDB(FakeDB):
    """FakeDB + the data_knowledge_graphs and kg_metric_reviews tables."""
    def __init__(self, wf, files):
        super().__init__(wf, files)
        self.kg, self.reviews = {}, {}

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO data_knowledge_graphs"):
            self.kg[params[0]] = (params[1], params[2])
        elif sql.startswith("INSERT INTO kg_metric_reviews"):
            self.reviews[(params[0], params[1], params[2])] = params[3]
        elif sql.startswith("DELETE FROM kg_metric_reviews"):
            self.reviews.pop((params[0], params[1], params[2]), None)

    def query(self, sql, params=()):
        if "FROM kg_metric_reviews" in sql:
            return [{"name_key": k[1], "formula_hash": k[2], "decision": d} for k, d in self.reviews.items() if k[0] == params[0]]
        return super().query(sql, params)

    def one(self, sql, params=()):
        if "FROM data_knowledge_graphs" in sql:
            r = self.kg.get(params[0])
            return {"fingerprint": r[0], "graph": r[1]} if r else None
        return super().one(sql, params)


class ScriptedLLM:
    def __init__(self, out): self.out = out
    def complete_json(self, *a, **k): return self.out


PROPOSAL = {
    "edges": [{"from_table": "a.csv", "from_column": "Region", "to_table": "a.csv", "to_column": "Country"}],   # same table: invalid
    "synonyms": [{"term": "turnover", "column": "Gross Sales"}, {"term": "revenue stream", "column": "Nope"}],
    "metrics": [
        {"name": "Net Sales", "formula": "[Gross Sales] - [Discount]", "description": "after discount"},
        {"name": "Bogus", "formula": "[Gross Sales] / [Missing]"},
        {"name": "Evil", "formula": "__import__('os').system('x')"},
        {"name": "Div", "formula": "[Gross Sales] / ([COGS] - [COGS])"},
    ],
}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        kb._MEM.clear()
        kb._ARANGO_DOWN_UNTIL = 0.0
        self.tables = tables_of(typed(SALES))
        self.db = GraphDB({}, [])

    def builder(self, llm=None, arango=None):
        return kb.KGraphBuilderService(self.db, Config(), llm, arango or FakeArango())

    def test_only_verified_claims_survive_and_rejections_are_recorded(self):
        g = self.builder(ScriptedLLM(PROPOSAL)).build(5, self.tables, "fp1")
        names = {m["name"] for m in g["metrics"]}
        self.assertEqual(names, {"Net Sales", "Gross Margin", "Discount Rate", "Average Revenue per Unit"})   # LLM + deterministic, all verified
        self.assertEqual({m["name"]: m["value"] for m in g["metrics"]}["Net Sales"], 4290.0)
        self.assertEqual({s["term"] for s in g["synonyms"]} >= {"turnover"}, True)
        rej = {(r["kind"], r["item"].get("name") or r["item"].get("term") or r["item"].get("from_column")): r["reason"] for r in g["rejected"]}
        self.assertIn(("metric", "Bogus"), rej)
        self.assertIn(("metric", "Evil"), rej)
        self.assertIn(("metric", "Div"), rej)
        self.assertIn("division by zero", rej[("metric", "Div")])
        self.assertIn(("edge", "Region"), rej)
        self.assertEqual(g["generated_by"], "llm+data")
        self.assertEqual(g["hierarchies"][0]["levels"], ["Country", "Region", "City"])

    def test_works_without_an_llm_from_the_data_alone(self):
        g = self.builder(None).build(5, self.tables, "fp1")
        self.assertEqual(g["generated_by"], "data")
        self.assertTrue({"Gross Margin", "Discount Rate"} <= {m["name"] for m in g["metrics"]})

    def test_llm_returning_garbage_does_not_break_the_build(self):
        class Bad:
            def complete_json(self, *a, **k): raise RuntimeError("prose, not json")
        self.assertEqual(self.builder(Bad()).build(5, self.tables, "fp1")["generated_by"], "data")

    def test_arango_documents_are_scoped_to_the_workflow(self):
        ar = FakeArango()
        g = self.builder(None, ar).build(5, self.tables, "fp1")
        nodes, edges = kb.KGraphBuilderService.arango_docs(g)
        self.assertTrue(all(d["workflow_id"] == 5 and d["_key"].startswith("wf5_") for d in nodes + edges))
        self.assertTrue(all(e["_from"].startswith("data_kg_nodes/wf5_") for e in edges))
        removes = [c for c in ar.calls if c[0] == "query" and "REMOVE" in c[1]]
        self.assertEqual(len(removes), 3)
        self.assertTrue(all("FILTER d.workflow_id == @wf" in c[1] and c[2] == {"wf": 5} for c in removes))   # never touches another tenant
        kinds = {e["type"] for e in edges}
        self.assertTrue({"has_measure", "has_dimension", "rolls_up_to", "synonym_of", "derived_from"} <= kinds)

    def test_arango_down_keeps_the_graph_in_mysql_and_chat_still_loads_it(self):
        g = self.builder(None, FakeArango(fail=True)).build(5, self.tables, "fp1")
        self.assertEqual(json.loads(self.db.kg[5][1])["fingerprint"], "fp1")
        self.assertTrue(g["metrics"])
        kb._MEM.clear()
        kb._ARANGO_DOWN_UNTIL = 0.0
        loaded = self.builder(None, FakeArango(fail=True)).load(5, "fp1")     # memory empty, Arango down -> MySQL copy
        self.assertEqual(loaded["fingerprint"], "fp1")

    def test_graph_is_read_back_from_arango_and_stale_versions_are_never_returned(self):
        ar = FakeArango()
        self.builder(None, ar).build(5, self.tables, "fp1")
        kb._MEM.clear()
        self.assertEqual(self.builder(None, ar).load(5, "fp1")["workflow_id"], 5)
        kb._MEM.clear()
        self.db.kg.clear()
        self.assertIsNone(self.builder(None, ar).load(5, "fp-NEW"))             # data changed: the old graph must not be used
        self.assertIsNone(self.builder(None, ar).load(6, "fp1"))                # another workflow's graph is never returned


class ChatWithGraphTests(unittest.TestCase):
    def setUp(self):
        acs._CACHE.clear()
        kb._MEM.clear()
        kb._ARANGO_DOWN_UNTIL = 0.0
        synth._FAIL_UNTIL = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(SALES, encoding="utf-8")
        self.cfg = Config(UPLOAD_DIR=self.tmp.name)
        self.file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                     "sha256": "abc", "graph_version_number": 3}
        self.wf = {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": None, "selected_blocks": None}
        self.db = GraphDB(dict(self.wf), [self.file])
        svc = AnalystChatService(self.db, self.cfg, None)
        wf, _ = svc.dash.resolve_model(2, "data_analyst", 5)
        files, _ = svc.dash._files(wf)
        kb.KGraphBuilderService(self.db, self.cfg, ScriptedLLM(PROPOSAL), FakeArango()).build(5, svc._tables(wf), svc.dash._fingerprint(wf, files))
        MetricReviewService(self.db, self.cfg).review(7, "data_architect", 5, all_pending=True, decision="approve")   # the Architect approves the metrics

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, q, graph=True):
        if not graph:
            kb._MEM.clear()
            self.db.kg.clear()
        return AnalystChatService(self.db, self.cfg, None).ask(2, "data_analyst", 5, q)

    def test_named_metric_is_computed_from_its_verified_formula(self):
        out = self.ask("What is the gross margin?")
        self.assertEqual(out["source"], "graph")
        self.assertIn("38.9%", out["answer"])
        self.assertAlmostEqual(out["table"]["rows"][0][0], (4500 - 2750) / 4500, places=5)
        self.assertTrue(any(e["source"] == "graph" and "Gross Sales - COGS" in e["text"] for e in out["evidence"]))

    def test_metric_by_dimension_uses_group_totals_not_averages_of_ratios(self):
        out = self.ask("Show gross margin by region")
        rows = {r[0]: r[1] for r in out["table"]["rows"]}
        self.assertAlmostEqual(rows["East"], (2300 - 1400) / 2300, places=5)
        self.assertAlmostEqual(rows["North"], (400 - 250) / 400, places=5)
        self.assertEqual(out["table"]["rows"][0][0], "East")                    # highest first
        self.assertTrue(any("39.1%" in d for d in out["drivers"]))               # ratios are displayed as percentages

    def test_metric_trend_is_chronological(self):
        out = self.ask("gross margin trend month by month")
        self.assertEqual([r[0] for r in out["table"]["rows"]], ["2026-01", "2026-02", "2026-03"])

    def test_metric_the_llm_proposed_works_when_verified(self):
        out = self.ask("What is the net sales?")
        self.assertEqual(out["source"], "graph")
        self.assertIn("4,290", out["answer"])

    def test_rejected_metric_is_not_available_to_the_chat(self):
        out = self.ask("What is the bogus?")
        self.assertNotEqual(out["source"], "graph")

    def test_verified_synonym_resolves_a_business_word_to_a_column(self):
        out = self.ask("total turnover by region")
        rows = {r[0]: r[1] for r in out["table"]["rows"]}
        self.assertEqual(rows["East"], 2300.0)

    def test_without_a_graph_the_chat_works_exactly_as_before(self):
        out = self.ask("Top 3 region by Gross Sales", graph=False)
        self.assertEqual(out["table"]["rows"][0][:2], ["East", 2300.0])
        self.assertNotEqual(out["source"], "graph")

    def test_why_questions_still_use_the_exact_variance_path(self):
        out = self.ask("Why did gross sales drop in the latest month?")
        self.assertEqual(out["intent"], "HYBRID")
        self.assertNotEqual(out["source"], "graph")


class MetricReviewTests(unittest.TestCase):
    def setUp(self):
        acs._CACHE.clear()
        kb._MEM.clear()
        kb._ARANGO_DOWN_UNTIL = 0.0
        synth._FAIL_UNTIL = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(SALES, encoding="utf-8")
        self.cfg = Config(UPLOAD_DIR=self.tmp.name)
        self.file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                     "sha256": "abc", "graph_version_number": 3}
        self.db = GraphDB({"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": None, "selected_blocks": None}, [self.file])
        svc = AnalystChatService(self.db, self.cfg, None)
        wf, _ = svc.dash.resolve_model(7, "data_architect", 5)
        files, _ = svc.dash._files(wf)
        kb.KGraphBuilderService(self.db, self.cfg, None, FakeArango()).build(5, svc._tables(wf), svc.dash._fingerprint(wf, files))
        self.review = MetricReviewService(self.db, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, q):
        return AnalystChatService(self.db, self.cfg, None).ask(2, "data_analyst", 5, q)

    def status(self, name="Gross Margin"):
        return next(m["status"] for m in self.review.list(7, "data_architect", 5)["metrics"] if m["name"] == name)

    def test_unreviewed_metric_is_not_calculated_and_says_why(self):
        out = self.ask("What is the gross margin?")
        self.assertTrue(out["unanswerable"])
        self.assertEqual(out["pending_metric"]["formula"], "(Gross Sales - COGS) / Gross Sales")
        self.assertNotIn("%", out["answer"])                                # no number is produced before approval
        self.assertEqual(self.status(), "proposed")

    def test_approval_enables_the_metric_and_reset_disables_it_again(self):
        self.review.review(7, "data_architect", 5, "Gross Margin", "approve")
        self.assertIn("38.9%", self.ask("What is the gross margin?")["answer"])
        self.assertEqual(self.status("Discount Rate"), "proposed")          # only the reviewed metric changed
        self.review.review(7, "data_architect", 5, "gross margin", "reset")
        self.assertTrue(self.ask("What is the gross margin?")["unanswerable"])

    def test_rejected_metric_is_treated_as_if_it_did_not_exist(self):
        self.review.review(7, "data_architect", 5, "Gross Margin", "reject")
        out = self.ask("What is the gross margin?")
        self.assertNotIn("pending_metric", out)
        self.assertNotEqual(out["source"], "graph")
        self.assertEqual(self.status(), "rejected")

    def test_approve_all_only_touches_pending_metrics(self):
        self.review.review(7, "data_architect", 5, "Discount Rate", "reject")
        self.review.review(7, "data_architect", 5, all_pending=True, decision="approve")
        self.assertEqual(self.status("Gross Margin"), "approved")
        self.assertEqual(self.status("Discount Rate"), "rejected")           # an earlier rejection is not overridden

    def test_only_the_owning_architect_can_review(self):
        from app.errors import ForbiddenError
        with self.assertRaises(ForbiddenError):
            self.review.review(2, "data_analyst", 5, "Gross Margin", "approve")
        with self.assertRaises(ForbiddenError):
            self.review.review(8, "data_architect", 5, "Gross Margin", "approve")      # a different architect
        self.assertEqual(self.status(), "proposed")

    def test_listing_shows_status_and_whether_the_caller_may_review(self):
        mine, theirs = self.review.list(7, "data_architect", 5), self.review.list(2, "data_analyst", 5)
        self.assertTrue(mine["can_review"])
        self.assertFalse(theirs["can_review"])
        gm = next(m for m in mine["metrics"] if m["name"] == "Gross Margin")
        self.assertEqual((gm["display"], gm["ratio"], gm["status"]), ("38.9%", True, "proposed"))

    def test_changing_a_formula_requires_a_new_review(self):
        self.review.review(7, "data_architect", 5, "Gross Margin", "approve")
        g = kb._MEM[5][1]
        for m in g["metrics"]:
            if m["name"] == "Gross Margin":
                m["formula"] = "([Gross Sales] - [Discount]) / [Gross Sales]"      # same name, different definition
        self.assertEqual(self.status(), "proposed")

    def test_bad_requests_are_refused(self):
        from app.errors import NotFoundError, ValidationError
        with self.assertRaises(NotFoundError):
            self.review.review(7, "data_architect", 5, "Not a metric", "approve")
        with self.assertRaises(ValidationError):
            self.review.review(7, "data_architect", 5, "Gross Margin", "maybe")
        with self.assertRaises(ValidationError):
            self.review.review(7, "data_architect", 5, all_pending=True, decision="reject")


if __name__ == "__main__":
    unittest.main()
