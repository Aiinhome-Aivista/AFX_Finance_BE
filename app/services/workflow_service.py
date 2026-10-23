"""Server-enforced 6-step business workflow (see the workflow diagram image1.jpg)."""
from ..errors import ConflictError, ValidationError, WorkflowError
from ..repositories.users import UserRepository
from ..repositories.workflows import WorkflowRepository

STEP_TITLES = {
    1: ("Industry sector", "Choose Finance"),
    2: ("Domain", "Pick a specific Finance domain"),
    3: ("Reference graph (KG0)", "Explore the domain template and choose the blocks that matter"),
    4: ("Client nuances (KG1)", "Chatbot collects your specifics and business experience"),
    5: ("Finalise ontology", "Review, compare and finalise your customised graph"),
    6: ("Telemetry (KG2)", "Upload CSV / XLSX / PDF / TXT / DOCX / SQL and generate the data-mapped graph"),
}
APPS = {"finance": {"key": "finance", "name": "Finance", "description": "Finance & EPM knowledge graphs — built from scratch for FMCG finance functions."}}


class WorkflowService:
    def __init__(self, db):
        self.db = db
        self.users = UserRepository(db)
        self.repo = WorkflowRepository(db)

    # ---- step 1
    def apps(self):
        return list(APPS.values())

    def select_app(self, user_id, app):
        if app not in APPS:
            raise ValidationError("Unknown application. Only 'finance' is available.")
        self.users.set_selected_app(user_id, app)
        return {"selected_app": app}

    # ---- step 2
    def start(self, user_id, domain):
        u = self.users.by_id(user_id)
        if u and u.get("selected_app") != "finance":
            self.users.set_selected_app(user_id, "finance")
        wf = self.repo.get(user_id, domain["id"])
        if not wf:
            self.repo.create(user_id, domain["id"])
        return self.state(user_id, domain)

    def get_wf(self, user_id, domain, required=True):
        wf = self.repo.get(user_id, domain["id"])
        if not wf and required:
            raise WorkflowError("Select this domain first (step 2).")
        return wf

    def require_completed(self, user_id, domain, step_no, message=None):
        wf = self.get_wf(user_id, domain)
        steps = {s["step_no"]: s["status"] for s in self.repo.steps(wf["id"])}
        if steps.get(step_no) != "completed":
            raise WorkflowError(message or f"Complete step {step_no} ({STEP_TITLES[step_no][0]}) first.")
        return wf

    # ---- transitions
    def confirm_blocks(self, user_id, domain, blocks):
        wf = self.get_wf(user_id, domain)
        if wf["finalized_version_number"]:
            raise ConflictError("The ontology is already finalised. Re-open it to change the focus blocks.")
        self.repo.set_blocks(wf["id"], blocks)
        steps = {s["step_no"]: s["status"] for s in self.repo.steps(wf["id"])}
        if steps[3] != "completed":
            self.repo.set_step_status(wf["id"], 3, "completed")
            if steps[4] == "locked":
                self.repo.set_step_status(wf["id"], 4, "active")
            self.repo.set_current_step(wf["id"], 4)
        return wf

    def on_version_created(self, wf, kind, is_first):
        steps = {s["step_no"]: s["status"] for s in self.repo.steps(wf["id"])}
        if kind == "customization":
            if steps[4] != "completed":
                self.repo.set_step_status(wf["id"], 4, "completed")
                if steps[5] == "locked":
                    self.repo.set_step_status(wf["id"], 5, "active")
                self.repo.set_current_step(wf["id"], 5)
        else:
            self.repo.set_step_status(wf["id"], 6, "completed")
            self.repo.set_current_step(wf["id"], 6)

    def on_finalized(self, wf, version_number):
        self.repo.set_finalized(wf["id"], version_number)
        self.repo.set_step_status(wf["id"], 5, "completed")
        steps = {s["step_no"]: s["status"] for s in self.repo.steps(wf["id"])}
        if steps[6] == "locked":
            self.repo.set_step_status(wf["id"], 6, "active")
        self.repo.set_current_step(wf["id"], 6)

    def on_reopened(self, wf):
        self.repo.set_finalized(wf["id"], None)
        self.repo.set_step_status(wf["id"], 5, "active")
        self.repo.set_step_status(wf["id"], 6, "locked")
        self.repo.set_current_step(wf["id"], 5)

    # ---- read
    def state(self, user_id, domain):
        wf = self.repo.get(user_id, domain["id"])
        u = self.users.by_id(user_id)
        if not wf:
            steps = [{"no": n, "title": t[0], "hint": t[1],
                      "status": "completed" if n == 1 and u["selected_app"] == "finance" else ("active" if n == (2 if u["selected_app"] == "finance" else 1) else "locked")}
                     for n, t in STEP_TITLES.items()]
            return {"started": False, "current_step": 2, "steps": steps, "selected_blocks": None, "finalized_version_number": None}
        steps = [{"no": s["step_no"], "title": STEP_TITLES[s["step_no"]][0], "hint": STEP_TITLES[s["step_no"]][1],
                  "status": s["status"], "completed_at": s["completed_at"]} for s in self.repo.steps(wf["id"])]
        return {"started": True, "workflow_id": wf["id"], "current_step": wf["current_step"], "steps": steps,
                "selected_blocks": wf["selected_blocks"], "finalized_version_number": wf["finalized_version_number"],
                "finalized_at": wf["finalized_at"]}

    def overview(self, user_id):
        out = {}
        for wf in self.repo.list_for_user(user_id):
            steps = self.repo.steps(wf["id"])
            out[wf["domain_slug"]] = {"current_step": wf["current_step"], "completed": sum(1 for s in steps if s["status"] == "completed"),
                                      "finalized": bool(wf["finalized_version_number"])}
        return out
