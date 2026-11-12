import unittest
from pathlib import Path
import sys

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.graph_ops import GraphState, diff_graphs
from app.services.graph_service import GraphService
from app.services.impact_service import build_proposal_context, enrich_diff_business_impact


def _make_node(key, name, **kw):
    base = {
        "key": key,
        "name": name,
        "kind": kw.get("kind", "core"),
        "origin": kw.get("origin", "reference"),
        "group_name": kw.get("group_name", ""),
        "color": kw.get("color", "#10b981"),
        "definition": kw.get("definition", ""),
        "attributes": kw.get("attributes", []),
        "formulas": kw.get("formulas", []),
        "notes": kw.get("notes", ""),
        "evidence": kw.get("evidence", []),
    }
    return base


class ImpactServiceTests(unittest.TestCase):
    def test_diff_and_dynamic_impact_for_modified_added_removed(self):
        sa = GraphState.from_lists(
            [
                _make_node(
                    "costing-allocation",
                    "CostingAllocationModel",
                    group_name="Activity-Based Costing & Allocation",
                    definition=(
                        "The formal costing machinery beneath profitability: technique choice "
                        "(standard · marginal vs absorption), the cost build-up ladder "
                        "Prime Cost → Works Cost → COGM → COGS → Cost of Sale, and absorption mechanics."
                    ),
                    attributes=["techniqueChoice"],
                    formulas=["FOAR = budgeted overhead / normal activity"],
                ),
                _make_node(
                    "profitability-record",
                    "ProfitabilityRecord",
                    group_name="Margin Analytics",
                    definition="Records unit profitability by SKU and channel.",
                ),
                _make_node(
                    "legacy-variance-check",
                    "LegacyVarianceAudit",
                    group_name="Internal Controls",
                    definition="Audits legacy standard cost variances before close.",
                    attributes=["varianceThresholdPct"],
                    formulas=["Variance = Actual - Standard"],
                ),
            ],
            [
                {"source": "costing-allocation", "target": "profitability-record", "label": "feeds unit cost"},
                {"source": "legacy-variance-check", "target": "profitability-record", "label": "validates"},
            ],
        )

        sb = GraphState.from_lists(
            [
                _make_node(
                    "costing-allocation",
                    "CostingAllocationModel",
                    group_name="Activity-Based Costing & Allocation",
                    definition=(
                        "The formal costing machinery beneath profitability: technique choice "
                        "(standard · marginal vs absorption), the cost build-up ladder "
                        "Direct Costs → Indirect Costs → Overhead Costs → Final Cost Allocation, and absorption mechanics."
                    ),
                    attributes=[
                        "techniqueChoice",
                        "costBuildUpLadder[Direct Costs → Indirect Costs → Overhead Costs → Final Cost Allocation]",
                    ],
                    formulas=[
                        "FOAR = budgeted overhead / normal activity",
                        "Final Allocation = Direct Costs + Indirect Costs + Absorbed Overhead",
                    ],
                ),
                _make_node(
                    "profitability-record",
                    "ProfitabilityRecord",
                    group_name="Margin Analytics",
                    definition="Records unit profitability by SKU and channel.",
                ),
                _make_node(
                    "channel-rebate-accrual",
                    "ChannelRebateAccrual",
                    kind="entity",
                    origin="user",
                    group_name="Client-specific entities",
                    definition="Tracks off-invoice promotional rebates and trade discounts per distributor tier.",
                    attributes=["rebateTierPct", "promoAccrualWindow"],
                    formulas=["Net Revenue = Gross Sales - ChannelRebateAccrual"],
                ),
            ],
            [
                {"source": "costing-allocation", "target": "profitability-record", "label": "feeds unit cost"},
                {"source": "channel-rebate-accrual", "target": "profitability-record", "label": "adjusts net margin"},
            ],
        )

        prop_ctx = build_proposal_context([
            (
                {"status": "approved", "source": "chat"},
                [
                    {
                        "status": "approved",
                        "op": "update_node",
                        "payload": {"node_key": "costing-allocation"},
                        "rationale": "Finance controller standardized plant allocation stages into Direct -> Indirect -> Overhead -> Final.",
                        "description": "Updated CostingAllocationModel ladder",
                    }
                ],
            )
        ])

        diff = diff_graphs(sa, sb)
        enriched = enrich_diff_business_impact(
            diff,
            sa,
            sb,
            domain={"name": "F3 Cost and Profitability Management", "slug": "cost-profitability-management"},
            proposal_context=prop_ctx,
        )

        # Modified assertion
        mod = enriched["nodes_modified"][0]
        self.assertEqual(
            mod["formulas_added"],
            ["Final Allocation = Direct Costs + Indirect Costs + Absorbed Overhead"],
        )
        self.assertEqual(len(mod["downstream"]), 1)
        self.assertEqual(mod["downstream"][0]["name"], "ProfitabilityRecord")

        bi_mod = mod["business_impact"]
        self.assertEqual(bi_mod["severity"], "high")
        self.assertGreaterEqual(len(bi_mod["pillars"]), 5)
        self.assertIn("Direct Costs", bi_mod["operational_effect"])
        self.assertIn("Prime Cost", bi_mod["operational_effect"])
        self.assertIn("ProfitabilityRecord", bi_mod["downstream_ripple"])
        self.assertIn("Finance controller standardized plant allocation stages", bi_mod["rationale"])
        self.assertTrue(bi_mod["missing_from_baseline"]["has_missing"])
        self.assertIn("Prime Cost", bi_mod["missing_from_baseline"]["missing_items"][0])
        self.assertTrue(len(bi_mod["suggestions"]["action_steps"]) >= 2)

        # Added assertion
        add = enriched["nodes_added"][0]
        bi_add = add["business_impact"]
        self.assertIn("ChannelRebateAccrual", bi_add["operational_effect"])
        self.assertIn("ProfitabilityRecord", bi_add["downstream_ripple"])
        self.assertIn("Rebate Tier Pct", bi_add["operational_effect"])
        self.assertTrue(len(bi_add["suggestions"]["action_steps"]) >= 2)

        # Removed assertion
        rem = enriched["nodes_removed"][0]
        bi_rem = rem["business_impact"]
        self.assertEqual(bi_rem["tone"], "red")
        self.assertEqual(bi_rem["severity"], "high")
        self.assertIn("ProfitabilityRecord", bi_rem["downstream_ripple"])
        self.assertIn("Omission & audit check", bi_rem["governance_risk"])
        self.assertTrue(bi_rem["missing_from_baseline"]["has_missing"])
        self.assertIn("ProfitabilityRecord", bi_rem["missing_from_baseline"]["business_consequences"])
        self.assertTrue(len(bi_rem["suggestions"]["action_steps"]) >= 2)

        # Removed relationship assertion
        rem_edge = enriched["edges_removed"][0]
        self.assertEqual(rem_edge["source_name"], "LegacyVarianceAudit")
        self.assertEqual(rem_edge["target_name"], "ProfitabilityRecord")
        self.assertIn("missing in the Current Model", rem_edge["business_impact"]["impact"])

    def test_graph_service_compare_returns_enriched_impact(self):
        sa = GraphState.from_lists(
            [_make_node("budget-ctrl", "BudgetControl", definition="Annual static budget.")],
            [],
        )
        sb = GraphState.from_lists(
            [
                _make_node(
                    "budget-ctrl",
                    "BudgetControl",
                    definition="Rolling zero-based budget with variance thresholds.",
                    attributes=["zbbVarianceThreshold"],
                )
            ],
            [],
        )

        class StubProposals:
            def list(self, user_id, wf_id):
                return []

            def items(self, pid):
                return []

        gs = object.__new__(GraphService)
        gs.proposals = StubProposals()
        gs.state_of = lambda user_id, wf, domain, num: sa if num == 0 else sb

        res = gs.compare(1, {"id": 9}, {"name": "F4 Planning & Budgeting", "slug": "planning-budgeting"}, 0, 1)
        self.assertIn("business_impact", res["nodes_modified"][0])
        self.assertIn("ZBB Variance Threshold", res["nodes_modified"][0]["business_impact"]["operational_effect"])

    def test_finalization_preview_and_definition_delta_preservation(self):
        sa = GraphState.from_lists(
            [_make_node("cost-center", "CostCenter", definition="Tracks cost allocations across plant.")],
            [],
        )
        sb = GraphState.from_lists(
            [_make_node("cost-center", "CostCenter", definition="Tracks activity drivers across plant.")],
            [],
        )

        class StubGraphs:
            def latest(self, uid, wfid):
                return {"id": 5, "version_number": 1, "label": "KG1", "kind": "chat", "status": "draft",
                        "summary": "s", "created_at": "2026-11-12", "parent_version_number": 0, "proposal_id": 2}

            def versions(self, uid, wfid):
                return [self.latest(uid, wfid)]

            def version(self, uid, wfid, num):
                return self.latest(uid, wfid)

        class StubProposals:
            def list(self, uid, wfid):
                return [{"id": 2, "title": "Tune CostCenter", "source": "chat", "status": "approved",
                         "created_at": "2026-11-12", "decided_at": "2026-11-12", "result_version_number": 1}]

            def items(self, pid):
                return [{"op": "update_node", "description": "Updated definition", "rationale": "Match ERP feed",
                         "status": "approved", "payload": {"node_key": "cost-center"}}]

        class StubRef:
            def core_keys(self, slug):
                return ["cost-center"]

        gs = object.__new__(GraphService)
        gs.graphs = StubGraphs()
        gs.proposals = StubProposals()
        gs.ref = StubRef()
        gs._baseline = lambda domain, wf: sa
        gs.working = lambda uid, wf, domain: (1, sb)

        prev = gs.finalization_preview(1, {"id": 9, "finalized_version_number": None}, {"name": "F3 Cost", "slug": "f3"})
        bi = prev["comparison"]["nodes_modified"][0]["business_impact"]
        self.assertIn("cost allocations", bi["operational_effect"])
        self.assertIn("activity drivers", bi["operational_effect"])
        self.assertNotIn("across plant", bi["operational_effect"].split("Recalibrated")[1].split(".")[0])
        self.assertIn("Match ERP feed", bi["rationale"])

    def test_db_operational_error_returns_503_and_pool_reuses_healthy_conn(self):
        from flask import Flask
        import pymysql
        from app.config import Config
        from app.db import Session, _get_pool
        from app.errors import register_error_handlers

        app = Flask(__name__)
        register_error_handlers(app)

        @app.route("/boom")
        def _boom():
            raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server (timed out)")

        res = app.test_client().get("/boom")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["error"]["code"], "database_unavailable")

        class FakeConn:
            def __init__(self):
                self.closed = False
                self.autocommit_calls = []

            def autocommit(self, val):
                self.autocommit_calls.append(val)

            def close(self):
                self.closed = True

        key = ("test-host", 3306, "u", "db")
        pool = _get_pool(key)
        while not pool.empty():
            pool.get_nowait()

        healthy = FakeConn()
        s1 = Session(healthy, pool_key=key)
        s1.close(exc=None)
        self.assertFalse(healthy.closed)
        self.assertIs(pool.get_nowait(), healthy)

        broken = FakeConn()
        s2 = Session(broken, pool_key=key)
        s2._broken = True
        s2.close(exc=None)
        self.assertTrue(broken.closed)

        cfg = Config.from_env()
        self.assertTrue(bool(cfg.DB_HOST))


if __name__ == "__main__":
    unittest.main()
