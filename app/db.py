"""Thin PyMySQL wrapper: thread-safe connection reuse + request-scoped session + explicit transactions."""
from contextlib import contextmanager
import json
import queue
import threading
import time

import pymysql
from flask import current_app, g
from pymysql.cursors import DictCursor

_POOL_LOCK = threading.Lock()
_POOLS: dict[tuple, queue.LifoQueue] = {}
_MAX_POOL_SIZE = 6


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
    def __init__(self, conn, pool_key=None):
        self.conn = conn
        self._depth = 0
        self._pool_key = pool_key
        self._broken = False

    def query(self, sql, params=None):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params or ())
                return list(cur.fetchall())
        except pymysql.err.MySQLError:
            self._broken = True
            raise

    def one(self, sql, params=None):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchone()
        except pymysql.err.MySQLError:
            self._broken = True
            raise

    def execute(self, sql, params=None):
        try:
            with self.conn.cursor() as cur:
                return cur.execute(sql, params or ())
        except pymysql.err.MySQLError:
            self._broken = True
            raise

    def insert(self, sql, params=None):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.lastrowid
        except pymysql.err.MySQLError:
            self._broken = True
            raise

    def executemany(self, sql, seq):
        if not seq:
            return 0
        try:
            with self.conn.cursor() as cur:
                return cur.executemany(sql, seq)
        except pymysql.err.MySQLError:
            self._broken = True
            raise

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
            try:
                self.conn.rollback()
            except Exception:
                self._broken = True
            raise
        finally:
            self._depth = 0

    def close(self, exc=None):
        if self._pool_key is not None and exc is None and not self._broken and self._depth == 0:
            try:
                self.conn.autocommit(True)
                _get_pool(self._pool_key).put_nowait(self.conn)
                return
            except Exception:
                pass
        try:
            self.conn.close()
        except Exception:
            pass


def _pool_key(cfg, database=True):
    return (cfg.DB_HOST, cfg.DB_PORT, cfg.DB_USER, cfg.DB_NAME if database else None)


def _get_pool(key):
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = queue.LifoQueue(maxsize=_MAX_POOL_SIZE)
            _POOLS[key] = pool
        return pool


def connect(cfg, database=True):
    last_exc = None
    for attempt in range(2):
        try:
            return pymysql.connect(
                host=cfg.DB_HOST, port=cfg.DB_PORT, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
                database=cfg.DB_NAME if database else None, charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
                connect_timeout=5, read_timeout=15, write_timeout=15,
            )
        except pymysql.err.OperationalError as e:
            last_exc = e
            if attempt == 0:
                time.sleep(0.3)
    if last_exc:
        raise last_exc
    raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")


def _borrow_conn(cfg, database=True):
    key = _pool_key(cfg, database=database)
    pool = _get_pool(key)
    while True:
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            break
        try:
            conn.ping(reconnect=False)
            return conn, key
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    return connect(cfg, database=database), key


def open_session(cfg):
    """Stand-alone session (background threads, CLI scripts)."""
    return Session(connect(cfg))


def get_db() -> Session:
    if "db" not in g:
        cfg = current_app.config["APP_CONFIG"]
        conn, key = _borrow_conn(cfg, database=True)
        g.db = Session(conn, pool_key=key)
    return g.db


def close_db(exc=None):
    s = g.pop("db", None)
    if s is not None:
        s.close(exc=exc)
