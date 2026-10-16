import logging

from flask import Flask, jsonify
from flask_cors import CORS

from .config import Config
from .db import close_db
from .errors import register_error_handlers
from .llm import build_provider


def create_app(config: Config | None = None, llm_factory=None) -> Flask:
    cfg = config or Config.from_env()
    app = Flask(__name__)
    app.config["APP_CONFIG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = (cfg.MAX_UPLOAD_MB + 2) * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    logging.basicConfig(level=logging.INFO)
    CORS(app, origins=cfg.CORS_ORIGINS, allow_headers=["Content-Type", "Authorization"],
         methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    app.extensions["llm"] = llm_factory() if llm_factory else build_provider(cfg)

    from .blueprints import auth, chat, domains, files, graphs, workflow
    for m in (auth, domains, workflow, chat, graphs, files):
        app.register_blueprint(m.bp)

    @app.get("/api/health")
    def health():
        llm = app.extensions.get("llm")
        return jsonify({"status": "ok", "llm": llm.name if llm else "none (rule-based fallback)"})

    app.teardown_appcontext(close_db)
    register_error_handlers(app)
    return app
