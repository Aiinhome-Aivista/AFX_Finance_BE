from flask import current_app, g, jsonify, request

from app.db import get_db
from app.errors import ValidationError
from app.security import auth_required
from app.services.file_service import FileService
from app.services.telemetry_service import TelemetryService
from ._helpers import audit, domain_wf


def _svc():
    return FileService(get_db(), current_app.config["APP_CONFIG"])


@auth_required
def list_files(slug: str):
    domain, _ = domain_wf(slug)
    return jsonify({"files": _svc().list(g.user_id, domain)})


@auth_required
def upload(slug: str):
    domain, _ = domain_wf(slug)
    if "file" not in request.files:
        raise ValidationError("Attach the file in a multipart field named 'file'.")
    out = _svc().upload(g.user_id, domain, request.files["file"])
    audit("file.upload", "file", out["id"], {"name": out["name"], "size": out["size_bytes"]})
    return jsonify(out), 201


@auth_required
def detail(fid: int):
    return jsonify(_svc().detail(g.user_id, fid))


@auth_required
def delete(fid: int):
    _svc().delete(g.user_id, fid)
    audit("file.delete", "file", fid)
    return jsonify({"ok": True})


@auth_required
def generate_telemetry(slug: str):
    domain, _ = domain_wf(slug)
    out = TelemetryService(get_db()).generate_proposal(g.user_id, domain)
    audit("telemetry.generate", "proposal", out["proposal"]["id"])
    return jsonify(out), 201


@auth_required
def gap_analysis(slug: str):
    domain, wf = domain_wf(slug)
    try:
        out = TelemetryService(get_db()).gap_analysis(g.user_id, domain)
    except Exception:
        out = TelemetryService(get_db()).get_gap_analysis(g.user_id, domain, wf)
    audit("telemetry.gap_analysis", "domain", slug)
    return jsonify(out), 200

