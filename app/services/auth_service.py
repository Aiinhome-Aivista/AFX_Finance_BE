import datetime as dt
import re

from ..errors import AuthError, ConflictError, ValidationError
from ..repositories.users import UserRepository
from ..security import hash_password, issue_token, verify_password

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[^@\s]{2,24}$")


def _public(u):
    return {"id": u["id"], "email": u["email"], "display_name": u["display_name"], "selected_app": u["selected_app"]}


class AuthService:
    def __init__(self, db):
        self.users = UserRepository(db)

    def register(self, email, display_name, password):
        email = (email or "").strip().lower()
        display_name = (display_name or "").strip()
        if not EMAIL_RE.match(email):
            raise ValidationError("Enter a valid email address.")
        if not (2 <= len(display_name) <= 120):
            raise ValidationError("Name must be between 2 and 120 characters.")
        if len(password or "") < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            raise ValidationError("Password must be at least 8 characters and include a letter and a number.")
        if self.users.by_email(email):
            raise ConflictError("An account with this email already exists.")
        uid = self.users.create(email, display_name, hash_password(password))
        token, exp = issue_token(uid)
        return {"token": token, "expires_at": exp.isoformat(), "user": _public(self.users.by_id(uid))}

    def login(self, email, password):
        u = self.users.by_email((email or "").strip().lower())
        # same message for unknown email / wrong password (no account enumeration)
        if not u or not verify_password(u["password_hash"], password or ""):
            raise AuthError("Incorrect email or password.")
        self.users.touch_login(u["id"])
        token, exp = issue_token(u["id"])
        return {"token": token, "expires_at": exp.isoformat(), "user": _public(self.users.by_id(u["id"]))}

    def logout(self, claims):
        exp = dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc).replace(tzinfo=None)
        self.users.revoke_token(claims["jti"], int(claims["sub"]), exp)

    def me(self, user_id):
        u = self.users.by_id(user_id)
        if not u:
            raise AuthError("Account no longer exists.")
        return _public(u)
