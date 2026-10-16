"""Smoke test of the complete 6-step workflow through the REST API."""
from .conftest import Api


def test_full_workflow_to_kg2(client, tmp_path):
    api = Api(client)
    slug = "working-capital-treasury"
    api.kg1(slug)
    # KG1 exists, step 4 completed
    v = api.get(f"/api/domains/{slug}/versions").get_json()
    assert [x["label"] for x in v["versions"]] == ["KG1"]
    g1 = api.get(f"/api/domains/{slug}/versions/1/graph").get_json()
    assert any(n["change_state"] == "added" and n["kind"] == "experience" for n in g1["nodes"])
    assert any(n["change_state"] == "modified" for n in g1["nodes"])
    assert any(n["name"] == "Receivables Control Desk" for n in g1["nodes"])
    # finalize
    pre = api.get(f"/api/domains/{slug}/finalization")
    assert pre.status_code == 200
    r = api.post(f"/api/domains/{slug}/finalize", {"confirm": True, "acknowledge_warnings": True})
    assert r.status_code == 200, r.get_json()
    # upload a CSV mapped to debtor attributes
    csv = b"arDso,ageingBuckets,overduePct,mystery\n41,\"0-30\",3.2,x\n"
    import io
    up = client.post(f"/api/domains/{slug}/files", headers=api.h, data={"file": (io.BytesIO(csv), "debtors.csv")}, content_type="multipart/form-data")
    assert up.status_code == 201, up.get_json()
    assert up.get_json()["status"] == "processed"
    gen = api.post(f"/api/domains/{slug}/telemetry/generate")
    assert gen.status_code == 201, gen.get_json()
    pid = gen.get_json()["proposal"]["id"]
    d = api.post(f"/api/domains/{slug}/proposals/{pid}/decision", {"mode": "approve_all"})
    assert d.status_code == 200, d.get_json()
    assert d.get_json()["version"]["label"] == "KG2"
    st = api.get(f"/api/domains/{slug}/workflow").get_json()
    assert [s["status"] for s in st["steps"]] == ["completed"] * 6
