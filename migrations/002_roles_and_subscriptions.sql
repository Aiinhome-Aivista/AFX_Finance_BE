-- Role-based navigation & user app/model subscriptions (Data Architect vs Data Analyst)

ALTER TABLE users
  ADD COLUMN role ENUM('data_architect','data_analyst') NOT NULL DEFAULT 'data_architect' AFTER display_name;
-- @@
CREATE TABLE user_app_subscriptions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NOT NULL,
  app_key VARCHAR(32) NOT NULL,
  subscribed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_user_app_sub (user_id, app_key),
  KEY ix_sub_user (user_id),
  CONSTRAINT fk_sub_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

