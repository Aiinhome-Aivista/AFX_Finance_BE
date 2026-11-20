"""Human review of LLM/rule-proposed business metrics (the semantic check the verifier cannot do).

The verifier proves a formula is valid arithmetic on real columns; only a person can say "Growth rate = actual / prior year" is
mislabelled. Every verified metric therefore starts as `proposed`; the owning Data Architect approves or rejects it, and the analyst
chat calculates only approved metrics. Decisions are stored per (workflow, metric name, formula), so they survive graph rebuilds but a
changed formula needs a new review.
"""
import hashlib
import re

from ..errors import ForbiddenError, NotFoundError, ValidationError
from ..repositories.kg_metric_reviews import MetricReviewRepository
from . import kgraph_metrics as kgm
from . import kgraph_verifier as ver

PROPOSED, APPROVED, REJECTED = "proposed", "approved", "rejected"
DECISIONS = {"approve": APPROVED, "reject": REJECTED, "reset": None}


def metric_key(m):
    """(name_key, formula_hash) identifying a metric definition."""
    return ver.norm(m["name"])[:120], hashlib.md5(re.sub(r"\s+", "", m["formula"]).lower().encode()).hexdigest()[:16]


class MetricReviewService:
    def __init__(self, db, cfg, llm=None):
        self.db, self.cfg, self.llm = db, cfg, llm
        self.repo = MetricReviewRepository(db)

    # ------------------------------------------------------------------ used by the chat
    def annotate(self, graph, workflow_id):
        """Copy of the graph whose metrics carry a status. Unreviewed (or unreadable reviews) means `proposed`: never silently trusted."""
        try:
            decided = self.repo.decisions(workflow_id)
        except Exception:
            decided = {}
        out = []
        for m in graph.get("metrics", []):
            out.append({**m, "status": decided.get(metric_key(m), PROPOSED)})
        return {**graph, "metrics": out}

    # ------------------------------------------------------------------ API
    def _graph_for(self, user_id, role, workflow_id):
        from .analyst_chat_service import AnalystChatService
        chat = AnalystChatService(self.db, self.cfg, self.llm)
        wf, _ = chat.dash.resolve_model(user_id, role, workflow_id)  # same server-side access rule as the dashboard
        return wf, chat._graph(wf)

    def list(self, user_id, role, workflow_id):
        wf, graph = self._graph_for(user_id, role, workflow_id)
        can = role == "data_architect" and int(wf["user_id"]) == int(user_id)
        if graph is None:
            return {"state": "building", "can_review": can, "metrics": []}
        metrics = [{"name": m["name"], "formula": re.sub(r"[\[\]]", "", m["formula"]), "description": m.get("description", ""),
                    "columns": m["columns"], "value": m["value"], "ratio": kgm.is_ratio(m),
                    "display": kgm._fmt(m)(m["value"]), "status": m["status"]} for m in graph["metrics"]]
        return {"state": "ready", "can_review": can, "metrics": metrics}

    def review(self, user_id, role, workflow_id, name=None, decision=None, all_pending=False):
        wf, graph = self._graph_for(user_id, role, workflow_id)
        if role != "data_architect" or int(wf["user_id"]) != int(user_id):
            raise ForbiddenError("Only the Data Architect who owns this model can review its metrics.")
        if decision not in DECISIONS:
            raise ValidationError("decision must be approve, reject or reset.")
        if graph is None:
            raise NotFoundError("The knowledge graph for this model is not ready yet.")
        if all_pending:
            if decision != "approve":
                raise ValidationError("Only 'approve' can be applied to all pending metrics.")
            targets = [m for m in graph["metrics"] if m["status"] == PROPOSED]
        else:
            targets = [m for m in graph["metrics"] if ver.norm(m["name"]) == ver.norm(name or "")]
            if not targets:
                raise NotFoundError("That metric is not part of this model's graph.")
        for m in targets:
            nk, fh = metric_key(m)
            if DECISIONS[decision] is None:
                self.repo.clear(workflow_id, nk, fh)
            else:
                self.repo.set(workflow_id, nk, fh, DECISIONS[decision], user_id)
        return self.list(user_id, role, workflow_id)
