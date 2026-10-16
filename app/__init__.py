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

    @app.get("/api/graphs/<path:filename>")
    def serve_graph(filename):
        from flask import send_from_directory, current_app
        import os
        from pathlib import Path
        graph_dir = os.path.abspath(current_app.config["APP_CONFIG"].GRAPH_DIR)
        target_file = Path(graph_dir) / filename
        if not target_file.exists() and filename.endswith(".html"):
            slug = filename[:-5]
            try:
                from .db import get_db
                from .services.reference_service import ReferenceService
                from .services.html_graph_service import HTMLGraphService
                ref = ReferenceService(get_db())
                gdata = ref.domain_graph(slug, neighbours=True)
                HTMLGraphService.save_html(slug, gdata.get("domain", {}), gdata.get("nodes", []), gdata.get("edges", []), output_dir=graph_dir)
            except Exception as err:
                logging.getLogger(__name__).warning("On-the-fly HTML graph generation failed for %s: %s", filename, err)
        return send_from_directory(graph_dir, filename)

    app.teardown_appcontext(close_db)
    register_error_handlers(app)
    return app
