from flask import g, jsonify

from app.db import get_db
from app.errors import ValidationError
from app.security import auth_required
from app.services.reference_service import ReferenceService
from app.services.workflow_service import WorkflowService
from ._helpers import audit, json_body


@auth_required
def state(slug: str):
    db = get_db()
    return jsonify(WorkflowService(db).state(g.user_id, ReferenceService(db).domain(slug)))


@auth_required
def select_domain(slug: str):
    """Step 2: start (or resume) the workflow for this domain."""
    db = get_db()
    d = ReferenceService(db).domain(slug)
    out = WorkflowService(db).start(g.user_id, d)
    audit("workflow.start", "domain", slug)
    return jsonify(out)


@auth_required
def confirm_blocks(slug: str):
    """Step 3 -> 4: confirm which KG0 blocks the client wants to customise."""
    db = get_db()
    ref = ReferenceService(db)
    d = ref.domain(slug)
    ids = json_body().get("blocks")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValidationError("Select at least one block.")
    ref.baseline(slug, ids)  # validates ids + minimum size
    wfs = WorkflowService(db)
    wfs.confirm_blocks(g.user_id, d, ids)
    audit("workflow.blocks", "domain", slug, {"blocks": ids})
    return jsonify(wfs.state(g.user_id, d))
