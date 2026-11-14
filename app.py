"""AgentForgeX Finance backend entry point.

app.py defines all API route endpoints explicitly with @app.route
and delegates execution directly to their respective controller functions.
"""
import time
from flask import Flask, g, request
from dotenv import load_dotenv

from app import create_base_app
from controller import (
    auth_controller,
    chat_controller,
    domain_controller,
    evaluation_controller,
    file_controller,
    graph_controller,
    health_controller,
    workflow_controller,
)

load_dotenv()


def create_app() -> Flask:
    app = create_base_app()

    # ------------------------------------------------------------
    # Request Hooks
    # ------------------------------------------------------------
    @app.before_request
    def _before():
        g._start_time = time.time()

    @app.after_request
    def _after(response):
        duration_ms = (time.time() - getattr(g, "_start_time", time.time())) * 1000
        status_code = response.status_code
        status_tag = f"[{status_code} OK]" if status_code < 400 else f"[{status_code} ERR]"
        print(f">> [HTTP {request.method}] {request.path} -> {status_tag} ({duration_ms:.1f}ms)", flush=True)
        return response

    # ============================================================
    # 1. Health & System Endpoints
    # ============================================================
    @app.route("/", methods=["GET"])
    def root():
        return health_controller.root()

    @app.route("/api/health", methods=["GET"])
    def api_health():
        return health_controller.health()

    @app.route("/api/graphs/<path:filename>", methods=["GET"])
    def api_serve_graph(filename):
        return health_controller.serve_graph(filename)

    # ============================================================
    # 2. Authentication Endpoints
    # ============================================================
    @app.route("/api/auth/register", methods=["POST"])
    def api_auth_register():
        return auth_controller.register()

    @app.route("/api/auth/login", methods=["POST"])
    def api_auth_login():
        return auth_controller.login()

    @app.route("/api/auth/logout", methods=["POST"])
    def api_auth_logout():
        return auth_controller.logout()

    @app.route("/api/auth/me", methods=["GET"])
    def api_auth_me():
        return auth_controller.me()

    # ============================================================
    # 3. Apps & Domains Endpoints
    # ============================================================
    @app.route("/api/apps", methods=["GET"])
    def api_apps():
        return domain_controller.apps()

    @app.route("/api/apps/subscribe", methods=["POST"])
    def api_subscribe_app():
        return domain_controller.subscribe_app()

    @app.route("/api/apps/unsubscribe", methods=["POST"])
    def api_unsubscribe_app():
        return domain_controller.unsubscribe_app()

    @app.route("/api/apps/select", methods=["POST"])
    def api_select_app():
        return domain_controller.select_app()

    @app.route("/api/workspace/models", methods=["GET"])
    def api_workspace_models():
        return domain_controller.workspace_models()

    @app.route("/api/workspace/custom-models", methods=["GET"])
    def api_list_custom_models():
        return domain_controller.list_custom_models()

    @app.route("/api/workspace/custom-models/save", methods=["POST"])
    def api_save_custom_model():
        return domain_controller.save_custom_model()

    @app.route("/api/workspace/custom-models/publish", methods=["POST"])
    def api_publish_custom_model():
        return domain_controller.publish_custom_model()

    @app.route("/api/workspace/custom-models/<name>", methods=["DELETE"])
    def api_delete_custom_model(name):
        return domain_controller.delete_custom_model(name)

    @app.route("/api/workspace/custom-models/sync", methods=["POST"])
    def api_sync_custom_models():
        return domain_controller.sync_custom_models()

    @app.route("/api/domains", methods=["GET"])
    def api_list_domains():
        return domain_controller.list_domains()

    @app.route("/api/ontology/overview-graph", methods=["GET"])
    @app.route("/api/overview-graph", methods=["GET"])
    def api_overview_graph():
        return domain_controller.overview_graph()

    @app.route("/api/domains/<slug>", methods=["GET"])
    def api_domain(slug):
        return domain_controller.domain(slug)

    @app.route("/api/domains/<slug>/graph", methods=["GET"])
    def api_domain_graph(slug):
        return domain_controller.domain_graph(slug)

    @app.route("/api/domains/<slug>/blocks", methods=["GET"])
    def api_domain_blocks(slug):
        return domain_controller.blocks(slug)

    @app.route("/api/domains/<slug>/flows", methods=["GET"])
    def api_domain_flows(slug):
        return domain_controller.domain_flows(slug)

    @app.route("/api/flows/<key>", methods=["GET"])
    def api_flow(key):
        return domain_controller.flow(key)

    @app.route("/api/nodes/<key>", methods=["GET"])
    def api_node(key):
        return domain_controller.node(key)

    @app.route("/api/search", methods=["GET"])
    def api_search():
        return domain_controller.search()

    # ============================================================
    # 4. Workflow Endpoints
    # ============================================================
    @app.route("/api/domains/<slug>/workflow", methods=["GET"])
    def api_workflow_state(slug):
        return workflow_controller.state(slug)

    @app.route("/api/domains/<slug>/select", methods=["POST"])
    def api_workflow_select(slug):
        return workflow_controller.select_domain(slug)

    @app.route("/api/domains/<slug>/blocks", methods=["POST"])
    def api_workflow_blocks(slug):
        return workflow_controller.confirm_blocks(slug)

    @app.route("/api/domains/<slug>/reset", methods=["POST"])
    def api_workflow_reset_domain(slug):
        return workflow_controller.reset_domain(slug)

    @app.route("/api/workspace/reset", methods=["POST"])
    def api_workspace_reset():
        return workflow_controller.reset_all()

    # ============================================================
    # 5. Conversation / Chat Endpoints
    # ============================================================
    @app.route("/api/domains/<slug>/conversation", methods=["GET"])
    def api_chat_get(slug):
        return chat_controller.get(slug)

    @app.route("/api/domains/<slug>/conversation", methods=["POST"])
    def api_chat_start(slug):
        return chat_controller.start(slug)

    @app.route("/api/domains/<slug>/conversation/answer", methods=["POST"])
    def api_chat_answer(slug):
        return chat_controller.answer(slug)

    @app.route("/api/domains/<slug>/conversation/message", methods=["POST"])
    def api_chat_message(slug):
        return chat_controller.message(slug)

    @app.route("/api/domains/<slug>/conversation/ask-node", methods=["POST"])
    def api_chat_ask_node(slug):
        return chat_controller.ask_node(slug)

    @app.route("/api/domains/<slug>/conversation/focus", methods=["POST"])
    def api_chat_focus(slug):
        return chat_controller.focus(slug)

    @app.route("/api/domains/<slug>/conversation/propose", methods=["POST"])
    def api_chat_propose(slug):
        return chat_controller.propose(slug)

    @app.route("/api/domains/<slug>/conversation/decision", methods=["POST"])
    def api_chat_session_decision(slug):
        return chat_controller.session_decision(slug)

    # ============================================================
    # 6. Files & Telemetry Endpoints
    # ============================================================
    @app.route("/api/domains/<slug>/files", methods=["GET"])
    def api_files_list(slug):
        return file_controller.list_files(slug)

    @app.route("/api/domains/<slug>/files", methods=["POST"])
    def api_files_upload(slug):
        return file_controller.upload(slug)

    @app.route("/api/files/<int:fid>", methods=["GET"])
    def api_file_detail(fid):
        return file_controller.detail(fid)

    @app.route("/api/files/<int:fid>", methods=["DELETE"])
    def api_file_delete(fid):
        return file_controller.delete(fid)

    @app.route("/api/domains/<slug>/telemetry/generate", methods=["POST"])
    def api_telemetry_generate(slug):
        return file_controller.generate_telemetry(slug)

    @app.route("/api/domains/<slug>/telemetry/gap-analysis", methods=["GET", "POST"])
    def api_telemetry_gap_analysis(slug):
        return file_controller.gap_analysis(slug)

    # ============================================================
    # 7. Graphs, Proposals & Finalization Endpoints
    # ============================================================
    @app.route("/api/domains/<slug>/versions", methods=["GET"])
    def api_graph_versions(slug):
        return graph_controller.versions(slug)

    @app.route("/api/domains/<slug>/versions/<int:number>/graph", methods=["GET"])
    def api_graph_version(slug, number):
        return graph_controller.version_graph(slug, number)

    @app.route("/api/domains/<slug>/working-graph", methods=["GET"])
    def api_graph_working(slug):
        return graph_controller.working_graph(slug)

    @app.route("/api/domains/<slug>/graph-html", methods=["GET"])
    def api_graph_html(slug):
        return graph_controller.graph_html(slug)

    @app.route("/api/domains/<slug>/generate-graph", methods=["POST"])
    def api_graph_generate(slug):
        return graph_controller.generate_graph(slug)

    @app.route("/api/domains/<slug>/compare", methods=["GET"])
    def api_graph_compare(slug):
        return graph_controller.compare(slug)

    @app.route("/api/domains/<slug>/proposals", methods=["GET"])
    def api_proposals_list(slug):
        return graph_controller.proposals(slug)

    @app.route("/api/domains/<slug>/proposals/<int:pid>", methods=["GET"])
    def api_proposal_detail(slug, pid):
        return graph_controller.proposal(slug, pid)

    @app.route("/api/domains/<slug>/proposals/<int:pid>/decision", methods=["POST"])
    def api_proposal_decide(slug, pid):
        return graph_controller.decide(slug, pid)

    @app.route("/api/domains/<slug>/finalization", methods=["GET"])
    def api_finalization_preview(slug):
        return graph_controller.finalization(slug)

    @app.route("/api/domains/<slug>/finalize", methods=["POST"])
    def api_finalize(slug):
        return graph_controller.finalize(slug)

    @app.route("/api/domains/<slug>/reopen", methods=["POST"])
    def api_reopen(slug):
        return graph_controller.reopen(slug)

    @app.route("/api/domains/<slug>/experience", methods=["GET"])
    def api_experience(slug):
        return graph_controller.experience(slug)

    # ============================================================
    # 7. Model Evaluation & Governance Benchmarking Endpoints
    # ============================================================
    @app.route("/api/domains/<slug>/evaluation", methods=["GET"])
    def api_evaluation_summary(slug):
        return evaluation_controller.summary(slug)

    @app.route("/api/domains/<slug>/evaluation/validate", methods=["POST"])
    def api_evaluation_validate(slug):
        return evaluation_controller.validate_prompt(slug)

    @app.route("/api/domains/<slug>/evaluation/run-benchmark", methods=["POST"])
    def api_evaluation_run_benchmark(slug):
        return evaluation_controller.run_benchmark(slug)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5008, debug=True)
