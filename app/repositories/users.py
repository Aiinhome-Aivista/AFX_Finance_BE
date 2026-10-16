from ..db import Session


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, email, display_name, password_hash):
        return self.db.insert("INSERT INTO users(email,display_name,password_hash) VALUES (%s,%s,%s)",
                              (email, display_name, password_hash))

    def by_email(self, email):
        return self.db.one("SELECT * FROM users WHERE email=%s", (email,))

    def by_id(self, uid):
        return self.db.one("SELECT id,email,display_name,selected_app,created_at,last_login_at FROM users WHERE id=%s", (uid,))

    def touch_login(self, uid):
        self.db.execute("UPDATE users SET last_login_at=NOW() WHERE id=%s", (uid,))

    def set_selected_app(self, uid, app):
        self.db.execute("UPDATE users SET selected_app=%s WHERE id=%s", (app, uid))

    def revoke_token(self, jti, uid, exp):
        self.db.execute("INSERT IGNORE INTO revoked_tokens(jti,user_id,expires_at) VALUES (%s,%s,%s)", (jti, uid, exp))
        self.db.execute("DELETE FROM revoked_tokens WHERE expires_at < NOW()")
