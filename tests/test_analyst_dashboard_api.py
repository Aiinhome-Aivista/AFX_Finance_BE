"""HTTP-level check: the real route + auth + role handling for GET /api/published/<wf>/dashboard."""
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

spec = importlib.util.spec_from_file_location("afx_app_entry_dash", BE_ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
from app.security import issue_token  # noqa: E402
from tests.test_analyst_dashboard import CSV, FakeDB as _DashDB  # noqa: E402


class ApiDB(_DashDB):
    def one(self, sql, params=()):
        if "FROM revoked_tokens" in sql:
            return None
        if "SELECT role FROM users" in sql:
            return {"role": self.role_by_id[int(params[0])]}
        return super().one(sql, params)

    def execute(self, *a, **k):
        return 0

    def insert(self, *a, **k):
        return 1


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "7").mkdir()
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(CSV, encoding="utf-8")
        file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                "sha256": "abc", "graph_version_number": 3}
        wf = {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": 3, "finalized_at": datetime(2026, 11, 1),
              "selected_blocks": None}
        self.db = ApiDB(wf, [file])
        self.db.role_by_id = {2: "data_analyst", 7: "data_architect"}
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.app.config["APP_CONFIG"].UPLOAD_DIR = self.tmp.name
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, path, body, uid=None, role=None):
        h = {}
        with self.app.app_context():
            if uid:
                h["Authorization"] = f"Bearer {issue_token(uid, role)[0]}"
        with patch("controller._helpers.get_db", return_value=self.db), patch("controller.analyst_controller.get_db", return_value=self.db),                 patch("app.security.get_db", return_value=self.db):
            return self.client.post(path, json=body, headers=h)

    def _get(self, path, uid=None, role=None):
        h = {}
        with self.app.app_context():
            if uid:
                h["Authorization"] = f"Bearer {issue_token(uid, role)[0]}"
        with patch("controller._helpers.get_db", return_value=self.db), patch("controller.analyst_controller.get_db", return_value=self.db), \
                patch("app.security.get_db", return_value=self.db):
            return self.client.get(path, headers=h)

    def test_requires_login(self):
        self.assertEqual(self._get("/api/published/5/dashboard").status_code, 401)

    def test_analyst_gets_dashboard(self):
        r = self._get("/api/published/5/dashboard", 2, "data_analyst")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["model"]["version"], "KG3")
        self.assertEqual(next(k for k in body["kpis"] if k["id"] == "total_records")["value"], 5)

    def test_unpublished_is_403_for_analyst(self):
        self.db.wf["finalized_version_number"] = None
        self.assertEqual(self._get("/api/published/5/dashboard", 2, "data_analyst").status_code, 403)

    def test_chat_requires_login(self):
        self.assertEqual(self._post("/api/published/5/chat", {"question": "total weight"}).status_code, 401)

    def test_chat_answers_from_the_data(self):
        r = self._post("/api/published/5/chat", {"question": "what is the total weight"}, 2, "data_analyst")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["table"]["rows"][0][0], 58.0)

    def test_chat_blocked_on_unpublished_model(self):
        self.db.wf["finalized_version_number"] = None
        r = self._post("/api/published/5/chat", {"question": "total weight"}, 2, "data_analyst")
        self.assertEqual(r.status_code, 403)

    def test_chat_rejects_empty_question(self):
        self.assertEqual(self._post("/api/published/5/chat", {"question": ""}, 2, "data_analyst").status_code, 422)

    def test_unknown_model_is_404(self):
        self.assertEqual(self._get("/api/published/999/dashboard", 2, "data_analyst").status_code, 404)


if __name__ == "__main__":
    unittest.main()
