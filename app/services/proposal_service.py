"""Change proposals: validated bundles of graph operations that a user reviews, approves or rejects."""
from ..errors import ConflictError, NotFoundError, ValidationError
from ..repositories.proposals import ProposalRepository
from .graph_ops import validate_sequence
from .graph_service import GraphService


class ProposalService:
    def __init__(self, db):
        self.db = db
        self.repo = ProposalRepository(db)
        self.graphs = GraphService(db)

    # ------------------------------------------------------------------ create
    def create(self, user_id, wf, domain, raw_ops, title, summary, source, llm_used=False):
        """Validate raw ops against the current working graph; persist the valid ones as a pending proposal.
        Returns (proposal_dict | None, rejected_list)."""
        number, state = self.graphs.working(user_id, wf, domain)
        origin = "telemetry" if source == "telemetry" else "user"
        accepted, rejected, _ = validate_sequence(state, raw_ops, number + 1, origin)
        if not accepted:
            return None, rejected
        with self.db.transaction():
            self.repo.supersede_pending(user_id, wf["id"], source=source)
            pid = self.repo.create(user_id, wf["id"], number, source, title[:200], summary, llm_used)
            for i, a in enumerate(accepted, start=1):
                self.repo.add_item(pid, i, a["op"], a["payload"], a["description"], a.get("rationale"), a["warnings"])
        return self.detail(user_id, wf, pid), rejected

    # ------------------------------------------------------------------ read
    def _public(self, p, items):
        return {"id": p["id"], "title": p["title"], "summary": p["summary"], "source": p["source"], "status": p["status"],
                "base_version": p["base_version_number"], "result_version": p["result_version_number"],
                "llm_used": bool(p["llm_used"]), "created_at": p["created_at"], "decided_at": p["decided_at"],
                "items": [{"id": i["id"], "seq": i["seq"], "op": i["op"], "description": i["description"],
                           "rationale": i["rationale"], "warnings": i["warnings"], "status": i["status"], "payload": i["payload"]}
                          for i in items]}

    def detail(self, user_id, wf, proposal_id):
        p = self.repo.get(user_id, proposal_id)
        if not p or p["workflow_id"] != wf["id"]:
            raise NotFoundError("Proposal not found.")
        return self._public(p, self.repo.items(proposal_id))

    def list(self, user_id, wf):
        return [self._public(p, self.repo.items(p["id"])) for p in self.repo.list(user_id, wf["id"])]

    # ------------------------------------------------------------------ decide
    def decide(self, user_id, wf, domain, proposal_id, mode, approved_ids=None):
        p = self.repo.get(user_id, proposal_id)
        if not p or p["workflow_id"] != wf["id"]:
            raise NotFoundError("Proposal not found.")
        if p["status"] != "pending":
            raise ConflictError(f"This proposal was already {p['status']}.")
        items = self.repo.items(proposal_id)
        if mode not in ("approve_all", "reject_all", "selected"):
            raise ValidationError("mode must be approve_all, reject_all or selected.")
        if mode == "approve_all":
            chosen = [i["id"] for i in items]
        elif mode == "selected":
            chosen = list(approved_ids or [])
            unknown = set(chosen) - {i["id"] for i in items}
            if unknown:
                raise ValidationError("Some selected change ids do not belong to this proposal.")
        else:
            chosen = []

        if not chosen:
            with self.db.transaction():
                for i in items:
                    self.repo.set_item_status(i["id"], "rejected")
                self.repo.decide(proposal_id, "rejected")
            return {"proposal": self.detail(user_id, wf, proposal_id), "version": None}

        if p["source"] == "chat" and wf["finalized_version_number"]:
            raise ConflictError("The ontology is finalised. Re-open it to approve further customisation.")
        if p["source"] == "telemetry" and not wf["finalized_version_number"]:
            raise ConflictError("Telemetry changes can only be approved after the ontology is finalised.")
        latest = self.graphs.graphs.latest(user_id, wf["id"])
        current = latest["version_number"] if latest else 0
        if current != p["base_version_number"]:
            with self.db.transaction():
                self.repo.decide(proposal_id, "superseded")
            raise ConflictError("The graph changed since this proposal was made, so it has been superseded. Please generate a fresh proposal.",
                                code="proposal_stale")

        approved_items = [i for i in items if i["id"] in set(chosen)]
        kind = "telemetry" if p["source"] == "telemetry" else "customization"
        origin = "telemetry" if kind == "telemetry" else "user"
        with self.db.transaction():
            number = self.graphs.create_version(user_id, wf, domain, approved_items, kind, proposal_id,
                                                f"{len(approved_items)} approved change(s): {p['title']}", origin)
            for i in items:
                self.repo.set_item_status(i["id"], "approved" if i["id"] in set(chosen) else "rejected")
            self.repo.decide(proposal_id, "approved", number)
        return {"proposal": self.detail(user_id, wf, proposal_id), "version": {"number": number, "label": f"KG{number}"}}
