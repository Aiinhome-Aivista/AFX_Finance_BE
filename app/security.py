import datetime as dt
import uuid
from functools import wraps

import jwt
from flask import current_app, g, request
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .errors import AuthError, ForbiddenError


def hash_password(pw: str) -> str:
    return generate_password_hash(pw, method="pbkdf2:sha256:600000")


def verify_password(pw_hash: str, pw: str) -> bool:
    return check_password_hash(pw_hash, pw)


def issue_token(user_id: int, role: str = "data_architect") -> tuple[str, dt.datetime]:
    cfg = current_app.config["APP_CONFIG"]
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=cfg.JWT_EXPIRES_HOURS)
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": uuid.uuid4().hex,
        "exp": exp,
        "iat": dt.datetime.now(dt.timezone.utc),
    }
    return jwt.encode(payload, cfg.SECRET_KEY, algorithm="HS256"), exp


def decode_token(token: str) -> dict:
    cfg = current_app.config["APP_CONFIG"]
    try:
        return jwt.decode(token, cfg.SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise AuthError("Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise AuthError("Invalid authentication token.")


def auth_required(fn):
    """Protects an API route; sets g.user_id / g.user_role / g.token_claims. Every repository call is scoped cleanly."""

    @wraps(fn)
    def wrapper(*a, **kw):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthError("Authentication required.")
        claims = decode_token(header[7:])
        db = get_db()
        row = db.one("SELECT jti FROM revoked_tokens WHERE jti=%s", (claims["jti"],))
        if row:
            raise AuthError("This session was signed out.")
        uid = int(claims["sub"])
        try:
            u = db.one("SELECT role FROM users WHERE id=%s", (uid,))
        except Exception:
            base = db.one("SELECT id FROM users WHERE id=%s", (uid,))
            u = {"role": claims.get("role") or "data_architect"} if base else None
        if not u:
            raise AuthError("Account no longer exists.")
        g.user_id = uid
        g.user_role = u.get("role") or claims.get("role") or "data_architect"
        g.token_claims = claims
        return fn(*a, **kw)

    return wrapper


def role_required(*allowed_roles):
    """Restricts a route to specific user roles (e.g. 'data_architect'). Must run after @auth_required."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            role = getattr(g, "user_role", None)
            if role not in allowed_roles:
                raise ForbiddenError("Your role does not have permission to perform this action.")
            return fn(*a, **kw)
        return wrapper
    return decorator


def auth_optional(fn):
    """Allows authenticated or unauthenticated access. Sets g.user_id if token is present and valid."""

    @wraps(fn)
    def wrapper(*a, **kw):
        g.user_id = None
        g.token_claims = None
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            try:
                claims = decode_token(header[7:])
                g.user_id = int(claims.get("sub", 0)) or None
                g.token_claims = claims
            except Exception:
                pass
        return fn(*a, **kw)

    return wrapper

