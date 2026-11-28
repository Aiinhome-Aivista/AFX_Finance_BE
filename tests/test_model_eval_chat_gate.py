"""Integration test: the Model Eval gate inside ChatService.answer() (Business Input chat).

[MODEL-EVAL | Created 2026-11-28 11:29 IST (+05:30)]
Repositories/DB are mocked; graph logic (graph_ops) and Model Eval run for real.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.chat_service import ChatService  # noqa: E402
from app.services.graph_ops import GraphState  # noqa: E402

DOMAIN = {"id": 1, "slug": "planning-budgeting-forecasting", "name": "Planning, Budgeting and Forecasting", "color": "#000"}


def _node(key, name):
    return {"key": key, "name": name, "ref_name": name, "group_key": "g", "group_name": "G", "color": "#000",
            "kind": "entity", "role": "core", "definition": "A governed planning entity used in the cycle.",
            "attributes": ["Version", "Period"], "formulas": [], "evidence": None, "notes": None,
            "origin": "reference", "change_state": "unchanged", "added_in_version": 0}


def _state():
    return GraphState.from_lists(
        [_node("budget", "Budget"), _node("rebate-agreement", "Rebate Agreement"), _node("plan-version", "Plan Version")],
        [{"source": "plan-version", "target": "budget", "label": "feeds", "origin": "reference",
          "change_state": "unchanged", "added_in_version": 0, "cross_domain": False},
         {"source": "rebate-agreement", "target": "budget", "label": "reduces", "origin": "reference",
          "change_state": "unchanged", "added_in_version": 0, "cross_domain": False}])


def _service(analysis_ops):
    db = MagicMock()

    @contextmanager
    def tx():
        yield db
    db.transaction = tx

    svc = ChatService(db, llm=None)
    wf = {"id": 10, "user_id": 1, "finalized_version_number": None}
    svc._wf = MagicMock(return_value=wf)
    svc.convs = MagicMock()
    svc.convs.get.return_value = {"id": 5}
    svc.convs.open_question.return_value = {"id": 7, "seq": 1, "node_key": "budget", "topic": "missing",
                                            "question_text": "Anything missing?", "is_followup": 0}
    svc.convs.messages.return_value = [{"id": 100, "meta": {}}]
    svc.convs.unproposed.return_value = []
    svc.convs.questions.return_value = [{"seq": 1, "status": "answered", "is_followup": 0}]
    svc.graphs = MagicMock()
    svc.graphs.working.return_value = (0, _state())
    svc.graphs.create_version.return_value = 1
    svc.graphs.graph.return_value = {"nodes": [], "edges": []}
    svc.refs = MagicMock()
    svc.refs.core_keys.return_value = {"budget", "plan-version", "rebate-agreement"}
    svc._analyze = MagicMock(return_value=({"ops": analysis_ops, "followup": None, "ack": "Noted.", "skipped": False,
                                            "duplicate_nodes": []}, None))
    svc._ask_next = MagicMock(return_value=True)
    svc.snapshot = MagicMock(return_value={"messages": []})
    return svc


@patch("app.services.chat_service.ModelEvalRepository")
def test_answer_holds_near_duplicate_and_applies_the_rest(repo_cls):
    good = {"op": "add_node", "payload": {"name": "Scenario Version", "kind": "entity",
                                          "definition": "A named copy of the plan used to compare base and downside cases.",
                                          "attributes": ["Scenario Name", "Owner"]}, "rationale": "r"}
    good_edge = {"op": "add_edge", "payload": {"source": "Scenario Version", "target": "plan-version",
                                               "label": "derives from"}, "rationale": "r"}
    dup = {"op": "add_node", "payload": {"name": "Rebate Agreements", "kind": "entity",
                                         "definition": "Contracted rebate terms with a customer.", "attributes": ["Rate"]},
           "rationale": "r"}
    svc = _service([good, good_edge, dup])

    out = svc.answer(1, DOMAIN, "We keep scenario versions per plan and also rebate agreements per customer.")

    # only the approved ops reached the K graph
    applied = svc.graphs.create_version.call_args[0][3]
    names = [o["payload"].get("name") for o in applied if o["op"] == "add_node"]
    assert names == ["Scenario Version"]
    # the held op is not stored as a pending change for the question
    stored_ops = svc.convs.answer_question.call_args[0][2]
    assert dup not in stored_ops and good in stored_ops
    # report returned to the UI + recorded
    assert out["model_eval"]["summary"]["ops_held"] >= 1
    assert "approved_ops" not in out["model_eval"]
    repo_cls.return_value.record.assert_called_once()
    # ack carries the gate outcome
    ack_calls = [c for c in svc.convs.add_message.call_args_list if c[0][3] == "ack"]
    assert "Quick check" in ack_calls[-1][0][2]  # [MODEL-EVAL | 2026-11-28 12:40 IST] plain wording
    assert ack_calls[-1][0][4]["model_eval"]["verdict"] in ("warn", "fail")


@patch("app.services.chat_service.ModelEvalRepository")
def test_answer_with_personal_data_applies_nothing(repo_cls):
    op = {"op": "add_node", "payload": {"name": "Budget Approver", "kind": "entity",
                                        "definition": "Person approving the departmental budget.", "attributes": ["Role"]}}
    svc = _service([op])
    out = svc.answer(1, DOMAIN, "Budget approvals go to the budget approver at cfo.office@example.com")
    svc.graphs.create_version.assert_not_called()
    svc.convs.add_experience.assert_not_called()
    assert out["model_eval"]["verdict"] == "blocked"
