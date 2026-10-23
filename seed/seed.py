#!/usr/bin/env python3
"""Load the KG0 reference ontology (domains, nodes, edges, flows) from seed/data/finance_kg0.json.

This is the ONLY code path allowed to write the reference tables: it flips the session variable
@kg0_seeding that the DB triggers check. The Flask app never does.   Usage: python seed/seed.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from app.config import Config  # noqa: E402
from app.db import connect, jdump  # noqa: E402

DATA = Path(__file__).resolve().parent / "data" / "finance_kg0.json"


def seed(cfg=None):
    cfg = cfg or Config.from_env()
    d = json.loads(DATA.read_text(encoding="utf-8"))
    conn = connect(cfg)
    cur = conn.cursor()
    cur.execute("SET @kg0_seeding = 1")
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for t in ("flow_steps", "flows", "domain_nodes", "kg_ref_edges", "kg_ref_nodes"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    # domains: upsert by code so user workflows (FK) survive a re-seed
    for dm in d["domains"]:
        cur.execute(
            """INSERT INTO domains(code,slug,name,description,scope,color,sort_order,knowledge,knowledge_evidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE slug=VALUES(slug),name=VALUES(name),description=VALUES(description),scope=VALUES(scope),
               color=VALUES(color),sort_order=VALUES(sort_order),knowledge=VALUES(knowledge),knowledge_evidence=VALUES(knowledge_evidence)""",
            (dm["code"], dm["slug"], dm["name"], dm["description"], dm["scope"], dm["color"], dm["sort_order"],
             jdump(dm["knowledge"]), dm["knowledge_evidence"]))
    for n in d["nodes"]:
        cur.execute(
            """INSERT INTO kg_ref_nodes(node_key,name,group_key,group_name,color,kind,definition,attributes,formulas,evidence,
               canon_attributes,canon_ref,canon_title,canon_joins,canon_evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (n["key"], n["name"], n["group"], n["group_name"], n["color"], n["kind"], n["definition"],
             jdump(n["attributes"]), jdump(n["formulas"]), n["evidence"], jdump(n.get("canon_attributes", [])), n.get("canon_ref"), n.get("canon_title"),
             n.get("canon_joins"), n.get("canon_evidence")))
    for e in d["edges"]:
        cur.execute("INSERT INTO kg_ref_edges(source_key,target_key,label,origin_graph,cross_domain) VALUES (%s,%s,%s,%s,%s)",
                    (e["source"], e["target"], e["label"], e["origin_graph"], int(e["cross_domain"])))
    cur.execute("SELECT id, slug FROM domains")
    ids = {r["slug"]: r["id"] for r in cur.fetchall()}
    for dn in d["domain_nodes"]:
        cur.execute("INSERT INTO domain_nodes(domain_id,node_key,role) VALUES (%s,%s,%s)", (ids[dn["domain"]], dn["node"], dn["role"]))
    for f in d["flows"]:
        cur.execute("INSERT INTO flows(flow_key,title,narrative,sort_order) VALUES (%s,%s,%s,%s)",
                    (f["key"], f["title"], f["narrative"], f["sort_order"]))
        fid = cur.lastrowid
        for i, s in enumerate(f["steps"], start=1):
            cur.execute("INSERT INTO flow_steps(flow_id,step_order,node_key) VALUES (%s,%s,%s)", (fid, i, s))
    cur.execute("SET @kg0_seeding = 0")
    conn.close()
    return {"domains": len(d["domains"]), "nodes": len(d["nodes"]), "edges": len(d["edges"]), "flows": len(d["flows"])}


if __name__ == "__main__":
    print("seeded:", seed())
