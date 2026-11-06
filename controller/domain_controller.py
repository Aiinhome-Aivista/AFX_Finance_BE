from flask import current_app, g, jsonify, request

from app.db import get_db
from app.security import auth_optional, auth_required
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService
from ._helpers import audit, json_body


def _flag(name, default=True):
    v = request.args.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@auth_required
def apps():
    return jsonify({"apps": WorkflowService(get_db()).apps()})


@auth_required
def select_app():
    out = WorkflowService(get_db()).select_app(g.user_id, json_body().get("app"))
    audit("app.select", "app", out["selected_app"])
    return jsonify(out)


@auth_required
def list_domains():
    db = get_db()
    return jsonify({
        "domains": ReferenceService(db).list_domains(request.args.get("q")),
        "progress": WorkflowService(db).overview(g.user_id),
    })


@auth_optional
def overview_graph():
    """Returns the complete dynamic finance ontology overview graph from ArangoDB / database."""
    ref = ReferenceService()
    res = ref.overview_graph()
    base_url = current_app.config["APP_CONFIG"].BASE_URL
    overview_url = f"{base_url}/api/graphs/finance-overview.html"
    for d in res.get("domains", []):
        slug = d.get("slug")
        if slug:
            d["graph_url"] = f"{base_url}/api/graphs/{slug}.html"
    progress = {}
    if g.user_id:
        try:
            db = get_db()
            progress = WorkflowService(db).overview(g.user_id)
        except Exception:
            pass

    # Ensure graph_url is the very FIRST key in the response JSON
    response_data = {
        "graph_url": overview_url,
        "counts": res.get("counts", {}),
        "domains": res.get("domains", []),
        "nodes": res.get("nodes", []),
        "edges": res.get("edges", []),
        "flows": res.get("flows", []),
        "progress": progress,
    }
    return jsonify(response_data)


@auth_required
def domain(slug: str):
    db = get_db()
    ref = ReferenceService(db)
    d = ref.domain_detail(slug)
    d["workflow"] = WorkflowService(db).state(g.user_id, ref.domain(slug))
    return jsonify(d)


@auth_required
def domain_graph(slug: str):
    """Domain graph: returns latest customized working graph if available, else KG0 baseline."""
    db = get_db()
    ref = ReferenceService(db)
    d = ref.domain(slug)
    wf = WorkflowService(db).get_wf(g.user_id, d, required=False)

    res = None
    if wf:
        from app.services.graph_service import GraphService
        gs = GraphService(db)
        try:
            latest = gs.graphs.latest(g.user_id, wf["id"])
            if latest:
                res = gs.graph(g.user_id, wf, d, latest["version_number"])
        except Exception:
            res = None

    if not res:
        res = ref.domain_graph(slug, neighbours=_flag("neighbours", True))

    try:
        from app.services.html_graph_service import HTMLGraphService
        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
        html_url = HTMLGraphService.save_html(
            slug,
            res.get("domain", {}),
            res.get("nodes", []),
            res.get("edges", []),
            output_dir=graph_dir,
        )
        res["html_url"] = html_url
        base_url = current_app.config["APP_CONFIG"].BASE_URL
        res["graph_url"] = f"{base_url}{html_url}"
    except Exception:
        pass
    return jsonify(res)


@auth_required
def blocks(slug: str):
    return jsonify({"blocks": ReferenceService(get_db()).blocks(slug)})


@auth_required
def domain_flows(slug: str):
    return jsonify({"flows": ReferenceService(get_db()).flows(slug, user_id=g.user_id)})


@auth_required
def flow(key: str):
    slug = request.args.get("domain")
    return jsonify(ReferenceService(get_db()).flow_detail(key, user_id=g.user_id, slug=slug))


@auth_required
def node(key: str):
    return jsonify(ReferenceService(get_db()).node_detail(key))


@auth_required
def search():
    res = ReferenceService(get_db()).search(
        request.args.get("q"),
        request.args.get("domain"),
        request.args.get("scope", "all"),
    )
    return jsonify({"results": res, "count": len(res)})
