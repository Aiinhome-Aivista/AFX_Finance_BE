"""Workflow gating, proposals, KG1/KG2+ versioning, transactions, comparison, finalisation."""
from app.db import connect
from tests.conftest import Api

S = "working-capital-treasury"


def _versions(a):
    return a.get(f"/api/domains/{S}/versions").get_json()["versions"]


def test_steps_are_enforced_in_order(client):
    a = Api(client)
    # domain before app selection
    assert a.post(f"/api/domains/{S}/select").status_code == 409
    a.post("/api/apps/select", {"app": "finance"})
    # blocks / chat / finalize / upload before earlier steps
    assert a.post(f"/api/domains/{S}/blocks", {"blocks": ["core"]}).status_code == 409
    assert a.post(f"/api/domains/{S}/conversation").status_code == 409
    a.post(f"/api/domains/{S}/select")
    assert a.post(f"/api/domains/{S}/conversation").status_code == 409          # blocks not chosen yet
    a.post(f"/api/domains/{S}/blocks", {"blocks": ["core", "shared"]})
    assert a.post(f"/api/domains/{S}/finalize", {"confirm": True}).status_code == 409   # no KG1 yet
    assert a.get(f"/api/domains/{S}/finalization").status_code == 409
    r = a.post(f"/api/domains/{S}/files", content_type="multipart/form-data")
    assert r.status_code in (409, 422)
    st = a.get(f"/api/domains/{S}/workflow").get_json()
    assert [s["status"] for s in st["steps"]] == ["completed", "completed", "completed", "active", "locked", "locked"]


def test_invalid_blocks_rejected(client):
    a = Api(client)
    a.post("/api/apps/select", {"app": "finance"})
    a.post(f"/api/domains/{S}/select")
    assert a.post(f"/api/domains/{S}/blocks", {"blocks": []}).status_code == 422
    assert a.post(f"/api/domains/{S}/blocks", {"blocks": ["nope"]}).status_code == 422
    assert a.post("/api/apps/select", {"app": "hr"}).status_code == 422


def test_kg1_then_kg2_and_history_preserved(client):
    a = Api(client)
    a.kg1(S)
    kg1_before = a.get(f"/api/domains/{S}/versions/1/graph").get_json()
    # second round of customisation -> KG2
    snap = a.post(f"/api/domains/{S}/conversation").get_json()
    guard = 0
    while snap["open_question"] and guard < 40:
        guard += 1
        snap = a.post(f"/api/domains/{S}/conversation/answer", {"message": "We also track bankGuaranteeCover"
                                                                  if snap["open_question"]["topic"] == "attributes" else "same"}).get_json()
        if snap["pending_change_count"]:
            break
    prop = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    d = a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "approve_all"}).get_json()
    assert d["version"]["label"] == "KG2"
    assert [v["label"] for v in _versions(a)] == ["KG1", "KG2"]
    # KG1 is immutable history
    assert a.get(f"/api/domains/{S}/versions/1/graph").get_json()["nodes"] == kg1_before["nodes"]
    # highlight flags are relative to the previous version
    g2 = a.get(f"/api/domains/{S}/versions/2/graph").get_json()
    assert any(n["change_state"] == "modified" for n in g2["nodes"])
    assert not any(n["change_state"] == "added" for n in g2["nodes"]) or all(n["added_in_version"] == 2 for n in g2["nodes"] if n["change_state"] == "added")
    # comparison
    diff = a.get(f"/api/domains/{S}/compare?from=1&to=2").get_json()
    assert diff["counts"]["nodes_modified"] >= 1
    diff0 = a.get(f"/api/domains/{S}/compare?from=0&to=2").get_json()
    assert diff0["counts"]["nodes_added"] >= 1 and diff0["counts"]["nodes_modified"] >= 1


def test_provenance_of_nodes_and_edges(client):
    a = Api(client)
    a.kg1(S)
    g = a.get(f"/api/domains/{S}/versions/1/graph").get_json()
    origins = {n["origin"] for n in g["nodes"]}
    assert origins == {"reference", "user"}
    assert all(n["added_in_version"] == 1 for n in g["nodes"] if n["origin"] == "user" and n["change_state"] == "added")
    assert all(e["origin"] in ("reference", "user") for e in g["edges"])


def test_reject_all_creates_no_version(client):
    a = Api(client)
    a.start_domain(S)
    a.answer_until(S, ["We call it Receivables Control Desk"])
    prop = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    d = a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "reject_all"}).get_json()
    assert d["version"] is None and d["proposal"]["status"] == "rejected"
    assert _versions(a) == []
    # cannot be decided twice
    assert a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "approve_all"}).status_code == 409


def test_partial_approval_applies_only_selected(client):
    a = Api(client)
    a.start_domain(S)
    a.answer_until(S, ["We call it Receivables Control Desk", "creditLimit, region code"])
    prop = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    assert len(prop["items"]) == 2
    rename = next(i for i in prop["items"] if "rename" in i["description"])
    d = a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "selected", "approved_item_ids": [rename["id"]]}).get_json()
    st = {i["id"]: i["status"] for i in d["proposal"]["items"]}
    assert sorted(st.values()) == ["approved", "rejected"]
    g = a.get(f"/api/domains/{S}/versions/1/graph").get_json()
    n = next(x for x in g["nodes"] if x["key"] == "debtor")
    assert n["name"] == "Receivables Control Desk" and "creditLimit" not in n["attributes"]


def test_atomic_version_creation_rolls_back(client, cfg):
    """Approving an edge whose new endpoint (add_node) was NOT approved must fail without leaving a half-built version."""
    a = Api(client)
    a.start_domain(S)
    # relationships answer creates add_node + add_edge
    snap = a.post(f"/api/domains/{S}/conversation").get_json()
    guard = 0
    while snap["open_question"] and snap["open_question"]["topic"] != "relationships" and guard < 10:
        guard += 1
        snap = a.post(f"/api/domains/{S}/conversation/answer", {"message": "same"}).get_json()
    snap = a.post(f"/api/domains/{S}/conversation/answer", {"message": "We also have Distributor Rebate Ledger that feeds it"}).get_json()
    prop = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    edge = next(i for i in prop["items"] if i["op"] == "add_edge")
    r = a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "selected", "approved_item_ids": [edge["id"]]})
    assert r.status_code == 409
    assert _versions(a) == []
    conn = connect(cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM user_graph_versions v JOIN users u ON u.id=v.user_id WHERE u.email=%s", (a.email,))
        assert cur.fetchone()["c"] == 0
        cur.execute("SELECT status FROM change_proposals WHERE id=%s", (prop["id"],))
        assert cur.fetchone()["status"] == "pending"        # still reviewable
    conn.close()
    ok = a.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "approve_all"})
    assert ok.status_code == 200


def test_stale_proposal_is_superseded(client):
    a = Api(client)
    a.start_domain(S)
    a.answer_until(S, ["We call it Receivables Control Desk"])
    p1 = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    a.post(f"/api/domains/{S}/conversation/answer", {"message": "creditLimit"})
    p2 = a.post(f"/api/domains/{S}/conversation/propose").get_json()["proposal"]
    assert p2["id"] != p1["id"]
    old = a.get(f"/api/domains/{S}/proposals/{p1['id']}").get_json()
    assert old["status"] == "superseded"
    assert a.post(f"/api/domains/{S}/proposals/{p1['id']}/decision", {"mode": "approve_all"}).status_code == 409


def test_finalisation_rules(client):
    a = Api(client)
    a.kg1(S)
    pre = a.get(f"/api/domains/{S}/finalization").get_json()
    assert pre["graph"]["nodes"] and "warnings" in pre["validation"] and pre["comparison"]["counts"] and pre["history"]
    assert a.post(f"/api/domains/{S}/finalize", {}).status_code == 422                     # explicit confirm required
    if pre["validation"]["warnings"]:
        r = a.post(f"/api/domains/{S}/finalize", {"confirm": True})
        assert r.status_code == 409 and r.get_json()["error"]["code"] == "warnings_unacknowledged"
    assert a.post(f"/api/domains/{S}/finalize", {"confirm": True, "acknowledge_warnings": True}).status_code == 200
    st = a.get(f"/api/domains/{S}/workflow").get_json()
    assert [s["status"] for s in st["steps"]][:5] == ["completed"] * 5 and st["finalized_version_number"] == 1
    # the interview is locked once finalised
    assert a.post(f"/api/domains/{S}/conversation/answer", {"message": "x"}).status_code == 409
    assert a.post(f"/api/domains/{S}/conversation/propose").status_code == 409
    # not automatic: nothing finalised before the explicit call (covered above); re-open works with no telemetry
    assert a.post(f"/api/domains/{S}/reopen").status_code == 200
    assert a.get(f"/api/domains/{S}/workflow").get_json()["finalized_version_number"] is None


def test_never_finalised_automatically(client):
    a = Api(client)
    a.kg1(S)
    st = a.get(f"/api/domains/{S}/workflow").get_json()
    assert st["finalized_version_number"] is None
    assert [s["status"] for s in st["steps"]][4] == "active"
