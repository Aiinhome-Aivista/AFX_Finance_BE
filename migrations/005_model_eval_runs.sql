-- [MODEL-EVAL | Created 2026-11-28 11:29 IST (+05:30)]
-- Audit trail of Model Eval runs: every Business Architect input evaluated before it is added to the
-- Finance / FMCG K graph template inside the Business Input chatbox (source = answer | message).
-- [MODEL-EVAL | updated 2026-11-28 12:05 IST] chatbox-only; no separate Model Eval window.
CREATE TABLE IF NOT EXISTS model_eval_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  domain_slug VARCHAR(120) NOT NULL,
  template_key VARCHAR(32) NOT NULL,
  source VARCHAR(16) NOT NULL,
  input_text TEXT NULL,
  verdict VARCHAR(16) NOT NULL,
  score DECIMAL(5,1) NOT NULL DEFAULT 0,
  ops_total INT NOT NULL DEFAULT 0,
  ops_held INT NOT NULL DEFAULT 0,
  enforced TINYINT(1) NOT NULL DEFAULT 1,
  report LONGTEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_model_eval_user_wf (user_id, workflow_id, id),
  CONSTRAINT fk_model_eval_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_model_eval_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
