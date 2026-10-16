from ..db import Session, jdump


class AuditRepository:
    def __init__(self, db: Session):
        self.db = db

    def log(self, user_id, action, entity_type=None, entity_id=None, detail=None, ip=None):
        self.db.execute("INSERT INTO audit_logs(user_id,action,entity_type,entity_id,detail,ip) VALUES (%s,%s,%s,%s,%s,%s)",
                        (user_id, action, entity_type, str(entity_id) if entity_id is not None else None,
                         jdump(detail) if detail is not None else None, ip))
