"""HTTP-level check of the metric review endpoints: real routes, auth and role gates."""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

spec = importlib.util.spec_from_file_location("afx_app_entry_review", BE_ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
from app.security import issue_token  # noqa: E402
from app.services import analyst_chat_service as acs  # noqa: E402
from app.services import kgraph_builder_service as kb  # noqa: E402
from app.services.analyst_chat_service import AnalystChatService  # noqa: E402
from tests.test_graphrag_phase3 import SALES, FakeArango, GraphDB  # noqa: E402


class ApiGraphDB(GraphDB):
    def one(self, sql, params=()):
        if "FROM revoked_tokens" in sql:
            return None
        if "SELECT role FROM users" in sql:
            return {"role": self.role_by_id[int(params[0])]}
        return super().one(sql, params)

    def insert(self, *a, **k):
        return 1


class MetricReviewApiTests(unittest.TestCase):
    def setUp(self):
        acs._CACHE.clear()
        kb._MEM.clear()
        kb._ARANGO_DOWN_UNTIL = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(SALES, encoding="utf-8")
        file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                "sha256": "abc", "graph_version_number": 3}
        wf = {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": datetime(2026, 11, 1), "selected_blocks": None}
        self.db = ApiGraphDB(wf, [file])
        self.db.role_by_id = {2: "data_analyst", 7: "data_architect", 8: "data_architect"}
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.app.extensions["llm"] = None
        self.app.config["APP_CONFIG"].UPLOAD_DIR = self.tmp.name
        self.client = self.app.test_client()
        cfg = self.app.config["APP_CONFIG"]
        svc = AnalystChatService(self.db, cfg, None)
        w, _ = svc.dash.resolve_model(7, "data_architect", 5)
        files, _ = svc.dash._files(w)
        kb.KGraphBuilderService(self.db, cfg, None, FakeArango()).build(5, svc._tables(w), svc.dash._fingerprint(w, files))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None, uid=None, role=None):
        h = {}
        with self.app.app_context():
            if uid:
                h["Authorization"] = f"Bearer {issue_token(uid, role)[0]}"
        with patch("controller._helpers.get_db", return_value=self.db), patch("controller.analyst_controller.get_db", return_value=self.db), \
                patch("app.security.get_db", return_value=self.db):
            return getattr(self.client, method)(path, json=body, headers=h) if method == "post" else self.client.get(path, headers=h)

    URL = "/api/published/5/graph/metrics"

    def test_requires_login(self):
        self.assertEqual(self.call("get", self.URL).status_code, 401)
        self.assertEqual(self.call("post", self.URL + "/review", {"name": "Gross Margin", "decision": "approve"}).status_code, 401)

    def test_architect_sees_proposed_metrics_and_can_review(self):
        r = self.call("get", self.URL, uid=7, role="data_architect")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual((body["state"], body["can_review"]), ("ready", True))
        self.assertTrue(all(m["status"] == "proposed" for m in body["metrics"]))

    def test_analyst_can_look_but_not_review(self):
        self.assertFalse(self.call("get", self.URL, uid=2, role="data_analyst").get_json()["can_review"])
        r = self.call("post", self.URL + "/review", {"name": "Gross Margin", "decision": "approve"}, 2, "data_analyst")
        self.assertEqual(r.status_code, 403)

    def test_a_different_architect_cannot_review_someone_elses_model(self):
        r = self.call("post", self.URL + "/review", {"name": "Gross Margin", "decision": "approve"}, 8, "data_architect")
        self.assertEqual(r.status_code, 403)

    def test_approval_flows_through_to_the_analyst_chat(self):
        ask = lambda: self.call("post", "/api/published/5/chat", {"question": "What is the gross margin?"}, 2, "data_analyst").get_json()  # noqa: E731
        self.assertTrue(ask()["unanswerable"])
        r = self.call("post", self.URL + "/review", {"name": "Gross Margin", "decision": "approve"}, 7, "data_architect")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(next(m for m in r.get_json()["metrics"] if m["name"] == "Gross Margin")["status"], "approved")
        out = ask()
        self.assertIn("38.9%", out["answer"])
        self.assertEqual(out["source"], "graph")

    def test_pending_metric_does_not_block_the_computable_part(self):
        out = self.call("post", "/api/published/5/chat", {"question": "What is the gross margin and the total gross sales by region?"}, 2, "data_analyst").get_json()
        self.assertFalse(out["unanswerable"])
        self.assertIn("East", out["answer"])
        self.assertIn("not approved", out["answer"])
        self.assertEqual(out["pending_metric"]["name"], "Gross Margin")

    def test_validation_errors(self):
        self.assertEqual(self.call("post", self.URL + "/review", {"name": "Gross Margin", "decision": "maybe"}, 7, "data_architect").status_code, 422)
        self.assertEqual(self.call("post", self.URL + "/review", {"name": "Nope", "decision": "approve"}, 7, "data_architect").status_code, 404)


if __name__ == "__main__":
    unittest.main()
