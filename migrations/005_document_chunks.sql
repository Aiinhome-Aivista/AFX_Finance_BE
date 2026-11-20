-- Passages extracted from uploaded PDF / Word / text files, used by the analyst chat's document retrieval.
-- The ChromaDB vector index is derived from this table and rebuilt on demand.
CREATE TABLE IF NOT EXISTS document_chunks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  file_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  chunk_idx INT NOT NULL,
  page INT NULL,
  kind VARCHAR(16) NOT NULL,
  text MEDIUMTEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_file_chunk (file_id, chunk_idx),
  KEY idx_chunks_workflow (workflow_id),
  CONSTRAINT fk_chunks_file FOREIGN KEY (file_id) REFERENCES uploaded_files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
