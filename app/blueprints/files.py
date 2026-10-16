from flask import Blueprint, current_app, g, jsonify, request

from ..db import get_db
from ..errors import ValidationError
from ..security import auth_required
from ..services.file_service import FileService
from ..services.telemetry_service import TelemetryService
from ._helpers import audit, domain_wf

bp = Blueprint("files", __name__, url_prefix="/api")


def _svc():
    return FileService(get_db(), current_app.config["APP_CONFIG"])


@bp.get("/domains/<slug>/files")
@auth_required
def list_files(slug):
    domain, _ = domain_wf(slug)
    return jsonify({"files": _svc().list(g.user_id, domain)})


@bp.post("/domains/<slug>/files")
@auth_required
def upload(slug):
    domain, _ = domain_wf(slug)
    if "file" not in request.files:
        raise ValidationError("Attach the file in a multipart field named 'file'.")
    out = _svc().upload(g.user_id, domain, request.files["file"])
    audit("file.upload", "file", out["id"], {"name": out["name"], "size": out["size_bytes"]})
    return jsonify(out), 201


@bp.get("/files/<int:fid>")
@auth_required
def detail(fid):
    return jsonify(_svc().detail(g.user_id, fid))


@bp.delete("/files/<int:fid>")
@auth_required
def delete(fid):
    _svc().delete(g.user_id, fid)
    audit("file.delete", "file", fid)
    return jsonify({"ok": True})


@bp.post("/domains/<slug>/telemetry/generate")
@auth_required
def generate(slug):
    domain, _ = domain_wf(slug)
    out = TelemetryService(get_db()).generate_proposal(g.user_id, domain)
    audit("telemetry.generate", "proposal", out["proposal"]["id"])
    return jsonify(out), 201
