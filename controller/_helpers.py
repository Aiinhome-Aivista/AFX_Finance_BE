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


def domain_ctx(slug):
    """(domain row, workflow row | None) for the authenticated user."""
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    wf = WorkflowService(db).get_wf(g.user_id, domain, required=False)
    return domain, wf


def domain_wf(slug):
    """(domain row, workflow row) for the authenticated user, auto-starting if not started."""
    if getattr(g, "user_role", "data_architect") == "data_analyst":
        raise ForbiddenError("Data Analysts can only view published models.")
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    wfs = WorkflowService(db)
    wf = wfs.get_wf(g.user_id, domain, required=False)
    if not wf:
        wfs.start(g.user_id, domain)
        wf = wfs.get_wf(g.user_id, domain, required=True)
    return domain, wf


def read_domain_wf(slug, wf_id=None):
    """
    Read-only resolver for a domain + workflow without auto-creating drafts.
    - If `wf_id` (or `?wf=` query arg) is provided:
      * Data Analysts may only inspect finalized (published) workflows.
      * Data Architects may inspect their own workflows or any finalized workflow.
    - Otherwise:
      * Data Analysts receive the latest published workflow for `slug` (404 if none).
      * Data Architects receive their own workflow for `slug` (or None if not started).
    """
    db = get_db()
    clean_slug = str(slug).replace("_", "-").lower() if slug else slug
    domain = ReferenceService(db).domain(clean_slug)
    repo = WorkflowRepository(db)
    role = getattr(g, "user_role", "data_architect")

    raw_wf_id = wf_id if wf_id is not None else request.args.get("wf")
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

    wf = WorkflowService(db).get_wf(g.user_id, domain, required=False)
    return domain, wf


def llm():
    return current_app.extensions.get("llm")
