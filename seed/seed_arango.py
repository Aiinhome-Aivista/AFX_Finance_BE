#!/usr/bin/env python3
"""Seed the Finance Knowledge Graph (KG0) into ArangoDB from seed/data/finance_kg0.json.

Creates:
  - Database: finance_kg
  - Collections:
      * kg_ref_nodes (Document collection, _key = node_key)
      * kg_ref_edges (Edge collection, _from = kg_ref_nodes/src, _to = kg_ref_nodes/tgt)
      * domains (Document collection, _key = domain slug)
      * flows (Document collection, _key = flow_key)

Usage:
  python seed/seed_arango.py
"""
import json
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.arango_db import ArangoHttpDriver

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("seed_arango")

DATA_FILE = Path(__file__).resolve().parent / "data" / "finance_kg0.json"


def seed_arango(cfg=None):
    cfg = cfg or Config.from_env()
    log.info("Connecting to ArangoDB at %s as %s...", cfg.ARANGO_URL, cfg.ARANGO_USER)

    driver = ArangoHttpDriver(
        url=cfg.ARANGO_URL,
        username=cfg.ARANGO_USER,
        password=cfg.ARANGO_PASSWORD,
        db_name=cfg.ARANGO_DB,
    )

    ok, info = driver.ping()
    if not ok:
        raise ConnectionError(f"Cannot reach ArangoDB server at {cfg.ARANGO_URL}: {info}")
    log.info("Connected to ArangoDB: %s", info)

    # 1. Ensure Database exists
    driver.ensure_database()

    # 2. Ensure Collections exist
    driver.ensure_collection("kg_ref_nodes", is_edge=False)
    driver.ensure_collection("kg_ref_edges", is_edge=True)
    driver.ensure_collection("domains", is_edge=False)
    driver.ensure_collection("flows", is_edge=False)
    driver.ensure_collection("domain_nodes", is_edge=False)

    # 3. Read dataset
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    nodes_raw = data.get("nodes", [])
    edges_raw = data.get("edges", [])
    domains_raw = data.get("domains", [])
    flows_raw = data.get("flows", [])
    domain_nodes_raw = data.get("domain_nodes", [])

    log.info("Found %d nodes, %d edges, %d domains, %d flows, %d domain_nodes in seed JSON.",
             len(nodes_raw), len(edges_raw), len(domains_raw), len(flows_raw), len(domain_nodes_raw))

    # 4. Prepare Document Nodes
    nodes_docs = []
    for n in nodes_raw:
        k = n["key"]
        nodes_docs.append({
            "_key": k,
            "id": k,
            "node_key": k,
            "key": k,
            "name": n.get("name", k),
            "n": n.get("name", k),
            "group_key": n.get("group", "general"),
            "d": n.get("group", "general"),
            "group_name": n.get("group_name", ""),
            "color": n.get("color", "#2563EB"),
            "kind": n.get("kind", "domain"),
            "definition": n.get("definition", ""),
            "def": n.get("definition", ""),
            "attributes": n.get("attributes", []),
            "a": n.get("attributes", []),
            "formulas": n.get("formulas", []),
            "f": n.get("formulas", []),
            "evidence": n.get("evidence", ""),
            "ev": n.get("evidence", ""),
            "canon_attributes": n.get("canon_attributes", []),
            "canon_ref": n.get("canon_ref"),
            "canon_title": n.get("canon_title"),
            "canon_joins": n.get("canon_joins"),
            "canon_evidence": n.get("canon_evidence"),
        })

    # 5. Prepare Edge Documents
    edges_docs = []
    for idx, e in enumerate(edges_raw):
        src = e["source"]
        tgt = e["target"]
        label = e.get("label", "relates_to")
        safe_label = "".join(c if c.isalnum() else "_" for c in label)
        edge_key = f"{src}_{tgt}_{safe_label}"
        edges_docs.append({
            "_key": edge_key,
            "_from": f"kg_ref_nodes/{src}",
            "_to": f"kg_ref_nodes/{tgt}",
            "source": src,
            "target": tgt,
            "source_key": src,
            "target_key": tgt,
            "label": label,
            "origin_graph": e.get("origin_graph", "fmcg_canon"),
            "cross_domain": bool(e.get("cross_domain", 0)),
        })

    # 6. Prepare Domains Documents
    domains_docs = []
    for dm in domains_raw:
        slug = dm["slug"]
        domains_docs.append({
            "_key": slug,
            "code": dm["code"],
            "slug": slug,
            "name": dm["name"],
            "description": dm.get("description", ""),
            "scope": dm.get("scope", ""),
            "color": dm.get("color", "#2563EB"),
            "sort_order": dm.get("sort_order", 0),
            "knowledge": dm.get("knowledge", {}),
            "knowledge_evidence": dm.get("knowledge_evidence", ""),
        })

    # 7. Prepare Flows Documents
    flows_docs = []
    for fl in flows_raw:
        fkey = fl["key"]
        steps = fl.get("steps", [])
        flows_docs.append({
            "_key": fkey,
            "flow_key": fkey,
            "key": fkey,
            "title": fl.get("title", fkey),
            "t": fl.get("title", fkey),
            "narrative": fl.get("narrative", ""),
            "x": fl.get("narrative", ""),
            "sort_order": fl.get("sort_order", 0),
            "steps": steps,
            "ids": steps,
            "step_count": len(steps),
        })

    # 8. Prepare Domain Nodes Documents
    domain_nodes_docs = []
    for dn in domain_nodes_raw:
        dom = dn["domain"]
        node = dn["node"]
        safe_dom = dom.replace("-", "_")
        domain_nodes_docs.append({
            "_key": f"{safe_dom}__{node}",
            "domain_slug": dom,
            "domain_id": dom,
            "node_key": node,
            "role": dn.get("role", "core"),
        })

    # 9. Import into ArangoDB
    log.info("Importing %d nodes into collection kg_ref_nodes...", len(nodes_docs))
    driver.import_documents("kg_ref_nodes", nodes_docs, overwrite=True)

    log.info("Importing %d edges into edge collection kg_ref_edges...", len(edges_docs))
    driver.import_documents("kg_ref_edges", edges_docs, overwrite=True)

    log.info("Importing %d domains into collection domains...", len(domains_docs))
    driver.import_documents("domains", domains_docs, overwrite=True)

    log.info("Importing %d flows into collection flows...", len(flows_docs))
    driver.import_documents("flows", flows_docs, overwrite=True)

    log.info("Importing %d domain_nodes into collection domain_nodes...", len(domain_nodes_docs))
    driver.import_documents("domain_nodes", domain_nodes_docs, overwrite=True)

    summary = {
        "database": cfg.ARANGO_DB,
        "nodes": len(nodes_docs),
        "edges": len(edges_docs),
        "domains": len(domains_docs),
        "flows": len(flows_docs),
        "domain_nodes": len(domain_nodes_docs),
        "status": "success",
    }
    log.info("ArangoDB seeding completed successfully: %s", summary)
    return summary


if __name__ == "__main__":
    res = seed_arango()
    print("Seed result:", res)
