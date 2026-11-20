-- Verified data-derived knowledge graph per workflow (fact tables, joins, hierarchies, synonyms, metric formulas).
-- ArangoDB holds the graph for graph queries; this row is the reliable copy the analyst chat falls back to.
CREATE TABLE IF NOT EXISTS data_knowledge_graphs (
  workflow_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  fingerprint VARCHAR(32) NOT NULL,
  graph LONGTEXT NOT NULL,
  arango_synced TINYINT(1) NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
