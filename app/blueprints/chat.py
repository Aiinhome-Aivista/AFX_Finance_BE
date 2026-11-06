from flask import Blueprint, g, jsonify

from ..db import get_db
from ..security import auth_required
from ..services.chat_service import ChatService
from ._helpers import audit, domain_ctx, json_body, llm

bp = Blueprint("chat", __name__, url_prefix="/api/domains/<slug>/conversation")


def _svc():
    return ChatService(get_db(), llm())


@bp.get("")
@auth_required
def get(slug):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().get(g.user_id, domain))


@bp.post("")
@auth_required
def start(slug):
    domain, _ = domain_ctx(slug)
    out = _svc().start(g.user_id, domain)
    audit("chat.start", "domain", slug)
    return jsonify(out), 201


@bp.post("/answer")
@auth_required
def answer(slug):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().answer(g.user_id, domain, json_body().get("message")))


@bp.post("/message")
@auth_required
def message(slug):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().chat_message(g.user_id, domain, d.get("message"), d.get("node_key")))


@bp.post("/ask-node")
@auth_required
def ask_node(slug):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().ask_node(g.user_id, domain, d.get("node_key"), d.get("question")))


@bp.post("/focus")
@auth_required
def focus(slug):
    domain, _ = domain_ctx(slug)
    return jsonify(_svc().focus(g.user_id, domain, json_body().get("node_key")))


@bp.post("/propose")
@auth_required
def propose(slug):
    domain, _ = domain_ctx(slug)
    out = _svc().propose(g.user_id, domain)
    audit("chat.propose", "proposal", (out.get("proposal") or {}).get("id"))
    return jsonify(out)


@bp.post("/decision")
@auth_required
def session_decision(slug):
    domain, _ = domain_ctx(slug)
    d = json_body()
    return jsonify(_svc().decide_session(g.user_id, domain, d.get("action")))
