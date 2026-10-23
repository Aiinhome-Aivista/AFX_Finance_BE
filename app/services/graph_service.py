"""Client graph versions (KG1, KG2, ...): creation (transactional), retrieval, comparison, finalization."""
from ..errors import ConflictError, NotFoundError, ValidationError
from ..repositories.graphs import GraphRepository
from ..repositories.proposals import ProposalRepository
from .graph_ops import GraphState, apply_op, diff_graphs, validate_graph, validate_sequence
from .reference_service import ReferenceService
from .workflow_service import WorkflowService


def serialize_state(state: GraphState, version=None, domain=None):
    nodes = [{k: n.get(k) for k in ("key", "name", "ref_name", "group_key", "group_name", "color", "kind", "role", "definition",
                                    "attributes", "formulas", "evidence", "notes", "origin", "change_state", "added_in_version")}
             for n in state.node_list()]
    edges = [{k: e.get(k) for k in ("source", "target", "label", "origin", "change_state", "added_in_version", "cross_domain")}
             for e in state.edge_list()]
    out = {"nodes": nodes, "edges": edges}
    if version is not None:
        out["version"] = version
    if domain is not None:
        out["domain"] = domain
    return out


class GraphService:
    def __init__(self, db):
        self.db = db
        self.graphs = GraphRepository(db)
        self.proposals = ProposalRepository(db)
        self.ref = ReferenceService(db)
        self.wfs = WorkflowService(db)

    def _baseline(self, domain, wf):
        if not wf.get("selected_blocks"):
            default_b = [b["id"] for b in self.ref.blocks(domain["slug"]) if b.get("default")] or ["core", "shared"]
            try:
                self.wfs.confirm_blocks(wf["user_id"], domain, default_b)
            except Exception:
                pass
            wf["selected_blocks"] = default_b
        return self.ref.baseline(domain["slug"], wf["selected_blocks"])

    def working(self, user_id, wf, domain):
        """(version_number, GraphState): the latest client version, or the KG0 baseline when none exists yet."""
        latest = self.graphs.latest(user_id, wf["id"])
        if not latest:
            return 0, self._baseline(domain, wf)
        return latest["version_number"], GraphState.from_lists(self.graphs.nodes(latest["id"]), self.graphs.edges(latest["id"]))

    def state_of(self, user_id, wf, domain, number):
        if number == 0:
            return self._baseline(domain, wf)
        v = self.graphs.version(user_id, wf["id"], number)
        if not v:
            raise NotFoundError(f"Version KG{number} does not exist.")
        return GraphState.from_lists(self.graphs.nodes(v["id"]), self.graphs.edges(v["id"]))

    def version_meta(self, v):
        return {"number": v["version_number"], "label": v["label"], "kind": v["kind"], "status": v["status"],
                "summary": v["summary"], "created_at": v["created_at"], "parent": v["parent_version_number"],
                "proposal_id": v["proposal_id"], "node_count": v.get("node_count"), "edge_count": v.get("edge_count")}

    def versions(self, user_id, wf):
        return [self.version_meta(v) for v in self.graphs.versions(user_id, wf["id"])]

    def graph(self, user_id, wf, domain, number):
        state = self.state_of(user_id, wf, domain, number)
        if number == 0:
            meta = {"number": 0, "label": "KG0 (selected blocks)", "kind": "reference", "status": "reference", "readonly": True}
        else:
            meta = self.version_meta(self.graphs.version(user_id, wf["id"], number))
        return serialize_state(state, meta, {"slug": domain["slug"], "name": domain["name"], "color": domain["color"]})

    def compare(self, user_id, wf, domain, a, b):
        sa, sb = self.state_of(user_id, wf, domain, a), self.state_of(user_id, wf, domain, b)
        d = diff_graphs(sa, sb)
        d["from"], d["to"] = {"number": a, "label": f"KG{a}"}, {"number": b, "label": f"KG{b}"}
        return d

    # ------------------------------------------------------------------ create (atomic)
    def create_version(self, user_id, wf, domain, items, kind, proposal_id, summary, origin):
        """Validate `items` against the CURRENT graph and persist the next version atomically.
        Raises ConflictError (and rolls back everything) if any item no longer validates."""
        with self.db.transaction():
            self.db.one("SELECT id FROM workflows WHERE id=%s AND user_id=%s FOR UPDATE", (wf["id"], user_id))
            latest = self.graphs.latest(user_id, wf["id"])
            number = (latest["version_number"] if latest else 0) + 1
            base_number, base = self.working(user_id, wf, domain)
            base.reset_change_states()
            raw = [{"op": i["op"], "payload": i["payload"], "rationale": i.get("rationale")} for i in items]
            accepted, rejected, _ = validate_sequence(base, raw, number, origin)
            if rejected:
                raise ConflictError("Some approved changes can no longer be applied (a change they depend on may have been rejected).",
                                    details=[{"op": r["op"], "errors": r["errors"]} for r in rejected])
            state = base
            for a in accepted:
                apply_op(state, a["op"], a["payload"], number, origin)
            vid = self.graphs.create_version(user_id, wf["id"], number, kind, base_number, proposal_id, summary)
            self.graphs.insert_nodes(vid, state.node_list())
            self.graphs.insert_edges(vid, state.edge_list())
            self.wfs.on_version_created(wf, kind, latest is None)
        return number

    # ------------------------------------------------------------------ finalization
    def finalization_preview(self, user_id, wf, domain):
        latest = self.graphs.latest(user_id, wf["id"])
        if not latest:
            raise ConflictError("There is no customised graph yet. Collect your nuances and approve changes first (step 4).")
        number, state = self.working(user_id, wf, domain)
        validation = validate_graph(state, self.ref.core_keys(domain["slug"]))
        base = self._baseline(domain, wf)
        history = []
        for p in self.proposals.list(user_id, wf["id"]):
            history.append({"id": p["id"], "title": p["title"], "source": p["source"], "status": p["status"], "created_at": p["created_at"],
                            "decided_at": p["decided_at"], "result_version": p["result_version_number"],
                            "items": [{"op": i["op"], "description": i["description"], "status": i["status"]} for i in self.proposals.items(p["id"])]})
        return {"version": self.version_meta(latest), "graph": serialize_state(state), "validation": validation,
                "comparison": diff_graphs(base, state), "versions": self.versions(user_id, wf), "history": history,
                "finalized_version_number": wf["finalized_version_number"]}

    def finalize(self, user_id, wf, domain, confirm, acknowledge_warnings):
        if wf["finalized_version_number"]:
            raise ConflictError("The ontology is already finalised.")
        if not confirm:
            raise ValidationError("Finalisation needs your explicit confirmation.")
        prev = self.finalization_preview(user_id, wf, domain)
        if prev["validation"]["errors"]:
            raise ConflictError("Fix the validation errors before finalising.", details=prev["validation"]["errors"])
        if prev["validation"]["warnings"] and not acknowledge_warnings:
            raise ConflictError("There are validation warnings. Acknowledge them to finalise anyway.", code="warnings_unacknowledged",
                                details=prev["validation"]["warnings"])
        number = prev["version"]["number"]
        with self.db.transaction():
            v = self.graphs.version(user_id, wf["id"], number)
            self.graphs.mark_status(v["id"], "finalized")
            self.wfs.on_finalized(wf, number)
        return number

    def reopen(self, user_id, wf, domain):
        if not wf["finalized_version_number"]:
            raise ConflictError("The ontology is not finalised.")
        count = self.db.one("SELECT COUNT(*) c FROM uploaded_files WHERE workflow_id=%s", (wf["id"],))["c"]
        tele = self.db.one("SELECT COUNT(*) c FROM user_graph_versions WHERE workflow_id=%s AND kind='telemetry'", (wf["id"],))["c"]
        if count or tele:
            raise ConflictError("Telemetry has already been uploaded for this ontology, so it can no longer be re-opened.")
        with self.db.transaction():
            self.graphs.clear_finalized(wf["id"])
            self.wfs.on_reopened(wf)
