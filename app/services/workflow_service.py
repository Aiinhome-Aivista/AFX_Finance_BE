"""Server-enforced 6-step business workflow (see the workflow diagram image1.jpg)."""
from ..errors import ConflictError, ValidationError, WorkflowError
from ..repositories.users import UserRepository
from ..repositories.workflows import WorkflowRepository

STEP_TITLES = {
    1: ("Industry sector", "Choose Finance"),
    2: ("Domain", "Pick a specific Finance domain"),
    3: ("Reference graph", "Explore the domain template and choose the blocks that matter"),
    4: ("Domain nuances", "Chatbot collects your specifics and business experience"),
    5: ("Finalise ontology", "Review, compare and finalise your customised graph"),
    6: ("Business telemetry", "Upload CSV / XLSX / PDF / TXT / DOCX / SQL and generate the data-mapped graph"),
}
APPS = {
    "finance": {
        "key": "finance",
        "name": "Finance",
        "description": "Finance & EPM knowledge graphs — built from scratch for enterprise finance functions.",
        "route": "/finance",
        "active": True,
    },
    "fmcg": {
        "key": "fmcg",
        "name": "FMCG",
        "description": "Fast-moving consumer goods supply-chain, trade promotions, route-to-market, and retail execution models.",
        "route": "/fmcg",
        "active": True,
    },
    "others": {
        "key": "others",
        "name": "Others",
        "description": "Additional enterprise domains (Sales, Operations, HR) — currently disabled.",
        "route": "/others",
        "active": False,
    },
}
# Alias for legacy supply_chain references
APPS["supply_chain"] = APPS["fmcg"]


class WorkflowService:
    def __init__(self, db):
        self.db = db
        self.users = UserRepository(db)
        self.repo = WorkflowRepository(db)

    # ---- step 1: active catalog & subscriptions (Finance, FMCG, and Others disabled)
    def apps(self, user_id=None):
        unique_apps = [APPS["finance"], APPS["fmcg"], APPS["others"]]
        if user_id is None:
            return unique_apps
        u = self.users.by_id(user_id)
        sub_keys = self.users.list_subscriptions(user_id)
        sub_set = set(sub_keys)
        # Normalize legacy 'supply_chain' to 'fmcg'
        if "supply_chain" in sub_set:
            sub_set.add("fmcg")
        available = [a for a in unique_apps if a["key"] not in sub_set and (a["key"] != "fmcg" or "supply_chain" not in sub_set)]
        subscribed = []
        for k in sub_keys:
            app_def = APPS.get(k)
            if app_def and app_def not in subscribed:
                subscribed.append(app_def)
        return {
            "apps": unique_apps,
            "available_apps": available,
            "subscribed_apps": subscribed,
            "selected_app": u.get("selected_app") if u else None,
        }

    def subscribe_app(self, user_id, app_key):
        norm_key = "fmcg" if app_key == "supply_chain" else app_key
        if norm_key not in APPS:
            raise ValidationError("Unknown domain model.")
        if not APPS[norm_key]["active"]:
            raise ValidationError(f"The '{APPS[norm_key]['name']}' option is disabled.")
        self.users.subscribe_app(user_id, norm_key)
        return self.apps(user_id)

    def unsubscribe_app(self, user_id, app_key):
        norm_key = "fmcg" if app_key == "supply_chain" else app_key
        if norm_key not in APPS:
            raise ValidationError("Unknown domain model.")
        self.users.unsubscribe_app(user_id, norm_key)
        if norm_key == "fmcg":
            self.users.unsubscribe_app(user_id, "supply_chain")
        u = self.users.by_id(user_id)
        if u and u.get("selected_app") in (norm_key, app_key):
            self.users.set_selected_app(user_id, None)
        return self.apps(user_id)

    def select_app(self, user_id, app):
        norm_key = "fmcg" if app == "supply_chain" else app
        if norm_key not in APPS:
            raise ValidationError("Unknown domain model.")
        if not APPS[norm_key]["active"]:
            raise ValidationError(f"The '{APPS[norm_key]['name']}' option is disabled.")
        self.users.subscribe_app(user_id, norm_key)
        self.users.set_selected_app(user_id, norm_key)
        return {"selected_app": norm_key, **self.apps(user_id)}

    def workspace_models(self, user_id, role="data_architect"):
        published = [
            {
                "workflow_id": r["id"],
                "domain_id": r["domain_id"],
                "slug": r["domain_slug"],
                "code": r["domain_code"],
                "name": r["domain_name"],
                "color": r["domain_color"],
                "description": r["domain_description"],
                "finalized_version_number": r["finalized_version_number"],
                "finalized_at": r.get("finalized_at").isoformat() if r.get("finalized_at") else None,
                "owner_id": r["user_id"],
                "owner_name": r.get("owner_name") or "Data Architect",
            }
            for r in self.repo.list_published()
        ]
        if role == "data_analyst":
            return {
                "role": role,
                "published_models": published,
            }

        my_models = []
        for w in self.repo.list_for_user(user_id):
            steps = self.repo.steps(w["id"])
            my_models.append({
                "workflow_id": w["id"],
                "domain_id": w["domain_id"],
                "slug": w["domain_slug"],
                "code": w.get("domain_code"),
                "name": w.get("domain_name"),
                "color": w.get("domain_color"),
                "description": w.get("domain_description"),
                "current_step": w["current_step"],
                "completed_steps": sum(1 for s in steps if s["status"] == "completed"),
                "latest_version": w.get("latest_version") or 0,
                "finalized": bool(w["finalized_version_number"]),
                "finalized_version_number": w["finalized_version_number"],
                "updated_at": w.get("updated_at").isoformat() if w.get("updated_at") else None,
            })

        return {
            "role": role,
            "my_models": my_models,
            "published_models": published,
        }

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
        dom_meta = {"id": domain["id"], "slug": domain["slug"], "name": domain["name"]}
        if not wf:
            steps = [{"no": n, "title": t[0], "hint": t[1],
                      "status": "completed" if n == 1 and u["selected_app"] == "finance" else ("active" if n == (2 if u["selected_app"] == "finance" else 1) else "locked")}
                     for n, t in STEP_TITLES.items()]
            return {"started": False, "current_step": 2, "steps": steps, "selected_blocks": None, "finalized_version_number": None, "domain": dom_meta}
        steps = [{"no": s["step_no"], "title": STEP_TITLES[s["step_no"]][0], "hint": STEP_TITLES[s["step_no"]][1],
                  "status": s["status"], "completed_at": s["completed_at"]} for s in self.repo.steps(wf["id"])]
        return {"started": True, "workflow_id": wf["id"], "current_step": wf["current_step"], "steps": steps,
                "selected_blocks": wf["selected_blocks"], "finalized_version_number": wf["finalized_version_number"],
                "finalized_at": wf["finalized_at"], "domain": dom_meta}

    def overview(self, user_id):
        out = {}
        for wf in self.repo.list_for_user(user_id):
            steps = self.repo.steps(wf["id"])
            out[wf["domain_slug"]] = {"current_step": wf["current_step"], "completed": sum(1 for s in steps if s["status"] == "completed"),
                                      "finalized": bool(wf["finalized_version_number"])}
        return out
