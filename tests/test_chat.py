import json

from app import create_app
from app.llm import LLMError, LLMProvider
from tests.conftest import Api

S = "working-capital-treasury"


def test_one_question_at_a_time_and_history(api):
    api.start_domain(S)
    snap = api.post(f"/api/domains/{S}/conversation").get_json()
    qs = [m for m in snap["messages"] if m["kind"] == "question"]
    assert len(qs) == 1 and snap["open_question"]["text"] == qs[0]["content"]
    assert snap["open_question"]["topic"] == "naming" and snap["engine"] == "rule-based"
    snap2 = api.post(f"/api/domains/{S}/conversation/answer", {"message": "same"}).get_json()
    assert snap2["open_question"]["topic"] == "attributes"
    # history persists and re-start is idempotent
    again = api.post(f"/api/domains/{S}/conversation").get_json()
    assert again["conversation_id"] == snap["conversation_id"]
    full = api.get(f"/api/domains/{S}/conversation").get_json()
    kinds = [m["kind"] for m in full["messages"]]
    assert kinds.count("question") == 2 and kinds.count("answer") == 1


def test_question_is_grounded_in_kg0(api):
    api.start_domain(S)
    q = api.post(f"/api/domains/{S}/conversation").get_json()["open_question"]
    node = api.get(f"/api/nodes/{q['node_key']}").get_json()
    assert node["name"] in q["text"]
    nxt = api.post(f"/api/domains/{S}/conversation/answer", {"message": "same"}).get_json()["open_question"]
    assert any(a.split(" (")[0] in nxt["text"] for a in node["attributes"][:4])


def test_followup_on_unclear_answer_then_moves_on(api):
    api.start_domain(S)
    api.post(f"/api/domains/{S}/conversation")
    api.post(f"/api/domains/{S}/conversation/answer", {"message": "same"})
    snap = api.post(f"/api/domains/{S}/conversation/answer", {"message": "hmm"}).get_json()
    assert snap["open_question"]["is_followup"] is True and snap["open_question"]["topic"] == "attributes"
    snap = api.post(f"/api/domains/{S}/conversation/answer", {"message": "hmm"}).get_json()
    # only ONE clarifying follow-up per question; then the interview moves on
    assert snap["open_question"]["is_followup"] is False and snap["open_question"]["topic"] != "attributes"


def test_skip_and_validation(api):
    api.start_domain(S)
    api.post(f"/api/domains/{S}/conversation")
    assert api.post(f"/api/domains/{S}/conversation/answer", {"message": "   "}).status_code == 422
    snap = api.post(f"/api/domains/{S}/conversation/answer", {"message": "skip"}).get_json()
    assert snap["pending_change_count"] == 0 and snap["open_question"]["topic"] == "attributes"


def test_ask_about_selected_node_grounded(api):
    api.start_domain(S)
    api.post(f"/api/domains/{S}/conversation")
    r = api.post(f"/api/domains/{S}/conversation/ask-node", {"node_key": "debtor", "question": "What is the formula?"})
    assert r.status_code == 200
    text = [m for m in r.get_json()["messages"] if m["role"] == "assistant"][-1]["content"]
    assert "arDso = meanDebt ÷ dailySales" in text
    # asking does not consume the open question
    assert r.get_json()["open_question"]["topic"] == "naming"
    assert api.post(f"/api/domains/{S}/conversation/ask-node", {"node_key": "zzz", "question": "hi"}).status_code == 404


def test_propose_requires_answers_and_compares_with_kg0(api):
    api.start_domain(S)
    api.post(f"/api/domains/{S}/conversation")
    assert api.post(f"/api/domains/{S}/conversation/propose").status_code == 422
    api.post(f"/api/domains/{S}/conversation/answer", {"message": "same"})
    snap = api.post(f"/api/domains/{S}/conversation/propose").get_json()
    assert snap["proposal"] is None                       # answers matched KG0 -> nothing to change
    assert any("no graph changes" in m["content"] for m in snap["messages"])


def test_business_experience_is_collected(client):
    a = Api(client)
    a.kg1(S)
    exp = a.get(f"/api/domains/{S}/experience").get_json()["experience"]
    assert exp and any(e["topic"] == "experience" and "quarter end" in e["content"] for e in exp)


def test_focus_on_selected_entity(api):
    api.start_domain(S)
    snap = api.post(f"/api/domains/{S}/conversation").get_json()
    assert api.post(f"/api/domains/{S}/conversation/focus", {"node_key": "ccc"}).status_code == 409  # must answer first
    api.post(f"/api/domains/{S}/conversation/answer", {"message": "skip"})
    snap = api.post(f"/api/domains/{S}/conversation/answer", {"message": "skip"}).get_json()
    # answer the open one then jump
    while snap["open_question"]:
        snap = api.post(f"/api/domains/{S}/conversation/answer", {"message": "skip"}).get_json()
        break
    f = api.post(f"/api/domains/{S}/conversation/focus", {"node_key": "ccc"})
    if f.status_code == 200:
        assert f.get_json()["open_question"]["node_key"] == "ccc"


# ------------------------------------------------------------------ configurable LLM
class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, 0

    def complete(self, system, user, max_tokens=1200):
        self.calls += 1
        if self.mode == "down":
            raise LLMError("offline")
        if "BASE QUESTION" in user:
            return "LLM-phrased: what do you call this entity in your business?"
        if "Summarise these proposed" in user:
            return "The client renames one entity and adds a custom attribute."
        return "ok"

    def complete_json(self, system, user, max_tokens=1500):
        self.calls += 1
        if self.mode == "down":
            raise LLMError("offline")
        ctx = json.loads(user)
        return {"acknowledgement": "LLM ack", "followup": None, "ops": [
            {"op": "update_node", "payload": {"node_key": ctx["CONTEXT"]["key"], "add_attributes": ["llmAttr"]}, "rationale": "from answer"},
            {"op": "drop_database", "payload": {}},                                     # unknown op -> dropped
            {"op": "add_edge", "payload": {"source": "ghost", "target": ctx["CONTEXT"]["key"], "label": "x"}},  # unknown node -> dropped
        ]}


def test_llm_used_when_configured_and_output_is_validated(cfg):
    llm = FakeLLM()
    app = create_app(cfg, llm_factory=lambda: llm)
    a = Api(app.test_client())
    a.start_domain(S)
    snap = a.post(f"/api/domains/{S}/conversation").get_json()
    assert snap["engine"] == "llm:fake" and snap["open_question"]["text"].startswith("LLM-phrased")
    snap = a.post(f"/api/domains/{S}/conversation/answer", {"message": "We call it Receivables Control Desk"}).get_json()
    assert snap["pending_change_count"] == 1            # 2 invalid LLM ops were discarded by validation
    prop = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    assert prop["llm_used"] is True and [i["op"] for i in prop["items"]] == ["update_node"]
    assert prop["summary"].startswith("The client renames")


def test_llm_failure_falls_back_to_rules(cfg):
    app = create_app(cfg, llm_factory=lambda: FakeLLM("down"))
    a = Api(app.test_client())
    a.start_domain(S)
    snap = a.post(f"/api/domains/{S}/conversation").get_json()
    assert snap["open_question"]["text"]               # base question still produced
    snap = a.post(f"/api/domains/{S}/conversation/answer", {"message": "We call it Receivables Control Desk"}).get_json()
    assert snap["pending_change_count"] == 1           # rule-based extraction still worked
    ack = [m for m in snap["messages"] if m["kind"] == "ack"][-1]
    assert ack["meta"]["engine"] == "rule-based" and "fallback_reason" in ack["meta"]
