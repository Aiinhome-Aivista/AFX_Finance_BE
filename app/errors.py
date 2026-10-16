"""Centralised error types and handlers. Route handlers raise AppError subclasses; nothing else leaks out."""
import logging
from flask import jsonify
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, *, details=None, code=None, status=None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code
        if status:
            self.status = status


class ValidationError(AppError):
    status, code = 422, "validation_error"


class AuthError(AppError):
    status, code = 401, "unauthorized"


class ForbiddenError(AppError):
    status, code = 403, "forbidden"


class NotFoundError(AppError):
    status, code = 404, "not_found"


class ConflictError(AppError):
    status, code = 409, "conflict"


class WorkflowError(AppError):
    """A step was attempted before its prerequisites are complete."""
    status, code = 409, "workflow_step_locked"


class PayloadTooLarge(AppError):
    status, code = 413, "payload_too_large"


def register_error_handlers(app):
    @app.errorhandler(AppError)
    def _app_error(e):
        body = {"error": {"code": e.code, "message": e.message}}
        if e.details is not None:
            body["error"]["details"] = e.details
        return jsonify(body), e.status

    @app.errorhandler(HTTPException)
    def _http(e):
        return jsonify({"error": {"code": e.name.lower().replace(" ", "_"), "message": e.description}}), e.code

    @app.errorhandler(Exception)
    def _unhandled(e):
        log.exception("Unhandled error")
        return jsonify({"error": {"code": "internal_error", "message": "Something went wrong on the server."}}), 500
