import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))


def _load_app_entry():
    spec = importlib.util.spec_from_file_location("afx_app_entry", BE_ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


app_module = _load_app_entry()
from app.config import Config  # noqa: E402
from app.errors import ValidationError  # noqa: E402
from app.security import issue_token  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.workflow_service import APPS, WorkflowService  # noqa: E402


class FakeDB:
    """In-memory stub for app.db.Session so RBAC and service tests run without MySQL."""

    def __init__(self, users=None, subscriptions=None, domains=None, workflows=None):
        self.users = list(users or [])
        self.subscriptions = list(subscriptions or [])  # list of (user_id, app_key)
        self.domains = list(domains or [
            {"id": 1, "slug": "gl", "code": "F2", "name": "General Ledger", "color": "#10b981", "description": "GL", "knowledge": "{}"},
        ])
        self.workflows = list(workflows or [])
        self.steps = {}
        self.versions = {}
        self.audits = []
        self._next_id = 100

    def one(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "from revoked_tokens" in s:
            return None
        if "from users where id=%s" in s:
            uid = int(params[0])
            for u in self.users:
                if int(u["id"]) == uid:
                    return dict(u)
            return None
        if "from users where email=%s" in s:
            email = params[0]
            for u in self.users:
                if u["email"] == email:
                    return dict(u)
            return None
        if "from domains where slug=%s" in s:
            for d in self.domains:
                if d["slug"] == params[0]:
                    return dict(d)
            return None
        if "from domains where id=%s" in s:
            for d in self.domains:
                if int(d["id"]) == int(params[0]):
                    return dict(d)
            return None
        if "from workflows where id=%s" in s:
            wid = int(params[0])
            for w in self.workflows:
                if int(w["id"]) == wid:
                    return dict(w)
            return None
        if "from workflows where user_id=%s and domain_id=%s" in s:
            uid, did = int(params[0]), int(params[1])
            for w in self.workflows:
                if int(w["user_id"]) == uid and int(w["domain_id"]) == did:
                    return dict(w)
            return None
        if "from workflows where domain_id=%s and finalized_version_number is not null" in s:
            did = int(params[0])
            cand = [w for w in self.workflows if int(w["domain_id"]) == did and w.get("finalized_version_number")]
            cand.sort(key=lambda w: w["id"], reverse=True)
            return dict(cand[0]) if cand else None
        return None

    def query(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "from user_app_subscriptions" in s:
            uid = int(params[0])
            return [{"app_key": k} for u, k in self.subscriptions if int(u) == uid]
        if "from workflows w join domains d" in s and "finalized_version_number is not null" in s:
            rows = []
            for w in self.workflows:
                if w.get("finalized_version_number") is not None:
                    d = next((x for x in self.domains if int(x["id"]) == int(w["domain_id"])), self.domains[0])
                    u = next((x for x in self.users if int(x["id"]) == int(w["user_id"])), {"display_name": "Architect", "email": "arch@afx.io"})
                    rows.append({
                        **w,
                        "domain_slug": d["slug"],
                        "domain_code": d["code"],
                        "domain_name": d["name"],
                        "domain_color": d["color"],
                        "domain_description": d["description"],
                        "owner_name": u.get("display_name"),
                        "owner_email": u.get("email"),
                    })
            return rows
        if "from workflows w join domains d" in s and "where w.user_id=%s" in s:
            uid = int(params[0])
            rows = []
            for w in self.workflows:
                if int(w["user_id"]) == uid:
                    d = next((x for x in self.domains if int(x["id"]) == int(w["domain_id"])), self.domains[0])
                    rows.append({
                        **w,
                        "domain_slug": d["slug"],
                        "domain_code": d["code"],
                        "domain_name": d["name"],
                        "domain_color": d["color"],
                        "domain_description": d["description"],
                        "latest_version": self.versions.get(w["id"], 0),
                    })
            return rows
        if "from workflow_steps" in s:
            return list(self.steps.get(int(params[0]), []))
        return []

    def insert(self, sql, params=()):
        s = " ".join(sql.lower().split())
        self._next_id += 1
        nid = self._next_id
        if "into users(" in s:
            email, display_name, role, pw_hash = params
            self.users.append({
                "id": nid,
                "email": email,
                "display_name": display_name,
                "role": role,
                "password_hash": pw_hash,
                "selected_app": None,
                "created_at": datetime.now(timezone.utc),
                "last_login_at": None,
            })
        elif "into audit_logs" in s:
            self.audits.append(params)
        return nid

    def execute(self, sql, params=()):
        s = " ".join(sql.lower().split())
        if "into user_app_subscriptions" in s:
            pair = (int(params[0]), params[1])
            if pair not in self.subscriptions:
                self.subscriptions.append(pair)
        elif "delete from user_app_subscriptions" in s:
            pair = (int(params[0]), params[1])
            self.subscriptions = [p for p in self.subscriptions if p != pair]
        elif "update users set selected_app=%s where id=%s" in s:
            app_key, uid = params[0], int(params[1])
            for u in self.users:
                if int(u["id"]) == uid:
                    u["selected_app"] = app_key
        elif "update users set last_login_at=now()" in s:
            pass


class RoleNavigationServiceTests(unittest.TestCase):
    def test_finance_fmcg_and_others_domains(self):
        self.assertTrue(APPS["finance"]["active"])
        self.assertTrue(APPS["fmcg"]["active"])
        self.assertFalse(APPS["others"]["active"])

        db = FakeDB(users=[{"id": 1, "email": "arch@afx.io", "display_name": "Arch", "role": "data_architect", "selected_app": None}])
        svc = WorkflowService(db)
        with self.assertRaises(ValidationError):
            svc.subscribe_app(1, "others")
        with self.assertRaises(ValidationError):
            svc.select_app(1, "others")

    def test_subscribe_unsubscribe_and_select_flow(self):
        db = FakeDB(users=[{"id": 1, "email": "arch@afx.io", "display_name": "Arch", "role": "data_architect", "selected_app": None}])
        svc = WorkflowService(db)

        initial = svc.apps(1)
        self.assertEqual([a["key"] for a in initial["available_apps"]], ["finance", "fmcg", "others"])
        self.assertEqual(initial["subscribed_apps"], [])

        after_sub = svc.subscribe_app(1, "finance")
        self.assertEqual([a["key"] for a in after_sub["available_apps"]], ["fmcg", "others"])
        self.assertEqual([a["key"] for a in after_sub["subscribed_apps"]], ["finance"])

        after_sel = svc.select_app(1, "finance")
        self.assertEqual(after_sel["selected_app"], "finance")

        after_unsub = svc.unsubscribe_app(1, "finance")
        self.assertEqual(after_unsub["subscribed_apps"], [])
        self.assertIsNone(after_unsub["selected_app"])

    def test_workspace_models_visibility_by_role(self):
        db = FakeDB(
            users=[
                {"id": 1, "email": "arch@afx.io", "display_name": "Architect One", "role": "data_architect", "selected_app": "finance"},
                {"id": 2, "email": "analyst@afx.io", "display_name": "Analyst One", "role": "data_analyst", "selected_app": None},
            ],
            workflows=[
                {"id": 10, "user_id": 1, "domain_id": 1, "current_step": 4, "selected_blocks": "[]", "finalized_version_number": None, "finalized_at": None, "updated_at": datetime.now(timezone.utc)},
                {"id": 11, "user_id": 1, "domain_id": 1, "current_step": 6, "selected_blocks": "[]", "finalized_version_number": 2, "finalized_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
            ],
        )
        db.steps[10] = [{"step_no": 1, "status": "completed"}, {"step_no": 2, "status": "completed"}, {"step_no": 3, "status": "completed"}, {"step_no": 4, "status": "active"}]
        db.steps[11] = [{"step_no": i, "status": "completed"} for i in range(1, 7)]

        svc = WorkflowService(db)
        arch_ws = svc.workspace_models(1, "data_architect")
        self.assertEqual(len(arch_ws["my_models"]), 2)
        self.assertEqual(len(arch_ws["published_models"]), 1)
        self.assertEqual(arch_ws["published_models"][0]["workflow_id"], 11)

        analyst_ws = svc.workspace_models(2, "data_analyst")
        self.assertNotIn("my_models", analyst_ws)
        self.assertEqual(len(analyst_ws["published_models"]), 1)
        self.assertEqual(analyst_ws["published_models"][0]["workflow_id"], 11)

    def test_register_validates_role_and_returns_role_token(self):
        db = FakeDB()
        flask_app = app_module.create_app()
        flask_app.config["APP_CONFIG"] = Config(SECRET_KEY="test-secret-key-at-least-32-bytes-long")
        with flask_app.app_context():
            auth = AuthService(db)
            with self.assertRaises(ValidationError):
                auth.register("bad@afx.io", "Bad Role", "Password123", role="admin")

            res = auth.register("analyst@afx.io", "Maya Analyst", "Password123", role="data_analyst")
            self.assertEqual(res["user"]["role"], "data_analyst")
            self.assertEqual(res["user"]["subscribed_apps"], [])
            self.assertTrue(res["token"])


class RoleNavigationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(
            users=[
                {"id": 1, "email": "arch@afx.io", "display_name": "Arch", "role": "data_architect", "selected_app": "finance"},
                {"id": 2, "email": "analyst@afx.io", "display_name": "Analyst", "role": "data_analyst", "selected_app": None},
            ],
            workflows=[
                {"id": 10, "user_id": 1, "domain_id": 1, "current_step": 4, "selected_blocks": "[]", "finalized_version_number": None, "finalized_at": None, "updated_at": datetime.now(timezone.utc)},
                {"id": 11, "user_id": 1, "domain_id": 1, "current_step": 6, "selected_blocks": "[]", "finalized_version_number": 2, "finalized_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
            ],
        )
        self.app = app_module.create_app()
        self.app.config["APP_CONFIG"] = Config(SECRET_KEY="test-secret-key-at-least-32-bytes-long")
        self.client = self.app.test_client()
        with self.app.app_context():
            self.arch_token, _ = issue_token(1, "data_architect")
            self.analyst_token, _ = issue_token(2, "data_analyst")

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_data_analyst_blocked_from_architect_routes(self):
        with patch("app.security.get_db", return_value=self.db), \
             patch("controller.domain_controller.get_db", return_value=self.db), \
             patch("controller.workflow_controller.get_db", return_value=self.db), \
             patch("controller.chat_controller.get_db", return_value=self.db), \
             patch("controller.file_controller.get_db", return_value=self.db), \
             patch("controller.graph_controller.get_db", return_value=self.db), \
             patch("controller._helpers.get_db", return_value=self.db):
            for method, path, payload in [
                ("POST", "/api/apps/subscribe", {"app": "finance"}),
                ("POST", "/api/apps/unsubscribe", {"app": "finance"}),
                ("POST", "/api/apps/select", {"app": "finance"}),
                ("POST", "/api/domains/gl/select", {}),
                ("POST", "/api/domains/gl/blocks", {"blocks": ["core"]}),
                ("GET", "/api/domains/gl/conversation", None),
                ("POST", "/api/domains/gl/finalize", {"confirm": True}),
                ("POST", "/api/domains/gl/telemetry/gap-analysis", {}),
            ]:
                resp = self.client.open(path, method=method, json=payload, headers=self._auth(self.analyst_token))
                self.assertEqual(resp.status_code, 403, f"Expected 403 for {method} {path}, got {resp.status_code}")

            # Analyst trying to access an unpublished draft workflow gets 403
            draft_resp = self.client.get("/api/domains/gl/finalization?wf=10", headers=self._auth(self.analyst_token))
            self.assertEqual(draft_resp.status_code, 403)

    def test_workspace_models_and_apps_endpoints(self):
        with patch("app.security.get_db", return_value=self.db), \
             patch("controller.domain_controller.get_db", return_value=self.db), \
             patch("controller._helpers.get_db", return_value=self.db):
            ws_analyst = self.client.get("/api/workspace/models", headers=self._auth(self.analyst_token))
            self.assertEqual(ws_analyst.status_code, 200)
            body = ws_analyst.get_json()
            self.assertNotIn("my_models", body)
            self.assertEqual(len(body["published_models"]), 1)

            sub_arch = self.client.post("/api/apps/subscribe", json={"app": "fmcg"}, headers=self._auth(self.arch_token))
            self.assertEqual(sub_arch.status_code, 200)
            self.assertEqual([a["key"] for a in sub_arch.get_json()["subscribed_apps"]], ["fmcg"])
            self.assertEqual([a["key"] for a in sub_arch.get_json()["available_apps"]], ["finance", "others"])


if __name__ == "__main__":
    unittest.main()
