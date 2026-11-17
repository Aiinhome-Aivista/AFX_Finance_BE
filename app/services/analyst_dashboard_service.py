"""Analyst dashboard for a published model.

The published model (workflow + finalized graph version) is resolved and authorised on the server; the browser only
supplies a workflow id, which is never trusted by itself. Metrics are computed from the full stored records by
dataset_profiler (no LLM involved); an LLM, when configured, only writes the narrative from those computed facts.
Results are cached per (workflow, published version, uploaded-file fingerprint) so a republish or new upload can never
serve stale or mixed data.
"""
import datetime as dt
import hashlib
import json
import logging
import re
from pathlib import Path

from ..errors import ForbiddenError, NotFoundError
from ..repositories.workflows import WorkflowRepository
from . import dataset_profiler as prof

log = logging.getLogger(__name__)
PROFILER_VERSION = "1"
NARRATIVE_SYSTEM = (
    "You write the executive summary for a data dashboard. Use ONLY the facts in the JSON provided. "
    "Never invent, estimate or compute any number; quote figures exactly as given. If something is not in the facts, do not mention it. "
    "Return JSON: {\"title\": string (max 90 chars), \"summary\": string (2-3 short paragraphs, plain text)}."
)


class AnalystDashboardService:
    def __init__(self, db, cfg, llm=None):
        self.db, self.cfg, self.llm = db, cfg, llm
        self.wfs = WorkflowRepository(db)

    # ------------------------------------------------------------------ access
    def _custom_published(self, wf, domain):
        """True when the owner published a named custom model for this domain (custom_finance_models, status PUBLISHED)."""
        slug = str(domain["slug"]).lower()
        try:
            row = self.db.one(
                "SELECT id FROM custom_finance_models WHERE user_id=%s AND slug IN (%s, %s) "
                "AND (status='PUBLISHED' OR published_at IS NOT NULL) LIMIT 1",
                (wf["user_id"], slug, slug.replace("-", "_")))
        except Exception:  # table absent in this deployment
            return False
        return bool(row)

    def resolve_model(self, user_id, role, workflow_id):
        """Return (workflow, domain) the caller may view. Analysts: published only. Architects: own or published.

        A model counts as published when its workflow is finalized OR the owner published a custom model for the domain.
        """
        wf = self.wfs.get_by_id(workflow_id)
        if not wf:
            raise NotFoundError("Published model not found.")
        domain = self.db.one("SELECT id, slug, code, name, description FROM domains WHERE id=%s", (wf["domain_id"],))
        published = bool(wf.get("finalized_version_number")) or self._custom_published(wf, domain)
        if role == "data_analyst":
            if not published:
                raise ForbiddenError("Data Analysts can only access published models.")
        elif int(wf["user_id"]) != int(user_id) and not published:
            raise ForbiddenError("You do not have access to another user's draft workflow.")
        owner = self.db.one("SELECT display_name FROM users WHERE id=%s", (wf["user_id"],)) or {}
        wf["owner_name"] = owner.get("display_name") or "Data Architect"
        wf["published"] = published
        return wf, domain

    # ------------------------------------------------------------------ data sources
    def _files(self, wf):
        """Processed files bound to the model's published graph version (older versions are excluded on purpose).

        Finalized workflows use their finalized version. A custom-published model has no finalized version, so the
        newest graph version that has processed data is used; files from earlier versions are still excluded.
        """
        rows = self.db.query(
            "SELECT id, user_id, original_name, stored_name, ext, size_bytes, sha256, graph_version_number "
            "FROM uploaded_files WHERE workflow_id=%s AND status='processed' ORDER BY id", (wf["id"],))
        ver = wf.get("finalized_version_number") or max((int(r["graph_version_number"]) for r in rows), default=0)
        wf["data_version"] = ver
        use = [r for r in rows if int(r["graph_version_number"]) == int(ver)]
        return use, len(rows) - len(use)

    def _path(self, f):
        root = Path(self.cfg.UPLOAD_DIR).resolve()
        p = (root / str(f["user_id"]) / f["stored_name"]).resolve()
        if root not in p.parents:  # never read outside the upload dir
            raise ForbiddenError("Invalid file location.")
        return p

    @staticmethod
    def _fingerprint(wf, files):
        h = hashlib.sha256(f"{PROFILER_VERSION}|{wf['id']}|{wf.get('data_version', wf.get('finalized_version_number'))}".encode())
        for f in sorted(files, key=lambda x: x["id"]):
            h.update(f"|{f['id']}:{f['sha256']}".encode())
        return h.hexdigest()[:16]

    def _cache_file(self, wf, fp):
        d = Path(self.cfg.UPLOAD_DIR) / "_dashboard_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"wf{int(wf['id'])}_{fp}.json", d

    # ------------------------------------------------------------------ main
    def dashboard(self, user_id, role, workflow_id, refresh=False):
        wf, domain = self.resolve_model(user_id, role, workflow_id)
        files, excluded = self._files(wf)
        ver = wf["data_version"]
        model = {
            "workflow_id": wf["id"], "slug": domain["slug"], "name": domain["name"], "code": domain.get("code"),
            "version": f"KG{ver}" if ver else None,
            "published": bool(wf["published"]),
            "published_at": wf["finalized_at"].isoformat() if wf.get("finalized_at") else None,
            "owner_name": wf["owner_name"],
        }
        base = {"model": model, "excluded_files": excluded}
        if not files:
            msg = ("The Architect has not uploaded any processed data for this published version yet."
                   if not excluded else
                   "Data exists only for an earlier graph version. Upload data again for the currently published version.")
            return {**base, "status": "no_data", "message": msg, "summary": None, "kpis": [], "charts": [], "tables": [],
                    "data_sources": [], "warnings": [], "date_range": None, "cached": False, "generated_at": None}

        fp = self._fingerprint(wf, files)
        cache_path, cache_dir = self._cache_file(wf, fp)
        if cache_path.exists() and not (refresh and int(wf["user_id"]) == int(user_id)):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("status") == "ready":  # a failed/empty result is never trusted: the files may have appeared since
                    return {**base, **cached, "cached": True}
            except (OSError, ValueError):
                log.warning("dashboard cache unreadable for wf %s; recomputing", wf["id"])

        result = self._compute(model, files)
        payload = {k: result[k] for k in ("status", "message", "summary", "kpis", "charts", "tables", "data_sources", "warnings",
                                         "date_range", "generated_at")}
        try:
            for old in cache_dir.glob(f"wf{int(wf['id'])}_*.json"):  # drop stale versions of this model's cache
                old.unlink(missing_ok=True)
            if payload["status"] == "ready":
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.warning("could not write dashboard cache for wf %s", wf["id"])
        return {**base, **payload, "cached": False}

    # ------------------------------------------------------------------ compute
    def _compute(self, model, files):
        tables, sources, warnings, missing = [], [], [], []
        for f in files:
            src = {"id": f["id"], "name": f["original_name"], "ext": f["ext"], "tabular": f["ext"] in prof.TABULAR_EXT, "rows": None}
            if src["tabular"] and not self._path(f).exists():
                src["missing"] = True
                missing.append(f["original_name"])
                warnings.append(f"'{f['original_name']}' is registered for this model but its data file is not available on this server "
                                "(it may have been uploaded through a different server, or removed). Re-upload it.")
            elif src["tabular"]:
                try:
                    for tname, df in prof.load_tables(self._path(f), f["ext"], f["original_name"]):
                        if len(df.columns) == 0 or len(df) == 0:
                            continue
                        summary, cols = prof.profile_table(tname, f["id"], df)
                        tables.append((summary, cols))
                        src["rows"] = (src["rows"] or 0) + summary["rows"]
                        if summary["row_cap_applied"]:
                            warnings.append(f"'{tname}' was capped at {prof.ROW_CAP:,} rows for analysis.")
                except Exception as e:  # one bad file must not blank the whole dashboard
                    log.exception("dashboard: could not read file %s", f["id"])
                    warnings.append(f"'{f['original_name']}' could not be analysed ({type(e).__name__}).")
            else:
                src["document"] = True  # informational only: documents are expected and are not a problem
            sources.append(src)
        built = prof.build_dashboard(tables)
        if not tables:
            msg = ("The data file(s) uploaded for this model are not available on this server, so no metrics can be built. "
                   "Ask the Data Architect to upload them again." if missing else
                   "None of the uploaded files contain tabular data (CSV or Excel) to build metrics from.")
            return {"status": "no_data", "message": msg,
                    "summary": None, "kpis": [], "charts": [], "tables": [], "data_sources": sources, "warnings": warnings,
                    "date_range": None, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        return {"status": "ready", "message": None, "summary": self._summary(model, built), "kpis": built["kpis"],
                "charts": built["charts"], "tables": built["tables"], "data_sources": sources, "warnings": warnings,
                "date_range": built["date_range"], "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    # ------------------------------------------------------------------ narrative
    def _summary(self, model, built):
        rules = self._rule_summary(model, built)
        if not self.llm:
            return rules
        facts = {"model": model["name"], **built["facts"]}
        try:
            out = self.llm.complete_json(NARRATIVE_SYSTEM, json.dumps(facts, default=str), max_tokens=700)
            title, text = str(out.get("title", "")).strip(), str(out.get("summary", "")).strip()
            if title and text and self._numbers_supported(text + " " + title, facts):
                return {"title": title[:120], "text": text, "source": "llm"}
            log.info("dashboard narrative rejected (empty or unsupported numbers); using rule-based summary")
        except Exception as e:  # LLMError or anything else: narrative is optional; never fail the dashboard over it
            log.info("dashboard narrative unavailable: %s", e)
        return rules

    @staticmethod
    def _numbers_supported(text, facts):
        """Reject a narrative that quotes a number not present in the computed facts."""
        haystack = re.sub(r"[,\s]", "", json.dumps(facts, default=str))
        for num in re.findall(r"\d[\d,]*\.?\d*", text):
            n = num.replace(",", "").rstrip(".")
            if len(n) >= 2 and n not in haystack:
                return False
        return True

    @staticmethod
    def _rule_summary(model, built):
        t = built["tables"]
        rows = sum(x["rows"] for x in t)
        parts = [f"This dashboard analyses {len(t)} table{'s' if len(t) != 1 else ''} with {rows:,} records uploaded for the published "
                 f"{model['name']} model ({model['version']})."]
        if built["date_range"]:
            parts.append(f"The data covers {built['date_range']['from']} to {built['date_range']['to']}.")
        sums = [x for x in built["kpis"] if x["id"].startswith("sum_")]
        if sums:
            parts.append("Key totals: " + "; ".join(f"{x['label']} {x['display']}" for x in sums) + ".")
        top = next((x for x in built["kpis"] if x["id"].startswith("top_")), None)
        if top:
            parts.append(f"{top['label']}: {top['display']}.")
        return {"title": f"{model['name']} — Data Analytics Report", "text": " ".join(parts), "source": "rules"}
