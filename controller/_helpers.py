from flask import current_app, g, request

from app.db import get_db
from app.errors import ForbiddenError, NotFoundError, ValidationError
from app.repositories.audit import AuditRepository
from app.repositories.workflows import WorkflowRepository
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object.")
    return data


def audit(action, entity_type=None, entity_id=None, detail=None):
    AuditRepository(get_db()).log(
        getattr(g, "user_id", None),
        action,
        entity_type,
        entity_id,
        detail,
        request.remote_addr,
    )


def resolve_workspace_wf(db, user_id, domain, create_if_missing=False):
    """Resolves the exact workspace workflow using X-Workflow-Id / ?wf= / X-Model-Name / ?model= to ensure 100% workspace isolation."""
    repo = WorkflowRepository(db)
    raw_wf = request.headers.get("X-Workflow-Id") or request.args.get("wf")
    if not raw_wf and request.is_json:
        raw_wf = (request.get_json(silent=True) or {}).get("wf")

    if raw_wf:
        try:
            wf = repo.get_by_id(int(raw_wf))
            if wf and int(wf["domain_id"]) == int(domain["id"]):
                return wf
        except (TypeError, ValueError):
            pass

    model_name = request.headers.get("X-Model-Name") or request.args.get("model")
    if not model_name and request.is_json:
        model_name = (request.get_json(silent=True) or {}).get("model")
    if model_name:
        name_clean = str(model_name).strip()
        name_u = name_clean.replace(" ", "_")
        name_s = name_clean.replace("_", " ")
        row = db.one(
            "SELECT * FROM workflows WHERE user_id = %s AND domain_id = %s AND (name = %s OR name = %s OR name = %s) ORDER BY updated_at DESC LIMIT 1",
            (user_id, domain["id"], name_clean, name_u, name_s)
        )
        if row:
            return repo._hydrate(row)
        if create_if_missing:
            wid = repo.create(user_id, domain["id"], name=name_clean)
            return repo.get_by_id(wid)

    is_fresh = request.args.get("fresh") == "1" or request.headers.get("X-Fresh") == "1"
    if is_fresh and create_if_missing:
        wid = repo.create(user_id, domain["id"])
        return repo.get_by_id(wid)

    wfs = WorkflowService(db)
    wf = wfs.get_wf(user_id, domain, required=False)
    if not wf and create_if_missing:
        wfs.start(user_id, domain)
        wf = wfs.get_wf(user_id, domain, required=True)
    return wf


def domain_ctx(slug):
    """(domain row, workflow row | None) for the authenticated user, respecting workspace/model isolation."""
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    wf = resolve_workspace_wf(db, g.user_id, domain, create_if_missing=False)
    g._active_wf = wf
    return domain, wf


def domain_wf(slug):
    """(domain row, workflow row) for the authenticated user, auto-starting if not started, respecting workspace isolation."""
    if getattr(g, "user_role", "data_architect") == "data_analyst":
        raise ForbiddenError("Data Analysts can only view published models.")
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    wf = resolve_workspace_wf(db, g.user_id, domain, create_if_missing=True)
    g._active_wf = wf
    return domain, wf


def read_domain_wf(slug, wf_id=None):
    """
    Read-only resolver for a domain + workflow without auto-creating drafts.
    - If `wf_id` (or `?wf=` query arg or X-Workflow-Id header) is provided:
      * Data Analysts may only inspect finalized (published) workflows.
      * Data Architects may inspect their own workflows or any finalized workflow.
    - Otherwise:
      * Data Analysts receive the latest published workflow for `slug` (404 if none).
      * Data Architects receive their workspace workflow for `slug`.
    """
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    repo = WorkflowRepository(db)
    role = getattr(g, "user_role", "data_architect")

    raw_wf_id = wf_id if wf_id is not None else (request.headers.get("X-Workflow-Id") or request.args.get("wf"))
    if not raw_wf_id and request.is_json:
        raw_wf_id = (request.get_json(silent=True) or {}).get("wf")

    if raw_wf_id is not None and str(raw_wf_id).strip() != "":
        try:
            parsed_id = int(raw_wf_id)
        except (TypeError, ValueError):
            raise ValidationError("Invalid workflow id.")
        wf = repo.get_by_id(parsed_id)
        if not wf or int(wf["domain_id"]) != int(domain["id"]):
            raise NotFoundError("Published model workflow not found for this domain.")
        if role == "data_analyst":
            if not wf.get("finalized_version_number"):
                raise ForbiddenError("Data Analysts can only access published models.")
            return domain, wf
        if int(wf["user_id"]) != int(g.user_id) and not wf.get("finalized_version_number"):
            raise ForbiddenError("You do not have access to another user's draft workflow.")
        return domain, wf

    if role == "data_analyst":
        wf = repo.latest_published_for_domain(domain["id"])
        if not wf:
            raise NotFoundError("No published model exists yet for this domain.")
        return domain, wf

    wf = resolve_workspace_wf(db, g.user_id, domain, create_if_missing=False)
    return domain, wf


def llm():
    return current_app.extensions.get("llm")
