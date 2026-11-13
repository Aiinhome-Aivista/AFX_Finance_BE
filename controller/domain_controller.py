import json
from flask import current_app, g, jsonify, request

from app.db import get_db
from app.security import auth_optional, auth_required, role_required
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService
from ._helpers import audit, json_body


def _flag(name, default=True):
    v = request.args.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@auth_required
def apps():
    return jsonify(WorkflowService(get_db()).apps(g.user_id))


@auth_required
@role_required("data_architect")
def subscribe_app():
    app_key = json_body().get("app")
    out = WorkflowService(get_db()).subscribe_app(g.user_id, app_key)
    audit("app.subscribe", "app", app_key)
    return jsonify(out)


@auth_required
@role_required("data_architect")
def unsubscribe_app():
    app_key = json_body().get("app")
    out = WorkflowService(get_db()).unsubscribe_app(g.user_id, app_key)
    audit("app.unsubscribe", "app", app_key)
    return jsonify(out)


@auth_required
@role_required("data_architect", "data_analyst")
def select_app():
    out = WorkflowService(get_db()).select_app(g.user_id, json_body().get("app"))
    audit("app.select", "app", out["selected_app"])
    return jsonify(out)


@auth_required
def workspace_models():
    role = getattr(g, "user_role", "data_architect")
    return jsonify(WorkflowService(get_db()).workspace_models(g.user_id, role))


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


@auth_required
def list_custom_models():
    db = get_db()
    user_id = g.user_id
    saved_rows = db.query(
        "SELECT id, name, slug, domain_name, status, nodes_json, edges_json, created_at, updated_at "
        "FROM custom_finance_models WHERE user_id = %s AND status = 'SAVED' ORDER BY updated_at DESC",
        (user_id,)
    )
    pub_rows = db.query(
        "SELECT id, name, slug, domain_name, status, nodes_json, edges_json, published_at, created_at, updated_at "
        "FROM custom_finance_models WHERE status = 'PUBLISHED' ORDER BY COALESCE(published_at, updated_at) DESC"
    )

    saved = []
    seen_saved = set()
    for r in saved_rows:
        if r["name"] in seen_saved:
            continue
        seen_saved.add(r["name"])
        saved.append({
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "domainName": r["domain_name"],
            "savedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
        })

    published = []
    seen_pub = set()
    for r in pub_rows:
        if r["name"] in seen_pub:
            continue
        seen_pub.add(r["name"])
        published.append({
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "domainName": r["domain_name"],
            "governed": True,
            "publishedAt": r["published_at"].isoformat() if r.get("published_at") else (r["updated_at"].isoformat() if r.get("updated_at") else None),
        })

    return jsonify({
        "saved_models": saved,
        "published_models": published,
    })


@auth_required
def save_custom_model():
    data = json_body()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Model name is required"}), 400
    slug = data.get("slug") or "planning-budgeting-forecasting"
    domain_name = data.get("domainName") or "Planning, Budgeting and Forecasting"
    nodes_json = json.dumps(data.get("nodes")) if data.get("nodes") is not None else None
    edges_json = json.dumps(data.get("edges")) if data.get("edges") is not None else None

    db = get_db()
    user_id = g.user_id
    db.execute(
        "INSERT INTO custom_finance_models (user_id, name, slug, domain_name, status, nodes_json, edges_json) "
        "VALUES (%s, %s, %s, %s, 'SAVED', %s, %s) "
        "ON DUPLICATE KEY UPDATE slug=VALUES(slug), domain_name=VALUES(domain_name), status='SAVED', "
        "nodes_json=VALUES(nodes_json), edges_json=VALUES(edges_json), updated_at=NOW()",
        (user_id, name, slug, domain_name, nodes_json, edges_json)
    )
    return jsonify({"ok": True, "name": name, "status": "SAVED"})


@auth_required
def publish_custom_model():
    data = json_body()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Model name is required"}), 400
    slug = data.get("slug") or "planning-budgeting-forecasting"
    domain_name = data.get("domainName") or "Planning, Budgeting and Forecasting"

    db = get_db()
    user_id = g.user_id
    db.execute(
        "INSERT INTO custom_finance_models (user_id, name, slug, domain_name, status, published_at) "
        "VALUES (%s, %s, %s, %s, 'PUBLISHED', NOW()) "
        "ON DUPLICATE KEY UPDATE status='PUBLISHED', published_at=NOW(), updated_at=NOW()",
        (user_id, name, slug, domain_name)
    )
    try:
        db.execute(
            "UPDATE workflows SET status='PUBLISHED', finalized_version_number=COALESCE(finalized_version_number, 1), finalized_at=NOW(), name=%s "
            "WHERE user_id=%s AND domain_id=(SELECT id FROM domains WHERE slug=%s LIMIT 1)",
            (name, user_id, slug)
        )
    except Exception:
        pass

    return jsonify({"ok": True, "name": name, "status": "PUBLISHED"})


@auth_required
def delete_custom_model(name: str):
    db = get_db()
    user_id = g.user_id
    db.execute(
        "DELETE FROM custom_finance_models WHERE user_id = %s AND name = %s AND status = 'SAVED'",
        (user_id, name)
    )
    return jsonify({"ok": True})


@auth_required
def sync_custom_models():
    data = json_body()
    db = get_db()
    user_id = g.user_id

    saved = data.get("saved") or []
    published = data.get("published") or []

    for s in saved:
        s_name = (s.get("name") or "").strip()
        if not s_name:
            continue
        s_slug = s.get("slug") or "planning-budgeting-forecasting"
        s_domain = s.get("domainName") or "Planning, Budgeting and Forecasting"
        db.execute(
            "INSERT INTO custom_finance_models (user_id, name, slug, domain_name, status) "
            "VALUES (%s, %s, %s, %s, 'SAVED') "
            "ON DUPLICATE KEY UPDATE slug=VALUES(slug), domain_name=VALUES(domain_name)",
            (user_id, s_name, s_slug, s_domain)
        )

    for p in published:
        p_name = (p.get("name") or "").strip()
        if not p_name:
            continue
        p_slug = p.get("slug") or "planning-budgeting-forecasting"
        p_domain = p.get("domainName") or "Planning, Budgeting and Forecasting"
        db.execute(
            "INSERT INTO custom_finance_models (user_id, name, slug, domain_name, status, published_at) "
            "VALUES (%s, %s, %s, %s, 'PUBLISHED', NOW()) "
            "ON DUPLICATE KEY UPDATE status='PUBLISHED', published_at=COALESCE(published_at, NOW())",
            (user_id, p_name, p_slug, p_domain)
        )

    return list_custom_models()

