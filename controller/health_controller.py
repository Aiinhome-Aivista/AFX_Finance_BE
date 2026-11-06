import logging
import os
from pathlib import Path
from flask import current_app, jsonify, send_from_directory

log = logging.getLogger(__name__)


def root():
    return jsonify({
        "status": "UP",
        "message": "AgentForgeX Finance API is running",
    })


def health():
    llm = current_app.extensions.get("llm")
    return jsonify({
        "status": "ok",
        "llm": llm.name if llm else "none (rule-based fallback)",
    })


def serve_graph(filename: str):
    graph_dir = os.path.abspath(current_app.config["APP_CONFIG"].GRAPH_DIR)
    target_file = Path(graph_dir) / filename
    if not target_file.exists() and filename.endswith(".html"):
        slug = filename[:-5]
        try:
            from app.services.reference_service import ReferenceService
            from app.services.html_graph_service import HTMLGraphService
            ref = ReferenceService()
            if slug in ("finance-overview", "overview"):
                gdata = ref.overview_graph()
                HTMLGraphService.save_html(
                    slug,
                    {"name": "Finance Knowledge Graph (Overview)", "color": "#2563EB"},
                    gdata.get("nodes", []),
                    gdata.get("edges", []),
                    output_dir=graph_dir,
                )
            else:
                gdata = ref.domain_graph(slug, neighbours=True)
                HTMLGraphService.save_html(
                    slug,
                    gdata.get("domain", {}),
                    gdata.get("nodes", []),
                    gdata.get("edges", []),
                    output_dir=graph_dir,
                )
        except Exception as err:
            log.warning("On-the-fly HTML graph generation failed for %s: %s", filename, err)
    return send_from_directory(graph_dir, filename)
