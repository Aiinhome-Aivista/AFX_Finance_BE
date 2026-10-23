import datetime as dt
import uuid
from functools import wraps

import jwt
from flask import current_app, g, request
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .errors import AuthError


def hash_password(pw: str) -> str:
    return generate_password_hash(pw, method="pbkdf2:sha256:600000")


def verify_password(pw_hash: str, pw: str) -> bool:
    return check_password_hash(pw_hash, pw)


def issue_token(user_id: int) -> tuple[str, dt.datetime]:
    cfg = current_app.config["APP_CONFIG"]
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=cfg.JWT_EXPIRES_HOURS)
    payload = {"sub": str(user_id), "jti": uuid.uuid4().hex, "exp": exp, "iat": dt.datetime.now(dt.timezone.utc)}
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
    """Protects an API route; sets g.user_id / g.token_claims. Every repository call is then scoped by g.user_id."""

    @wraps(fn)
    def wrapper(*a, **kw):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthError("Authentication required.")
        claims = decode_token(header[7:])
        row = get_db().one("SELECT jti FROM revoked_tokens WHERE jti=%s", (claims["jti"],))
        if row:
            raise AuthError("This session was signed out.")
        g.user_id = int(claims["sub"])
        g.token_claims = claims
        return fn(*a, **kw)

    return wrapper


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

