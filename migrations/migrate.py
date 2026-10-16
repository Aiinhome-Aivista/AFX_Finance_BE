#!/usr/bin/env python3
"""Minimal forward-only migration runner.  Usage: python migrations/migrate.py [--reset]"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from app.config import Config  # noqa: E402
from app.db import connect  # noqa: E402

DIR = Path(__file__).resolve().parent


def split_statements(sql: str):
    parts = re.split(r"^-- @@\s*$", sql, flags=re.M)
    out = []
    for p in parts:
        # strip comment-only lines but keep statement text
        stmt = "\n".join(l for l in p.splitlines() if not l.strip().startswith("--")).strip().rstrip(";")
        if stmt:
            out.append(stmt)
    return out


def run(cfg=None, reset=False):
    cfg = cfg or Config.from_env()
    conn = connect(cfg)
    cur = conn.cursor()
    if reset:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        cur.execute("SHOW FULL TABLES WHERE Table_type='BASE TABLE'")
        for row in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS `{list(row.values())[0]}`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(80) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("SELECT version FROM schema_migrations")
    done = {r["version"] for r in cur.fetchall()}
    applied = []
    for f in sorted(DIR.glob("*.sql")):
        if f.name in done:
            continue
        for stmt in split_statements(f.read_text()):
            cur.execute(stmt)
        cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (f.name,))
        applied.append(f.name)
    conn.close()
    return applied


if __name__ == "__main__":
    print("applied:", run(reset="--reset" in sys.argv) or "nothing (up to date)")
