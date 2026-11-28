"""Audit trail for Model Eval runs (evaluations done inside the Business Input chatbox).

[MODEL-EVAL | Created 2026-11-28 11:29 IST (+05:30)]

Recording is best-effort: if migration 005_model_eval_runs.sql has not been applied yet, the repository detects the
missing table once per process and silently becomes a no-op, so the Business Input chat never fails because of
the evaluation audit trail.
"""
import logging

from ..db import Session, jdump

log = logging.getLogger(__name__)
_TABLE_READY = None  # tri-state cache: None = unknown, True/False = checked


class ModelEvalRepository:
    def __init__(self, db: Session):
        self.db = db

    def available(self) -> bool:
        global _TABLE_READY
        if _TABLE_READY is None:
            try:
                _TABLE_READY = bool(self.db.one("SHOW TABLES LIKE 'model_eval_runs'"))
                if not _TABLE_READY:
                    log.warning("model_eval_runs table missing — run `python migrations/migrate.py` to enable Model Eval history.")
            except Exception as e:  # noqa: BLE001
                log.warning("Could not check model_eval_runs table: %s", e)
                return False
        return _TABLE_READY

    def record(self, user_id, workflow_id, domain_slug, report, source):
        if not self.available():
            return None
        try:
            s = report.get("summary") or {}
            return self.db.insert(
                "INSERT INTO model_eval_runs(user_id,workflow_id,domain_slug,template_key,source,input_text,verdict,score,"
                "ops_total,ops_held,enforced,report) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, workflow_id, (domain_slug or "")[:120], (report.get("template") or {}).get("key", "finance"),
                 source[:16], (report.get("input_excerpt") or "")[:4000], report.get("verdict", "pass"),
                 report.get("score") or 0, s.get("ops_total", 0), s.get("ops_held", 0), int(bool(report.get("enforced"))),
                 jdump(report)))
        except Exception as e:  # noqa: BLE001 — history must never break the chat flow
            log.warning("Model Eval run could not be recorded: %s", e)
            return None
