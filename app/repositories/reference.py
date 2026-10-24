"""READ-ONLY access to KG0 via ArangoDB (primary) with MySQL fallback."""
import logging
from ..db import Session, jload
from ..arango_db import get_arango

log = logging.getLogger(__name__)

_cached_counts = None

DOMAIN_SLUG_TO_ID = {
    "planning-budgeting-forecasting": 1,
    "management-reporting-gross-to-net": 2,
    "cost-profitability-management": 3,
    "working-capital-treasury": 4,
    "consolidation-close-statutory": 5,
    "statement-analysis-valuation": 6,
    "risk-governance-controls": 7,
    "epm-platforms-operating-model": 8,
}


def _clean_str(s):
    if not isinstance(s, str):
        return s
    return (s.replace("â€“", "—")
             .replace("â€”", "—")
             .replace("â€\"", "—")
             .replace("â€\x9d", "\"")
             .replace("â€\x9c", "\"")
             .replace("â€˜", "'")
             .replace("â€™", "'")
             .replace("â€œ", "\"")
             .replace("Ã—", "×")
             .replace("â†’", "→")
             .replace("âˆ’", "-")
             .replace("Ã·", "/")
             .replace("Â§", "")
             .replace("§", "")
             .replace("Â", ""))


def _node(r):
    if not r:
        return None
    r = dict(r)
    r["node_key"] = r.get("node_key") or r.get("key") or r.get("_key")
    r["name"] = r.get("name") or r.get("n") or r["node_key"]
    r["group_key"] = r.get("group_key") or r.get("d") or "general"

    if "name" in r and r["name"]:
        r["name"] = _clean_str(r["name"])
    if "definition" in r and r["definition"]:
        r["definition"] = _clean_str(r["definition"])

    if isinstance(r.get("attributes"), str):
        raw_attrs = jload(r["attributes"], [])
    else:
        raw_attrs = r.get("attributes") or []
    r["attributes"] = [_clean_str(a) if isinstance(a, str) else a for a in raw_attrs]

    if isinstance(r.get("formulas"), str):
        raw_forms = jload(r["formulas"], [])
    else:
        raw_forms = r.get("formulas") or []
    r["formulas"] = [_clean_str(f) if isinstance(f, str) else f for f in raw_forms]

    if isinstance(r.get("canon_attributes"), str):
        raw_canon = jload(r.get("canon_attributes"), [])
    else:
        raw_canon = r.get("canon_attributes") or []
    r["canon_attributes"] = [_clean_str(c) if isinstance(c, str) else c for c in raw_canon]

    if "evidence" in r and r["evidence"]:
        r["evidence"] = _clean_str(r["evidence"])
    if "canon_ref" in r and r["canon_ref"]:
        r["canon_ref"] = _clean_str(r["canon_ref"])
    if "canon_title" in r and r["canon_title"]:
        r["canon_title"] = _clean_str(r["canon_title"])
    if "canon_joins" in r and r["canon_joins"]:
        r["canon_joins"] = _clean_str(r["canon_joins"])
    if "canon_evidence" in r and r["canon_evidence"]:
        r["canon_evidence"] = _clean_str(r["canon_evidence"])
    return r


class ReferenceRepository:
    def __init__(self, db: Session = None, arango=None):
        self.db = db
        try:
            self.arango = arango or get_arango()
        except Exception as e:
            log.warning("Could not initialize ArangoDB client: %s", e)
            self.arango = None

    def overview_raw(self):
        """Fetches entire graph in one single network roundtrip from ArangoDB."""
        if self.arango:
            try:
                aql = """
                RETURN {
                  domains: (FOR d IN domains SORT d.sort_order RETURN d),
                  nodes: (FOR n IN kg_ref_nodes SORT n._key RETURN n),
                  edges: (FOR e IN kg_ref_edges RETURN e),
                  flows: (FOR f IN flows SORT f.sort_order RETURN f),
                  counts: (FOR dn IN domain_nodes COLLECT domain = dn.domain_slug, role = dn.role WITH COUNT INTO c RETURN {domain: domain, role: role, count: c})
                }
                """
                res = self.arango.query(aql)
                if res:
                    return res[0]
            except Exception as e:
                log.warning("ArangoDB single-query overview_raw failed: %s", e)
        return None

    # ---- domains
    def domains(self):
        # 1. Try ArangoDB
        if self.arango:
            try:
                rows = self.arango.query("FOR d IN domains SORT d.sort_order RETURN d")
                if rows:
                    id_map = {}
                    if self.db:
                        try:
                            id_map = {r["slug"]: r["id"] for r in self.db.query("SELECT id, slug FROM domains")}
                        except Exception:
                            pass
                    for r in rows:
                        slug = r.get("slug")
                        domain_id = id_map.get(slug) or DOMAIN_SLUG_TO_ID.get(slug)
                        r["id"] = domain_id if domain_id is not None else slug
                        if isinstance(r.get("knowledge"), str):
                            r["knowledge"] = jload(r["knowledge"], {})
                        r.setdefault("knowledge", {})
                    return rows
            except Exception as e:
                log.warning("ArangoDB domains() failed, falling back to MySQL: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            rows = self.db.query("SELECT * FROM domains ORDER BY sort_order")
            for r in rows:
                r["knowledge"] = jload(r["knowledge"], {})
            return rows
        return []

    def domain_by_slug(self, slug):
        if not slug:
            return None
        normalized = str(slug).strip().replace(" ", "-").replace("%20", "-").lower()

        # 1. Try ArangoDB
        if self.arango:
            try:
                res = self.arango.query(
                    "FOR d IN domains FILTER d.slug == @slug OR d.slug == @norm OR d._key == @slug OR d._key == @norm LIMIT 1 RETURN d",
                    {"slug": slug, "norm": normalized}
                )
                if res:
                    d = res[0]
                    domain_id = None
                    if self.db:
                        try:
                            row = self.db.one("SELECT id FROM domains WHERE slug=%s OR slug=%s", (slug, normalized))
                            if row:
                                domain_id = row.get("id")
                            else:
                                # Auto-insert into MySQL so foreign key constraint never fails
                                target_slug = d.get("slug") or slug
                                self.db.execute("SET @kg0_seeding = 1")
                                self.db.execute(
                                    """INSERT INTO domains(code,slug,name,description,scope,color,sort_order,knowledge,knowledge_evidence)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                       ON DUPLICATE KEY UPDATE slug=VALUES(slug)""",
                                    (d.get("code", slug[:4].upper()), target_slug, d.get("name", slug), d.get("description", ""),
                                     d.get("scope", ""), d.get("color", "#2563EB"), d.get("sort_order", 0),
                                     jdump(d.get("knowledge", {})), d.get("knowledge_evidence", ""))
                                )
                                self.db.execute("SET @kg0_seeding = 0")
                                row = self.db.one("SELECT id FROM domains WHERE slug=%s OR slug=%s", (slug, normalized))
                                if row:
                                    domain_id = row.get("id")
                        except Exception as ex:
                            log.warning("Auto-sync domain %s to MySQL failed: %s", slug, ex)
                    if not domain_id:
                        domain_id = DOMAIN_SLUG_TO_ID.get(d.get("slug")) or DOMAIN_SLUG_TO_ID.get(slug)
                    d["id"] = domain_id if domain_id is not None else d.get("slug")
                    if isinstance(d.get("knowledge"), str):
                        d["knowledge"] = jload(d["knowledge"], {})
                    d.setdefault("knowledge", {})
                    return d
            except Exception as e:
                log.warning("ArangoDB domain_by_slug(%s) failed: %s", slug, e)

        # 2. Fallback to MySQL
        if self.db:
            r = self.db.one("SELECT * FROM domains WHERE slug=%s", (slug,))
            if not r and slug:
                r = self.db.one("SELECT * FROM domains WHERE slug=%s", (normalized,))
            if r:
                r["knowledge"] = jload(r["knowledge"], {})
            return r
        return None

    def domain_node_roles(self, domain_id):
        # 1. Try ArangoDB
        if self.arango:
            try:
                # domain_id can be slug or numeric id
                slug = str(domain_id)
                mapped_slug = None
                if slug.isdigit():
                    if self.db:
                        try:
                            row = self.db.one("SELECT slug FROM domains WHERE id=%s", (int(slug),))
                            if row:
                                mapped_slug = row.get("slug")
                        except Exception:
                            pass
                    if not mapped_slug:
                        id_to_slug = {v: k for k, v in DOMAIN_SLUG_TO_ID.items()}
                        mapped_slug = id_to_slug.get(int(slug))

                res = self.arango.query(
                    """
                    FOR dn IN domain_nodes
                      FILTER dn.domain_slug == @slug OR dn.domain_id == @slug
                             OR dn.domain_slug == @mapped OR dn.domain_id == @mapped
                      RETURN {node_key: dn.node_key, role: dn.role}
                    """,
                    {"slug": slug, "mapped": mapped_slug or slug}
                )
                if res:
                    return {r["node_key"]: r["role"] for r in res}
            except Exception as e:
                log.warning("ArangoDB domain_node_roles(%s) failed: %s", domain_id, e)

        # 2. Fallback to MySQL
        if self.db:
            rows = self.db.query("SELECT node_key, role FROM domain_nodes WHERE domain_id=%s", (domain_id,))
            if not rows and str(domain_id).isdigit():
                id_to_slug = {v: k for k, v in DOMAIN_SLUG_TO_ID.items()}
                mapped_slug = id_to_slug.get(int(domain_id))
                if mapped_slug:
                    rows = self.db.query("SELECT node_key, role FROM domain_nodes WHERE domain_id=%s", (mapped_slug,))
            return {r["node_key"]: r["role"] for r in rows}
        return {}

    def domain_node_counts(self):
        global _cached_counts
        if _cached_counts:
            return dict(_cached_counts)

        # 1. Try ArangoDB
        if self.arango:
            try:
                res = self.arango.query(
                    """
                    FOR dn IN domain_nodes
                      COLLECT domain = dn.domain_slug, role = dn.role WITH COUNT INTO c
                      RETURN {domain: domain, role: role, count: c}
                    """
                )
                if res:
                    out = {}
                    for r in res:
                        out.setdefault(r["domain"], {})[r["role"]] = r["count"]
                        did = DOMAIN_SLUG_TO_ID.get(r["domain"])
                        if did is not None:
                            out.setdefault(did, {})[r["role"]] = r["count"]
                    _cached_counts = out
                    return out
            except Exception as e:
                log.warning("ArangoDB domain_node_counts() failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            rows = self.db.query("SELECT domain_id, role, COUNT(*) c FROM domain_nodes GROUP BY domain_id, role")
            out = {}
            for r in rows:
                out.setdefault(r["domain_id"], {})[r["role"]] = r["c"]
            return out
        return {}

    # ---- nodes / edges
    def nodes_by_keys(self, keys):
        keys = list(keys)
        if not keys:
            return []

        # 1. Try ArangoDB
        if self.arango:
            try:
                docs = self.arango.query(
                    "FOR n IN kg_ref_nodes FILTER n._key IN @keys OR n.node_key IN @keys RETURN n",
                    {"keys": keys}
                )
                if docs:
                    return [_node(r) for r in docs]
            except Exception as e:
                log.warning("ArangoDB nodes_by_keys failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            ph = ",".join(["%s"] * len(keys))
            return [_node(r) for r in self.db.query(f"SELECT * FROM kg_ref_nodes WHERE node_key IN ({ph})", keys)]
        return []

    def all_nodes(self):
        # 1. Try ArangoDB
        if self.arango:
            try:
                docs = self.arango.query("FOR n IN kg_ref_nodes SORT n._key RETURN n")
                if docs:
                    return [_node(r) for r in docs]
            except Exception as e:
                log.warning("ArangoDB all_nodes failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            return [_node(r) for r in self.db.query("SELECT * FROM kg_ref_nodes ORDER BY id")]
        return []

    def all_edges(self):
        # 1. Try ArangoDB
        if self.arango:
            try:
                edges = self.arango.query(
                    """
                    FOR e IN kg_ref_edges
                      RETURN {
                        source_key: e.source_key ? e.source_key : e.source,
                        target_key: e.target_key ? e.target_key : e.target,
                        label: e.label,
                        origin_graph: e.origin_graph,
                        cross_domain: e.cross_domain
                      }
                    """
                )
                if edges:
                    return edges
            except Exception as e:
                log.warning("ArangoDB all_edges failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.query(
                "SELECT source_key, target_key, label, origin_graph, cross_domain FROM kg_ref_edges ORDER BY id"
            )
        return []

    def node(self, key):
        if not key:
            return None

        # 1. Try ArangoDB
        if self.arango:
            try:
                docs = self.arango.query(
                    "FOR n IN kg_ref_nodes FILTER n._key == @k OR n.node_key == @k LIMIT 1 RETURN n",
                    {"k": key}
                )
                if docs:
                    return _node(docs[0])
            except Exception as e:
                log.warning("ArangoDB node(%s) failed: %s", key, e)

        # 2. Fallback to MySQL
        if self.db:
            return _node(self.db.one("SELECT * FROM kg_ref_nodes WHERE node_key=%s", (key,)))
        return None

    def edges_within(self, keys):
        keys = list(keys)
        if not keys:
            return []

        # 1. Try ArangoDB
        if self.arango:
            try:
                edges = self.arango.query(
                    """
                    FOR e IN kg_ref_edges
                      FILTER (e.source_key IN @keys OR e.source IN @keys)
                         AND (e.target_key IN @keys OR e.target IN @keys)
                      RETURN {
                        source_key: e.source_key ? e.source_key : e.source,
                        target_key: e.target_key ? e.target_key : e.target,
                        label: e.label,
                        origin_graph: e.origin_graph,
                        cross_domain: e.cross_domain
                      }
                    """,
                    {"keys": keys}
                )
                if edges is not None:
                    return edges
            except Exception as e:
                log.warning("ArangoDB edges_within failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            ph = ",".join(["%s"] * len(keys))
            return self.db.query(
                f"SELECT source_key, target_key, label, origin_graph, cross_domain FROM kg_ref_edges "
                f"WHERE source_key IN ({ph}) AND target_key IN ({ph}) ORDER BY id", keys + keys)
        return []

    def edges_of(self, key):
        if not key:
            return []

        # 1. Try ArangoDB
        if self.arango:
            try:
                edges = self.arango.query(
                    """
                    FOR e IN kg_ref_edges
                      FILTER e.source_key == @k OR e.source == @k OR e.target_key == @k OR e.target == @k
                      RETURN {
                        source_key: e.source_key ? e.source_key : e.source,
                        target_key: e.target_key ? e.target_key : e.target,
                        label: e.label,
                        origin_graph: e.origin_graph,
                        cross_domain: e.cross_domain
                      }
                    """,
                    {"k": key}
                )
                if edges is not None:
                    return edges
            except Exception as e:
                log.warning("ArangoDB edges_of(%s) failed: %s", key, e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.query(
                "SELECT source_key, target_key, label, origin_graph, cross_domain FROM kg_ref_edges "
                "WHERE source_key=%s OR target_key=%s ORDER BY id", (key, key))
        return []

    def node_domains(self, key):
        if not key:
            return []

        # 1. Try ArangoDB
        if self.arango:
            try:
                res = self.arango.query(
                    """
                    FOR dn IN domain_nodes
                      FILTER dn.node_key == @k
                      FOR d IN domains
                        FILTER d.slug == dn.domain_slug OR d._key == dn.domain_slug
                        SORT d.sort_order
                        RETURN {slug: d.slug, code: d.code, name: d.name, role: dn.role}
                    """,
                    {"k": key}
                )
                if res is not None:
                    return res
            except Exception as e:
                log.warning("ArangoDB node_domains(%s) failed: %s", key, e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.query(
                "SELECT d.slug, d.code, d.name, dn.role FROM domain_nodes dn JOIN domains d ON d.id=dn.domain_id "
                "WHERE dn.node_key=%s ORDER BY d.sort_order", (key,))
        return []

    def search_nodes(self, q, domain_id=None, limit=60):
        # 1. Try ArangoDB
        if self.arango:
            try:
                low = str(q).lower()
                slug = str(domain_id) if domain_id else None
                aql = """
                FOR n IN kg_ref_nodes
                  LET matched_domain = (@slug == null ? true : (
                    LENGTH(FOR dn IN domain_nodes FILTER dn.node_key == n.node_key AND (dn.domain_slug == @slug OR dn.domain_id == @slug) RETURN 1) > 0
                  ))
                  FILTER matched_domain
                  FILTER CONTAINS(LOWER(n.name), @low)
                      OR CONTAINS(LOWER(n.node_key), @low)
                      OR CONTAINS(LOWER(n.definition), @low)
                  SORT n.name
                  LIMIT @limit
                  RETURN n
                """
                docs = self.arango.query(aql, {"low": low, "slug": slug, "limit": limit})
                if docs is not None:
                    return [_node(r) for r in docs]
            except Exception as e:
                log.warning("ArangoDB search_nodes failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            like = f"%{q}%"
            sql = ("SELECT n.* FROM kg_ref_nodes n " +
                   ("JOIN domain_nodes dn ON dn.node_key=n.node_key AND dn.domain_id=%s " if domain_id else "") +
                   "WHERE n.name LIKE %s OR n.node_key LIKE %s OR n.definition LIKE %s OR CAST(n.attributes AS CHAR) LIKE %s "
                   "OR CAST(n.formulas AS CHAR) LIKE %s OR CAST(n.canon_attributes AS CHAR) LIKE %s ORDER BY n.name LIMIT %s")
            params = ([domain_id] if domain_id else []) + [like] * 6 + [limit]
            return [_node(r) for r in self.db.query(sql, params)]
        return []

    # ---- flows
    def flows(self):
        # 1. Try ArangoDB
        if self.arango:
            try:
                fls = self.arango.query("FOR f IN flows SORT f.sort_order RETURN f")
                if fls:
                    for f in fls:
                        f.setdefault("id", f.get("flow_key") or f.get("_key"))
                    return fls
            except Exception as e:
                log.warning("ArangoDB flows() failed: %s", e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.query("SELECT * FROM flows ORDER BY sort_order")
        return []

    def flow(self, key):
        if not key:
            return None

        # 1. Try ArangoDB
        if self.arango:
            try:
                res = self.arango.query(
                    "FOR f IN flows FILTER f._key == @k OR f.flow_key == @k LIMIT 1 RETURN f",
                    {"k": key}
                )
                if res:
                    f = res[0]
                    f.setdefault("id", f.get("flow_key") or f.get("_key"))
                    return f
            except Exception as e:
                log.warning("ArangoDB flow(%s) failed: %s", key, e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.one("SELECT * FROM flows WHERE flow_key=%s", (key,))
        return None

    def flow_steps(self, flow_id):
        # 1. Try ArangoDB
        if self.arango:
            try:
                fid = str(flow_id)
                res = self.arango.query(
                    "FOR f IN flows FILTER f._key == @fid OR f.flow_key == @fid OR f.id == @fid LIMIT 1 RETURN f.steps",
                    {"fid": fid}
                )
                if res and res[0]:
                    steps = res[0]
                    return [{"step_order": idx + 1, "node_key": k} for idx, k in enumerate(steps)]
            except Exception as e:
                log.warning("ArangoDB flow_steps(%s) failed: %s", flow_id, e)

        # 2. Fallback to MySQL
        if self.db:
            return self.db.query("SELECT step_order, node_key FROM flow_steps WHERE flow_id=%s ORDER BY step_order", (flow_id,))
        return []
