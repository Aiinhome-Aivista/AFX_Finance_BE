from flask import Blueprint, g, jsonify, request

from ..db import get_db
from ..errors import ValidationError
from ..repositories.conversations import ConversationRepository
from ..security import auth_required
from ..services.graph_service import GraphService
from ..services.proposal_service import ProposalService
from ._helpers import audit, domain_wf, json_body, llm

bp = Blueprint("graphs", __name__, url_prefix="/api/domains/<slug>")


@bp.get("/versions")
@auth_required
def versions(slug):
    domain, wf = domain_wf(slug)
    gs = GraphService(get_db())
    return jsonify({"versions": gs.versions(g.user_id, wf), "finalized_version_number": wf["finalized_version_number"]})


@bp.get("/versions/<int:number>/graph")
@auth_required
def version_graph(slug, number):
    domain, wf = domain_wf(slug)
    return jsonify(GraphService(get_db()).graph(g.user_id, wf, domain, number))


@bp.get("/working-graph")
@auth_required
def working_graph(slug):
    """The graph the user is currently shaping: latest version, or the KG0 baseline for their selected blocks."""
    domain, wf = domain_wf(slug)
    gs = GraphService(get_db())
    n, _ = gs.working(g.user_id, wf, domain)
    return jsonify(gs.graph(g.user_id, wf, domain, n))


@bp.post("/generate-graph")
@auth_required
def generate_graph(slug):
    """Generate a custom Knowledge Graph via LLM and optionally persist it."""
    from ..services.kg_generator_service import KGGeneratorService
    domain, wf = domain_wf(slug)
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    persist = body.get("persist", True)

    gen = KGGeneratorService(get_db(), llm())
    if persist and wf:
        out = gen.generate_and_persist(g.user_id, domain, wf, prompt)
        audit("graph.generate_llm", "version", out.get("version", {}).get("number"))
    else:
        out = gen.generate_graph(domain, prompt)
        audit("graph.generate_llm_preview", "domain", slug)
    return jsonify(out), 200


@bp.get("/compare")
@auth_required
def compare(slug):
    domain, wf = domain_wf(slug)
    try:
        a, b = int(request.args.get("from", 0)), int(request.args.get("to"))
    except (TypeError, ValueError):
        raise ValidationError("Provide integer 'from' and 'to' version numbers.")
    return jsonify(GraphService(get_db()).compare(g.user_id, wf, domain, a, b))


@bp.get("/proposals")
@auth_required
def proposals(slug):
    domain, wf = domain_wf(slug)
    return jsonify({"proposals": ProposalService(get_db()).list(g.user_id, wf)})


@bp.get("/proposals/<int:pid>")
@auth_required
def proposal(slug, pid):
    domain, wf = domain_wf(slug)
    return jsonify(ProposalService(get_db()).detail(g.user_id, wf, pid))


@bp.post("/proposals/<int:pid>/decision")
@auth_required
def decide(slug, pid):
    domain, wf = domain_wf(slug)
    d = json_body()
    ids = d.get("approved_item_ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
        raise ValidationError("approved_item_ids must be a list of integers.")
    out = ProposalService(get_db()).decide(g.user_id, wf, domain, pid, d.get("mode", "selected"), ids)
    audit("proposal.decision", "proposal", pid, {"mode": d.get("mode"), "version": out["version"]})
    return jsonify(out)


@bp.get("/finalization")
@auth_required
def finalization(slug):
    domain, wf = domain_wf(slug)
    return jsonify(GraphService(get_db()).finalization_preview(g.user_id, wf, domain))


@bp.post("/finalize")
@auth_required
def finalize(slug):
    domain, wf = domain_wf(slug)
    d = json_body()
    n = GraphService(get_db()).finalize(g.user_id, wf, domain, d.get("confirm") is True, d.get("acknowledge_warnings") is True)
    audit("ontology.finalize", "domain", slug, {"version": n})
    return jsonify({"finalized_version": {"number": n, "label": f"KG{n}"}})


@bp.post("/reopen")
@auth_required
def reopen(slug):
    domain, wf = domain_wf(slug)
    GraphService(get_db()).reopen(g.user_id, wf, domain)
    audit("ontology.reopen", "domain", slug)
    return jsonify({"ok": True})


@bp.get("/experience")
@auth_required
def experience(slug):
    domain, wf = domain_wf(slug)
    rows = ConversationRepository(get_db()).experience(g.user_id, wf["id"])
    return jsonify({"experience": [{"id": r["id"], "node_key": r["node_key"], "topic": r["topic"], "content": r["content"],
                                    "created_at": r["created_at"]} for r in rows]})
