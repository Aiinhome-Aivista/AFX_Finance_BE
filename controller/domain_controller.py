from datetime import datetime
import json
from flask import current_app, g, jsonify, request

from app.db import get_db
from app.security import auth_optional, auth_required, role_required
from app.repositories.workflows import WorkflowRepository
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService
from ._helpers import audit, json_body, resolve_workspace_wf


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
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    d = ref.domain(clean_slug)

    is_fresh = (
        request.args.get("fresh") == "1"
        or request.headers.get("X-Fresh") == "1"
        or request.headers.get("fresh") == "1"
    )

    res = None
    if not is_fresh:
        wf = resolve_workspace_wf(db, g.user_id, d, create_if_missing=False)
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
        res = ref.domain_graph(clean_slug, neighbours=_flag("neighbours", True))

    try:
        from app.services.html_graph_service import HTMLGraphService
        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
        html_url = HTMLGraphService.save_html(
            clean_slug,
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


def _parse_iso_datetime(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        s = str(val).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def _repair_synced_model_dates(db):
    """Repair models erroneously stamped with 2026-11-16 on initial sync."""
    try:
        db.execute(
            "UPDATE custom_finance_models "
            "SET created_at = '2026-11-14 16:30:00', updated_at = '2026-11-14 16:30:00' "
            "WHERE name IN ('Finance_part1_Model_Nov_26', 'Finance_part2_Model_Nov_26', 'Test_Save_1') "
            "AND DATE(created_at) = '2026-11-16'"
        )
    except Exception:
        pass


@auth_required
def list_custom_models():
    db = get_db()
    user_id = g.user_id
    repo = WorkflowRepository(db)
    pub_rows = db.query(
        "SELECT id, user_id, workflow_id, name, slug, domain_name, status, nodes_json, edges_json, published_at, created_at, updated_at "
        "FROM custom_finance_models WHERE status = 'PUBLISHED' OR published_at IS NOT NULL "
        "ORDER BY COALESCE(published_at, updated_at) DESC"
    )

    published = []
    published_names = set()
    for r in pub_rows:
        if r["name"] in published_names:
            continue
        published_names.add(r["name"])
        nodes = None
        edges = None
        try:
            if r.get("nodes_json"):
                nodes = json.loads(r["nodes_json"]) if isinstance(r["nodes_json"], str) else r["nodes_json"]
            if r.get("edges_json"):
                edges = json.loads(r["edges_json"]) if isinstance(r["edges_json"], str) else r["edges_json"]
        except Exception:
            pass
        clean_slug = str(r["slug"] or "").replace("_", "-").lower()
        wf_id = r.get("workflow_id")
        if not wf_id:
            wf_row = db.one(
                "SELECT w.id FROM workflows w JOIN domains d ON d.id = w.domain_id "
                "WHERE (w.name = %s OR (w.user_id = %s AND d.slug = %s) OR d.slug = %s) "
                "ORDER BY (w.name = %s) DESC, (w.finalized_version_number IS NOT NULL) DESC, (w.user_id = %s) DESC, w.updated_at DESC, w.id DESC LIMIT 1",
                (r["name"], r["user_id"], clean_slug, clean_slug, r["name"], r["user_id"]),
            )
            if wf_row:
                wf_id = wf_row["id"]
                try:
                    db.execute("UPDATE custom_finance_models SET workflow_id = %s WHERE id = %s", (wf_id, r["id"]))
                except Exception:
                    pass
        published.append({
            "id": r["id"],
            "workflow_id": wf_id,
            "name": r["name"],
            "slug": r["slug"],
            "domainName": r["domain_name"],
            "governed": True,
            "nodes": nodes,
            "edges": edges,
            "publishedAt": r["published_at"].isoformat() if r.get("published_at") else (r["updated_at"].isoformat() if r.get("updated_at") else None),
        })

    saved_rows = db.query(
        "SELECT id, user_id, workflow_id, name, slug, domain_name, status, nodes_json, edges_json, created_at, updated_at "
        "FROM custom_finance_models WHERE user_id = %s AND status = 'SAVED' AND published_at IS NULL "
        "ORDER BY updated_at DESC",
        (user_id,)
    )

    saved = []
    seen_saved = set()
    for r in saved_rows:
        if r["name"] in seen_saved or r["name"] in published_names:
            continue
        seen_saved.add(r["name"])
        saved_at = r.get("updated_at") or r.get("created_at")
        nodes = None
        edges = None
        try:
            if r.get("nodes_json"):
                nodes = json.loads(r["nodes_json"]) if isinstance(r["nodes_json"], str) else r["nodes_json"]
            if r.get("edges_json"):
                edges = json.loads(r["edges_json"]) if isinstance(r["edges_json"], str) else r["edges_json"]
        except Exception:
            pass

        clean_slug = str(r["slug"] or "").replace("_", "-").lower()
        wf_id = r.get("workflow_id")
        if not wf_id:
            wf_row = db.one(
                "SELECT id FROM workflows WHERE user_id = %s AND (name = %s OR name = %s OR name = %s) LIMIT 1",
                (user_id, r["name"], r["name"].replace(" ", "_"), r["name"].replace("_", " "))
            )
            if wf_row:
                wf_id = wf_row["id"]
                try:
                    db.execute("UPDATE custom_finance_models SET workflow_id = %s WHERE id = %s", (wf_id, r["id"]))
                except Exception:
                    pass
            else:
                dom_row = db.one("SELECT id FROM domains WHERE slug = %s LIMIT 1", (clean_slug,))
                dom_id = dom_row["id"] if dom_row else 1
                wf_id = repo.create(user_id, dom_id, name=r["name"])
            try:
                db.execute("UPDATE custom_finance_models SET workflow_id = %s WHERE id = %s", (wf_id, r["id"]))
            except Exception:
                pass

        saved.append({
            "id": r["id"],
            "workflow_id": wf_id,
            "name": r["name"],
            "slug": r["slug"],
            "domainName": r["domain_name"],
            "nodes": nodes,
            "edges": edges,
            "savedAt": saved_at.isoformat() if saved_at else None,
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
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
    domain_name = data.get("domainName")
    nodes_json = json.dumps(data.get("nodes")) if data.get("nodes") is not None else None
    edges_json = json.dumps(data.get("edges")) if data.get("edges") is not None else None
    now_dt = datetime.now()

    db = get_db()
    user_id = g.user_id
    clean_slug = str(slug).replace("_", "-").lower()

    dom_row = db.one("SELECT id FROM domains WHERE slug = %s LIMIT 1", (clean_slug,))
    dom_id = dom_row["id"] if dom_row else 1

    repo = WorkflowRepository(db)

    # 1. Did client pass a specific workflow id?
    req_wf = data.get("wf") or request.headers.get("X-Workflow-Id") or request.args.get("wf")
    wf_id = None
    if req_wf:
        try:
            cand = repo.get_by_id(int(req_wf))
            if cand and int(cand["user_id"]) == int(user_id) and int(cand["domain_id"]) == int(dom_id):
                wf_id = cand["id"]
        except (TypeError, ValueError):
            pass

    # 2. Check if a workflow exists with this model name
    if not wf_id:
        wf_row = db.one(
            "SELECT id FROM workflows WHERE user_id = %s AND domain_id = %s AND (name = %s OR name = %s OR name = %s) LIMIT 1",
            (user_id, dom_id, name, name.replace(" ", "_"), name.replace("_", " "))
        )
        if wf_row:
            wf_id = wf_row["id"]

    # 3. Check if there is an un-named draft workflow for this user and domain
    if not wf_id:
        draft_row = db.one(
            "SELECT id FROM workflows WHERE user_id = %s AND domain_id = %s AND (name IS NULL OR name = '') "
            "AND id NOT IN (SELECT workflow_id FROM custom_finance_models WHERE workflow_id IS NOT NULL) "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, dom_id)
        )
        if draft_row:
            wf_id = draft_row["id"]

    # 4. If none, create a new workflow
    if not wf_id:
        wf_id = repo.create(user_id, dom_id, name=name)
    else:
        db.execute("UPDATE workflows SET name = %s, updated_at = NOW() WHERE id = %s", (name, wf_id))

    db.execute(
        "INSERT INTO custom_finance_models (user_id, workflow_id, name, slug, domain_name, status, nodes_json, edges_json) "
        "VALUES (%s, %s, %s, %s, %s, 'SAVED', %s, %s) "
        "ON DUPLICATE KEY UPDATE workflow_id=VALUES(workflow_id), slug=VALUES(slug), domain_name=VALUES(domain_name), "
        "status = IF(status = 'PUBLISHED' OR published_at IS NOT NULL, 'PUBLISHED', 'SAVED'), "
        "nodes_json=VALUES(nodes_json), edges_json=VALUES(edges_json), updated_at=NOW()",
        (user_id, wf_id, name, slug, domain_name, nodes_json, edges_json)
    )
    return jsonify({
        "ok": True,
        "name": name,
        "workflow_id": wf_id,
        "status": "SAVED",
        "savedAt": now_dt.isoformat(),
        "updatedAt": now_dt.isoformat(),
    })


@auth_required
def publish_custom_model():
    data = json_body()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Model name is required"}), 400
    slug = data.get("slug") or "planning-budgeting-forecasting"
    domain_name = data.get("domainName")

    db = get_db()
    user_id = g.user_id
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug

    dom_row = db.one("SELECT id FROM domains WHERE slug = %s LIMIT 1", (clean_slug,))
    dom_id = dom_row["id"] if dom_row else 1

    repo = WorkflowRepository(db)
    wf_row = db.one(
        "SELECT id FROM workflows WHERE user_id = %s AND domain_id = %s AND (name = %s OR name = %s OR name = %s) LIMIT 1",
        (user_id, dom_id, name, name.replace(" ", "_"), name.replace("_", " "))
    )
    if wf_row:
        wf_id = wf_row["id"]
    else:
        active_wf = getattr(g, "_active_wf", None)
        if active_wf and not active_wf.get("name") and int(active_wf.get("domain_id", 0)) == int(dom_id):
            wf_id = active_wf["id"]
            db.execute("UPDATE workflows SET name = %s WHERE id = %s", (name, wf_id))
        else:
            wf_id = repo.create(user_id, dom_id, name=name)

    db.execute(
        "INSERT INTO custom_finance_models (user_id, workflow_id, name, slug, domain_name, status, published_at) "
        "VALUES (%s, %s, %s, %s, %s, 'PUBLISHED', NOW()) "
        "ON DUPLICATE KEY UPDATE workflow_id=COALESCE(VALUES(workflow_id), workflow_id), status='PUBLISHED', published_at=COALESCE(published_at, NOW()), updated_at=NOW()",
        (user_id, wf_id, name, slug, domain_name)
    )
    try:
        db.execute(
            "UPDATE custom_finance_models SET status='PUBLISHED', published_at=COALESCE(published_at, NOW()), workflow_id=COALESCE(workflow_id, %s) WHERE name=%s OR name=%s",
            (wf_id, name, name.replace(" ", "_"))
        )
    except Exception:
        pass

    try:
        db.execute(
            "UPDATE workflows SET status='PUBLISHED', finalized_version_number=COALESCE(finalized_version_number, 1), finalized_at=NOW(), name=%s "
            "WHERE id=%s",
            (name, wf_id)
        )
    except Exception:
        pass

    return jsonify({"ok": True, "name": name, "workflow_id": wf_id, "status": "PUBLISHED"})


@auth_required
def delete_custom_model(name: str):
    db = get_db()
    user_id = g.user_id
    is_architect = getattr(g, "user_role", "") == "data_architect"
    name_clean = (name or "").strip()
    name_underscores = name_clean.replace(" ", "_")
    name_spaces = name_clean.replace("_", " ")

    wfs = db.query(
        "SELECT id FROM workflows WHERE (name = %s OR name = %s OR name = %s)" + ("" if is_architect else " AND user_id = %s"),
        (name_clean, name_underscores, name_spaces) if is_architect else (name_clean, name_underscores, name_spaces, user_id)
    )
    for wf in wfs:
        wid = wf["id"]
        try:
            db.execute("DELETE FROM conversation_messages WHERE conversation_id IN (SELECT id FROM conversations WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM chat_questions WHERE conversation_id IN (SELECT id FROM conversations WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM conversations WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM user_graph_versions WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM uploaded_files WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM workflow_steps WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM workflows WHERE id = %s", (wid,))
        except Exception:
            pass

    if is_architect:
        db.execute(
            "DELETE FROM custom_finance_models WHERE name = %s OR name = %s OR name = %s",
            (name_clean, name_underscores, name_spaces),
        )
    else:
        db.execute(
            "DELETE FROM custom_finance_models WHERE user_id = %s AND (name = %s OR name = %s OR name = %s)",
            (user_id, name_clean, name_underscores, name_spaces),
        )
    return jsonify({"ok": True})


@auth_required
def sync_custom_models():
    """Disabled permanently: Prevents client localStorage/cache from auto-re-inserting truncated models."""
    return list_custom_models()

