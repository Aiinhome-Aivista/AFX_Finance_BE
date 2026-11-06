from flask import g, jsonify

from app.db import get_db
from app.security import auth_required
from app.services.chat_service import ChatService
from ._helpers import audit, domain_ctx, json_body, llm


def _svc():
    return ChatService(get_db(), llm())


@auth_required
def get(slug: str):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().get(g.user_id, domain))


@auth_required
def start(slug: str):
    domain, _ = domain_ctx(slug)
    out = _svc().start(g.user_id, domain)
    audit("chat.start", "domain", slug)
    return jsonify(out), 201


@auth_required
def answer(slug: str):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().answer(g.user_id, domain, json_body().get("message")))


@auth_required
def message(slug: str):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().chat_message(g.user_id, domain, d.get("message"), d.get("node_key")))


@auth_required
def ask_node(slug: str):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().ask_node(g.user_id, domain, d.get("node_key"), d.get("question")))


@auth_required
def focus(slug: str):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().focus(g.user_id, domain, json_body().get("node_key")))


@auth_required
def propose(slug: str):
    domain, _ = domain_ctx(slug)
    out = _svc().propose(g.user_id, domain)
    audit("chat.propose", "proposal", (out.get("proposal") or {}).get("id"))
    return jsonify(out)


@auth_required
def session_decision(slug: str):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().decide_session(g.user_id, domain, d.get("action")))
