import logging
from flask import current_app, g, jsonify, request

from app.db import get_db
from app.errors import ValidationError
from app.repositories.conversations import ConversationRepository
from app.security import auth_required
from app.services.graph_service import GraphService
from app.services.proposal_service import ProposalService
from ._helpers import audit, domain_wf, json_body, llm

log = logging.getLogger(__name__)


@auth_required
def versions(slug: str):
    domain, wf = domain_wf(slug)
    gs = GraphService(get_db())
    return jsonify({
        "versions": gs.versions(g.user_id, wf),
        "finalized_version_number": wf["finalized_version_number"],
    })


@auth_required
def version_graph(slug: str, number: int):
    domain, wf = domain_wf(slug)
    return jsonify(GraphService(get_db()).graph(g.user_id, wf, domain, number))


@auth_required
def working_graph(slug: str):
    """The graph the user is currently shaping: latest version, or the KG0 baseline for their selected blocks."""
    domain, wf = domain_wf(slug)
    gs = GraphService(get_db())
    n, _ = gs.working(g.user_id, wf, domain)
    res = gs.graph(g.user_id, wf, domain, n)
    try:
        from app.services.html_graph_service import HTMLGraphService
        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
        html_url = HTMLGraphService.save_html(slug, domain, res.get("nodes", []), res.get("edges", []), output_dir=graph_dir)
        res["html_url"] = html_url
    except Exception as e:
        log.warning("Could not auto-generate HTML graph for working graph: %s", e)
    return jsonify(res)


@auth_required
def graph_html(slug: str):
    """Returns the URL of the generated HTML graph, generating it if needed."""
    domain, wf = domain_wf(slug)
    from app.services.html_graph_service import HTMLGraphService
    graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
    p = HTMLGraphService.get_graph_path(graph_dir, slug)
    if not p.exists():
        gs = GraphService(get_db())
        try:
            n, _ = gs.working(g.user_id, wf, domain)
            gdata = gs.graph(g.user_id, wf, domain, n)
        except Exception:
            from app.services.reference_service import ReferenceService
            gdata = ReferenceService(get_db()).domain_graph(slug, neighbours=True)
        html_url = HTMLGraphService.save_html(slug, domain, gdata.get("nodes", []), gdata.get("edges", []), output_dir=graph_dir)
    else:
        clean_slug = slug.strip().replace(" ", "-").replace("%20", "-").lower()
        html_url = f"/api/graphs/{clean_slug}.html"

    return jsonify({"html_url": html_url, "exists": True, "slug": slug})


@auth_required
def generate_graph(slug: str):
    """Generate a custom Knowledge Graph via LLM and optionally persist it."""
    from app.services.kg_generator_service import KGGeneratorService
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

    try:
        import time
        from app.services.html_graph_service import HTMLGraphService
        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
        html_url = HTMLGraphService.save_html(slug, domain, out.get("nodes", []), out.get("edges", []), output_dir=graph_dir)
        out["html_url"] = f"{html_url}?t={int(time.time())}"
    except Exception as e:
        log.warning("Could not auto-generate HTML graph for generated graph: %s", e)

    return jsonify(out), 200


@auth_required
def compare(slug: str):
    domain, wf = domain_wf(slug)
    try:
        a, b = int(request.args.get("from", 0)), int(request.args.get("to"))
    except (TypeError, ValueError):
        raise ValidationError("Provide integer 'from' and 'to' version numbers.")
    return jsonify(GraphService(get_db()).compare(g.user_id, wf, domain, a, b))


@auth_required
def proposals(slug: str):
    domain, wf = domain_wf(slug)
    return jsonify({"proposals": ProposalService(get_db()).list(g.user_id, wf)})


@auth_required
def proposal(slug: str, pid: int):
    domain, wf = domain_wf(slug)
    return jsonify(ProposalService(get_db()).detail(g.user_id, wf, pid))


@auth_required
def decide(slug: str, pid: int):
    domain, wf = domain_wf(slug)
    d = json_body()
    ids = d.get("approved_item_ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
        raise ValidationError("approved_item_ids must be a list of integers.")
    out = ProposalService(get_db()).decide(g.user_id, wf, domain, pid, d.get("mode", "selected"), ids)
    audit("proposal.decision", "proposal", pid, {"mode": d.get("mode"), "version": out["version"]})
    return jsonify(out)


@auth_required
def finalization(slug: str):
    domain, wf = domain_wf(slug)
    return jsonify(GraphService(get_db()).finalization_preview(g.user_id, wf, domain))


@auth_required
def finalize(slug: str):
    domain, wf = domain_wf(slug)
    d = json_body()
    n = GraphService(get_db()).finalize(
        g.user_id,
        wf,
        domain,
        d.get("confirm") is True,
        d.get("acknowledge_warnings") is True,
    )
    audit("ontology.finalize", "domain", slug, {"version": n})
    return jsonify({"finalized_version": {"number": n, "label": f"KG{n}"}})


@auth_required
def reopen(slug: str):
    domain, wf = domain_wf(slug)
    GraphService(get_db()).reopen(g.user_id, wf, domain)
    audit("ontology.reopen", "domain", slug)
    return jsonify({"ok": True})


@auth_required
def experience(slug: str):
    domain, wf = domain_wf(slug)
    rows = ConversationRepository(get_db()).experience(g.user_id, wf["id"])
    return jsonify({
        "experience": [
            {
                "id": r["id"],
                "node_key": r["node_key"],
                "topic": r["topic"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    })
