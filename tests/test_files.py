"""Upload gating, validation, parsing (CSV/XLSX/PDF/TXT/DOCX/SQL), honest status, telemetry -> KG2."""
import io
import zipfile
from pathlib import Path

import pytest

from app.db import connect
from tests.conftest import Api, make_docx, make_pdf, make_xlsx

S = "working-capital-treasury"


@pytest.fixture()
def fin(client):
    """A user whose ontology is finalised (uploads unlocked)."""
    a = Api(client)
    a.kg1(S)
    assert a.post(f"/api/domains/{S}/finalize", {"confirm": True, "acknowledge_warnings": True}).status_code == 200
    return a


def up(a, name, data, ct="multipart/form-data"):
    return a.c.post(f"/api/domains/{S}/files", headers=a.h, data={"file": (io.BytesIO(data), name)}, content_type=ct)


def test_upload_blocked_until_ontology_finalised(client):
    a = Api(client)
    a.kg1(S)                                            # KG1 exists but NOT finalised
    r = up(a, "x.csv", b"a,b\n1,2\n")
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "workflow_step_locked"


def test_csv_processed_with_metadata_preview_and_mapping(fin):
    r = up(fin, "debtors.csv", b"arDso,ageingBuckets,overduePct,mystery\n41,0-30,3.2,x\n55,31-60,6.1,y\n")
    d = r.get_json()
    assert r.status_code == 201 and d["status"] == "processed" and d["job"]["status"] == "succeeded"
    assert d["metadata"]["rows_read"] == 2 and d["metadata"]["columns"] == 4
    assert {c["name"]: c["type"] for c in d["metadata"]["column_profile"]}["arDso"] == "number"
    det = fin.get(f"/api/files/{d['id']}").get_json()
    assert det["preview"]["columns"] == ["arDso", "ageingBuckets", "overduePct", "mystery"] and len(det["preview"]["rows"]) == 2
    m = det["mapping"]
    assert {x["term"] for x in m["matches"]} >= {"arDso", "ageingBuckets", "overduePct"}
    assert "mystery" in m["unmatched"]
    assert det["graph_version"] == "KG1"


def test_xlsx_docx_txt_pdf(fin):
    x = up(fin, "w.xlsx", make_xlsx({"arDso": [1, 2], "creditLimit": [5, 6]})).get_json()
    assert x["status"] == "processed" and x["metadata"]["sheets"][0]["columns"] == 2
    dx = up(fin, "n.docx", make_docx(["Review of DebtorPosition handling"], table=[["arDso", "note"], ["1", "a"]])).get_json()
    assert dx["status"] == "processed" and dx["metadata"]["tables"] == 1
    t = up(fin, "n.txt", b"Treasury function owns the cash conversion cycle.\n").get_json()
    assert t["status"] == "processed" and t["metadata"]["text_chars"] > 10
    p = up(fin, "r.pdf", make_pdf("Debtor Position arDso report")).get_json()
    assert p["status"] == "processed" and p["metadata"]["pages"] == 1 and p["metadata"]["text_chars"] > 5


def test_scanned_pdf_is_not_claimed_as_content(fin):
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(200, 200); b = io.BytesIO(); w.write(b)
    d = up(fin, "scan.pdf", b.getvalue()).get_json()
    assert d["status"] == "processed" and "No extractable text" in d["metadata"]["warning"]
    assert d["metadata"]["text_chars"] == 0


def test_sql_is_parsed_never_executed(fin, cfg):
    sql = (b"-- evil\nCREATE TABLE pwned_marker (id int, arDso int);\nINSERT INTO debtors (arDso, creditLimit) VALUES (1,2);\n"
           b"DROP TABLE users; DELETE FROM kg_ref_nodes; UPDATE users SET email='x';\n")
    d = up(fin, "dump.sql", sql).get_json()
    assert d["status"] == "processed" and d["metadata"]["executed"] is False
    assert d["metadata"]["tables"]["debtors"] == ["arDso", "creditLimit"]
    assert d["metadata"]["statement_types"]["DROP"] == 1
    conn = connect(cfg)
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'pwned_marker'")
        assert cur.fetchone() is None                         # CREATE TABLE was never run
        cur.execute("SELECT COUNT(*) c FROM users")
        assert cur.fetchone()["c"] >= 1                        # DROP/DELETE/UPDATE never ran
        cur.execute("SELECT COUNT(*) c FROM kg_ref_nodes")
        assert cur.fetchone()["c"] == 77
    conn.close()


@pytest.mark.parametrize("name,data,msg", [
    ("virus.exe", b"MZ\x90\x00", "Unsupported file type"),
    ("noext", b"hello", "Unsupported file type"),
    ("fake.pdf", b"this is just text", "not a valid PDF"),
    ("fake.docx", b"plain text pretending", "not a valid DOCX"),
    ("fake.xlsx", b"plain text pretending", "not a valid XLSX"),
    ("bin.csv", b"a,b\x00\x01\x02\n1,2", "binary content"),
    ("zip.txt", b"PK\x03\x04 zipped content pretending to be text", "not plain text"),
    ("pe.txt", b"MZ\x90\x00\x03\x00\x00", "binary content"),
    ("empty.csv", b"", "empty"),
])
def test_validation_rejects(fin, name, data, msg):
    r = up(fin, name, data)
    assert r.status_code == 422 and msg in r.get_json()["error"]["message"]
    assert fin.get(f"/api/domains/{S}/files").get_json()["files"] == []          # nothing stored


def test_text_starting_with_mz_is_still_accepted(fin):
    d = up(fin, "codes.csv", b"MZ-100,arDso\nMZ-200,5\n").get_json()
    assert d["status"] == "processed" and d["metadata"]["columns"] == 2


def test_single_column_and_semicolon_csv(fin):
    one = up(fin, "one.csv", b"arDso\n1\n2\n").get_json()
    assert one["status"] == "processed" and one["metadata"]["columns"] == 1 and one["metadata"]["rows_read"] == 2
    semi = up(fin, "semi.csv", b"arDso;creditLimit\n1;5\n").get_json()
    assert semi["metadata"]["columns"] == 2


def test_zip_that_is_not_an_office_doc_rejected(fin):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("hello.txt", "x")
    r = up(fin, "sneaky.docx", b.getvalue())
    assert r.status_code == 422 and "not a real DOCX" in r.get_json()["error"]["message"]


def test_size_limit_enforced(fin):
    r = up(fin, "big.csv", b"a,b\n" + b"1,2\n" * 400_000)      # > 1 MB test limit
    assert r.status_code in (413, 422)
    assert fin.get(f"/api/domains/{S}/files").get_json()["files"] == []


def test_path_traversal_filename_is_neutralised(fin, cfg):
    r = up(fin, "../../../etc/passwd.txt", b"root:x:0:0\n").get_json()
    assert ".." not in r["name"] and "/" not in r["name"]
    stored = list(Path(cfg.UPLOAD_DIR).rglob("*.txt"))
    assert stored and all(Path(cfg.UPLOAD_DIR).resolve() in p.resolve().parents for p in stored)


def test_corrupt_file_reports_failure_honestly(fin):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:                          # looks like xlsx (has xl/workbook.xml) but is garbage
        z.writestr("xl/workbook.xml", "<not-valid/>")
    d = up(fin, "broken.xlsx", b.getvalue()).get_json()
    assert d["status"] == "failed" and d["error"] and d["job"]["status"] == "failed"


def test_delete_file(fin, cfg):
    d = up(fin, "a.csv", b"arDso\n1\n").get_json()
    before = len(list(Path(cfg.UPLOAD_DIR).rglob("*.csv")))
    assert fin.delete(f"/api/files/{d['id']}").status_code == 200
    assert fin.get(f"/api/files/{d['id']}").status_code == 404
    assert len(list(Path(cfg.UPLOAD_DIR).rglob("*.csv"))) == before - 1


def test_telemetry_generates_kg2_with_data_source_node(fin):
    d = up(fin, "debtors.csv", b"arDso,ageingBuckets\n41,0-30\n").get_json()
    gen = fin.post(f"/api/domains/{S}/telemetry/generate")
    assert gen.status_code == 201
    prop = gen.get_json()["proposal"]
    assert prop["source"] == "telemetry" and any(i["op"] == "add_node" for i in prop["items"])
    dec = fin.post(f"/api/domains/{S}/proposals/{prop['id']}/decision", {"mode": "approve_all"}).get_json()
    assert dec["version"]["label"] == "KG2"
    g = fin.get(f"/api/domains/{S}/versions/2/graph").get_json()
    ds = [n for n in g["nodes"] if n["kind"] == "datasource"]
    assert len(ds) == 1 and ds[0]["origin"] == "telemetry" and ds[0]["name"] == "Data: debtors.csv"
    assert any(e["source"] == ds[0]["key"] and e["origin"] == "telemetry" for e in g["edges"])
    assert [v["kind"] for v in fin.get(f"/api/domains/{S}/versions").get_json()["versions"]] == ["customization", "telemetry"]
    # KG1 (the finalised ontology) is untouched
    g1 = fin.get(f"/api/domains/{S}/versions/1/graph").get_json()
    assert not any(n["kind"] == "datasource" for n in g1["nodes"])
    # cannot re-open once telemetry exists
    assert fin.post(f"/api/domains/{S}/reopen").status_code == 409


def test_unmatched_file_does_not_fabricate_a_graph(fin):
    up(fin, "random.csv", b"foo,bar,baz\n1,2,3\n")
    r = fin.post(f"/api/domains/{S}/telemetry/generate")
    assert r.status_code == 422 and "nothing to add" in r.get_json()["error"]["message"]
    assert fin.post(f"/api/domains/{S}/telemetry/generate").status_code == 422


def test_generate_needs_a_processed_file(fin):
    assert fin.post(f"/api/domains/{S}/telemetry/generate").status_code == 422
