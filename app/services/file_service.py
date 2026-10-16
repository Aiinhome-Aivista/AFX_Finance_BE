"""Upload, validate, store and process telemetry files. Gate: only after the ontology is finalised."""
import hashlib
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from werkzeug.utils import secure_filename

from ..db import open_session
from ..errors import ConflictError, NotFoundError, PayloadTooLarge, ValidationError
from ..repositories.files import FileRepository
from ..repositories.reference import ReferenceRepository
from .graph_service import GraphService
from .parsers import ALLOWED_EXT, PARSERS, FileRejected, sniff
from .telemetry_service import map_terms
from .workflow_service import WorkflowService

log = logging.getLogger(__name__)
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telemetry")


def _public(f, job=None):
    out = {"id": f["id"], "name": f["original_name"], "ext": f["ext"], "size_bytes": f["size_bytes"], "status": f["status"], "error": f["error"],
           "graph_version": f"KG{f['graph_version_number']}", "metadata": f["metadata"], "created_at": f["created_at"], "processed_at": f["processed_at"]}
    if job:
        out["job"] = {"status": job["status"], "started_at": job["started_at"], "finished_at": job["finished_at"], "error": job["error"]}
    return out


class FileService:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.files = FileRepository(db)
        self.wfs = WorkflowService(db)

    def _dir(self, user_id):
        d = Path(self.cfg.UPLOAD_DIR) / str(user_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------ upload
    def upload(self, user_id, domain, storage):
        wf = self.wfs.require_completed(user_id, domain, 5, "Uploads are only allowed after you finalise the ontology (step 5).")
        if not storage or not storage.filename:
            raise ValidationError("No file was provided.")
        original = secure_filename(storage.filename) or "upload"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in ALLOWED_EXT:
            raise ValidationError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(e.upper() for e in ALLOWED_EXT))}.")
        stored = f"{uuid.uuid4().hex}.{ext}"
        path = self._dir(user_id) / stored
        max_bytes = self.cfg.MAX_UPLOAD_MB * 1024 * 1024
        h, size = hashlib.sha256(), 0
        try:
            with open(path, "wb") as out:
                while True:
                    chunk = storage.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise PayloadTooLarge(f"The file is larger than the {self.cfg.MAX_UPLOAD_MB} MB limit.")
                    h.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ValidationError("The file is empty.")
            try:
                sniff(path, ext)
            except FileRejected as e:
                raise ValidationError(str(e))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        fid = self.files.create(user_id, wf["id"], domain["id"], wf["finalized_version_number"], original, stored, ext, size, h.hexdigest())
        self.files.create_job(fid, user_id)
        if self.cfg.PROCESSING_MODE == "sync":
            process_file(self.db, self.cfg, fid)
        else:
            _POOL.submit(_process_in_thread, self.cfg, fid)
        return self.detail(user_id, fid)

    # ------------------------------------------------------------------ read / delete
    def list(self, user_id, domain):
        wf = self.wfs.get_wf(user_id, domain)
        return [_public(f, self.files.job_for_file(f["id"])) for f in self.files.list(user_id, wf["id"])]

    def detail(self, user_id, file_id):
        f = self.files.get(user_id, file_id)
        if not f:
            raise NotFoundError("File not found.")
        job = self.files.job_for_file(file_id)
        out = _public(f, job)
        out["preview"] = f["preview"]
        out["mapping"] = (job or {}).get("result", {}) and (job["result"] or {}).get("mapping")
        return out

    def delete(self, user_id, file_id):
        f = self.files.get(user_id, file_id)
        if not f:
            raise NotFoundError("File not found.")
        if f["status"] == "processing":
            raise ConflictError("The file is being processed. Try again in a moment.")
        self.files.delete(user_id, file_id)
        (self._dir(user_id) / f["stored_name"]).unlink(missing_ok=True)


# ---------------------------------------------------------------------- processing (runs in a worker or inline)
def _process_in_thread(cfg, file_id):
    db = open_session(cfg)
    try:
        process_file(db, cfg, file_id)
    except Exception:  # pragma: no cover
        log.exception("telemetry worker crashed")
    finally:
        db.close()


def process_file(db, cfg, file_id):
    files = FileRepository(db)
    f = files.get_system(file_id)
    job = files.job_for_file(file_id)
    files.update_job(job["id"], "running")
    files.set_status(file_id, "processing")
    path = Path(cfg.UPLOAD_DIR) / str(f["user_id"]) / f["stored_name"]
    try:
        parsed = PARSERS[f["ext"]](path)
        # map against the exact finalised graph version this file was uploaded for
        wf = WorkflowRow(db, f["workflow_id"])
        gs = GraphService(db)
        state = gs.state_of(f["user_id"], wf.row, wf.domain, f["graph_version_number"])
        refs = ReferenceRepository(db).nodes_by_keys([k for k, n in state.nodes.items() if n["origin"] == "reference"])
        mapping = map_terms(state, parsed["terms"], parsed["text"], {r["node_key"]: r["canon_attributes"] for r in refs})
        files.set_status(file_id, "processed", None, parsed["metadata"], parsed["preview"])
        files.update_job(job["id"], "succeeded", None, {"mapping": mapping})
    except FileRejected as e:
        files.set_status(file_id, "failed", str(e))
        files.update_job(job["id"], "failed", str(e))
    except Exception as e:
        log.exception("processing failed for file %s", file_id)
        msg = f"Processing failed ({type(e).__name__}). The file could not be parsed."
        files.set_status(file_id, "failed", msg)
        files.update_job(job["id"], "failed", msg)


class WorkflowRow:
    """Loads the workflow + domain for a background job (no request context available)."""

    def __init__(self, db, workflow_id):
        self.row = db.one("SELECT * FROM workflows WHERE id=%s", (workflow_id,))
        from ..db import jload
        self.row["selected_blocks"] = jload(self.row.get("selected_blocks"))
        self.domain = db.one("SELECT * FROM domains WHERE id=%s", (self.row["domain_id"],))
