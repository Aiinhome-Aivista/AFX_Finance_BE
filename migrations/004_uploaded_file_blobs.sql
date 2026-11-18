-- Add uploaded_file_blobs table to persist uploaded file contents in MySQL
-- Ensures files uploaded on one machine are accessible across all machines running the app.
CREATE TABLE IF NOT EXISTS uploaded_file_blobs (
  file_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  content LONGBLOB NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_file_blobs_file FOREIGN KEY (file_id) REFERENCES uploaded_files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
