"""KG0 read-side service: domain graphs, node details, blocks, search and flows. Strictly read-only."""
from ..errors import NotFoundError, ValidationError
from ..repositories.reference import ReferenceRepository
from .graph_ops import GraphState, reference_edge_to_state, reference_node_to_state


class ReferenceService:
    def __init__(self, db):
        self.repo = ReferenceRepository(db)

    # ------------------------------------------------------------------ domains
    def domain(self, slug):
        d = self.repo.domain_by_slug(slug)
        if not d:
            raise NotFoundError("Unknown Finance domain.")
        return d

    def list_domains(self, q=None):
        counts = self.repo.domain_node_counts()
        out = []
        for d in self.repo.domains():
            c = counts.get(d["id"], {})
            hay = " ".join([d["name"], d["description"], d["scope"], d["code"]]).lower()
            if q and q.lower() not in hay:
                continue
            out.append({"id": d["id"], "code": d["code"], "slug": d["slug"], "name": d["name"], "description": d["description"],
                        "scope": d["scope"], "color": d["color"],
                        "counts": {"core": c.get("core", 0), "shared": c.get("shared", 0), "neighbour": c.get("neighbour", 0)}})
        return out

    def domain_detail(self, slug):
        d = self.domain(slug)
        c = self.repo.domain_node_counts().get(d["id"], {})
        return {"id": d["id"], "code": d["code"], "slug": d["slug"], "name": d["name"], "description": d["description"],
                "scope": d["scope"], "color": d["color"], "knowledge": d["knowledge"], "knowledge_evidence": d["knowledge_evidence"],
                "counts": {"core": c.get("core", 0), "shared": c.get("shared", 0), "neighbour": c.get("neighbour", 0)}}

    # ------------------------------------------------------------------ graph
    @staticmethod
    def _public_node(n, role):
        return {"key": n["node_key"], "name": n["name"], "group_key": n["group_key"], "group_name": n["group_name"],
                "color": n["color"], "kind": n["kind"], "role": role, "definition": n["definition"],
                "attributes": n["attributes"], "formulas": n["formulas"], "origin": "reference", "change_state": "unchanged"}

    def domain_graph(self, slug, neighbours=True):
        d = self.domain(slug)
        roles = self.repo.domain_node_roles(d["id"])
        keep = {k for k, r in roles.items() if r in ("core", "shared") or (neighbours and r == "neighbour")}
        nodes = [self._public_node(n, roles[n["node_key"]]) for n in self.repo.nodes_by_keys(keep)]
        edges = [{"source": e["source_key"], "target": e["target_key"], "label": e["label"], "origin": "reference",
                  "change_state": "unchanged", "cross_domain": bool(e["cross_domain"]), "origin_graph": e["origin_graph"]}
                 for e in self.repo.edges_within(keep)]
        return {"domain": {"slug": d["slug"], "code": d["code"], "name": d["name"], "color": d["color"]},
                "version": {"number": 0, "label": "KG0", "readonly": True}, "nodes": nodes, "edges": edges}

    def node_detail(self, key, domain_slug=None):
        n = self.repo.node(key)
        if not n:
            raise NotFoundError("Unknown entity.")
        edges = self.repo.edges_of(key)
        other = {k for e in edges for k in (e["source_key"], e["target_key"])} - {key}
        names = {x["node_key"]: x for x in self.repo.nodes_by_keys(other)}
        rels = []
        for e in edges:
            outgoing = e["source_key"] == key
            o = names[e["target_key"] if outgoing else e["source_key"]]
            rels.append({"direction": "out" if outgoing else "in", "label": e["label"], "node_key": o["node_key"],
                         "node_name": o["name"], "group_name": o["group_name"], "color": o["color"],
                         "cross_domain": bool(e["cross_domain"]), "origin_graph": e["origin_graph"]})
        return {"key": n["node_key"], "name": n["name"], "group_key": n["group_key"], "group_name": n["group_name"],
                "color": n["color"], "kind": n["kind"], "definition": n["definition"], "attributes": n["attributes"],
                "formulas": n["formulas"], "evidence": n["evidence"], "canon_attributes": n["canon_attributes"],
                "canon": {"section": n["canon_ref"], "title": n["canon_title"], "joins": n["canon_joins"], "evidence": n["canon_evidence"]}
                if n["canon_ref"] else None,
                "relationships": rels, "joins": [{"node_key": r["node_key"], "node_name": r["node_name"]} for r in rels],
                "domains": self.repo.node_domains(key), "origin": "reference"}

    # ------------------------------------------------------------------ blocks (graph sections a client can focus on)
    def blocks(self, slug):
        d = self.domain(slug)
        roles = self.repo.domain_node_roles(d["id"])
        by_role = {"core": [], "shared": [], "neighbour": []}
        for k, r in roles.items():
            by_role[r].append(k)
        nodes = {n["node_key"]: n for n in self.repo.nodes_by_keys(roles.keys())}
        mk = lambda bid, title, desc, keys, default: {  # noqa: E731
            "id": bid, "title": title, "description": desc, "node_keys": sorted(keys),
            "node_names": [nodes[k]["name"] for k in sorted(keys)], "default": default}
        out = []
        if by_role["core"]:
            out.append(mk("core", f"{d['name']} — core entities", "Entities that belong to this sub-domain in the ontology.", by_role["core"], True))
        if by_role["shared"]:
            out.append(mk("shared", "Shared master-data spine", "Finance-wide dimensions this sub-domain joins to (accounts, entities, time, FX, product, customer…).", by_role["shared"], True))
        if by_role["neighbour"]:
            out.append(mk("neighbours", "Cross-domain neighbours", "Entities in other sub-domains or commercial domains linked to this one by ontology relationships.", by_role["neighbour"], False))
        member = set(roles)
        for f in self.repo.flows():
            keys = [s["node_key"] for s in self.repo.flow_steps(f["id"])]
            inside = [k for k in keys if k in member]
            if len(inside) >= 2 and any(roles[k] == "core" for k in inside):
                out.append(mk(f"flow:{f['flow_key']}", f"Flow — {f['title']}", f["narrative"], inside, False))
        return out

    def baseline(self, slug, block_ids=None):
        """KG0 restricted to the selected blocks, as an independent (copied) GraphState."""
        d = self.domain(slug)
        roles = self.repo.domain_node_roles(d["id"])
        blocks = {b["id"]: b for b in self.blocks(slug)}
        ids = block_ids or [b["id"] for b in blocks.values() if b["default"]]
        unknown = [i for i in ids if i not in blocks]
        if unknown:
            raise ValidationError(f"Unknown block(s): {', '.join(unknown)}")
        keys = {k for i in ids for k in blocks[i]["node_keys"]}
        if len(keys) < 2:
            raise ValidationError("Select blocks containing at least two entities.")
        nodes = [reference_node_to_state(n, roles.get(n["node_key"], "core")) for n in self.repo.nodes_by_keys(keys)]
        order = {k: i for i, k in enumerate(sorted(keys))}
        nodes.sort(key=lambda n: order[n["key"]])
        edges = [reference_edge_to_state(e) for e in self.repo.edges_within(keys)]
        return GraphState.from_lists(nodes, edges)

    def core_keys(self, slug):
        d = self.domain(slug)
        return {k for k, r in self.repo.domain_node_roles(d["id"]).items() if r == "core"}

    # ------------------------------------------------------------------ search
    def search(self, q, slug=None, scope="all"):
        q = (q or "").strip()
        if len(q) < 2:
            raise ValidationError("Type at least 2 characters to search.")
        if scope not in ("all", "entity", "attribute", "formula"):
            raise ValidationError("scope must be one of all, entity, attribute, formula.")
        did = self.domain(slug)["id"] if slug else None
        low, out = q.lower(), []
        for n in self.repo.search_nodes(q, did):
            hits = []
            if scope in ("all", "entity") and (low in n["name"].lower() or low in n["node_key"].lower()):
                hits.append({"field": "entity", "text": n["name"]})
            if scope in ("all", "attribute"):
                hits += [{"field": "attribute", "text": a} for a in n["attributes"] if low in a.lower()]
                hits += [{"field": "attribute (canon)", "text": a} for a in n["canon_attributes"] if low in a.lower()
                         and a.lower() not in [x.lower() for x in n["attributes"]]]
            if scope in ("all", "formula"):
                hits += [{"field": "formula", "text": f} for f in n["formulas"] if low in f.lower()]
            if scope == "all" and not hits and low in n["definition"].lower():
                i = n["definition"].lower().index(low)
                hits.append({"field": "definition", "text": "…" + n["definition"][max(0, i - 40): i + 80] + "…"})
            if hits:
                out.append({"key": n["node_key"], "name": n["name"], "group_name": n["group_name"], "color": n["color"], "hits": hits[:6]})
        return out

    # ------------------------------------------------------------------ flows
    def flows(self, slug=None):
        roles = {}
        if slug:
            roles = self.repo.domain_node_roles(self.domain(slug)["id"])
        out = []
        for f in self.repo.flows():
            keys = [s["node_key"] for s in self.repo.flow_steps(f["id"])]
            out.append({"key": f["flow_key"], "title": f["title"], "narrative": f["narrative"], "step_count": len(keys),
                        "relevant": bool(slug) and any(roles.get(k) == "core" for k in keys),
                        "touches_domain": bool(slug) and any(k in roles for k in keys)})
        return out

    def flow_detail(self, key):
        f = self.repo.flow(key)
        if not f:
            raise NotFoundError("Unknown flow.")
        steps = self.repo.flow_steps(f["id"])
        keys = [s["node_key"] for s in steps]
        nodes = {n["node_key"]: n for n in self.repo.nodes_by_keys(keys)}
        edges = self.repo.edges_within(keys)
        return {"key": f["flow_key"], "title": f["title"], "narrative": f["narrative"],
                "steps": [{"order": s["step_order"], "node_key": s["node_key"], "name": nodes[s["node_key"]]["name"],
                           "group_name": nodes[s["node_key"]]["group_name"], "color": nodes[s["node_key"]]["color"],
                           "definition": nodes[s["node_key"]]["definition"], "attributes": nodes[s["node_key"]]["attributes"],
                           "evidence": nodes[s["node_key"]]["evidence"]} for s in steps],
                "nodes": [self._public_node(n, "flow") for n in nodes.values()],
                "edges": [{"source": e["source_key"], "target": e["target_key"], "label": e["label"], "origin": "reference",
                           "change_state": "unchanged", "cross_domain": bool(e["cross_domain"])} for e in edges]}
