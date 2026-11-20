-- Data Architect approval of business metrics proposed for a workflow's data knowledge graph.
-- The analyst chat only calculates a proposed metric once it is approved here.
CREATE TABLE IF NOT EXISTS kg_metric_reviews (
  workflow_id BIGINT UNSIGNED NOT NULL,
  name_key VARCHAR(120) NOT NULL,
  formula_hash CHAR(16) NOT NULL,
  decision VARCHAR(10) NOT NULL,
  reviewed_by BIGINT UNSIGNED NULL,
  reviewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (workflow_id, name_key, formula_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
