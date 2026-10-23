from ..db import Session, jload, jdump


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
             .replace("Â§", "§")
             .replace("Â", ""))


def _n(r):
    r = dict(r)
    if "name" in r:
        r["name"] = _clean_str(r["name"])
    if "definition" in r:
        r["definition"] = _clean_str(r["definition"])
    raw_attrs = jload(r["attributes"], [])
    r["attributes"] = [_clean_str(a) if isinstance(a, str) else a for a in raw_attrs]
    raw_forms = jload(r["formulas"], [])
    r["formulas"] = [_clean_str(f) if isinstance(f, str) else f for f in raw_forms]
    if "evidence" in r:
        r["evidence"] = _clean_str(r["evidence"])
    if "notes" in r:
        r["notes"] = _clean_str(r["notes"])
    return r


class GraphRepository:
    """User-owned graph versions (KG1, KG2, ...). Never touches the reference tables."""

    def __init__(self, db: Session):
        self.db = db

    def versions(self, user_id, workflow_id):
        return self.db.query(
            "SELECT v.*, (SELECT COUNT(*) FROM user_graph_nodes WHERE version_id=v.id) AS node_count, "
            "(SELECT COUNT(*) FROM user_graph_edges WHERE version_id=v.id) AS edge_count "
            "FROM user_graph_versions v WHERE v.user_id=%s AND v.workflow_id=%s ORDER BY v.version_number", (user_id, workflow_id))

    def version(self, user_id, workflow_id, number):
        return self.db.one("SELECT * FROM user_graph_versions WHERE user_id=%s AND workflow_id=%s AND version_number=%s",
                           (user_id, workflow_id, number))

    def latest(self, user_id, workflow_id):
        return self.db.one("SELECT * FROM user_graph_versions WHERE user_id=%s AND workflow_id=%s ORDER BY version_number DESC LIMIT 1",
                           (user_id, workflow_id))

    def create_version(self, user_id, workflow_id, number, kind, parent, proposal_id, summary):
        return self.db.insert(
            "INSERT INTO user_graph_versions(user_id,workflow_id,version_number,label,kind,parent_version_number,proposal_id,summary) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (user_id, workflow_id, number, f"KG{number}", kind, parent, proposal_id, summary))

    def insert_nodes(self, version_id, nodes):
        self.db.executemany(
            "INSERT INTO user_graph_nodes(version_id,node_key,name,ref_name,group_key,group_name,color,kind,role,definition,"
            "attributes,formulas,evidence,notes,origin,change_state,added_in_version) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(version_id, n["key"], n["name"], n.get("ref_name"), n["group_key"], n["group_name"], n["color"], n["kind"],
              n.get("role", "core"), n["definition"], jdump(n["attributes"]), jdump(n["formulas"]), n.get("evidence"),
              n.get("notes"), n["origin"], n["change_state"], n["added_in_version"]) for n in nodes])

    def insert_edges(self, version_id, edges):
        self.db.executemany(
            "INSERT INTO user_graph_edges(version_id,source_key,target_key,label,origin,change_state,added_in_version,cross_domain) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(version_id, e["source"], e["target"], e["label"], e["origin"], e["change_state"], e["added_in_version"],
              int(e.get("cross_domain", False))) for e in edges])

    def nodes(self, version_id):
        out = []
        for r in self.db.query("SELECT * FROM user_graph_nodes WHERE version_id=%s ORDER BY id", (version_id,)):
            r = _n(r)
            r["key"] = r.pop("node_key")
            out.append(r)
        return out

    def edges(self, version_id):
        out = []
        for r in self.db.query("SELECT * FROM user_graph_edges WHERE version_id=%s ORDER BY id", (version_id,)):
            r = dict(r)
            r["source"], r["target"] = r.pop("source_key"), r.pop("target_key")
            r["cross_domain"] = bool(r["cross_domain"])
            out.append(r)
        return out

    def mark_status(self, version_id, status):
        self.db.execute("UPDATE user_graph_versions SET status=%s WHERE id=%s", (status, version_id))

    def clear_finalized(self, workflow_id):
        self.db.execute("UPDATE user_graph_versions SET status='active' WHERE workflow_id=%s", (workflow_id,))
