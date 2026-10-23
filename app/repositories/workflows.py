from ..db import Session, jload, jdump


class WorkflowRepository:
    def __init__(self, db: Session):
        self.db = db

    def _resolve_domain_id(self, domain_id):
        # 1. If it's a slug string, lookup id by slug
        if isinstance(domain_id, str) and not domain_id.isdigit():
            row = self.db.one("SELECT id FROM domains WHERE slug=%s", (domain_id,))
            if row and row.get("id"):
                return row["id"]

        # 2. If it's numeric, check if this id exists in domains table
        num_id = int(domain_id) if str(domain_id).isdigit() else None
        if num_id is not None:
            row = self.db.one("SELECT id FROM domains WHERE id=%s", (num_id,))
            if row and row.get("id"):
                return row["id"]

            # If not found directly, map static ID (1-8) to known slug and find real DB id
            from .reference import DOMAIN_SLUG_TO_ID
            id_to_slug = {v: k for k, v in DOMAIN_SLUG_TO_ID.items()}
            mapped_slug = id_to_slug.get(num_id)
            if mapped_slug:
                row = self.db.one("SELECT id FROM domains WHERE slug=%s", (mapped_slug,))
                if row and row.get("id"):
                    return row["id"]

        return domain_id

    def get(self, user_id, domain_id):
        domain_id = self._resolve_domain_id(domain_id)
        return self._hydrate(self.db.one("SELECT * FROM workflows WHERE user_id=%s AND domain_id=%s", (user_id, domain_id)))

    def list_for_user(self, user_id):
        return [self._hydrate(r) for r in self.db.query(
            "SELECT w.*, d.slug AS domain_slug FROM workflows w JOIN domains d ON d.id=w.domain_id WHERE w.user_id=%s", (user_id,))]

    def create(self, user_id, domain_id):
        domain_id = self._resolve_domain_id(domain_id)
        wid = self.db.insert("INSERT INTO workflows(user_id,domain_id,current_step) VALUES (%s,%s,3)", (user_id, domain_id))
        for step in range(1, 7):
            status = "completed" if step <= 2 else ("active" if step == 3 else "locked")
            self.db.execute("INSERT INTO workflow_steps(workflow_id,step_no,status,completed_at) VALUES (%s,%s,%s,%s)",
                            (wid, step, status, None if status != "completed" else self._now()))
        return wid

    @staticmethod
    def _now():
        import datetime as dt
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _hydrate(self, r):
        if r:
            r["selected_blocks"] = jload(r.get("selected_blocks"), None)
        return r

    def steps(self, workflow_id):
        return self.db.query("SELECT step_no, status, completed_at FROM workflow_steps WHERE workflow_id=%s ORDER BY step_no", (workflow_id,))

    def set_step_status(self, workflow_id, step_no, status):
        self.db.execute("UPDATE workflow_steps SET status=%s, completed_at=%s WHERE workflow_id=%s AND step_no=%s",
                        (status, self._now() if status == "completed" else None, workflow_id, step_no))

    def set_current_step(self, workflow_id, step):
        self.db.execute("UPDATE workflows SET current_step=%s WHERE id=%s", (step, workflow_id))

    def set_blocks(self, workflow_id, blocks):
        self.db.execute("UPDATE workflows SET selected_blocks=%s WHERE id=%s", (jdump(blocks), workflow_id))

    def set_finalized(self, workflow_id, version_number):
        if version_number is None:
            self.db.execute("UPDATE workflows SET finalized_version_number=NULL, finalized_at=NULL WHERE id=%s", (workflow_id,))
        else:
            self.db.execute("UPDATE workflows SET finalized_version_number=%s, finalized_at=NOW() WHERE id=%s", (version_number, workflow_id))
