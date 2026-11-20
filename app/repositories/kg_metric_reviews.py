"""Data Architect decisions on proposed business metrics. Kept apart from the graph itself so a review survives a graph rebuild:
a decision is keyed by (workflow, metric name, formula hash), so changing a metric's formula requires a fresh review."""


class MetricReviewRepository:
    def __init__(self, db):
        self.db = db

    def ensure_table(self):
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS kg_metric_reviews ("
                "  workflow_id BIGINT UNSIGNED NOT NULL,"
                "  name_key VARCHAR(120) NOT NULL,"
                "  formula_hash CHAR(16) NOT NULL,"
                "  decision VARCHAR(10) NOT NULL,"
                "  reviewed_by BIGINT UNSIGNED NULL,"
                "  reviewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "  PRIMARY KEY (workflow_id, name_key, formula_hash)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        except Exception:
            pass

    def decisions(self, workflow_id):
        """{(name_key, formula_hash): "approved" | "rejected"} for the workflow."""
        self.ensure_table()
        rows = self.db.query("SELECT name_key, formula_hash, decision FROM kg_metric_reviews WHERE workflow_id=%s", (workflow_id,))
        return {(r["name_key"], r["formula_hash"]): r["decision"] for r in rows}

    def set(self, workflow_id, name_key, formula_hash, decision, user_id):
        self.ensure_table()
        self.db.execute(
            "INSERT INTO kg_metric_reviews(workflow_id, name_key, formula_hash, decision, reviewed_by) VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE decision=VALUES(decision), reviewed_by=VALUES(reviewed_by)",
            (workflow_id, name_key, formula_hash, decision, user_id))

    def clear(self, workflow_id, name_key, formula_hash):
        self.ensure_table()
        self.db.execute("DELETE FROM kg_metric_reviews WHERE workflow_id=%s AND name_key=%s AND formula_hash=%s",
                        (workflow_id, name_key, formula_hash))
