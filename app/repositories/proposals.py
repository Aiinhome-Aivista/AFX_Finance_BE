from ..db import Session, jload, jdump


class ProposalRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id, workflow_id, base_version, source, title, summary, llm_used):
        return self.db.insert(
            "INSERT INTO change_proposals(user_id,workflow_id,base_version_number,source,title,summary,llm_used) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (user_id, workflow_id, base_version, source, title, summary, int(llm_used)))

    def add_item(self, proposal_id, seq, op, payload, description, rationale, warnings):
        return self.db.insert(
            "INSERT INTO change_items(proposal_id,seq,op,payload,description,rationale,warnings) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (proposal_id, seq, op, jdump(payload), description[:500], rationale, jdump(warnings or [])))

    def get(self, user_id, proposal_id):
        return self.db.one("SELECT * FROM change_proposals WHERE id=%s AND user_id=%s", (proposal_id, user_id))

    def list(self, user_id, workflow_id):
        return self.db.query("SELECT * FROM change_proposals WHERE user_id=%s AND workflow_id=%s ORDER BY id DESC", (user_id, workflow_id))

    def items(self, proposal_id):
        rows = self.db.query("SELECT * FROM change_items WHERE proposal_id=%s ORDER BY seq", (proposal_id,))
        for r in rows:
            r["payload"] = jload(r["payload"], {})
            r["warnings"] = jload(r["warnings"], [])
        return rows

    def set_item_status(self, item_id, status):
        self.db.execute("UPDATE change_items SET status=%s WHERE id=%s", (status, item_id))

    def decide(self, proposal_id, status, result_version=None):
        self.db.execute("UPDATE change_proposals SET status=%s, decided_at=NOW(), result_version_number=%s WHERE id=%s",
                        (status, result_version, proposal_id))

    def supersede_pending(self, user_id, workflow_id, source=None, except_id=None):
        sql = "UPDATE change_proposals SET status='superseded', decided_at=NOW() WHERE user_id=%s AND workflow_id=%s AND status='pending'"
        params = [user_id, workflow_id]
        if source:
            sql += " AND source=%s"; params.append(source)
        if except_id:
            sql += " AND id<>%s"; params.append(except_id)
        self.db.execute(sql, params)
