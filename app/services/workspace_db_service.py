import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def sanitize_ident(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(name).strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:48] or "workspace"


def clean_csv_dataframe(file_path: Path):
    """
    Phase 1: Data Reading & Cleaning
    - Drops completely blank rows (including whitespace-only rows).
    - Removes unnecessary Unnamed columns.
    - Normalizes column headers (lowercase, replaces spaces, dots, hyphens with _).
    - Removes internal duplicate records within the uploaded file itself.
    """
    df = pd.read_csv(file_path, skipinitialspace=True)
    
    # Filter out unnecessary Unnamed columns first
    valid_cols = [c for c in df.columns if not str(c).strip().lower().startswith("unnamed")]
    if valid_cols:
        df = df[valid_cols]

    # Strip whitespace across text values and replace empty string with NaN
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            df[c] = df[c].astype(str).str.strip()
            df[c] = df[c].replace(r"^\s*$", None, regex=True)
            df[c] = df[c].replace({"nan": None, "None": None, "": None})
            
    # Drop completely blank rows
    df = df.dropna(how="all")
        
    # Normalize all column headers: lowercase and replace spaces, dots, hyphens with _
    normalized_cols = []
    seen = {}
    for c in df.columns:
        norm = re.sub(r"[\s\.\-]+", "_", str(c).strip().lower())
        norm = re.sub(r"[^a-z0-9_]", "", norm)
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm:
            norm = "col"
        if norm in seen:
            seen[norm] += 1
            norm = f"{norm}_{seen[norm]}"
        else:
            seen[norm] = 1
        normalized_cols.append(norm)
    df.columns = normalized_cols
    
    # Remove internal duplicate records present within the uploaded file itself
    total_raw_records = len(df)
    df = df.drop_duplicates()
    internal_duplicates_dropped = total_raw_records - len(df)
    
    return df, total_raw_records, internal_duplicates_dropped


def detect_column_types(df: pd.DataFrame) -> dict:
    """
    Phase 2: Schema Detection & Data Typing
    Scan every column's values and detect specific SQL data types:
    - Integer: BIGINT / INT
    - Decimal / Float: DECIMAL(18, 4)
    - Date: DATE
    - Datetime: DATETIME
    - Text: TEXT / VARCHAR(255)
    """
    schema = {}
    for col in df.columns:
        s = df[col].dropna().astype(str).str.strip()
        if len(s) == 0:
            schema[col] = "VARCHAR(255)"
            continue
            
        # Check Integer (whole numbers, IDs)
        is_int = s.str.match(r"^-?\d+$")
        if is_int.all():
            schema[col] = "BIGINT"
            continue
            
        # Check Decimal / Float (monetary amounts, rates, percentages)
        is_float = s.str.match(r"^-?\d+(\.\d+)?$")
        if is_float.all():
            schema[col] = "DECIMAL(18, 4)"
            continue
            
        # Check Date / Datetime
        sample = s.iloc[:100]
        try:
            looks_like_date = sample.str.contains(r"[-/:]").all()
            has_day_component = (sample.str.count(r"[-/]") >= 2).all()
            if looks_like_date and has_day_component:
                parsed = pd.to_datetime(sample, errors="coerce")
                if parsed.notnull().all():
                    has_time = (parsed.dt.hour != 0) | (parsed.dt.minute != 0) | (parsed.dt.second != 0)
                    if has_time.any():
                        schema[col] = "DATETIME"
                    else:
                        schema[col] = "DATE"
                    continue
        except Exception:
            pass
            
        max_len = s.str.len().max()
        if max_len <= 255:
            schema[col] = "VARCHAR(255)"
        else:
            schema[col] = "TEXT"
            
    return schema


def build_schema_signature(schema: dict) -> str:
    """
    Schema Signature containing column names, column order, column count, and data types.
    """
    items = [{"name": c, "type": schema[c]} for c in schema]
    return json.dumps(items, separators=(",", ":"))


class WorkspaceDbService:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg

    def _resolve_name(self, workflow_id: int, workspace_name: str = "") -> str:
        if workspace_name and str(workspace_name).strip():
            return str(workspace_name).strip()
        if not workflow_id:
            return "ws"
        try:
            cm = self.db.one("SELECT name FROM custom_finance_models WHERE workflow_id = %s LIMIT 1", (workflow_id,))
            if cm and cm.get("name"):
                return cm["name"]
            wf = self.db.one("SELECT name FROM workflows WHERE id = %s LIMIT 1", (workflow_id,))
            if wf and wf.get("name"):
                return wf["name"]
        except Exception:
            pass
        try:
            base_dir = Path(self.cfg.UPLOAD_DIR) / "workspace_dbs"
            if base_dir.exists():
                matching = list(base_dir.glob(f"ws_{workflow_id}_*.db"))
                if matching:
                    stem = matching[0].stem
                    prefix = f"ws_{workflow_id}_"
                    if stem.startswith(prefix):
                        return stem[len(prefix):]
        except Exception:
            pass
        return "ws"

    def get_db_name(self, workflow_id: int, workspace_name: str = "") -> str:
        name = self._resolve_name(workflow_id, workspace_name)
        clean = sanitize_ident(name) if name else "ws"
        return f"ws_{workflow_id}_{clean}"

    def get_sqlite_path(self, workflow_id: int, workspace_name: str = "") -> Path:
        base_dir = Path(self.cfg.UPLOAD_DIR) / "workspace_dbs"
        base_dir.mkdir(parents=True, exist_ok=True)
        db_name = self.get_db_name(workflow_id, workspace_name)
        return base_dir / f"{db_name}.db"

    def create_workspace_db(self, user_id: int, workflow_id: int, workspace_name: str):
        workspace_name = self._resolve_name(workflow_id, workspace_name)
        db_name = self.get_db_name(workflow_id, workspace_name)
        sqlite_path = self.get_sqlite_path(workflow_id, workspace_name)

        # 1. Initialize SQLite workspace database (preserves existing tables on Save Progress)
        try:
            conn = sqlite3.connect(str(sqlite_path))
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS _workspace_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_name TEXT NOT NULL,
                    workflow_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("SELECT COUNT(*) FROM _workspace_metadata WHERE workflow_id = ?", (workflow_id,))
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO _workspace_metadata (workspace_name, workflow_id, user_id) VALUES (?, ?, ?)",
                    (workspace_name, workflow_id, user_id)
                )
                conn.commit()
            conn.close()
            log.info("Workspace SQLite database ready: %s", sqlite_path)
        except Exception as e:
            log.warning("Could not initialize SQLite workspace database: %s", e)

        # 2. Also initialize MySQL database for this workspace
        try:
            self.db.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4")
            self.db.execute(f"""
                CREATE TABLE IF NOT EXISTS `{db_name}`.`_workspace_metadata` (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    workspace_name VARCHAR(190) NOT NULL,
                    workflow_id BIGINT UNSIGNED NOT NULL,
                    user_id BIGINT UNSIGNED NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self.db.execute(
                f"INSERT INTO `{db_name}`.`_workspace_metadata` (workspace_name, workflow_id, user_id) VALUES (%s, %s, %s)",
                (workspace_name, workflow_id, user_id)
            )
            log.info("Initialized MySQL workspace database: %s", db_name)
        except Exception as e:
            log.warning("Could not initialize MySQL workspace database %s: %s", db_name, e)

        return {
            "status": "READY",
            "db_name": db_name,
            "sqlite_path": str(sqlite_path),
            "workflow_id": workflow_id,
            "workspace_name": workspace_name
        }

    def delete_workspace_db(self, workflow_id: int = None, workspace_name: str = ""):
        """Drops dedicated MySQL database schemas (e.g. ws_219_saikatf1) and unlinks SQLite database files."""
        clean_name = sanitize_ident(workspace_name) if workspace_name else ""
        dbs_to_drop = set()

        # 1. Identify MySQL databases to drop
        if workflow_id:
            if clean_name:
                dbs_to_drop.add(f"ws_{workflow_id}_{clean_name}")
            try:
                rows = self.db.query(
                    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME LIKE %s",
                    (f"ws_{workflow_id}_%",)
                )
                for r in rows:
                    if r and r.get("SCHEMA_NAME"):
                        dbs_to_drop.add(r["SCHEMA_NAME"])
            except Exception as e:
                log.warning("Could not query schemata for workflow_id %s: %s", workflow_id, e)

        if clean_name:
            try:
                rows = self.db.query(
                    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME LIKE %s",
                    (f"ws_%_{clean_name}",)
                )
                for r in rows:
                    if r and r.get("SCHEMA_NAME"):
                        dbs_to_drop.add(r["SCHEMA_NAME"])
            except Exception as e:
                log.warning("Could not query schemata for clean_name %s: %s", clean_name, e)

        # Drop the MySQL schemas safely (strictly validating ws_<wf_id>_<name> format)
        for db_name in dbs_to_drop:
            if re.match(r"^ws_\d+_[a-zA-Z0-9_]+$", db_name):
                try:
                    self.db.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
                    log.info("Dropped MySQL workspace database schema: %s", db_name)
                except Exception as e:
                    log.warning("Could not drop MySQL database %s: %s", db_name, e)

        # 2. Unlink SQLite database files
        try:
            base_dir = Path(self.cfg.UPLOAD_DIR) / "workspace_dbs"
            if base_dir.exists():
                sqlite_patterns = []
                if workflow_id:
                    sqlite_patterns.append(f"ws_{workflow_id}_*.db")
                if clean_name:
                    sqlite_patterns.append(f"ws_*_{clean_name}.db")
                for pat in sqlite_patterns:
                    for db_file in base_dir.glob(pat):
                        try:
                            db_file.unlink(missing_ok=True)
                            log.info("Unlinked SQLite database file: %s", db_file)
                        except Exception as e:
                            log.warning("Could not unlink SQLite file %s: %s", db_file, e)
        except Exception as e:
            log.warning("Could not clean up SQLite database files: %s", e)

    def is_workspace_db_ready(self, workflow_id: int, workspace_name: str = "") -> bool:
        if not workflow_id:
            return False
        workspace_name = self._resolve_name(workflow_id, workspace_name)
        sqlite_path = self.get_sqlite_path(workflow_id, workspace_name)
        if sqlite_path.exists():
            return True
        db_name = self.get_db_name(workflow_id, workspace_name)
        try:
            row = self.db.one("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s", (db_name,))
            if row:
                return True
        except Exception:
            pass

        try:
            base_dir = Path(self.cfg.UPLOAD_DIR) / "workspace_dbs"
            if base_dir.exists():
                matching = list(base_dir.glob(f"ws_{workflow_id}_*.db"))
                if matching:
                    return True
        except Exception:
            pass

        try:
            cm = self.db.one(
                "SELECT * FROM custom_finance_models WHERE workflow_id = %s LIMIT 1",
                (workflow_id,)
            )
            if cm and cm.get("status") in ("SAVED", "PUBLISHED"):
                self.create_workspace_db(cm["user_id"], workflow_id, cm["name"])
                return True
        except Exception:
            pass

        return False

    def _ensure_metadata_tables(self, db_name: str):
        """Ensures _workspace_tables_meta and _workspace_import_audit exist in the workspace database."""
        self.db.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4")
        self.db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{db_name}`.`_workspace_tables_meta` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                `table_name` VARCHAR(100) NOT NULL UNIQUE,
                `original_file_name` VARCHAR(255) NOT NULL,
                `schema_signature` TEXT NOT NULL,
                `column_count` INT NOT NULL,
                `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        self.db.execute(f"""
            CREATE TABLE IF NOT EXISTS `{db_name}`.`_workspace_import_audit` (
                `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                `user_id` BIGINT UNSIGNED NOT NULL,
                `workspace_name` VARCHAR(190) NOT NULL,
                `db_name` VARCHAR(100) NOT NULL,
                `uploaded_file_name` VARCHAR(255) NOT NULL,
                `target_table_name` VARCHAR(100) NOT NULL,
                `table_action` VARCHAR(50) NOT NULL,
                `matched_condition` INT NOT NULL,
                `total_uploaded_records` INT NOT NULL,
                `newly_inserted_records` INT NOT NULL,
                `duplicate_records_skipped` INT NOT NULL,
                `total_records_in_target_table` INT NOT NULL,
                `file_size_bytes` BIGINT NOT NULL,
                `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def _match_table(self, db_name: str, filename: str, schema_sig: str, col_count: int) -> tuple[str, str, int]:
        """
        Phase 3: Table Matching Logic (4 Conditions)
        - Condition 1 (Same File Name + Same Schema): Reuse existing table.
        - Condition 2 (Different File Name + Same Schema): Reuse existing matching table.
        - Condition 3 (Same File Name + Changed Schema): Keep previous table intact, generate a unique new table name.
        - Condition 4 (Different File Name + Different Schema): Create a fresh, separate new table.
        
        Returns: (target_table_name, table_action, condition_number)
        """
        table_base = sanitize_ident(filename.rsplit(".", 1)[0])
        default_base_name = f"telemetry_{table_base}"[:60]
        
        # Query known table metadata
        meta_rows = self.db.query(f"SELECT * FROM `{db_name}`.`_workspace_tables_meta` ORDER BY id ASC")
        
        # Check actual existing tables in MySQL schema
        existing_tables_rows = self.db.query(f"SHOW TABLES FROM `{db_name}`")
        actual_tables = set()
        for r in existing_tables_rows:
            tbl = list(r.values())[0] if r else ""
            if tbl and not tbl.startswith("_"):
                actual_tables.add(tbl)

        # 1. Check Condition 1: Same File Name + Same Schema
        for m in meta_rows:
            if m["table_name"] in actual_tables:
                if m["original_file_name"].strip().lower() == filename.strip().lower() and m["schema_signature"] == schema_sig:
                    log.info("Condition 1 matched (Same File + Same Schema): Reusing table %s", m["table_name"])
                    return m["table_name"], "REUSED", 1

        # 2. Check Condition 2: Different File Name + Same Schema
        for m in meta_rows:
            if m["table_name"] in actual_tables:
                if m["schema_signature"] == schema_sig:
                    log.info("Condition 2 matched (Diff File + Same Schema): Reusing table %s", m["table_name"])
                    return m["table_name"], "REUSED", 2

        # 3. Check Condition 3: Same File Name + Changed Schema
        same_name_entries = [m for m in meta_rows if m["original_file_name"].strip().lower() == filename.strip().lower() and m["table_name"] in actual_tables]
        if same_name_entries:
            # Crucial Rule: Never modify, drop, or overwrite previous table. Generate unique versioned table.
            v = 2
            cand_name = f"{default_base_name}_v{v}"[:60]
            while cand_name in actual_tables or any(m["table_name"] == cand_name for m in meta_rows):
                v += 1
                cand_name = f"{default_base_name}_v{v}"[:60]
            log.info("Condition 3 matched (Same File + Changed Schema): Creating versioned table %s", cand_name)
            return cand_name, "CREATED", 3

        # 4. Condition 4: Different File Name + Different Schema
        cand_name = default_base_name
        if cand_name in actual_tables:
            v = 2
            cand_name = f"{default_base_name}_v{v}"[:60]
            while cand_name in actual_tables or any(m["table_name"] == cand_name for m in meta_rows):
                v += 1
                cand_name = f"{default_base_name}_v{v}"[:60]
        log.info("Condition 4 matched (Diff File + Diff Schema): Creating fresh table %s", cand_name)
        return cand_name, "CREATED", 4

    def ingest_csv(self, user_id: int, workflow_id: int, workspace_name: str, file_path: Path, filename: str):
        """
        9-Phase CSV Upload & Dynamic Table Ingestion Pipeline.
        """
        workspace_name = self._resolve_name(workflow_id, workspace_name)
        db_name = self.get_db_name(workflow_id, workspace_name)
        file_size = file_path.stat().st_size if file_path.exists() else 0

        # Phase 1: Data Reading & Cleaning
        df, total_raw_records, internal_duplicates_dropped = clean_csv_dataframe(file_path)
        if len(df.columns) == 0:
            raise ValueError(f"Uploaded file '{filename}' contains no valid data columns.")

        # Phase 2: Schema Detection & Data Typing
        detected_schema = detect_column_types(df)
        schema_sig = build_schema_signature(detected_schema)
        col_count = len(df.columns)

        # Normalize date/datetime columns to ISO standard string for reliable SQL insertion
        for c, col_type in detected_schema.items():
            if col_type == "DATE":
                df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d")
            elif col_type == "DATETIME":
                df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")

        # Prepare clean Python records with None for any missing/NaN values
        raw_rows = df.values.tolist()
        clean_records = [
            [None if (pd.isna(v) or v is None) else (str(v) if isinstance(v, (bool, np.bool_)) else v) for v in row]
            for row in raw_rows
        ]

        # Initialize metadata tables
        self._ensure_metadata_tables(db_name)

        # Phase 3: Table Matching Logic (4 Conditions)
        target_table, table_action, matched_cond = self._match_table(db_name, filename, schema_sig, col_count)

        cols = list(df.columns)
        cols_def = ", ".join([f"`{c}` {detected_schema[c]}" for c in cols])
        cols_str = ", ".join([f"`{c}`" for c in cols])
        vals_placeholder = ", ".join(["%s"] * len(cols))
        temp_table = f"_temp_stg_{uuid.uuid4().hex[:12]}"

        # Phase 4: Dynamic Table Creation (if newly created)
        if table_action == "CREATED":
            self.db.execute(f"CREATE TABLE IF NOT EXISTS `{db_name}`.`{target_table}` ({cols_def})")
            # Register in table metadata
            self.db.execute(
                f"""
                INSERT INTO `{db_name}`.`_workspace_tables_meta` 
                (table_name, original_file_name, schema_signature, column_count)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE schema_signature=VALUES(schema_signature), column_count=VALUES(column_count), updated_at=NOW()
                """,
                (target_table, filename, schema_sig, col_count)
            )

        # Phase 5: Temporary Staging & Incremental Deduplication
        newly_inserted = 0
        count_after = 0
        try:
            # 1. Create temporary table with identical structure
            self.db.execute(f"CREATE TEMPORARY TABLE `{db_name}`.`{temp_table}` ({cols_def})")

            # 2. Bulk insert all cleaned rows into temporary table
            if len(df) > 0:
                insert_temp_sql = f"INSERT INTO `{db_name}`.`{temp_table}` ({cols_str}) VALUES ({vals_placeholder})"
                for i in range(0, len(clean_records), 500):
                    batch = clean_records[i:i + 500]
                    self.db.executemany(insert_temp_sql, batch)

            # 3. Get pre-insert row count
            count_before_row = self.db.one(f"SELECT COUNT(*) AS c FROM `{db_name}`.`{target_table}`")
            count_before = (count_before_row or {}).get("c", 0)

            # 4. Insert only unique records using NULL-safe comparison (<=>)
            where_conditions = " AND ".join([f"tgt.`{c}` <=> t.`{c}`" for c in cols])
            cols_t_str = ", ".join([f"t.`{c}`" for c in cols])
            
            dedup_insert_sql = f"""
                INSERT INTO `{db_name}`.`{target_table}` ({cols_str})
                SELECT {cols_t_str}
                FROM `{db_name}`.`{temp_table}` t
                WHERE NOT EXISTS (
                    SELECT 1 FROM `{db_name}`.`{target_table}` tgt
                    WHERE {where_conditions}
                )
            """
            self.db.execute(dedup_insert_sql)

            # 5. Get post-insert row count
            count_after_row = self.db.one(f"SELECT COUNT(*) AS c FROM `{db_name}`.`{target_table}`")
            count_after = (count_after_row or {}).get("c", 0)
            newly_inserted = max(0, count_after - count_before)

        finally:
            # Guaranteed cleanup of temporary table
            try:
                self.db.execute(f"DROP TEMPORARY TABLE IF EXISTS `{db_name}`.`{temp_table}`")
            except Exception as ex:
                log.warning("Could not drop temp table %s: %s", temp_table, ex)

        # Phase 6: Duplicate Prevention Guarantee
        db_duplicates_skipped = len(df) - newly_inserted
        total_duplicates_skipped = internal_duplicates_dropped + max(0, db_duplicates_skipped)

        # Synchronize with SQLite workspace database
        sqlite_path = self.get_sqlite_path(workflow_id, workspace_name)
        try:
            conn = sqlite3.connect(str(sqlite_path))
            cur = conn.cursor()
            cur.execute(f"CREATE TABLE IF NOT EXISTS `{target_table}` ({cols_def})")
            if len(df) > 0:
                sqlite_where = " AND ".join([f"tgt.`{c}` IS t.`{c}`" for c in cols])
                cur.execute(f"CREATE TEMPORARY TABLE IF NOT EXISTS `{temp_table}` ({cols_def})")
                sqlite_records = clean_records
                sqlite_placeholders = ", ".join(["?"] * len(cols))
                sqlite_insert_temp = f"INSERT INTO `{temp_table}` ({cols_str}) VALUES ({sqlite_placeholders})"
                cur.executemany(sqlite_insert_temp, sqlite_records)
                cur.execute(f"""
                    INSERT INTO `{target_table}` ({cols_str})
                    SELECT {cols_t_str} FROM `{temp_table}` t
                    WHERE NOT EXISTS (
                        SELECT 1 FROM `{target_table}` tgt WHERE {sqlite_where}
                    )
                """)
                cur.execute(f"DROP TABLE IF EXISTS `{temp_table}`")
                conn.commit()
            conn.close()
        except Exception as e:
            log.warning("SQLite sync error for %s: %s", target_table, e)

        # Phase 7: Audit Logging & Import Summary
        try:
            self.db.execute(
                f"""
                INSERT INTO `{db_name}`.`_workspace_import_audit` (
                    user_id, workspace_name, db_name, uploaded_file_name, target_table_name,
                    table_action, matched_condition, total_uploaded_records, newly_inserted_records,
                    duplicate_records_skipped, total_records_in_target_table, file_size_bytes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id, workspace_name, db_name, filename, target_table,
                    table_action, matched_cond, total_raw_records, newly_inserted,
                    total_duplicates_skipped, count_after, file_size
                )
            )
        except Exception as ex:
            log.warning("Could not write import audit log: %s", ex)

        summary = {
            "import_status": "SUCCESS",
            "uploaded_file_name": filename,
            "target_table_name": target_table,
            "table_action": table_action,
            "matched_condition": matched_cond,
            "total_uploaded_records": total_raw_records,
            "cleaned_records": len(df),
            "newly_inserted_records": newly_inserted,
            "duplicate_records_skipped": total_duplicates_skipped,
            "total_records_in_target_table": count_after,
            "file_size_bytes": file_size,
            "column_count": col_count,
            "columns": cols,
            "schema": detected_schema,
            "db_name": db_name,
            "workspace_name": workspace_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        log.info("Dynamic CSV Ingestion completed: %s", summary)
        return summary

    def list_tables(self, workflow_id: int, workspace_name: str = "") -> list:
        workspace_name = self._resolve_name(workflow_id, workspace_name)
        sqlite_path = self.get_sqlite_path(workflow_id, workspace_name)
        if sqlite_path.exists():
            try:
                conn = sqlite3.connect(str(sqlite_path))
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB '_*'")
                tables = [r[0] for r in cur.fetchall()]
                conn.close()
                return tables
            except Exception:
                pass
        return []
