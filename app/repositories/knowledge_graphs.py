"""MySQL copy of the verified, data-derived knowledge graph (one row per workflow). ArangoDB is the graph store; this copy is the
reliable fallback the chat reads when Arango is slow or unreachable."""
import json

from ..db import jdump


class KnowledgeGraphRepository:
    def __init__(self, db):
        self.db = db

    def ensure_table(self):
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS data_knowledge_graphs ("
                "  workflow_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,"
                "  fingerprint VARCHAR(32) NOT NULL,"
                "  graph LONGTEXT NOT NULL,"
                "  arango_synced TINYINT(1) NOT NULL DEFAULT 0,"
                "  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        except Exception:
            pass

    def save(self, workflow_id, fingerprint, graph, arango_synced):
        self.ensure_table()
        self.db.execute(
            "INSERT INTO data_knowledge_graphs(workflow_id, fingerprint, graph, arango_synced) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint), graph=VALUES(graph), arango_synced=VALUES(arango_synced)",
            (workflow_id, fingerprint, jdump(graph), 1 if arango_synced else 0))

    def get(self, workflow_id):
        self.ensure_table()
        row = self.db.one("SELECT fingerprint, graph FROM data_knowledge_graphs WHERE workflow_id=%s", (workflow_id,))
        if not row:
            return None
        try:
            return row["fingerprint"], json.loads(row["graph"])
        except (TypeError, ValueError):
            return None
