from flask import Blueprint, g, jsonify

from ..db import get_db
from ..security import auth_required
from ..services.auth_service import AuthService
from ._helpers import audit, json_body

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/register")
def register():
    d = json_body()
    out = AuthService(get_db()).register(d.get("email"), d.get("display_name"), d.get("password"))
    g.user_id = out["user"]["id"]
    audit("auth.register", "user", g.user_id)
    return jsonify(out), 201


@bp.post("/login")
def login():
    d = json_body()
    out = AuthService(get_db()).login(d.get("email"), d.get("password"))
    g.user_id = out["user"]["id"]
    audit("auth.login", "user", g.user_id)
    return jsonify(out)


@bp.post("/logout")
@auth_required
def logout():
    AuthService(get_db()).logout(g.token_claims)
    audit("auth.logout", "user", g.user_id)
    return jsonify({"ok": True})


@bp.get("/me")
@auth_required
def me():
    return jsonify({"user": AuthService(get_db()).me(g.user_id)})
