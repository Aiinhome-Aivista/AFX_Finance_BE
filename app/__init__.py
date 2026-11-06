import logging
from flask import Flask, make_response, request
from flask_cors import CORS

from .config import Config
from .db import close_db
from .errors import register_error_handlers
from .llm import build_provider


def create_base_app(config: Config | None = None, llm_factory=None) -> Flask:
    cfg = config or Config.from_env()
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    app.config["APP_CONFIG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = (cfg.MAX_UPLOAD_MB + 2) * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    app.config["JSON_AS_ASCII"] = False
    try:
        app.json.sort_keys = False
        app.json.ensure_ascii = False
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO)
    CORS(
        app,
        resources={r"/*": {"origins": "*"}},
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    )

    @app.before_request
    def handle_preflight():
        if request.method == "OPTIONS":
            res = make_response()
            res.headers["Access-Control-Allow-Origin"] = "*"
            res.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
            res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
            return res, 204

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        return response

    app.extensions["llm"] = llm_factory() if llm_factory else build_provider(cfg)
    app.teardown_appcontext(close_db)
    register_error_handlers(app)
    return app
