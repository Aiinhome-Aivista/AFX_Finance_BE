"""Unit tests for the Model Eval gate (validation of Business Architect inputs before they reach the K graph).

[MODEL-EVAL | Created 2026-11-28 11:29 IST (+05:30)]
"""
import sys
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.graph_ops import GraphState, validate_sequence  # noqa: E402
from app.services.model_eval_service import ModelEvalService, check_formula, detect_sensitive  # noqa: E402

F1 = {"slug": "planning-budgeting-forecasting", "name": "Planning, Budgeting and Forecasting"}
F2 = {"slug": "management-reporting-gross-to-net", "name": "Management Reporting and Gross-to-Net"}


def _node(key, name, origin="reference", attrs=None, definition="A governed planning entity used in the cycle."):
    return {"key": key, "name": name, "ref_name": name if origin == "reference" else None, "group_key": "g",
            "group_name": "G", "color": "#000", "kind": "entity", "role": "core", "definition": definition,
            "attributes": list(attrs or ["Version", "Period"]), "formulas": [], "evidence": None, "notes": None,
            "origin": origin, "change_state": "unchanged", "added_in_version": 0}


def _edge(s, t, label="feeds"):
    return {"source": s, "target": t, "label": label, "origin": "reference", "change_state": "unchanged",
            "added_in_version": 0, "cross_domain": False}


def _state():
    nodes = [_node("plan-version", "Plan Version"), _node("budget", "Budget"),
             _node("rolling-forecast", "Rolling Forecast"), _node("driver", "Planning Driver"),
             _node("rebate-agreement", "Rebate Agreement")]
    edges = [_edge("plan-version", "budget"), _edge("budget", "rolling-forecast"), _edge("driver", "budget"),
             _edge("plan-version", "rolling-forecast"), _edge("rebate-agreement", "budget")]
    return GraphState.from_lists(nodes, edges)


def _add_node(name, definition="", attributes=None, formulas=None):
    return {"op": "add_node", "payload": {"name": name, "definition": definition, "attributes": attributes or [],
                                          "formulas": formulas or [], "kind": "entity"}, "rationale": "test"}


def _add_edge(s, t, label="feeds"):
    return {"op": "add_edge", "payload": {"source": s, "target": t, "label": label}, "rationale": "test"}


# --------------------------------------------------------------------------------------------- good input passes
def test_well_formed_input_is_approved():
    st = _state()
    ops = [_add_node("Scenario Version", "A named copy of the plan used to compare base, upside and downside cases.",
                     ["Scenario Name", "Owner", "Planning Horizon"]),
           _add_edge("Scenario Version", "plan-version", "derives from")]
    r = ModelEvalService.evaluate("We maintain scenario versions (base/upside/downside) of each plan version.",
                                  st, ops, domain=F1, enforce=True)
    assert r["template"]["key"] == "finance"
    assert r["summary"]["ops_held"] == 0
    assert r["summary"]["ops_approved"] == 2
    assert r["verdict"] in ("pass", "warn")
    assert r["score"] >= 80
    # approved ops must still validate structurally downstream
    accepted, rejected, _ = validate_sequence(st, r["approved_ops"], 1)
    assert len(accepted) == 2 and not rejected


# --------------------------------------------------------------------------------------------- near duplicates
def test_near_duplicate_entity_is_held_with_its_edge():
    st = _state()
    ops = [_add_node("Rebate Agreements", "Contracted rebate terms with a customer for a period.", ["Rate"]),
           _add_edge("Rebate Agreements", "budget", "reduces")]
    r = ModelEvalService.evaluate("We also track rebate agreements per customer.", st, ops, domain=F2, enforce=True)
    statuses = [o["status"] for o in r["ops"]]
    assert statuses == ["held", "held"], statuses
    assert any(f["code"] == "near_duplicate" for f in r["findings"])
    assert r["approved_ops"] == []
    assert r["verdict"] == "fail"


def test_generic_entity_name_is_held():
    r = ModelEvalService.evaluate("add a new node called data", _state(), [_add_node("Data")], domain=F1, enforce=True)
    assert r["ops"][0]["status"] == "held"
    assert any(f["code"] == "generic_name" for f in r["findings"])


# --------------------------------------------------------------------------------------------- data safety
def test_personal_data_blocks_the_whole_input():
    st = _state()
    ops = [_add_node("Budget Owner Contact", "Who approves the departmental budget.", ["Owner Name"])]
    r = ModelEvalService.evaluate("The budget owner is reachable at jane.doe@example.com for approvals.",
                                  st, ops, domain=F1, enforce=True)
    assert r["verdict"] == "blocked"
    assert r["summary"]["ops_held"] == 1
    assert any(f["code"] == "sensitive_data" for f in r["findings"])


def test_amounts_are_not_mistaken_for_phone_numbers():
    assert detect_sensitive("Annual opex budget is 2500000000 INR across 12 periods in FY2026") == []
    assert detect_sensitive("call +91 98765 43210")  # real phone number is caught


def test_shadow_mode_reports_but_holds_nothing():
    r = ModelEvalService.evaluate("add data", _state(), [_add_node("Data")], domain=F1, enforce=False)
    assert r["summary"]["ops_held"] == 0
    assert len(r["approved_ops"]) == 1
    assert any(f["severity"] == "fail" for f in r["findings"])
    assert r["enforced"] is False


# --------------------------------------------------------------------------------------------- formulas
def test_formula_checks():
    assert any(c == "formula_unbalanced" for _s, c, _m in check_formula("(Revenue - COGS / Revenue"))
    assert any(c == "formula_div_zero" for _s, c, _m in check_formula("Margin = Profit / 0"))
    assert any(c == "formula_bad_operator" for _s, c, _m in check_formula("Net = Gross - * Rebate"))
    assert check_formula("Gross Margin % = (Net Sales - COGS) / Net Sales") == []


def test_bad_formula_on_update_is_held():
    op = {"op": "update_node", "payload": {"node_key": "budget", "add_formulas": ["Budget = Plan / 0"]}}
    r = ModelEvalService.evaluate("Budget is the plan divided by zero", _state(), [op], domain=F1, enforce=True)
    assert r["ops"][0]["status"] == "held"


# --------------------------------------------------------------------------------------------- integrity
def test_reference_hub_removal_is_held():
    op = {"op": "remove_node", "payload": {"node_key": "budget"}}
    r = ModelEvalService.evaluate("We don't use budgets anymore", _state(), [op], domain=F1, enforce=True)
    assert r["ops"][0]["status"] == "held"
    assert any(f["code"] == "reference_hub_removal" for f in r["findings"])


def test_generic_relationship_label_warns_and_impact_is_reported():
    ops = [_add_node("Budget Holder Sign-off", "Formal approval of a departmental budget by its owner.",
                     ["Approver Role", "Sign-off Date"]),
           _add_edge("Budget Holder Sign-off", "budget", "relates to")]
    r = ModelEvalService.evaluate("Each budget needs a budget holder sign-off before lock.", _state(), ops,
                                  domain=F1, enforce=True)
    assert r["summary"]["ops_approved"] == 2
    assert any(f["code"] == "generic_relationship" for f in r["findings"])
    assert r["impact"]["delta"]["nodes"] == 1 and r["impact"]["delta"]["edges"] == 1


def test_structural_rejects_pass_through_untouched():
    # exact duplicate is a structural error: Model Eval must not hold it, the existing pipeline reports it
    r = ModelEvalService.evaluate("We have a budget", _state(), [_add_node("Budget", "dup")], domain=F1, enforce=True)
    assert r["ops"][0]["status"] == "structural_reject"
    assert len(r["approved_ops"]) == 1


# --------------------------------------------------------------------------------------------- templates
def test_template_resolution():
    assert ModelEvalService.resolve_template("planning-budgeting-forecasting") == "finance"
    assert ModelEvalService.resolve_template("fmcg-route-to-market") == "fmcg"
    assert ModelEvalService.resolve_template("planning-budgeting-forecasting", "fmcg") == "fmcg"
    assert ModelEvalService.resolve_template("anything", "bogus") == "finance"


def test_fmcg_template_relevance():
    st = _state()
    # FMCG-only vocabulary (distributor, van sales, outlet, beat) — no finance or current-graph terms
    ops = [_add_node("Distributor Van Route", "Fixed weekly sequence of outlets that a distributor van visits on a beat.",
                     ["Van Id", "Outlet", "Visit Day"])]
    text = "Our distributors run vans on a fixed beat across outlets."
    fin = ModelEvalService.evaluate(text, st, ops, domain=F1, template_key="finance", enforce=True)
    fm = ModelEvalService.evaluate(text, st, ops, domain=F1, template_key="fmcg", enforce=True)
    assert fm["template"]["key"] == "fmcg"
    assert not any(f["code"] in ("template_mismatch", "low_relevance") for f in fm["findings"])
    assert any(f["code"] in ("template_mismatch", "low_relevance") for f in fin["findings"])
    assert fm["dimensions"]["relevance"]["score"] > fin["dimensions"]["relevance"]["score"]


def test_out_of_scope_input_is_blocked():
    r = ModelEvalService.evaluate("Give me a recipe for chocolate cake", _state(), [], domain=F1, enforce=True)
    assert r["verdict"] == "blocked"


def test_public_and_compact_strip_internal_fields():
    r = ModelEvalService.evaluate("add data", _state(), [_add_node("Data")], domain=F1, enforce=True)
    pub = ModelEvalService.public(r)
    assert "approved_ops" not in pub and "held_ops" not in pub
    c = ModelEvalService.compact(r)
    assert c["verdict"] == r["verdict"] and c["held"]
    assert "was not added" in ModelEvalService.ack_lines(r)  # [MODEL-EVAL | 2026-11-28 12:40 IST] plain wording
