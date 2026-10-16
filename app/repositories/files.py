from ..db import Session, jload, jdump


def _f(r):
    if r:
        r["metadata"] = jload(r.get("metadata"), None)
        r["preview"] = jload(r.get("preview"), None)
    return r


class FileRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id, workflow_id, domain_id, version_number, original, stored, ext, size, sha):
        return self.db.insert(
            "INSERT INTO uploaded_files(user_id,workflow_id,domain_id,graph_version_number,original_name,stored_name,ext,size_bytes,sha256) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (user_id, workflow_id, domain_id, version_number, original, stored, ext, size, sha))

    def get(self, user_id, file_id):
        return _f(self.db.one("SELECT * FROM uploaded_files WHERE id=%s AND user_id=%s", (file_id, user_id)))

    def get_system(self, file_id):  # background worker only (job already bound to an owner)
        return _f(self.db.one("SELECT * FROM uploaded_files WHERE id=%s", (file_id,)))

    def list(self, user_id, workflow_id):
        return [_f(r) for r in self.db.query("SELECT * FROM uploaded_files WHERE user_id=%s AND workflow_id=%s ORDER BY id DESC", (user_id, workflow_id))]

    def delete(self, user_id, file_id):
        return self.db.execute("DELETE FROM uploaded_files WHERE id=%s AND user_id=%s", (file_id, user_id))

    def set_status(self, file_id, status, error=None, metadata=None, preview=None):
        self.db.execute(
            "UPDATE uploaded_files SET status=%s, error=%s, metadata=COALESCE(%s,metadata), preview=COALESCE(%s,preview), "
            "processed_at=IF(%s IN ('processed','failed'), NOW(), processed_at) WHERE id=%s",
            (status, error, jdump(metadata) if metadata is not None else None, jdump(preview) if preview is not None else None, status, file_id))

    # ---- jobs
    def create_job(self, file_id, user_id):
        return self.db.insert("INSERT INTO processing_jobs(file_id,user_id,status) VALUES (%s,%s,'queued')", (file_id, user_id))

    def job_for_file(self, file_id):
        r = self.db.one("SELECT * FROM processing_jobs WHERE file_id=%s ORDER BY id DESC LIMIT 1", (file_id,))
        if r:
            r["result"] = jload(r["result"], None)
        return r

    def update_job(self, job_id, status, error=None, result=None):
        self.db.execute(
            "UPDATE processing_jobs SET status=%s, error=%s, result=COALESCE(%s,result), "
            "started_at=IF(%s='running', NOW(), started_at), finished_at=IF(%s IN ('succeeded','failed'), NOW(), finished_at) WHERE id=%s",
            (status, error, jdump(result) if result is not None else None, status, status, job_id))
