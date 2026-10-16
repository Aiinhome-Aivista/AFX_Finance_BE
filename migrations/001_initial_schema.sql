-- Finance Knowledge Graph application - initial schema (MySQL 8 / MariaDB 10.6+, InnoDB, utf8mb4)
-- Statements are separated by lines containing only "-- @@" (needed because trigger bodies contain ';').

CREATE TABLE users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  email VARCHAR(190) NOT NULL,
  display_name VARCHAR(120) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  selected_app VARCHAR(32) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP NULL,
  UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE revoked_tokens (
  jti VARCHAR(64) NOT NULL PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  revoked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_revoked_exp (expires_at),
  CONSTRAINT fk_revoked_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@

-- ============================ KG0: immutable reference ontology ============================
CREATE TABLE domains (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(8) NOT NULL,
  slug VARCHAR(80) NOT NULL,
  name VARCHAR(160) NOT NULL,
  description TEXT NOT NULL,
  scope TEXT NOT NULL,
  color VARCHAR(16) NOT NULL,
  sort_order INT NOT NULL,
  knowledge JSON NOT NULL,
  knowledge_evidence TEXT NULL,
  UNIQUE KEY uq_domains_slug (slug),
  UNIQUE KEY uq_domains_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE kg_ref_nodes (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  node_key VARCHAR(64) NOT NULL,
  name VARCHAR(160) NOT NULL,
  group_key VARCHAR(24) NOT NULL,
  group_name VARCHAR(160) NOT NULL,
  color VARCHAR(16) NOT NULL,
  kind ENUM('spine','domain','external') NOT NULL,
  definition TEXT NOT NULL,
  attributes JSON NOT NULL,
  formulas JSON NOT NULL,
  evidence TEXT NULL,
  canon_attributes JSON NULL,
  canon_ref VARCHAR(16) NULL,
  canon_title VARCHAR(200) NULL,
  canon_joins TEXT NULL,
  canon_evidence TEXT NULL,
  UNIQUE KEY uq_ref_node_key (node_key),
  KEY ix_ref_node_group (group_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE kg_ref_edges (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  source_key VARCHAR(64) NOT NULL,
  target_key VARCHAR(64) NOT NULL,
  label VARCHAR(160) NOT NULL,
  origin_graph VARCHAR(32) NOT NULL,
  cross_domain TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_ref_edge (source_key, target_key, label),
  KEY ix_ref_edge_target (target_key),
  CONSTRAINT fk_ref_edge_s FOREIGN KEY (source_key) REFERENCES kg_ref_nodes(node_key),
  CONSTRAINT fk_ref_edge_t FOREIGN KEY (target_key) REFERENCES kg_ref_nodes(node_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE domain_nodes (
  domain_id INT UNSIGNED NOT NULL,
  node_key VARCHAR(64) NOT NULL,
  role ENUM('core','shared','neighbour') NOT NULL,
  PRIMARY KEY (domain_id, node_key),
  CONSTRAINT fk_dn_domain FOREIGN KEY (domain_id) REFERENCES domains(id),
  CONSTRAINT fk_dn_node FOREIGN KEY (node_key) REFERENCES kg_ref_nodes(node_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE flows (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  flow_key VARCHAR(64) NOT NULL,
  title VARCHAR(160) NOT NULL,
  narrative TEXT NOT NULL,
  sort_order INT NOT NULL,
  UNIQUE KEY uq_flow_key (flow_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE flow_steps (
  flow_id INT UNSIGNED NOT NULL,
  step_order INT NOT NULL,
  node_key VARCHAR(64) NOT NULL,
  PRIMARY KEY (flow_id, step_order),
  CONSTRAINT fk_fs_flow FOREIGN KEY (flow_id) REFERENCES flows(id),
  CONSTRAINT fk_fs_node FOREIGN KEY (node_key) REFERENCES kg_ref_nodes(node_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@

-- ---- KG0 immutability at the database level: writes allowed only while @kg0_seeding = 1 ----
-- (generated for each reference table below; the application never sets @kg0_seeding)
CREATE TRIGGER trg_ref_nodes_bi BEFORE INSERT ON kg_ref_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_ref_nodes_bu BEFORE UPDATE ON kg_ref_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_ref_nodes_bd BEFORE DELETE ON kg_ref_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_ref_edges_bi BEFORE INSERT ON kg_ref_edges FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_ref_edges_bu BEFORE UPDATE ON kg_ref_edges FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_ref_edges_bd BEFORE DELETE ON kg_ref_edges FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domain_nodes_bi BEFORE INSERT ON domain_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domain_nodes_bu BEFORE UPDATE ON domain_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domain_nodes_bd BEFORE DELETE ON domain_nodes FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flow_steps_bi BEFORE INSERT ON flow_steps FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flow_steps_bu BEFORE UPDATE ON flow_steps FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flow_steps_bd BEFORE DELETE ON flow_steps FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flows_bu BEFORE UPDATE ON flows FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flows_bd BEFORE DELETE ON flows FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_flows_bi BEFORE INSERT ON flows FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domains_bu BEFORE UPDATE ON domains FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domains_bd BEFORE DELETE ON domains FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@
CREATE TRIGGER trg_domains_bi BEFORE INSERT ON domains FOR EACH ROW
BEGIN IF IFNULL(@kg0_seeding,0) <> 1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'KG0 reference graph is read-only'; END IF; END
-- @@

-- ============================ per-user workflow & client graphs ============================
CREATE TABLE workflows (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  domain_id INT UNSIGNED NOT NULL,
  current_step TINYINT UNSIGNED NOT NULL DEFAULT 2,
  selected_blocks JSON NULL,
  finalized_version_number INT NULL,
  finalized_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_workflow_user_domain (user_id, domain_id),
  CONSTRAINT fk_wf_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_wf_domain FOREIGN KEY (domain_id) REFERENCES domains(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE workflow_steps (
  workflow_id BIGINT UNSIGNED NOT NULL,
  step_no TINYINT UNSIGNED NOT NULL,
  status ENUM('locked','active','completed') NOT NULL DEFAULT 'locked',
  completed_at TIMESTAMP NULL,
  PRIMARY KEY (workflow_id, step_no),
  CONSTRAINT fk_wfs_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE change_proposals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  base_version_number INT NOT NULL DEFAULT 0,
  source ENUM('chat','telemetry','manual') NOT NULL,
  status ENUM('pending','approved','rejected','superseded') NOT NULL DEFAULT 'pending',
  title VARCHAR(200) NOT NULL,
  summary TEXT NULL,
  result_version_number INT NULL,
  llm_used TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decided_at TIMESTAMP NULL,
  KEY ix_prop_user_wf (user_id, workflow_id),
  CONSTRAINT fk_prop_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_prop_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE change_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  proposal_id BIGINT UNSIGNED NOT NULL,
  seq INT NOT NULL,
  op VARCHAR(24) NOT NULL,
  payload JSON NOT NULL,
  description VARCHAR(500) NOT NULL,
  rationale TEXT NULL,
  warnings JSON NULL,
  status ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
  KEY ix_items_prop (proposal_id),
  CONSTRAINT fk_items_prop FOREIGN KEY (proposal_id) REFERENCES change_proposals(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE user_graph_versions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  version_number INT NOT NULL,
  label VARCHAR(16) NOT NULL,
  kind ENUM('customization','telemetry') NOT NULL DEFAULT 'customization',
  status ENUM('active','finalized') NOT NULL DEFAULT 'active',
  parent_version_number INT NOT NULL DEFAULT 0,
  proposal_id BIGINT UNSIGNED NULL,
  summary TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_version (workflow_id, version_number),
  KEY ix_version_user (user_id),
  CONSTRAINT fk_ver_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_ver_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE user_graph_nodes (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  version_id BIGINT UNSIGNED NOT NULL,
  node_key VARCHAR(64) NOT NULL,
  name VARCHAR(160) NOT NULL,
  ref_name VARCHAR(160) NULL,
  group_key VARCHAR(24) NOT NULL,
  group_name VARCHAR(160) NOT NULL,
  color VARCHAR(16) NOT NULL,
  kind VARCHAR(16) NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'core',
  definition TEXT NOT NULL,
  attributes JSON NOT NULL,
  formulas JSON NOT NULL,
  evidence TEXT NULL,
  notes TEXT NULL,
  origin ENUM('reference','user','telemetry') NOT NULL,
  change_state ENUM('unchanged','added','modified') NOT NULL DEFAULT 'unchanged',
  added_in_version INT NOT NULL DEFAULT 0,
  UNIQUE KEY uq_ugn (version_id, node_key),
  CONSTRAINT fk_ugn_ver FOREIGN KEY (version_id) REFERENCES user_graph_versions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE user_graph_edges (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  version_id BIGINT UNSIGNED NOT NULL,
  source_key VARCHAR(64) NOT NULL,
  target_key VARCHAR(64) NOT NULL,
  label VARCHAR(160) NOT NULL,
  origin ENUM('reference','user','telemetry') NOT NULL,
  change_state ENUM('unchanged','added','modified') NOT NULL DEFAULT 'unchanged',
  added_in_version INT NOT NULL DEFAULT 0,
  cross_domain TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_uge (version_id, source_key, target_key, label),
  CONSTRAINT fk_uge_ver FOREIGN KEY (version_id) REFERENCES user_graph_versions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@

-- ============================ conversations & business knowledge ============================
CREATE TABLE conversations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  plan JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_conv_wf (workflow_id),
  CONSTRAINT fk_conv_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_conv_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE conversation_messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  conversation_id BIGINT UNSIGNED NOT NULL,
  role ENUM('assistant','user','system') NOT NULL,
  kind VARCHAR(24) NOT NULL DEFAULT 'chat',
  content TEXT NOT NULL,
  meta JSON NULL,
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY ix_msg_conv (conversation_id, id),
  CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE chat_questions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  conversation_id BIGINT UNSIGNED NOT NULL,
  seq INT NOT NULL,
  node_key VARCHAR(64) NOT NULL,
  topic VARCHAR(24) NOT NULL,
  is_followup TINYINT(1) NOT NULL DEFAULT 0,
  question_text TEXT NOT NULL,
  status ENUM('asked','answered','skipped') NOT NULL DEFAULT 'asked',
  answer_text TEXT NULL,
  extracted_ops JSON NULL,
  proposed TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  answered_at TIMESTAMP NULL,
  KEY ix_q_conv (conversation_id, seq),
  CONSTRAINT fk_q_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE business_experience (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  question_id BIGINT UNSIGNED NULL,
  node_key VARCHAR(64) NOT NULL,
  topic VARCHAR(24) NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_exp_wf (workflow_id),
  CONSTRAINT fk_exp_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_exp_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@

-- ============================ telemetry files ============================
CREATE TABLE uploaded_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  workflow_id BIGINT UNSIGNED NOT NULL,
  domain_id INT UNSIGNED NOT NULL,
  graph_version_number INT NOT NULL,
  original_name VARCHAR(255) NOT NULL,
  stored_name VARCHAR(80) NOT NULL,
  ext VARCHAR(8) NOT NULL,
  size_bytes BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL,
  status ENUM('uploaded','processing','processed','failed') NOT NULL DEFAULT 'uploaded',
  error TEXT NULL,
  metadata JSON NULL,
  preview JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TIMESTAMP NULL,
  KEY ix_files_user_wf (user_id, workflow_id),
  CONSTRAINT fk_files_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_files_wf FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
  CONSTRAINT fk_files_domain FOREIGN KEY (domain_id) REFERENCES domains(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE processing_jobs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  file_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  status ENUM('queued','running','succeeded','failed') NOT NULL DEFAULT 'queued',
  started_at TIMESTAMP NULL,
  finished_at TIMESTAMP NULL,
  error TEXT NULL,
  result JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_jobs_file (file_id),
  CONSTRAINT fk_jobs_file FOREIGN KEY (file_id) REFERENCES uploaded_files(id) ON DELETE CASCADE,
  CONSTRAINT fk_jobs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- @@
CREATE TABLE audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NULL,
  action VARCHAR(64) NOT NULL,
  entity_type VARCHAR(40) NULL,
  entity_id VARCHAR(64) NULL,
  detail JSON NULL,
  ip VARCHAR(64) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_audit_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
