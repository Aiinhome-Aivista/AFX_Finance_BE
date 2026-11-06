from ..db import Session, jload, jdump


class ConversationRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id, workflow_id):
        r = self.db.one("SELECT * FROM conversations WHERE user_id=%s AND workflow_id=%s", (user_id, workflow_id))
        if r:
            r["plan"] = jload(r["plan"], [])
        return r

    def create(self, user_id, workflow_id, plan):
        cid = self.db.insert("INSERT INTO conversations(user_id,workflow_id,plan) VALUES (%s,%s,%s)", (user_id, workflow_id, jdump(plan)))
        return cid

    def update_plan(self, conv_id, plan):
        self.db.execute("UPDATE conversations SET plan=%s WHERE id=%s", (jdump(plan), conv_id))

    def add_message(self, conv_id, role, content, kind="chat", meta=None):
        return self.db.insert("INSERT INTO conversation_messages(conversation_id,role,kind,content,meta) VALUES (%s,%s,%s,%s,%s)",
                              (conv_id, role, kind, content, jdump(meta) if meta is not None else None))

    def messages(self, conv_id):
        rows = self.db.query("SELECT id, role, kind, content, meta, created_at FROM conversation_messages WHERE conversation_id=%s ORDER BY id", (conv_id,))
        for r in rows:
            r["meta"] = jload(r["meta"], None)
        return rows

    # ---- questions
    def add_question(self, conv_id, seq, node_key, topic, text, is_followup=False):
        return self.db.insert(
            "INSERT INTO chat_questions(conversation_id,seq,node_key,topic,is_followup,question_text) VALUES (%s,%s,%s,%s,%s,%s)",
            (conv_id, seq, node_key, topic, int(is_followup), text))

    def questions(self, conv_id):
        rows = self.db.query("SELECT * FROM chat_questions WHERE conversation_id=%s ORDER BY seq", (conv_id,))
        for r in rows:
            r["extracted_ops"] = jload(r["extracted_ops"], [])
        return rows

    def open_question(self, conv_id):
        r = self.db.one("SELECT * FROM chat_questions WHERE conversation_id=%s AND status='asked' ORDER BY seq DESC LIMIT 1", (conv_id,))
        if r:
            r["extracted_ops"] = jload(r["extracted_ops"], [])
        return r

    def answer_question(self, qid, answer, ops, status="answered"):
        self.db.execute("UPDATE chat_questions SET status=%s, answer_text=%s, extracted_ops=%s, answered_at=NOW() WHERE id=%s",
                        (status, answer, jdump(ops), qid))

    def unproposed(self, conv_id):
        rows = self.db.query("SELECT * FROM chat_questions WHERE conversation_id=%s AND status='answered' AND proposed=0 ORDER BY seq", (conv_id,))
        for r in rows:
            r["extracted_ops"] = jload(r["extracted_ops"], [])
        return rows

    def mark_proposed(self, ids):
        if ids:
            self.db.execute(f"UPDATE chat_questions SET proposed=1 WHERE id IN ({','.join(['%s'] * len(ids))})", list(ids))

    # ---- business experience
    def add_experience(self, user_id, workflow_id, question_id, node_key, topic, content):
        return self.db.insert(
            "INSERT INTO business_experience(user_id,workflow_id,question_id,node_key,topic,content) VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, workflow_id, question_id, node_key, topic, content))

    def experience(self, user_id, workflow_id):
        return self.db.query("SELECT * FROM business_experience WHERE user_id=%s AND workflow_id=%s ORDER BY id", (user_id, workflow_id))
