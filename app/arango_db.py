"""ArangoDB connector and client for Finance Knowledge Graph.

Supports both the official `python-arango` driver and a lightweight direct HTTP REST client
(via `requests`) so that it works seamlessly under all environments without friction.
"""
import logging
import json
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger(__name__)


class ArangoHttpDriver:
    """Robust REST-based ArangoDB driver using standard requests."""

    def __init__(self, url, username, password, db_name=None):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.db_name = db_name or os.environ.get("ARANGO_DB", "")
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.headers.update({"Content-Type": "application/json"})

    def _req(self, method, path, db=None, **kwargs):
        target_db = db or self.db_name
        # Database endpoint prefix
        if path.startswith("/_api/database"):
            full_url = f"{self.url}{path}"
        else:
            full_url = f"{self.url}/_db/{target_db}{path}"
        kwargs.setdefault("timeout", (10, 20))
        
        # Retry on transient network connect timeouts
        last_exc = None
        for attempt in range(3):
            try:
                resp = self.session.request(method, full_url, **kwargs)
                return resp
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                import time
                time.sleep(0.5)
        raise last_exc

    def ping(self):
        """Test connection to the ArangoDB server."""
        try:
            r = self._req("GET", "/_api/version", db="_system")
            return r.status_code == 200, r.json() if r.status_code == 200 else r.text
        except Exception as e:
            return False, str(e)

    def ensure_database(self):
        """Ensure the target database exists in ArangoDB, creating it if needed."""
        try:
            r = self._req("GET", "/_api/database", db="_system")
            if r.status_code == 200:
                dbs = r.json().get("result", [])
                if self.db_name not in dbs:
                    cr = self._req("POST", "/_api/database", db="_system", json={"name": self.db_name})
                    log.info("Created ArangoDB database %s: %s", self.db_name, cr.status_code)
            return True
        except Exception as e:
            log.warning("Could not ensure ArangoDB database %s: %s", self.db_name, e)
            return False

    def ensure_collection(self, name, is_edge=False):
        """Ensure a document or edge collection exists."""
        try:
            r = self._req("GET", f"/_api/collection/{name}")
            if r.status_code == 404:
                # 2 = Document collection, 3 = Edge collection
                payload = {"name": name, "type": 3 if is_edge else 2}
                cr = self._req("POST", "/_api/collection", json=payload)
                log.info("Created ArangoDB collection %s (edge=%s): %s", name, is_edge, cr.status_code)
            return True
        except Exception as e:
            log.warning("Could not ensure collection %s: %s", name, e)
            return False

    def truncate_collection(self, name):
        """Truncate (empty) a collection."""
        try:
            r = self._req("PUT", f"/_api/collection/{name}/truncate")
            return r.status_code in (200, 201)
        except Exception as e:
            log.warning("Could not truncate collection %s: %s", name, e)
            return False

    def query(self, aql, bind_vars=None):
        """Execute an AQL query and return all results as a list of dictionaries."""
        payload = {"query": aql, "bindVars": bind_vars or {}, "batchSize": 5000}
        r = self._req("POST", "/_api/cursor", json=payload)
        if r.status_code in (200, 201):
            data = r.json()
            results = data.get("result", [])
            cursor_id = data.get("id")
            has_more = data.get("hasMore", False)

            # Paginate if needed
            while has_more and cursor_id:
                nr = self._req("PUT", f"/_api/cursor/{cursor_id}")
                if nr.status_code in (200, 201):
                    ndata = nr.json()
                    results.extend(ndata.get("result", []))
                    has_more = ndata.get("hasMore", False)
                else:
                    break
            return results
        else:
            err = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            raise RuntimeError(f"AQL Error ({r.status_code}): {err}")

    def import_documents(self, collection_name, docs, overwrite=True):
        """Bulk import or upsert documents into a collection."""
        if not docs:
            return 0
        mode = "overwrite" if overwrite else "ignore"
        r = self._req("POST", f"/_api/document/{collection_name}?overwriteMode={mode}", json=docs)
        if r.status_code in (200, 201, 202):
            return len(docs)
        else:
            raise RuntimeError(f"Import Error ({r.status_code}): {r.text}")


_arango_instance = None


def get_arango(cfg=None) -> ArangoHttpDriver:
    """Get or create singleton ArangoDB driver instance."""
    global _arango_instance
    if _arango_instance is None:
        if cfg is None:
            from .config import Config
            cfg = Config.from_env()
        _arango_instance = ArangoHttpDriver(
            url=cfg.ARANGO_URL,
            username=cfg.ARANGO_USER,
            password=cfg.ARANGO_PASSWORD,
            db_name=cfg.ARANGO_DB,
        )
    return _arango_instance
