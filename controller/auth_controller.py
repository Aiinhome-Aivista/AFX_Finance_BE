from flask import g, jsonify

from app.db import get_db
from app.security import auth_required
from app.services.auth_service import AuthService
from ._helpers import audit, json_body


def register():
    d = json_body()
    out = AuthService(get_db()).register(d.get("email"), d.get("display_name"), d.get("password"))
    g.user_id = out["user"]["id"]
    audit("auth.register", "user", g.user_id)
    return jsonify(out), 201


def login():
    d = json_body()
    out = AuthService(get_db()).login(d.get("email"), d.get("password"))
    g.user_id = out["user"]["id"]
    audit("auth.login", "user", g.user_id)
    return jsonify(out)


@auth_required
def logout():
    AuthService(get_db()).logout(g.token_claims)
    audit("auth.logout", "user", g.user_id)
    return jsonify({"ok": True})


@auth_required
def me():
    return jsonify({"user": AuthService(get_db()).me(g.user_id)})
