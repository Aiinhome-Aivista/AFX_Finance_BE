"""User-level data isolation: one user can never read or change another user's graphs, chats, proposals or files."""
import io

from tests.conftest import Api, make_pdf

S = "working-capital-treasury"


def _finalised_with_file(a):
    a.kg1(S)
    a.post(f"/api/domains/{S}/finalize", {"confirm": True, "acknowledge_warnings": True})
    r = a.c.post(f"/api/domains/{S}/files", headers=a.h, data={"file": (io.BytesIO(b"arDso\n1\n"), "a.csv")}, content_type="multipart/form-data")
    return r.get_json()["id"]


def test_files_are_private(client):
    a, b = Api(client), Api(client)
    fid = _finalised_with_file(a)
    assert a.get(f"/api/files/{fid}").status_code == 200
    assert b.get(f"/api/files/{fid}").status_code == 404
    assert b.delete(f"/api/files/{fid}").status_code == 404
    assert a.get(f"/api/files/{fid}").status_code == 200                      # still there
    b.start_domain(S)
    assert b.get(f"/api/domains/{S}/files").get_json()["files"] == []          # b sees none of a's files
    assert b.c.post(f"/api/domains/{S}/files", headers=b.h, data={"file": (io.BytesIO(b"a\n1\n"), "x.csv")}, content_type="multipart/form-data").status_code == 409
    assert a.get(f"/api/domains/{S}/files").get_json()["files"][0]["id"] == fid


def test_graphs_conversations_proposals_are_private(client):
    a, b = Api(client), Api(client)
    a.kg1(S)
    pid = a.get(f"/api/domains/{S}/proposals").get_json()["proposals"][0]["id"]
    # b has not even started this domain
    for url in (f"/api/domains/{S}/versions", f"/api/domains/{S}/versions/1/graph", f"/api/domains/{S}/proposals",
                f"/api/domains/{S}/working-graph", f"/api/domains/{S}/finalization", f"/api/domains/{S}/experience"):
        assert b.get(url).status_code == 409, url
    # b starts the domain: sees only their own (empty) state, never a's KG1
    b.start_domain(S)
    assert b.get(f"/api/domains/{S}/versions").get_json()["versions"] == []
    assert b.get(f"/api/domains/{S}/proposals").get_json()["proposals"] == []
    assert b.get(f"/api/domains/{S}/versions/1/graph").status_code == 404
    assert b.get(f"/api/domains/{S}/proposals/{pid}").status_code == 404
    assert b.post(f"/api/domains/{S}/proposals/{pid}/decision", {"mode": "approve_all"}).status_code == 404
    conv = b.get(f"/api/domains/{S}/conversation").get_json()
    assert conv["messages"] == [] and conv["conversation_id"] is None
    assert b.get(f"/api/domains/{S}/experience").get_json()["experience"] == []
    # a's data intact
    assert [v["label"] for v in a.get(f"/api/domains/{S}/versions").get_json()["versions"]] == ["KG1"]


def test_workflow_progress_is_per_user(client):
    a, b = Api(client), Api(client)
    a.kg1(S)
    pa = a.get("/api/domains").get_json()["progress"]
    pb = b.get("/api/domains").get_json()["progress"]
    assert S in pa and pb == {}
    assert a.get("/api/auth/me").get_json()["user"]["selected_app"] == "finance"
    assert b.get("/api/auth/me").get_json()["user"]["selected_app"] is None


def test_finalise_and_reopen_only_affect_own_workflow(client):
    a, b = Api(client), Api(client)
    a.kg1(S); b.kg1(S)
    assert a.post(f"/api/domains/{S}/finalize", {"confirm": True, "acknowledge_warnings": True}).status_code == 200
    assert b.get(f"/api/domains/{S}/workflow").get_json()["finalized_version_number"] is None
