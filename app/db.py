"""Thin PyMySQL wrapper: request-scoped session + explicit transactions. No SQL lives in route handlers."""
import json
from contextlib import contextmanager

import pymysql
from flask import current_app, g
from pymysql.cursors import DictCursor


def jload(v, default=None):
    """JSON columns come back as str from PyMySQL (MariaDB stores JSON as LONGTEXT)."""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def jdump(v):
    return json.dumps(v, ensure_ascii=False)


class Session:
    def __init__(self, conn):
        self.conn = conn
        self._depth = 0

    def query(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())

    def one(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

    def execute(self, sql, params=None):
        with self.conn.cursor() as cur:
            return cur.execute(sql, params or ())

    def insert(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.lastrowid

    def executemany(self, sql, seq):
        if not seq:
            return 0
        with self.conn.cursor() as cur:
            return cur.executemany(sql, seq)

    @contextmanager
    def transaction(self):
        """Atomic block. Nested use joins the outer transaction (savepoint-free)."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.conn.begin()
        self._depth = 1
        try:
            yield self
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._depth = 0

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def connect(cfg, database=True):
    import time
    last_exc = None
    for attempt in range(5):
        try:
            return pymysql.connect(
                host=cfg.DB_HOST, port=cfg.DB_PORT, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
                database=cfg.DB_NAME if database else None, charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
                connect_timeout=8,
            )
        except pymysql.err.OperationalError as e:
            last_exc = e
            time.sleep(1)
    raise last_exc


def open_session(cfg):
    """Stand-alone session (background threads, CLI scripts)."""
    return Session(connect(cfg))


def get_db() -> Session:
    if "db" not in g:
        g.db = Session(connect(current_app.config["APP_CONFIG"]))
    return g.db


def close_db(_exc=None):
    s = g.pop("db", None)
    if s is not None:
        s.close()
