"""MySQL source of truth for document passages. The vector index (ChromaDB) is derived from these rows and can be rebuilt on any
server, so retrieval never depends on one machine's disk. Rows are partitioned by workflow_id and removed with their file."""

_MARKER = -1  # chunk_idx of a row that only records "this file was processed" (so image-only files are not re-read every query)


class DocumentChunkRepository:
    def __init__(self, db):
        self.db = db

    def ensure_table(self):
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS document_chunks ("
                "  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                "  file_id BIGINT UNSIGNED NOT NULL,"
                "  workflow_id BIGINT UNSIGNED NOT NULL,"
                "  chunk_idx INT NOT NULL,"
                "  page INT NULL,"
                "  kind VARCHAR(16) NOT NULL,"
                "  text MEDIUMTEXT NOT NULL,"
                "  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "  UNIQUE KEY uq_file_chunk (file_id, chunk_idx),"
                "  KEY idx_chunks_workflow (workflow_id),"
                "  CONSTRAINT fk_chunks_file FOREIGN KEY (file_id) REFERENCES uploaded_files(id) ON DELETE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        except Exception:
            pass

    def indexed_file_ids(self, workflow_id):
        self.ensure_table()
        rows = self.db.query("SELECT DISTINCT file_id FROM document_chunks WHERE workflow_id=%s", (workflow_id,))
        return {int(r["file_id"]) for r in rows}

    def replace_for_file(self, file_id, workflow_id, chunks):
        self.ensure_table()
        self.db.execute("DELETE FROM document_chunks WHERE file_id=%s", (file_id,))
        rows = [(file_id, workflow_id, _MARKER, None, "marker", "")]
        rows += [(file_id, workflow_id, c["chunk_idx"], c.get("page"), c["kind"], c["text"]) for c in chunks]
        self.db.executemany(
            "INSERT INTO document_chunks(file_id, workflow_id, chunk_idx, page, kind, text) VALUES (%s,%s,%s,%s,%s,%s)", rows)

    def for_files(self, workflow_id, file_ids):
        """Passages of the given files only (never another workflow's), in reading order."""
        if not file_ids:
            return []
        self.ensure_table()
        ph = ",".join(["%s"] * len(file_ids))
        return self.db.query(
            f"SELECT file_id, chunk_idx, page, kind, text FROM document_chunks WHERE workflow_id=%s AND file_id IN ({ph}) "
            "AND chunk_idx >= 0 ORDER BY file_id, chunk_idx", (workflow_id, *file_ids))
