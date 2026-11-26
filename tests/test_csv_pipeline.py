"""Unit tests for the 9-Phase Dynamic CSV Ingestion Pipeline in WorkspaceDbService."""
import sqlite3
import sys
import tempfile
from pathlib import Path
import pytest
import pandas as pd

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.workspace_db_service import (
    clean_csv_dataframe,
    detect_column_types,
    build_schema_signature,
    WorkspaceDbService,
)


class MockDb:
    def __init__(self):
        self.executed = []
        self.tables_meta = []
        self.tables_data = {}  # table_name -> list of dicts

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, params_list):
        self.executed.append((sql, params_list))

    def query(self, sql, params=None):
        sql_lower = sql.strip().lower()
        if "show tables from" in sql_lower:
            return [{"Tables_in_db": name} for name in self.tables_data]
        if "_workspace_tables_meta" in sql_lower:
            return list(self.tables_meta)
        return []

    def one(self, sql, params=None):
        sql_lower = sql.strip().lower()
        if "count(*)" in sql_lower and "from" in sql_lower:
            for tbl in self.tables_data:
                if f"`{tbl}`" in sql:
                    return {"c": len(self.tables_data[tbl])}
            return {"c": 0}
        return None


class MockConfig:
    UPLOAD_DIR = tempfile.gettempdir()


def test_phase_1_data_reading_and_cleaning():
    """Phase 1: Test dropping blank rows, unnamed cols, header normalization, and in-file deduplication."""
    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as f:
        # Messy headers, Unnamed col, blank rows, internal duplicates
        f.write("Account Code. , PO - Number , Amount USD ,Unnamed: 3\n")
        f.write("ACC001, PO100, 1500.50, foo\n")
        f.write("ACC001, PO100, 1500.50, bar\n")  # Internal duplicate on valid cols
        f.write(" , , , \n")                     # Blank row
        f.write("ACC002, PO200, 2500.00, baz\n")
        f_path = Path(f.name)

    try:
        df, raw_count, dropped_dups = clean_csv_dataframe(f_path)
        assert list(df.columns) == ["account_code", "po_number", "amount_usd"]
        assert "unnamed_3" not in df.columns
        assert len(df) == 2  # ACC001 and ACC002 (blank row dropped, duplicate dropped)
        assert dropped_dups == 1
        assert raw_count == 3  # 3 non-empty rows initially
    finally:
        f_path.unlink(missing_ok=True)


def test_phase_2_schema_detection_and_typing():
    """Phase 2: Test detecting Integer, Decimal, Date, Datetime, and Text types."""
    df = pd.DataFrame({
        "id_col": [1, 2, 3],
        "amt_col": ["100.50", "200.75", "300.00"],
        "date_col": ["2026-11-01", "2026-11-02", "2026-11-03"],
        "dt_col": ["2026-11-01 10:30:00", "2026-11-02 11:45:00", "2026-11-03 12:00:00"],
        "desc_col": ["Purchase order approval", "Vendor invoice clearance", "GL variance"]
    })

    schema = detect_column_types(df)
    assert schema["id_col"] == "BIGINT"
    assert schema["amt_col"] == "DECIMAL(18, 4)"
    assert schema["date_col"] == "DATE"
    assert schema["dt_col"] == "DATETIME"
    assert schema["desc_col"] in ("VARCHAR(255)", "TEXT")

    sig = build_schema_signature(schema)
    assert "id_col" in sig and "BIGINT" in sig
    assert "amt_col" in sig and "DECIMAL(18, 4)" in sig


def test_phase_3_table_matching_conditions():
    """Phase 3: Test all 4 matching conditions (Same/Diff name, Same/Diff schema)."""
    mock_db = MockDb()
    service = WorkspaceDbService(mock_db, MockConfig())
    db_name = "ws_test_db"

    schema_a = {"col1": "BIGINT", "col2": "DECIMAL(18, 4)"}
    sig_a = build_schema_signature(schema_a)

    schema_b = {"col1": "BIGINT", "col2": "DECIMAL(18, 4)", "col3": "TEXT"}
    sig_b = build_schema_signature(schema_b)

    # Populate existing table in mock
    mock_db.tables_data["telemetry_ledger"] = []
    mock_db.tables_meta.append({
        "table_name": "telemetry_ledger",
        "original_file_name": "ledger.csv",
        "schema_signature": sig_a,
        "column_count": 2,
    })

    # Condition 1: Same File Name + Same Schema -> REUSE existing
    tbl, action, cond = service._match_table(db_name, "ledger.csv", sig_a, 2)
    assert action == "REUSED"
    assert tbl == "telemetry_ledger"
    assert cond == 1

    # Condition 2: Different File Name + Same Schema -> REUSE existing table (prevent clutter)
    tbl, action, cond = service._match_table(db_name, "december_ledger.csv", sig_a, 2)
    assert action == "REUSED"
    assert tbl == "telemetry_ledger"
    assert cond == 2

    # Condition 3: Same File Name + Changed Schema -> KEEP previous intact, CREATE new versioned table
    tbl, action, cond = service._match_table(db_name, "ledger.csv", sig_b, 3)
    assert action == "CREATED"
    assert tbl == "telemetry_ledger_v2"
    assert cond == 3

    # Condition 4: Different File Name + Different Schema -> CREATE fresh table
    tbl, action, cond = service._match_table(db_name, "vendor_payroll.csv", sig_b, 3)
    assert action == "CREATED"
    assert tbl == "telemetry_vendor_payroll"
    assert cond == 4


def test_phase_6_and_7_sqlite_deduplication_and_summary():
    """Test zero duplicates guarantee and summary counts on repeated upload."""
    import sqlite3
    mock_db = MockDb()
    service = WorkspaceDbService(mock_db, MockConfig())

    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as f:
        f.write("id,name,amount\n")
        f.write("1,Acme,500.00\n")
        f.write("2,Beta,750.50\n")
        f_path = Path(f.name)

    try:
        # First upload
        res1 = service.ingest_csv(1, 999, "test_ws", f_path, "test.csv")
        assert res1["import_status"] == "SUCCESS"
        assert res1["table_action"] == "CREATED"
        assert res1["total_uploaded_records"] == 2
        assert res1["newly_inserted_records"] == 0 or res1["newly_inserted_records"] == 2 # mock vs sqlite

        # Verify SQLite table content
        sqlite_path = service.get_sqlite_path(999, "test_ws")
        assert sqlite_path.exists()
        conn = sqlite3.connect(str(sqlite_path))
        rows = conn.execute("SELECT * FROM telemetry_test").fetchall()
        assert len(rows) == 2
        conn.close()

        # Second upload of identical file (Condition 1 should trigger REUSED)
        mock_db.tables_meta.append({
            "table_name": "telemetry_test",
            "original_file_name": "test.csv",
            "schema_signature": build_schema_signature(res1["schema"]),
            "column_count": 3,
        })
        mock_db.tables_data["telemetry_test"] = [{}, {}] # 2 existing rows

        res2 = service.ingest_csv(1, 999, "test_ws", f_path, "test.csv")
        assert res2["table_action"] == "REUSED"
        assert res2["matched_condition"] == 1
        assert res2["duplicate_records_skipped"] >= 2

        # Verify SQLite table still has exactly 2 rows (Zero duplicate records added)
        conn = sqlite3.connect(str(sqlite_path))
        rows_after = conn.execute("SELECT * FROM telemetry_test").fetchall()
        assert len(rows_after) == 2
        conn.close()

        # Cleanup
        sqlite_path.unlink(missing_ok=True)
    finally:
        f_path.unlink(missing_ok=True)


def test_csv_with_missing_values_and_partial_dates():
    """Verify that partial dates (e.g. 2024-04) remain VARCHAR and missing values become None (not NaN)."""
    mock_db = MockDb()
    mock_cfg = MockConfig()
    service = WorkspaceDbService(mock_db, mock_cfg)

    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as f:
        f.write("journal_id,period,posting_date,ic_matched_flag,amount\n")
        f.write("JE01,2024-04,2024-04-10,,1500.50\n")
        f.write("JE02,2024-05,2024-05-15,1.0,2000.00\n")
        f_path = Path(f.name)

    try:
        res = service.ingest_csv(1, 888, "test_ws2", f_path, "financial_sample.csv")
        assert res["import_status"] == "SUCCESS"
        assert res["schema"]["period"] == "VARCHAR(255)"
        assert res["schema"]["posting_date"] == "DATE"

        # Check SQLite storage
        sqlite_path = service.get_sqlite_path(888, "test_ws2")
        assert sqlite_path.exists()
        conn = sqlite3.connect(str(sqlite_path))
        rows = conn.execute("SELECT * FROM telemetry_financial_sample").fetchall()
        assert len(rows) == 2
        # Verify first row has None for ic_matched_flag
        first_row = rows[0]
        assert first_row[3] is None  # ic_matched_flag was empty
        assert first_row[1] == "2024-04"
        assert first_row[2] == "2024-04-10"
        conn.close()
        sqlite_path.unlink(missing_ok=True)
    finally:
        f_path.unlink(missing_ok=True)

