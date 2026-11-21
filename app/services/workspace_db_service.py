import os
import re
import sqlite3
import logging
from pathlib import Path
import pandas as pd

log = logging.getLogger(__name__)


def sanitize_ident(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(name).strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:48] or "workspace"


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

        # 1. Initialize clean SQLite workspace database
        try:
            if sqlite_path.exists():
                sqlite_path.unlink()
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
            cur.execute(
                "INSERT INTO _workspace_metadata (workspace_name, workflow_id, user_id) VALUES (?, ?, ?)",
                (workspace_name, workflow_id, user_id)
            )
            conn.commit()
            conn.close()
            log.info("Initialized blank SQLite workspace database: %s", sqlite_path)
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

    def ingest_csv(self, user_id: int, workflow_id: int, workspace_name: str, file_path: Path, filename: str):
        db_name = self.get_db_name(workflow_id, workspace_name)
        table_base = sanitize_ident(filename.rsplit(".", 1)[0])
        table_name = f"telemetry_{table_base}"[:60]

        df = pd.read_csv(file_path)
        # Clean column names
        df.columns = [sanitize_ident(c) for c in df.columns]

        # 1. Ingest into SQLite
        sqlite_path = self.get_sqlite_path(workflow_id, workspace_name)
        try:
            conn = sqlite3.connect(str(sqlite_path))
            df.to_sql(table_name, conn, if_exists="replace", index=False)
            conn.close()
            log.info("Ingested %d rows into SQLite table %s", len(df), table_name)
        except Exception as e:
            log.warning("SQLite ingestion error for %s: %s", table_name, e)

        # 2. Ingest into MySQL database
        try:
            self.db.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
            cols_def = ", ".join([f"`{col}` TEXT" for col in df.columns])
            self.db.execute(f"DROP TABLE IF EXISTS `{db_name}`.`{table_name}`")
            self.db.execute(f"CREATE TABLE `{db_name}`.`{table_name}` ({cols_def})")
            if len(df) > 0:
                cols_str = ", ".join([f"`{col}`" for col in df.columns])
                vals_placeholder = ", ".join(["%s"] * len(df.columns))
                insert_sql = f"INSERT INTO `{db_name}`.`{table_name}` ({cols_str}) VALUES ({vals_placeholder})"
                records = df.where(pd.notnull(df), None).values.tolist()
                for i in range(0, len(records), 500):
                    batch = records[i:i + 500]
                    self.db.executemany(insert_sql, batch)
            log.info("Ingested %d rows into MySQL table %s.%s", len(df), db_name, table_name)
        except Exception as e:
            log.warning("MySQL ingestion error for %s: %s", table_name, e)

        return {
            "table_name": table_name,
            "db_name": db_name,
            "row_count": len(df),
            "column_count": len(df.columns),
            "columns": list(df.columns)
        }

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
