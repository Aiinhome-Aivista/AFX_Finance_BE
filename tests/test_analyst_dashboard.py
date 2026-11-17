import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.config import Config  # noqa: E402
from app.errors import ForbiddenError, NotFoundError  # noqa: E402
from app.services import dataset_profiler as prof  # noqa: E402
from app.services.analyst_dashboard_service import AnalystDashboardService  # noqa: E402

CSV = (
    "Order No,Karigar Name,Receipt Date,Purity,Weight,Status\n"
    "1001,Bhagya,2026-07-03,85.5,10.5,OK\n"
    "1002,Bhagya,2026-07-15,90.0,20,OK\n"
    "1003,Laxmi,2026-08-02,88.0,5,OK\n"
    "1004,Laxmi,2026-08-20,91.6,15,Rework\n"
    "1005,Ravi,2026-09-01,86.0,7.5,OK\n"
)


class FakeDB:
    def __init__(self, wf, files, custom_published=False):
        self.wf, self.files, self.custom_published = wf, files, custom_published

    def one(self, sql, params=()):
        if "FROM workflows" in sql:
            return dict(self.wf) if int(params[0]) == self.wf["id"] else None
        if "FROM custom_finance_models" in sql:
            return {"id": 1} if self.custom_published else None
        if "FROM domains" in sql:
            return {"id": 1, "slug": "gl", "code": "F2", "name": "General Ledger", "description": ""}
        if "FROM users" in sql:
            return {"display_name": "Arch"}

    def query(self, sql, params=()):
        return [dict(f) for f in self.files]


class ProfilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "a.csv"
        self.path.write_text(CSV, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_kpis_match_source_data(self):
        tables = []
        for name, df in prof.load_tables(self.path, "csv", "a.csv"):
            tables.append(prof.profile_table(name, 1, df))
        out = prof.build_dashboard(tables)
        k = {x["id"]: x for x in out["kpis"]}
        self.assertEqual(k["total_records"]["value"], 5)
        self.assertAlmostEqual(k["sum_Weight"]["value"], 58.0)
        self.assertAlmostEqual(k["sum_Purity"]["value"], 441.1)
        self.assertEqual(out["date_range"], {"from": "2026-07-03", "to": "2026-09-01"})
        self.assertNotIn("sum_Order No", k)  # identifiers are never summed
        bar = next(c for c in out["charts"] if c["type"] == "bar")
        self.assertEqual({d["label"] for d in bar["data"]}, {"Bhagya", "Laxmi", "Ravi"})
        line = next(c for c in out["charts"] if c["type"] == "line")
        self.assertEqual(len(line["data"]), 3)


class ServiceAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(UPLOAD_DIR=self.tmp.name)
        d = Path(self.tmp.name) / "7"
        d.mkdir()
        (d / "f1.csv").write_text(CSV, encoding="utf-8")
        self.file = {"id": 1, "user_id": 7, "original_name": "a.csv", "stored_name": "f1.csv", "ext": "csv", "size_bytes": 10,
                     "sha256": "abc", "graph_version_number": 3}

    def tearDown(self):
        self.tmp.cleanup()

    def _wf(self, final):
        return {"id": 5, "user_id": 7, "domain_id": 1, "finalized_version_number": final,
                "finalized_at": datetime(2026, 11, 1) if final else None, "selected_blocks": None}

    def svc(self, final=3, files=None, custom=False):
        return AnalystDashboardService(FakeDB(self._wf(final), files if files is not None else [self.file], custom), self.cfg, None)

    def test_custom_published_model_without_finalized_version(self):
        """Models published from the model selector have no finalized version; the dashboard must still open."""
        out = self.svc(final=None, custom=True).dashboard(2, "data_analyst", 5)
        self.assertEqual(out["status"], "ready")
        self.assertTrue(out["model"]["published"])
        self.assertEqual(out["model"]["version"], "KG3")  # newest version that has data

    def test_custom_published_uses_only_newest_data_version(self):
        old = dict(self.file, id=2, graph_version_number=1, sha256="old")
        out = self.svc(final=None, custom=True, files=[old, self.file]).dashboard(2, "data_analyst", 5)
        self.assertEqual(out["excluded_files"], 1)
        self.assertEqual(len(out["data_sources"]), 1)

    def test_missing_file_is_reported_and_never_cached(self):
        """File row exists (e.g. uploaded via another server) but bytes are absent: say so, and recover once they appear."""
        (Path(self.tmp.name) / "7" / "f1.csv").unlink()
        s = self.svc()
        out = s.dashboard(2, "data_analyst", 5)
        self.assertEqual(out["status"], "no_data")
        self.assertIn("not available on this server", out["message"])
        self.assertTrue(any("Re-upload" in w for w in out["warnings"]))
        (Path(self.tmp.name) / "7" / "f1.csv").write_text(CSV, encoding="utf-8")
        again = s.dashboard(2, "data_analyst", 5)
        self.assertEqual(again["status"], "ready")  # the failed attempt was not cached

    def test_attached_document_is_a_quiet_source_not_a_warning(self):
        pdf = dict(self.file, id=2, ext="pdf", original_name="spec.pdf", stored_name="spec.pdf", sha256="p")
        out = self.svc(files=[self.file, pdf]).dashboard(2, "data_analyst", 5)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["warnings"], [])
        self.assertTrue(next(s for s in out["data_sources"] if s["name"] == "spec.pdf")["document"])

    def test_unpublished_without_custom_row_still_blocked(self):
        with self.assertRaises(ForbiddenError):
            self.svc(final=None, custom=False).dashboard(2, "data_analyst", 5)

    def test_analyst_blocked_from_unpublished(self):
        with self.assertRaises(ForbiddenError):
            self.svc(final=None).dashboard(2, "data_analyst", 5)

    def test_other_architect_blocked_from_draft(self):
        with self.assertRaises(ForbiddenError):
            self.svc(final=None).dashboard(99, "data_architect", 5)

    def test_unknown_model(self):
        with self.assertRaises(NotFoundError):
            self.svc().dashboard(2, "data_analyst", 404)

    def test_published_dashboard_ready_and_cached(self):
        s = self.svc()
        out = s.dashboard(2, "data_analyst", 5)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out["model"]["version"], "KG3")
        self.assertEqual(out["summary"]["source"], "rules")
        self.assertFalse(out["cached"])
        self.assertTrue(s.dashboard(2, "data_analyst", 5)["cached"])

    def test_old_version_files_never_mixed_in(self):
        stale = dict(self.file, graph_version_number=2)
        out = self.svc(final=3, files=[stale]).dashboard(2, "data_analyst", 5)
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["excluded_files"], 1)
        self.assertEqual(out["kpis"], [])

    def test_fingerprint_changes_with_new_upload(self):
        s = self.svc()
        wf = self._wf(3)
        a = s._fingerprint(wf, [self.file])
        b = s._fingerprint(wf, [self.file, dict(self.file, id=2, sha256="def")])
        self.assertNotEqual(a, b)

    def test_narrative_rejects_invented_numbers(self):
        facts = {"kpis": {"Total": "58"}}
        self.assertTrue(AnalystDashboardService._numbers_supported("Total is 58.", facts))
        self.assertFalse(AnalystDashboardService._numbers_supported("Total is 99,000.", facts))


if __name__ == "__main__":
    unittest.main()
