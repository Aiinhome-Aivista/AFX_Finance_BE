from flask import Blueprint, g, jsonify, request

from ..db import get_db
from ..security import auth_required
from ..services.reference_service import ReferenceService
from ..services.workflow_service import WorkflowService
from ._helpers import audit, json_body

bp = Blueprint("domains", __name__, url_prefix="/api")


def _flag(name, default=True):
    v = request.args.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@bp.get("/apps")
@auth_required
def apps():
    return jsonify({"apps": WorkflowService(get_db()).apps()})


@bp.post("/apps/select")
@auth_required
def select_app():
    out = WorkflowService(get_db()).select_app(g.user_id, json_body().get("app"))
    audit("app.select", "app", out["selected_app"])
    return jsonify(out)


@bp.get("/domains")
@auth_required
def list_domains():
    db = get_db()
    return jsonify({"domains": ReferenceService(db).list_domains(request.args.get("q")),
                    "progress": WorkflowService(db).overview(g.user_id)})


@bp.get("/domains/<slug>")
@auth_required
def domain(slug):
    db = get_db()
    ref = ReferenceService(db)
    d = ref.domain_detail(slug)
    d["workflow"] = WorkflowService(db).state(g.user_id, ref.domain(slug))
    return jsonify(d)


@bp.get("/domains/<slug>/graph")
@auth_required
def domain_graph(slug):
    """Domain graph: returns latest customized working graph if available, else KG0 baseline."""
    db = get_db()
    ref = ReferenceService(db)
    d = ref.domain(slug)
    wf = WorkflowService(db).get_wf(g.user_id, d, required=False)
    
    res = None
    if wf:
        from ..services.graph_service import GraphService
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
        from flask import current_app
        from ..services.html_graph_service import HTMLGraphService
        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
        html_url = HTMLGraphService.save_html(slug, res.get("domain", {}), res.get("nodes", []), res.get("edges", []), output_dir=graph_dir)
        res["html_url"] = html_url
    except Exception as e:
        pass
    return jsonify(res)


@bp.get("/domains/<slug>/blocks")
@auth_required
def blocks(slug):
    return jsonify({"blocks": ReferenceService(get_db()).blocks(slug)})


@bp.get("/domains/<slug>/flows")
@auth_required
def domain_flows(slug):
    return jsonify({"flows": ReferenceService(get_db()).flows(slug)})


@bp.get("/flows/<key>")
@auth_required
def flow(key):
    return jsonify(ReferenceService(get_db()).flow_detail(key))


@bp.get("/nodes/<key>")
@auth_required
def node(key):
    return jsonify(ReferenceService(get_db()).node_detail(key))


@bp.get("/search")
@auth_required
def search():
    res = ReferenceService(get_db()).search(request.args.get("q"), request.args.get("domain"), request.args.get("scope", "all"))
    return jsonify({"results": res, "count": len(res)})
