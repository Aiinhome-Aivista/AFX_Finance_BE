from datetime import datetime
import json
import logging
from flask import current_app, g, jsonify, request

log = logging.getLogger(__name__)

from app.db import get_db
from app.security import auth_optional, auth_required, role_required
from app.repositories.workflows import WorkflowRepository
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService
from ._helpers import audit, domain_wf, json_body, resolve_workspace_wf


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
    domain_name = data.get("domainName") or "Planning, Budgeting & Forecasting"
    nodes_json = json.dumps(data.get("nodes")) if data.get("nodes") is not None else None
    edges_json = json.dumps(data.get("edges")) if data.get("edges") is not None else None
    now_dt = datetime.now()

    db = get_db()
    user_id = g.user_id
    clean_slug = str(slug).replace("_", "-").lower()

    dom_row = db.one("SELECT id FROM domains WHERE slug = %s LIMIT 1", (clean_slug,))
    dom_id = dom_row["id"] if dom_row else 1

    repo = WorkflowRepository(db)

    import urllib.parse
    raw_name = urllib.parse.unquote_plus(str(name)).strip()
    name_s = raw_name.replace("_", " ").replace("+", " ")
    name_u = name_s.replace(" ", "_")
    name_p = name_s.replace(" ", "+")
    name_variants = list({raw_name, name_s, name_u, name_p, str(name).strip()})
    name_placeholders = ", ".join(["%s"] * len(name_variants))

    # Validation: Prevent creating duplicate workspace name when requested
    check_duplicate = data.get("check_duplicate") or data.get("is_new") or data.get("mode") == "new"
    if check_duplicate:
        duplicate = db.one(
            f"SELECT id, name FROM custom_finance_models WHERE (user_id = %s OR status = 'PUBLISHED') AND name IN ({name_placeholders}) LIMIT 1",
            tuple([user_id] + name_variants)
        )
        if duplicate:
            return jsonify({
                "error": f"A workspace named '{duplicate['name']}' already exists. Please choose a different name.",
                "code": "duplicate_workspace_name"
            }), 409

    # 1. Check if the user already has a saved model with this name (Overwrite mode)
    existing_model = db.one(
        f"SELECT id, workflow_id FROM custom_finance_models WHERE user_id = %s AND name IN ({name_placeholders}) LIMIT 1",
        tuple([user_id] + name_variants)
    )
    wf_id = None
    if existing_model:
        wf_id = existing_model.get("workflow_id")

    # 2. If saving a new model name (new workspace), ensure it never shares a workflow with another workspace
    if not wf_id:
        req_wf = data.get("wf") or request.headers.get("X-Workflow-Id") or request.args.get("wf")
        if req_wf:
            try:
                cand_wf_id = int(req_wf)
                # Check if this workflow ID is ALREADY bound to another custom model
                in_use = db.one(
                    "SELECT id, name FROM custom_finance_models WHERE user_id = %s AND workflow_id = %s AND name != %s LIMIT 1",
                    (user_id, cand_wf_id, name)
                )
                if not in_use:
                    cand = repo.get_by_id(cand_wf_id)
                    if cand and int(cand["user_id"]) == int(user_id) and int(cand["domain_id"]) == int(dom_id):
                        wf_id = cand["id"]
            except (TypeError, ValueError):
                pass

    # 3. Check if there is an un-named draft workflow for this user and domain not claimed by any model
    if not wf_id:
        draft_row = db.one(
            "SELECT id FROM workflows WHERE user_id = %s AND domain_id = %s AND (name IS NULL OR name = '') "
            "AND id NOT IN (SELECT workflow_id FROM custom_finance_models WHERE workflow_id IS NOT NULL) "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, dom_id)
        )
        if draft_row:
            wf_id = draft_row["id"]

    stage = data.get("stage") or ""
    is_finalize_stage = (stage == "finalize" or stage == "twin" or data.get("status") == "PUBLISHED" or data.get("is_finalized"))
    is_new_workspace = bool(check_duplicate or data.get("is_new") or data.get("mode") == "new")

    # 4. If none, create a brand new workflow specifically for this workspace
    if not wf_id:
        wf_id = repo.create(user_id, dom_id, name=name)
        if is_finalize_stage and not is_new_workspace:
            db.execute(
                "UPDATE workflows SET finalized_version_number = 2, finalized_at = NOW(), updated_at = NOW() WHERE id = %s",
                (wf_id,)
            )
        else:
            db.execute(
                "UPDATE workflows SET finalized_version_number = NULL, finalized_at = NULL, updated_at = NOW() WHERE id = %s",
                (wf_id,)
            )
    else:
        if is_finalize_stage and not is_new_workspace:
            db.execute(
                "UPDATE workflows SET name = %s, finalized_version_number = COALESCE(finalized_version_number, 2), "
                "finalized_at = COALESCE(finalized_at, NOW()), updated_at = NOW() WHERE id = %s",
                (name, wf_id)
            )
        elif is_new_workspace:
            db.execute(
                "UPDATE workflows SET name = %s, finalized_version_number = NULL, finalized_at = NULL, updated_at = NOW() WHERE id = %s",
                (name, wf_id)
            )
        else:
            db.execute(
                "UPDATE workflows SET name = %s, updated_at = NOW() WHERE id = %s",
                (name, wf_id)
            )

    db.execute(
        "INSERT INTO custom_finance_models (user_id, workflow_id, name, slug, domain_name, status, nodes_json, edges_json) "
        "VALUES (%s, %s, %s, %s, %s, 'SAVED', %s, %s) "
        "ON DUPLICATE KEY UPDATE workflow_id=VALUES(workflow_id), slug=VALUES(slug), domain_name=VALUES(domain_name), "
        "status = IF(status = 'PUBLISHED' OR published_at IS NOT NULL, 'PUBLISHED', 'SAVED'), "
        "nodes_json=COALESCE(VALUES(nodes_json), nodes_json), edges_json=COALESCE(VALUES(edges_json), edges_json), updated_at=NOW()",
        (user_id, wf_id, name, slug, domain_name, nodes_json, edges_json)
    )

    # Initialize dedicated blank database for this workspace
    from app.services.workspace_db_service import WorkspaceDbService
    ws_db_svc = WorkspaceDbService(db, current_app.config["APP_CONFIG"])
    ws_db_res = ws_db_svc.create_workspace_db(user_id, wf_id, name)

    return jsonify({
        "ok": True,
        "name": name,
        "workflow_id": wf_id,
        "status": "SAVED",
        "db_name": ws_db_res.get("db_name"),
        "db_status": "READY",
        "savedAt": now_dt.isoformat(),
        "updatedAt": now_dt.isoformat(),
    })


@auth_required
def workspace_db_status(slug: str):
    domain, wf = domain_wf(slug)
    if not wf:
        return jsonify({"ready": False, "status": "LOCKED", "db_name": None})

    is_fresh = request.args.get("fresh") == "1" or request.headers.get("X-Fresh") == "1"
    model_name = request.headers.get("X-Model-Name") or request.args.get("model") or wf.get("name") or ""

    # If it is an unfinalized/unnamed draft or fresh session, it MUST be LOCKED until finalized & saved
    if is_fresh or (not model_name and not wf.get("name")):
        return jsonify({
            "ready": False,
            "status": "LOCKED",
            "db_name": None,
            "workflow_id": wf["id"],
            "workspace_name": None,
            "tables": [],
        })

    from app.services.workspace_db_service import WorkspaceDbService
    ws_svc = WorkspaceDbService(get_db(), current_app.config["APP_CONFIG"])

    # Check if a custom model exists for THIS workflow or THIS model name
    cm = get_db().one(
        "SELECT * FROM custom_finance_models WHERE workflow_id = %s OR (user_id = %s AND name = %s AND slug = %s) LIMIT 1",
        (wf["id"], g.user_id, model_name, domain["slug"])
    )

    is_finalized = False
    if cm:
        is_finalized = bool(cm.get("status") == "PUBLISHED" or cm.get("published_at") is not None or wf.get("finalized_version_number"))
        eff_name = cm["name"]
        eff_wfid = cm.get("workflow_id") or wf["id"]
        is_ready = ws_svc.is_workspace_db_ready(eff_wfid, eff_name)
        if not is_ready and is_finalized:
            ws_svc.create_workspace_db(g.user_id, eff_wfid, eff_name)
            is_ready = True
        tables = ws_svc.list_tables(eff_wfid, eff_name) if is_ready else []
        return jsonify({
            "ready": is_ready and is_finalized,
            "status": "READY" if (is_ready and is_finalized) else "LOCKED",
            "db_name": ws_svc.get_db_name(eff_wfid, eff_name) if is_ready else None,
            "workflow_id": eff_wfid,
            "workspace_name": eff_name,
            "tables": tables,
            "is_finalized": is_finalized,
        })

    # If workflow itself is named and finalized
    if wf.get("name") and (wf.get("finalized_version_number") or wf.get("status") in ("SAVED", "PUBLISHED")):
        is_ready = ws_svc.is_workspace_db_ready(wf["id"], wf["name"])
        if not is_ready:
            ws_svc.create_workspace_db(g.user_id, wf["id"], wf["name"])
            is_ready = True
        tables = ws_svc.list_tables(wf["id"], wf["name"])
        return jsonify({
            "ready": True,
            "status": "READY",
            "db_name": ws_svc.get_db_name(wf["id"], wf["name"]),
            "workflow_id": wf["id"],
            "workspace_name": wf["name"],
            "tables": tables,
            "is_finalized": True,
        })

    # Otherwise it is a draft that has not been saved yet -> LOCKED
    return jsonify({
        "ready": False,
        "status": "LOCKED",
        "db_name": None,
        "workflow_id": wf["id"],
        "workspace_name": None,
        "tables": [],
        "is_finalized": False,
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
    
    import urllib.parse
    raw_name = urllib.parse.unquote_plus(str(name)).strip()
    name_clean = raw_name
    name_s = raw_name.replace("_", " ").replace("+", " ")
    name_u = name_s.replace(" ", "_")
    name_p = name_s.replace(" ", "+")
    variants = list({raw_name, name_s, name_u, name_p, str(name).strip()})
    placeholders = ", ".join(["%s"] * len(variants))

    wf_ids = set()

    # Find all workflows matching this workspace name or any of its variations
    wfs_sql = f"SELECT id FROM workflows WHERE name IN ({placeholders})" + ("" if is_architect else " AND user_id = %s")
    wfs_params = tuple(variants if is_architect else variants + [user_id])
    wfs = db.query(wfs_sql, wfs_params)
    for wf in wfs:
        wf_ids.add(wf["id"])

    # Also find workflows linked via custom_finance_models
    cms_sql = f"SELECT workflow_id FROM custom_finance_models WHERE name IN ({placeholders})" + ("" if is_architect else " AND user_id = %s")
    cms_params = tuple(variants if is_architect else variants + [user_id])
    cms = db.query(cms_sql, cms_params)
    for cm in cms:
        if cm.get("workflow_id"):
            wf_ids.add(cm["workflow_id"])

    # Drop dynamic workspace MySQL databases (e.g. ws_219_saikatf1) & SQLite DBs
    try:
        from app.services.workspace_db_service import WorkspaceDbService
        ws_db_svc = WorkspaceDbService(db, current_app.config["APP_CONFIG"])
        for wid in wf_ids:
            try:
                ws_db_svc.delete_workspace_db(workflow_id=wid, workspace_name=name_s)
            except Exception as e:
                log.warning("Failed dropping workspace DB for wf %s: %s", wid, e)
        for var in variants:
            try:
                ws_db_svc.delete_workspace_db(workspace_name=var)
            except Exception as e:
                log.warning("Failed dropping workspace DB for name %s: %s", var, e)
    except Exception as e:
        log.warning("Error dropping workspace database: %s", e)

    # Completely wipe all progress, history, questions, and graph nodes for all workflows of this workspace
    for wid in wf_ids:
        try:
            db.execute("DELETE FROM conversation_messages WHERE conversation_id IN (SELECT id FROM conversations WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM chat_questions WHERE conversation_id IN (SELECT id FROM conversations WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM conversations WHERE workflow_id = %s", (wid,))
            try:
                db.execute("DELETE FROM change_items WHERE proposal_id IN (SELECT id FROM change_proposals WHERE workflow_id = %s)", (wid,))
                db.execute("DELETE FROM change_proposals WHERE workflow_id = %s", (wid,))
            except Exception:
                pass
            db.execute("DELETE FROM user_graph_nodes WHERE version_id IN (SELECT id FROM user_graph_versions WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM user_graph_edges WHERE version_id IN (SELECT id FROM user_graph_versions WHERE workflow_id = %s)", (wid,))
            db.execute("DELETE FROM user_graph_versions WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM uploaded_files WHERE workflow_id = %s", (wid,))
            try:
                db.execute("DELETE FROM export_jobs WHERE workflow_id = %s", (wid,))
            except Exception:
                pass
            db.execute("DELETE FROM workflow_steps WHERE workflow_id = %s", (wid,))
            db.execute("DELETE FROM workflows WHERE id = %s", (wid,))
        except Exception as e:
            log.warning("Cascade delete error for workflow %s: %s", wid, e)

    del_cm_sql = f"DELETE FROM custom_finance_models WHERE name IN ({placeholders})" + ("" if is_architect else " AND user_id = %s")
    del_cm_params = tuple(variants if is_architect else variants + [user_id])
    db.execute(del_cm_sql, del_cm_params)

    return jsonify({"ok": True, "deleted_workflows": list(wf_ids)})


@auth_required
def sync_custom_models():
    """Disabled permanently: Prevents client localStorage/cache from auto-re-inserting truncated models."""
    return list_custom_models()


@auth_required
def workspace_journey():
    """
    Returns the domain journey checkpoints for the active workspace.
    Tracks each domain (F1..F8) modified in this workspace and determines its latest checkpoint:
      - 'Data Input' (files uploaded/data ingested)
      - 'Business Input' (chat/interview in progress or questions answered)
      - 'Finalized' (final ontology published/saved)
    """
    db = get_db()
    user_id = g.user_id
    model_name = request.headers.get("X-Model-Name") or request.args.get("model")
    if not model_name and request.is_json:
        model_name = (request.get_json(silent=True) or {}).get("model")

    if not model_name:
        latest_cm = db.one(
            "SELECT name FROM custom_finance_models WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
            (user_id,)
        )
        if latest_cm and latest_cm.get("name"):
            model_name = latest_cm["name"]

    if not model_name:
        return jsonify({
            "workspace_name": None,
            "journey": []
        })

    import urllib.parse
    raw_m = urllib.parse.unquote_plus(str(model_name)).strip()
    name_clean = raw_m
    name_s = raw_m.replace("_", " ").replace("+", " ")
    name_u = name_s.replace(" ", "_")
    name_p = name_s.replace(" ", "+")
    variants = list({name_clean, name_s, name_u, name_p, str(model_name).strip()})
    placeholders = ", ".join(["%s"] * len(variants))

    rows = db.query(
        f"""
        SELECT DISTINCT w.id, w.domain_id, w.name, w.current_step, w.finalized_version_number, w.updated_at,
               d.code AS domain_code, d.slug AS domain_slug, d.name AS domain_name, d.color AS domain_color,
               d.description AS domain_description
        FROM workflows w
        JOIN domains d ON d.id = w.domain_id
        WHERE w.user_id = %s AND (
            w.name IN ({placeholders})
            OR w.id IN (SELECT workflow_id FROM custom_finance_models WHERE user_id = %s AND name IN ({placeholders}) AND workflow_id IS NOT NULL)
        )
        ORDER BY d.code ASC, w.updated_at DESC
        """,
        tuple([user_id] + variants + [user_id] + variants)
    )

    seen_domains = set()
    active_journey = []

    for r in rows:
        slug = r["domain_slug"]
        if slug in seen_domains:
            continue
        seen_domains.add(slug)

        wf_id = r["id"]

        # Check uploaded files (Data Input)
        file_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM uploaded_files WHERE workflow_id = %s", (wf_id,))
        file_cnt = (file_cnt_row or {}).get("cnt", 0)

        # Get detailed uploaded files list
        uploaded_files_list = []
        if file_cnt > 0:
            uf_rows = db.query(
                """
                SELECT id, original_name, ext, size_bytes, status, stage, created_at
                FROM uploaded_files
                WHERE workflow_id = %s
                ORDER BY id ASC
                """,
                (wf_id,)
            )
            for uf in uf_rows:
                uploaded_files_list.append({
                    "id": uf["id"],
                    "original_name": uf["original_name"],
                    "ext": uf.get("ext"),
                    "size_bytes": uf.get("size_bytes"),
                    "status": uf.get("status"),
                    "stage": uf.get("stage"),
                    "created_at": uf["created_at"].isoformat() if uf.get("created_at") else None
                })

        # Check user messages (Business Input)
        msg_cnt_row = db.one(
            """
            SELECT COUNT(*) AS cnt FROM conversation_messages cm
            JOIN conversations c ON cm.conversation_id = c.id
            WHERE c.workflow_id = %s AND cm.role = 'user'
            """,
            (wf_id,)
        )
        msg_cnt = (msg_cnt_row or {}).get("cnt", 0)

        # Get detailed Q&A list from chat_questions (only answered questions)
        qa_list = []
        cq_rows = db.query(
            """
            SELECT cq.id, cq.seq, cq.node_key, cq.topic, cq.question_text, cq.answer_text, cq.status, cq.answered_at
            FROM chat_questions cq
            JOIN conversations c ON cq.conversation_id = c.id
            WHERE c.workflow_id = %s AND cq.answer_text IS NOT NULL AND TRIM(cq.answer_text) != ''
            ORDER BY cq.seq ASC, cq.id ASC
            """,
            (wf_id,)
        )
        for cq in cq_rows:
            qa_list.append({
                "id": cq["id"],
                "seq": cq.get("seq"),
                "node_key": cq.get("node_key"),
                "topic": cq.get("topic"),
                "question": cq.get("question_text"),
                "answer": cq.get("answer_text"),
                "status": cq.get("status"),
                "answered_at": cq["answered_at"].isoformat() if cq.get("answered_at") else None
            })

        # If no chat_questions found but user messages exist, fallback to conversation_messages
        if not qa_list and msg_cnt > 0:
            cm_rows = db.query(
                """
                SELECT cm.id, cm.role, cm.content, cm.created_at
                FROM conversation_messages cm
                JOIN conversations c ON cm.conversation_id = c.id
                WHERE c.workflow_id = %s
                ORDER BY cm.id ASC
                """,
                (wf_id,)
            )
            # Pair assistant questions with subsequent user answers
            last_q = None
            for cm in cm_rows:
                if cm["role"] == "assistant":
                    # Simple heuristic: last sentence or question mark
                    content = (cm.get("content") or "").strip()
                    if "?" in content:
                        last_q = content
                elif cm["role"] == "user":
                    ans_text = (cm.get("content") or "").strip()
                    qa_list.append({
                        "id": cm["id"],
                        "question": last_q or "Business interview question",
                        "answer": ans_text,
                        "status": "answered",
                        "answered_at": cm["created_at"].isoformat() if cm.get("created_at") else None
                    })
                    last_q = None

        prop_cnt = 0
        try:
            prop_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM change_proposals WHERE workflow_id = %s", (wf_id,))
            prop_cnt = (prop_cnt_row or {}).get("cnt", 0)
        except Exception:
            pass

        is_finalized = bool(r.get("finalized_version_number"))

        # Only include domains that have genuine user activity in this workspace
        # (user chatted in Business Input, uploaded file in Data Input, or created proposals)
        has_actual_work = (file_cnt > 0) or (msg_cnt > 0) or (prop_cnt > 0)
        if not has_actual_work:
            continue

        encoded_m = urllib.parse.quote_plus(name_s)
        if is_finalized:
            checkpoint = "Finalized"
            checkpoint_key = "finalized"
            checkpoint_badge = "Finalized"
            checkpoint_color = "emerald"
            resume_url = f"/finance/{slug}/finalize?model={encoded_m}&wf={wf_id}"
        elif file_cnt > 0:
            checkpoint = "Data Input"
            checkpoint_key = "data_input"
            checkpoint_badge = "Data Input"
            checkpoint_color = "sky"
            resume_url = f"/finance/{slug}?mode=upload&model={encoded_m}&wf={wf_id}"
        else:
            checkpoint = "Business Input"
            checkpoint_key = "business_input"
            checkpoint_badge = "Business Input"
            checkpoint_color = "indigo"
            resume_url = f"/finance/{slug}?model={encoded_m}&wf={wf_id}"

        active_journey.append({
            "domain_code": r["domain_code"],
            "domain_slug": slug,
            "domain_name": r["domain_name"],
            "domain_color": r["domain_color"],
            "workflow_id": wf_id,
            "checkpoint": checkpoint,
            "checkpoint_key": checkpoint_key,
            "checkpoint_badge": checkpoint_badge,
            "checkpoint_color": checkpoint_color,
            "user_messages_count": msg_cnt,
            "files_count": file_cnt,
            "uploaded_files_list": uploaded_files_list,
            "qa_list": qa_list,
            "proposals_count": prop_cnt,
            "is_finalized": is_finalized,
            "resume_url": resume_url,
            "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None
        })

    return jsonify({
        "workspace_name": name_clean,
        "journey": active_journey
    })


@auth_required
def workspace_review():
    """
    Returns the workspace-level review data:
    1. The full baseline enterprise ontology graph (from ReferenceService().overview_graph()).
    2. Identifies all worked/touched domains in this workspace.
    3. Overlays any customized/added/modified nodes and edges from touched domains.
    4. Computes cumulative comparison metrics (entities added/modified/removed, links added/relabelled/removed).
    5. Returns worked_domains list and domain keys (e.g. ['f1', 'f2']).
    Never throws a 409 conflict error.
    """
    db = get_db()
    user_id = g.user_id
    model_name = request.headers.get("X-Model-Name") or request.args.get("model")
    if not model_name and request.is_json:
        model_name = (request.get_json(silent=True) or {}).get("model")

    if not model_name:
        latest_cm = db.one(
            "SELECT name FROM custom_finance_models WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
            (user_id,)
        )
        if latest_cm and latest_cm.get("name"):
            model_name = latest_cm["name"]

    import urllib.parse
    raw_m = urllib.parse.unquote_plus(str(model_name or "")).strip()
    name_clean = raw_m
    name_s = raw_m.replace("_", " ").replace("+", " ")
    name_u = name_s.replace(" ", "_")
    name_p = name_s.replace(" ", "+")
    variants = list({name_clean, name_s, name_u, name_p, str(model_name or "").strip()}) if model_name else []
    placeholders = ", ".join(["%s"] * len(variants)) if variants else "%s"

    ref = ReferenceService(db)
    base_overview = ref.overview_graph()
    base_nodes = list(base_overview.get("nodes", []))
    base_edges = list(base_overview.get("edges", []))
    flows = list(base_overview.get("flows", []))

    if not variants:
        return jsonify({
            "workspace_name": None,
            "worked_domains": [],
            "worked_domain_keys": [],
            "graph": {
                "nodes": base_nodes,
                "edges": base_edges,
                "flows": flows,
            },
            "comparison": {
                "counts": {
                    "nodes_added": 0,
                    "nodes_modified": 0,
                    "nodes_removed": 0,
                    "edges_added": 0,
                    "edges_modified": 0,
                    "edges_removed": 0,
                },
                "nodes_added": [],
                "nodes_modified": [],
                "nodes_removed": [],
                "edges_added": [],
                "edges_modified": [],
                "edges_removed": [],
            },
            "history": []
        })

    # Find workflows for this workspace
    rows = db.query(
        f"""
        SELECT DISTINCT w.id, w.domain_id, w.name, w.current_step, w.finalized_version_number, w.updated_at,
               d.code AS domain_code, d.slug AS domain_slug, d.name AS domain_name, d.color AS domain_color,
               d.description AS domain_description
        FROM workflows w
        JOIN domains d ON d.id = w.domain_id
        WHERE w.user_id = %s AND (
            w.name IN ({placeholders})
            OR w.id IN (SELECT workflow_id FROM custom_finance_models WHERE user_id = %s AND name IN ({placeholders}) AND workflow_id IS NOT NULL)
        )
        ORDER BY d.code ASC, w.updated_at DESC
        """,
        tuple([user_id] + variants + [user_id] + variants)
    )

    from app.services.graph_service import GraphService
    from app.repositories.workflows import WorkflowRepository
    repo = WorkflowRepository(db)
    gs = GraphService(db)

    seen_domains = set()
    worked_domains = []
    worked_domain_keys = set()

    total_counts = {
        "nodes_added": 0,
        "nodes_modified": 0,
        "nodes_removed": 0,
        "edges_added": 0,
        "edges_modified": 0,
        "edges_removed": 0,
    }
    all_nodes_added = []
    all_nodes_modified = []
    all_nodes_removed = []
    all_edges_added = []
    all_edges_modified = []
    all_edges_removed = []
    all_history = []

    # Map of existing node keys to avoid duplicate insertion
    existing_node_keys = {str(n.get("key") or n.get("id") or n.get("n")) for n in base_nodes}
    existing_edge_keys = {
        (str(e.get("srcId") or e.get("source") or e.get("from")),
         str(e.get("tgtId") or e.get("target") or e.get("to")))
        for e in base_edges
    }

    per_domain_diffs = {}

    for r in rows:
        slug = r["domain_slug"]
        if slug in seen_domains:
            continue
        seen_domains.add(slug)
        wf_id = r["id"]

        file_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM uploaded_files WHERE workflow_id = %s", (wf_id,))
        file_cnt = (file_cnt_row or {}).get("cnt", 0)

        msg_cnt_row = db.one(
            """
            SELECT COUNT(*) AS cnt FROM conversation_messages cm
            JOIN conversations c ON cm.conversation_id = c.id
            WHERE c.workflow_id = %s AND cm.role = 'user'
            """,
            (wf_id,)
        )
        msg_cnt = (msg_cnt_row or {}).get("cnt", 0)

        prop_cnt = 0
        try:
            prop_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM change_proposals WHERE workflow_id = %s", (wf_id,))
            prop_cnt = (prop_cnt_row or {}).get("cnt", 0)
        except Exception:
            pass

        has_actual_work = (file_cnt > 0) or (msg_cnt > 0) or (prop_cnt > 0)
        if not has_actual_work:
            continue

        d_code = (r["domain_code"] or "").lower()  # e.g. "f1"
        worked_domains.append({
            "domain_code": r["domain_code"],
            "domain_key": d_code,
            "domain_slug": slug,
            "domain_name": r["domain_name"],
            "domain_color": r["domain_color"],
            "workflow_id": wf_id,
            "is_finalized": bool(r.get("finalized_version_number")),
        })
        if d_code:
            worked_domain_keys.add(d_code)

        # Check customized graph versions
        latest = gs.graphs.latest(user_id, wf_id)
        if latest and latest["version_number"] > 0:
            try:
                wf_row = repo.get_by_id(wf_id)
                dom_row = ref.domain(slug)
                diff = gs.compare(user_id, wf_row, dom_row, 0, latest["version_number"])
                per_domain_diffs[slug] = diff

                counts = diff.get("counts", {})
                total_counts["nodes_added"] += counts.get("nodes_added", 0)
                total_counts["nodes_modified"] += counts.get("nodes_modified", 0)
                total_counts["nodes_removed"] += counts.get("nodes_removed", 0)
                total_counts["edges_added"] += counts.get("edges_added", 0)
                total_counts["edges_modified"] += counts.get("edges_modified", 0)
                total_counts["edges_removed"] += counts.get("edges_removed", 0)

                # Collect added nodes
                for n in diff.get("nodes_added", []):
                    n_copy = dict(n)
                    n_copy["domain_slug"] = slug
                    n_copy["domain_code"] = r["domain_code"]
                    n_copy["domain_name"] = r["domain_name"]
                    n_copy["is_new"] = True
                    n_copy["is_discrepancy"] = True
                    n_copy["gap_status"] = "missing"
                    all_nodes_added.append(n_copy)

                    # Also inject into the graph nodes if not already present
                    n_key = str(n.get("key") or n.get("name"))
                    if n_key not in existing_node_keys:
                        existing_node_keys.add(n_key)
                        base_nodes.append({
                            "id": n_key,
                            "key": n_key,
                            "name": n.get("name") or n_key,
                            "n": n.get("name") or n_key,
                            "d": d_code,
                            "group_key": d_code,
                            "group_name": r["domain_name"],
                            "color": "#ef4444",
                            "gap_color": "#ef4444",
                            "is_discrepancy": True,
                            "gap_status": "missing",
                            "role": "client",
                            "origin": "user",
                            "definition": n.get("definition") or "",
                            "attributes": n.get("attributes") or [],
                            "formulas": n.get("formulas") or [],
                        })

                # Collect modified nodes
                for n in diff.get("nodes_modified", []):
                    n_copy = dict(n)
                    n_copy["domain_slug"] = slug
                    n_copy["domain_code"] = r["domain_code"]
                    n_copy["domain_name"] = r["domain_name"]
                    all_nodes_modified.append(n_copy)

                for n in diff.get("nodes_removed", []):
                    all_nodes_removed.append(n)

                for e in diff.get("edges_added", []):
                    all_edges_added.append(e)
                    src = str(e.get("source") or e.get("srcId"))
                    tgt = str(e.get("target") or e.get("tgtId"))
                    if (src, tgt) not in existing_edge_keys:
                        existing_edge_keys.add((src, tgt))
                        base_edges.append({
                            "id": f"{src}->{tgt}",
                            "srcId": src,
                            "tgtId": tgt,
                            "source": src,
                            "target": tgt,
                            "label": e.get("label") or "relates to",
                            "is_new": True,
                        })

                # History
                with_items, _ = gs._proposal_context(user_id, wf_id)
                for p, items in with_items:
                    all_history.append({
                        "id": p["id"],
                        "title": p["title"],
                        "source": p["source"],
                        "status": p["status"],
                        "created_at": p["created_at"],
                        "decided_at": p["decided_at"],
                        "result_version": p["result_version_number"],
                        "domain_code": r["domain_code"],
                        "domain_slug": slug,
                        "items": [{"op": i["op"], "description": i["description"], "rationale": i.get("rationale"), "status": i["status"]} for i in items]
                    })
            except Exception as ex:
                log.warning("Error comparing graph for domain %s: %s", slug, ex)

    # Tag all nodes in base_nodes with whether their domain is worked on
    for n in base_nodes:
        n_d = str(n.get("group_key") or n.get("d") or "").lower()
        n["is_worked"] = (n_d in worked_domain_keys) or (n.get("role") == "client") or (n.get("origin") == "user")

    return jsonify({
        "ok": True,
        "workspace_name": name_clean,
        "worked_domains": worked_domains,
        "worked_domain_keys": list(worked_domain_keys),
        "graph": {
            "nodes": base_nodes,
            "edges": base_edges,
            "flows": flows,
        },
        "comparison": {
            "counts": total_counts,
            "nodes_added": all_nodes_added,
            "nodes_modified": all_nodes_modified,
            "nodes_removed": all_nodes_removed,
            "edges_added": all_edges_added,
            "edges_modified": all_edges_modified,
            "edges_removed": all_edges_removed,
            "per_domain": per_domain_diffs,
        },
        "history": all_history,
    })


@auth_required
@role_required("data_architect")
def workspace_finalize():
    """
    Finalizes and snapshots all worked domains in the active workspace.
    Saves and locks only the worked domains (e.g. F1, F2).
    """
    db = get_db()
    user_id = g.user_id
    body = json_body()
    model_name = body.get("name") or body.get("model") or request.headers.get("X-Model-Name") or request.args.get("model")

    if not model_name:
        latest_cm = db.one(
            "SELECT name FROM custom_finance_models WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
            (user_id,)
        )
        if latest_cm and latest_cm.get("name"):
            model_name = latest_cm["name"]

    if not model_name:
        return jsonify({"error": "Model name is required"}), 400

    import urllib.parse
    raw_m = urllib.parse.unquote_plus(str(model_name)).strip()
    name_clean = raw_m
    name_s = raw_m.replace("_", " ").replace("+", " ")
    name_u = name_s.replace(" ", "_")
    name_p = name_s.replace(" ", "+")
    variants = list({name_clean, name_s, name_u, name_p, str(model_name).strip()})
    placeholders = ", ".join(["%s"] * len(variants))

    rows = db.query(
        f"""
        SELECT DISTINCT w.id, w.domain_id, w.name, w.finalized_version_number,
               d.code AS domain_code, d.slug AS domain_slug, d.name AS domain_name
        FROM workflows w
        JOIN domains d ON d.id = w.domain_id
        WHERE w.user_id = %s AND (
            w.name IN ({placeholders})
            OR w.id IN (SELECT workflow_id FROM custom_finance_models WHERE user_id = %s AND name IN ({placeholders}) AND workflow_id IS NOT NULL)
        )
        """,
        tuple([user_id] + variants + [user_id] + variants)
    )

    from app.services.graph_service import GraphService
    gs = GraphService(db)

    finalized_domains = []
    seen = set()

    for r in rows:
        slug = r["domain_slug"]
        if slug in seen:
            continue
        seen.add(slug)
        wf_id = r["id"]

        # Only finalize domains that had actual work
        file_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM uploaded_files WHERE workflow_id = %s", (wf_id,))
        file_cnt = (file_cnt_row or {}).get("cnt", 0)

        msg_cnt_row = db.one(
            """
            SELECT COUNT(*) AS cnt FROM conversation_messages cm
            JOIN conversations c ON cm.conversation_id = c.id
            WHERE c.workflow_id = %s AND cm.role = 'user'
            """,
            (wf_id,)
        )
        msg_cnt = (msg_cnt_row or {}).get("cnt", 0)

        prop_cnt = 0
        try:
            prop_cnt_row = db.one("SELECT COUNT(*) AS cnt FROM change_proposals WHERE workflow_id = %s", (wf_id,))
            prop_cnt = (prop_cnt_row or {}).get("cnt", 0)
        except Exception:
            pass

        has_actual_work = (file_cnt > 0) or (msg_cnt > 0) or (prop_cnt > 0)
        if not has_actual_work:
            continue

        latest = gs.graphs.latest(user_id, wf_id)
        ver_to_finalize = (latest["version_number"] if latest else 1) or 1
        with db.transaction():
            if latest:
                gs.graphs.mark_status(latest["id"], "finalized")
            db.execute(
                "UPDATE workflows SET finalized_version_number = %s, finalized_at = NOW(), current_step = 5 WHERE id = %s",
                (ver_to_finalize, wf_id)
            )
            db.execute(
                "UPDATE workflow_steps SET status = 'completed', completed_at = NOW() WHERE workflow_id = %s AND step_no <= 4",
                (wf_id,)
            )

        finalized_domains.append({
            "domain_code": r["domain_code"],
            "domain_slug": slug,
            "workflow_id": wf_id,
            "version": ver_to_finalize,
        })

    # Update custom_finance_models status
    db.execute(
        f"UPDATE custom_finance_models SET status = 'SAVED', updated_at = NOW() WHERE user_id = %s AND name IN ({placeholders})",
        tuple([user_id] + variants)
    )

    return jsonify({
        "ok": True,
        "workspace_name": name_clean,
        "finalized_domains": finalized_domains
    })


