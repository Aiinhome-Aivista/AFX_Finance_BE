"""READ-ONLY access to KG0. There are intentionally no write methods in this module."""
from ..db import Session, jload


def _node(r):
    if not r:
        return None
    r = dict(r)
    r["attributes"] = jload(r["attributes"], [])
    r["formulas"] = jload(r["formulas"], [])
    r["canon_attributes"] = jload(r.get("canon_attributes"), [])
    return r


class ReferenceRepository:
    def __init__(self, db: Session):
        self.db = db

    # ---- domains
    def domains(self):
        rows = self.db.query("SELECT * FROM domains ORDER BY sort_order")
        for r in rows:
            r["knowledge"] = jload(r["knowledge"], {})
        return rows

    def domain_by_slug(self, slug):
        r = self.db.one("SELECT * FROM domains WHERE slug=%s", (slug,))
        if r:
            r["knowledge"] = jload(r["knowledge"], {})
        return r

    def domain_node_roles(self, domain_id):
        return {r["node_key"]: r["role"] for r in
                self.db.query("SELECT node_key, role FROM domain_nodes WHERE domain_id=%s", (domain_id,))}

    def domain_node_counts(self):
        rows = self.db.query("SELECT domain_id, role, COUNT(*) c FROM domain_nodes GROUP BY domain_id, role")
        out = {}
        for r in rows:
            out.setdefault(r["domain_id"], {})[r["role"]] = r["c"]
        return out

    # ---- nodes / edges
    def nodes_by_keys(self, keys):
        keys = list(keys)
        if not keys:
            return []
        ph = ",".join(["%s"] * len(keys))
        return [_node(r) for r in self.db.query(f"SELECT * FROM kg_ref_nodes WHERE node_key IN ({ph})", keys)]

    def node(self, key):
        return _node(self.db.one("SELECT * FROM kg_ref_nodes WHERE node_key=%s", (key,)))

    def edges_within(self, keys):
        keys = list(keys)
        if not keys:
            return []
        ph = ",".join(["%s"] * len(keys))
        return self.db.query(
            f"SELECT source_key, target_key, label, origin_graph, cross_domain FROM kg_ref_edges "
            f"WHERE source_key IN ({ph}) AND target_key IN ({ph}) ORDER BY id", keys + keys)

    def edges_of(self, key):
        return self.db.query(
            "SELECT source_key, target_key, label, origin_graph, cross_domain FROM kg_ref_edges "
            "WHERE source_key=%s OR target_key=%s ORDER BY id", (key, key))

    def node_domains(self, key):
        return self.db.query(
            "SELECT d.slug, d.code, d.name, dn.role FROM domain_nodes dn JOIN domains d ON d.id=dn.domain_id "
            "WHERE dn.node_key=%s ORDER BY d.sort_order", (key,))

    def search_nodes(self, q, domain_id=None, limit=60):
        like = f"%{q}%"
        sql = ("SELECT n.* FROM kg_ref_nodes n " +
               ("JOIN domain_nodes dn ON dn.node_key=n.node_key AND dn.domain_id=%s " if domain_id else "") +
               "WHERE n.name LIKE %s OR n.node_key LIKE %s OR n.definition LIKE %s OR CAST(n.attributes AS CHAR) LIKE %s "
               "OR CAST(n.formulas AS CHAR) LIKE %s OR CAST(n.canon_attributes AS CHAR) LIKE %s ORDER BY n.name LIMIT %s")
        params = ([domain_id] if domain_id else []) + [like] * 6 + [limit]
        return [_node(r) for r in self.db.query(sql, params)]

    # ---- flows
    def flows(self):
        return self.db.query("SELECT * FROM flows ORDER BY sort_order")

    def flow(self, key):
        return self.db.one("SELECT * FROM flows WHERE flow_key=%s", (key,))

    def flow_steps(self, flow_id):
        return self.db.query("SELECT step_order, node_key FROM flow_steps WHERE flow_id=%s ORDER BY step_order", (flow_id,))
