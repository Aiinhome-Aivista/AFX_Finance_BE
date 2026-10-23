"""KG0 read-side service: domain graphs, node details, blocks, search and flows. Strictly read-only."""
from ..errors import NotFoundError, ValidationError
from ..repositories.reference import ReferenceRepository
from .graph_ops import GraphState, reference_edge_to_state, reference_node_to_state


class ReferenceService:
    def __init__(self, db=None):
        self.db = db
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
        key = n.get("node_key") or n.get("key")
        return {"key": key, "name": n.get("name") or key, "group_key": n.get("group_key", "general"), "group_name": n.get("group_name", ""),
                "color": n.get("color", "#3b82f6"), "kind": n.get("kind", "entity"), "role": role, "definition": n.get("definition", ""),
                "attributes": n.get("attributes") or [], "formulas": n.get("formulas") or [], "origin": n.get("origin", "reference"), "change_state": n.get("change_state", "unchanged")}

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

    def overview_graph(self):
        """Full Finance Knowledge Graph overview (Central master spine + 8 sub-domains + edges + flows)."""
        fast = self.repo.overview_raw()
        if fast:
            counts_map = {}
            for r in fast.get("counts", []):
                counts_map.setdefault(r["domain"], {})[r["role"]] = r["count"]

            domains = []
            for d in fast.get("domains", []):
                slug = d.get("slug")
                c = counts_map.get(slug, {})
                domains.append({
                    "id": slug,
                    "code": d.get("code"),
                    "slug": slug,
                    "name": d.get("name"),
                    "description": d.get("description", ""),
                    "scope": d.get("scope", ""),
                    "color": d.get("color", "#2563EB"),
                    "graph_url": f"http://127.0.0.1:5000/api/graphs/{slug}.html",
                    "counts": {"core": c.get("core", 0), "shared": c.get("shared", 0), "neighbour": c.get("neighbour", 0)}
                })

            nodes = []
            for n in fast.get("nodes", []):
                k = n.get("node_key") or n.get("key") or n.get("_key")
                nodes.append({
                    "id": k,
                    "key": k,
                    "n": n.get("name", k),
                    "name": n.get("name", k),
                    "d": n.get("group_key", "general"),
                    "group_key": n.get("group_key", "general"),
                    "group_name": n.get("group_name", ""),
                    "color": n.get("color", "#2563EB"),
                    "kind": n.get("kind", "domain"),
                    "def": n.get("definition", ""),
                    "definition": n.get("definition", ""),
                    "a": n.get("attributes") or [],
                    "attributes": n.get("attributes") or [],
                    "f": n.get("formulas") or [],
                    "formulas": n.get("formulas") or [],
                    "ev": n.get("evidence", ""),
                    "evidence": n.get("evidence", ""),
                    "canon_attributes": n.get("canon_attributes") or [],
                    "canon_ref": n.get("canon_ref"),
                    "canon_title": n.get("canon_title"),
                    "canon_joins": n.get("canon_joins"),
                    "canon_evidence": n.get("canon_evidence"),
                })

            edges = []
            for e in fast.get("edges", []):
                edges.append({
                    "source": e.get("source_key") or e.get("source"),
                    "target": e.get("target_key") or e.get("target"),
                    "label": e.get("label", "relates_to"),
                    "origin_graph": e.get("origin_graph"),
                    "cross_domain": bool(e.get("cross_domain", 0)),
                })

            flows = []
            for f in fast.get("flows", []):
                fkey = f.get("flow_key") or f.get("key") or f.get("_key")
                steps = f.get("steps") or []
                flows.append({
                    "key": fkey,
                    "title": f.get("title", fkey),
                    "narrative": f.get("narrative", ""),
                    "steps": steps,
                    "ids": steps,
                    "step_count": len(steps),
                })

            return {
                "graph_url": "http://127.0.0.1:5000/api/graphs/finance-overview.html",
                "domains": domains,
                "nodes": nodes,
                "edges": edges,
                "flows": flows,
                "counts": {
                    "domains": len(domains),
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "flows": len(flows),
                },
            }

        # Fallback to multi-query approach
        domains = self.list_domains()
        nodes_raw = self.repo.all_nodes()
        nodes = []
        for n in nodes_raw:
            nodes.append({
                "id": n["node_key"],
                "key": n["node_key"],
                "n": n["name"],
                "name": n["name"],
                "d": n["group_key"],
                "group_key": n["group_key"],
                "group_name": n["group_name"],
                "color": n["color"],
                "kind": n["kind"],
                "def": n["definition"],
                "definition": n["definition"],
                "a": n["attributes"] or [],
                "attributes": n["attributes"] or [],
                "f": n["formulas"] or [],
                "formulas": n["formulas"] or [],
                "ev": n["evidence"],
                "evidence": n["evidence"],
                "canon_attributes": n.get("canon_attributes") or [],
                "canon_ref": n.get("canon_ref"),
                "canon_title": n.get("canon_title"),
                "canon_joins": n.get("canon_joins"),
                "canon_evidence": n.get("canon_evidence"),
            })

        edges_raw = self.repo.all_edges()
        edges = []
        for e in edges_raw:
            edges.append({
                "source": e["source_key"],
                "target": e["target_key"],
                "label": e["label"],
                "origin_graph": e.get("origin_graph"),
                "cross_domain": bool(e.get("cross_domain", 0)),
            })

        flows = []
        for f in self.repo.flows():
            steps = f.get("steps") or [s["node_key"] for s in self.repo.flow_steps(f["id"])]
            flows.append({
                "key": f["flow_key"],
                "title": f["title"],
                "narrative": f["narrative"],
                "steps": steps,
                "ids": steps,
                "step_count": len(steps),
            })

        return {
            "graph_url": "http://127.0.0.1:5000/api/graphs/finance-overview.html",
            "domains": domains,
            "nodes": nodes,
            "edges": edges,
            "flows": flows,
            "counts": {
                "domains": len(domains),
                "nodes": len(nodes),
                "edges": len(edges),
                "flows": len(flows),
            },
        }

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

    # ------------------------------------------------------------------ user graph resolution for flows
    def _user_graph_map(self, user_id, slug=None):
        if not user_id:
            return {}, []
        try:
            import logging
            from .workflow_service import WorkflowService
            from ..repositories.graphs import GraphRepository
            d = self.domain(slug) if slug else None
            wf = WorkflowService(self.db).get_wf(user_id, d, required=False) if d else None
            if not wf:
                wfs = self.db.query("SELECT * FROM workflows WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
                wf = wfs[0] if wfs else None
            if wf:
                latest = GraphRepository(self.db).latest(user_id, wf["id"])
                if latest:
                    u_nodes = {n["key"]: n for n in GraphRepository(self.db).nodes(latest["id"])}
                    u_edges = GraphRepository(self.db).edges(latest["id"])
                    return u_nodes, u_edges
        except Exception as e:
            logging.getLogger(__name__).warning("Could not load user graph for flows: %s", e)
        return {}, []

    # ------------------------------------------------------------------ flows
    def flows(self, slug=None, user_id=None):
        roles = {}
        if slug:
            roles = self.repo.domain_node_roles(self.domain(slug)["id"])
        user_nodes, user_edges = self._user_graph_map(user_id, slug)

        out = []
        for f in self.repo.flows():
            steps = self.repo.flow_steps(f["id"])
            keys = [s["node_key"] for s in steps]
            flow_keys_set = set(keys)
            augmented_keys = list(keys)

            # Insert any newly added domain nodes that connect to this flow
            if user_nodes and user_edges:
                added_nodes = {k: n for k, n in user_nodes.items() if n.get("change_state") == "added"}
                for ue in user_edges:
                    src, tgt = ue.get("source"), ue.get("target")
                    if src in added_nodes and tgt in flow_keys_set and src not in flow_keys_set:
                        idx = augmented_keys.index(tgt) if tgt in augmented_keys else len(augmented_keys)
                        augmented_keys.insert(idx, src)
                        flow_keys_set.add(src)
                    elif tgt in added_nodes and src in flow_keys_set and tgt not in flow_keys_set:
                        idx = augmented_keys.index(src) + 1 if src in augmented_keys else len(augmented_keys)
                        augmented_keys.insert(idx, tgt)
                        flow_keys_set.add(tgt)

            # Resolve names for each step
            all_ref_nodes = {n["node_key"]: n for n in self.repo.nodes_by_keys(augmented_keys)} if augmented_keys else {}

            step_names = []
            has_customized_step = False
            for k in augmented_keys:
                ref_n = all_ref_nodes.get(k)
                if k in user_nodes:
                    un = user_nodes[k]
                    step_names.append(un["name"])
                    is_custom = (
                        un.get("change_state") in ("modified", "added")
                        or (un.get("ref_name") and un.get("name") != un.get("ref_name"))
                        or bool(un.get("notes"))
                        or (ref_n and un.get("name") != ref_n.get("name"))
                    )
                    if is_custom:
                        has_customized_step = True
                elif ref_n:
                    step_names.append(ref_n["name"])
                else:
                    step_names.append(k)

            dynamic_narrative = " → ".join(step_names) if step_names else f["narrative"]

            out.append({
                "key": f["flow_key"],
                "title": f["title"],
                "narrative": dynamic_narrative,
                "step_count": len(augmented_keys),
                "customized": has_customized_step,
                "relevant": bool(slug) and any(roles.get(k) == "core" for k in augmented_keys),
                "touches_domain": bool(slug) and any(k in roles for k in augmented_keys)
            })
        return out

    def flow_detail(self, key, user_id=None, slug=None):
        f = self.repo.flow(key)
        if not f:
            raise NotFoundError("Unknown flow.")
        user_nodes, user_edges = self._user_graph_map(user_id, slug)
        steps = self.repo.flow_steps(f["id"])
        keys = [s["node_key"] for s in steps]
        flow_keys_set = set(keys)
        augmented_steps = list(steps)

        if user_nodes and user_edges:
            added_nodes = {k: n for k, n in user_nodes.items() if n.get("change_state") == "added"}
            for ue in user_edges:
                src, tgt = ue.get("source"), ue.get("target")
                if src in added_nodes and tgt in flow_keys_set and src not in flow_keys_set:
                    augmented_steps.append({"step_order": len(augmented_steps) + 1, "node_key": src})
                    flow_keys_set.add(src)
                elif tgt in added_nodes and src in flow_keys_set and tgt not in flow_keys_set:
                    augmented_steps.append({"step_order": len(augmented_steps) + 1, "node_key": tgt})
                    flow_keys_set.add(tgt)

        all_keys = [s["node_key"] for s in augmented_steps]
        missing_keys = [k for k in all_keys if k not in user_nodes]
        ref_nodes = {n["node_key"]: n for n in self.repo.nodes_by_keys(missing_keys)} if missing_keys else {}

        resolved_steps = []
        step_names = []
        for idx, s in enumerate(augmented_steps, 1):
            k = s["node_key"]
            if k in user_nodes:
                un = user_nodes[k]
                step_names.append(un["name"])
                resolved_steps.append({
                    "order": idx,
                    "node_key": k,
                    "name": un["name"],
                    "group_name": un["group_name"],
                    "color": un["color"],
                    "definition": un["definition"],
                    "attributes": un.get("attributes", []),
                    "evidence": un.get("evidence", ""),
                    "change_state": un.get("change_state", "unchanged"),
                    "notes": un.get("notes")
                })
            elif k in ref_nodes:
                rn = ref_nodes[k]
                step_names.append(rn["name"])
                resolved_steps.append({
                    "order": idx,
                    "node_key": k,
                    "name": rn["name"],
                    "group_name": rn["group_name"],
                    "color": rn["color"],
                    "definition": rn["definition"],
                    "attributes": rn["attributes"],
                    "evidence": rn["evidence"],
                    "change_state": "unchanged",
                    "notes": rn.get("notes")
                })

        dynamic_narrative = " → ".join(step_names) if step_names else f["narrative"]
        edges = self.repo.edges_within(all_keys)
        edge_keys = {(e["source_key"], e["target_key"]) for e in edges}
        augmented_edges = [{"source": e["source_key"], "target": e["target_key"], "label": e["label"], "origin": "reference",
                            "change_state": "unchanged", "cross_domain": bool(e["cross_domain"])} for e in edges]
        for ue in user_edges:
            s, t = ue.get("source"), ue.get("target")
            if s in flow_keys_set and t in flow_keys_set and (s, t) not in edge_keys:
                augmented_edges.append({
                    "source": s,
                    "target": t,
                    "label": ue.get("label", "connects to"),
                    "origin": ue.get("origin", "customization"),
                    "change_state": ue.get("change_state", "added"),
                    "cross_domain": bool(ue.get("cross_domain", False))
                })

        all_flow_nodes = [self._public_node(n, "flow") for n in ref_nodes.values()] + [
            self._public_node(un, "flow") for un in user_nodes.values() if un["key"] in flow_keys_set
        ]

        return {
            "key": f["flow_key"],
            "title": f["title"],
            "narrative": dynamic_narrative,
            "steps": resolved_steps,
            "nodes": all_flow_nodes,
            "edges": augmented_edges
        }
