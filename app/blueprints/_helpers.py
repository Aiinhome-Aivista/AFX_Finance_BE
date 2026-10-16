from flask import current_app, g, request

from ..db import get_db
from ..errors import ValidationError
from ..repositories.audit import AuditRepository
from ..services.reference_service import ReferenceService
from ..services.workflow_service import WorkflowService


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object.")
    return data


def audit(action, entity_type=None, entity_id=None, detail=None):
    AuditRepository(get_db()).log(getattr(g, "user_id", None), action, entity_type, entity_id, detail, request.remote_addr)


def domain_ctx(slug):
    """(domain row, workflow row | None) for the authenticated user."""
    db = get_db()
    domain = ReferenceService(db).domain(slug)
    wf = WorkflowService(db).get_wf(g.user_id, domain, required=False)
    return domain, wf


def domain_wf(slug):
    """(domain row, workflow row) for the authenticated user, auto-starting if not started."""
    db = get_db()
    domain = ReferenceService(db).domain(slug)
    wfs = WorkflowService(db)
    wf = wfs.get_wf(g.user_id, domain, required=False)
    if not wf:
        wfs.start(g.user_id, domain)
        wf = wfs.get_wf(g.user_id, domain, required=True)
    return domain, wf


def llm():
    return current_app.extensions.get("llm")
