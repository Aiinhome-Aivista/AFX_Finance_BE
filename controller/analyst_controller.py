from flask import current_app, g, jsonify, request

from app.db import get_db
from app.security import auth_required
from app.services.analyst_dashboard_service import AnalystDashboardService
from ._helpers import audit, llm


def _svc():
    return AnalystDashboardService(get_db(), current_app.config["APP_CONFIG"], llm())


@auth_required
def dashboard(wf_id: int):
    """Read-only dashboard for a published model. Access is decided server-side from the authenticated role."""
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    out = _svc().dashboard(g.user_id, g.user_role, wf_id, refresh=refresh)
    audit("analyst.dashboard", "workflow", wf_id)
    return jsonify(out)


@auth_required
def chat(wf_id: int):
    """Read-only Q&A over the selected published model's own data. Access is re-checked on every request."""
    from app.services.analyst_chat_service import AnalystChatService
    from ._helpers import json_body
    body = json_body()
    out = AnalystChatService(get_db(), current_app.config["APP_CONFIG"], llm()).ask(
        g.user_id, g.user_role, wf_id, body.get("question"), body.get("history"))
    audit("analyst.chat", "workflow", wf_id, {"question": str(body.get("question") or "")[:200]})
    return jsonify(out)
