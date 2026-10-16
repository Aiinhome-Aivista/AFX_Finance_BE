"""Map extracted telemetry (columns / SQL identifiers / document text) onto the FINALISED client graph, and turn the
mapping into a change proposal that — once approved — becomes the next graph version (KG2 in the standard path)."""
import re

from ..errors import ConflictError, ValidationError
from ..repositories.files import FileRepository
from .graph_ops import norm
from .graph_service import GraphService
from .proposal_service import ProposalService
from .workflow_service import WorkflowService


def _clean_attr(a):
    return re.sub(r"\s*\(.*?\)|\[\]|\s*verbatim", "", a).strip()


def _tokens(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return {t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if len(t) > 1}


def map_terms(state, terms, text=None, extra_attributes=None):
    """Return {matches, unmatched, mentions, stats}. Pure function: deterministic, explainable, no guessing beyond name similarity."""
    index = []  # (node_key, attribute|None, norm, tokens)
    for k, n in state.nodes.items():
        for variant in {n["name"], n["name"].split(" / ")[0], re.sub(r"\s*\(.*?\)", "", n["name"])}:
            if norm(variant):
                index.append((k, None, norm(variant), _tokens(variant)))
        for a in n["attributes"]:
            c = _clean_attr(a)
            if len(norm(c)) >= 2:
                index.append((k, c, norm(c), _tokens(c)))
        for a in (extra_attributes or {}).get(k, []):           # canon attributes (FMCG_ONTOLOGY.md) not in the graph view
            c = _clean_attr(a)
            if len(norm(c)) >= 2 and norm(c) not in {norm(_clean_attr(x)) for x in n["attributes"]}:
                index.append((k, c, norm(c), _tokens(c)))
    matches, unmatched = [], []
    for term in terms:
        nt, tt = norm(term), _tokens(term)
        if not nt:
            continue
        best = {}
        for k, attr, na, ta in index:
            conf, kind = 0.0, None
            if nt == na:
                conf, kind = (1.0, "exact") if attr else (0.9, "entity-name")
            elif min(len(nt), len(na)) >= 5 and (nt in na or na in nt):
                conf, kind = 0.7, "contains"
            elif tt and ta:
                j = len(tt & ta) / len(tt | ta)
                if j >= 0.6:
                    conf, kind = round(0.5 + 0.4 * j, 2), "token-overlap"
            if conf >= 0.5 and conf > best.get(k, (0,))[0]:
                best[k] = (conf, kind, attr)
        if best:
            for k, (conf, kind, attr) in sorted(best.items(), key=lambda kv: -kv[1][0])[:2]:
                matches.append({"term": term, "node_key": k, "node_name": state.nodes[k]["name"], "attribute": attr,
                                "confidence": conf, "match_type": kind})
        else:
            unmatched.append(term)
    mentions = []
    if text:
        low = text.lower()
        for k, n in state.nodes.items():
            c = max((v for v in {n["name"].split(" / ")[0], re.sub(r"\s*\(.*?\)", "", n["name"])} if len(v) >= 5), key=len, default=None)
            if c:
                cnt = len(re.findall(r"(?<![a-z0-9])" + re.escape(c.lower()) + r"(?![a-z0-9])", low))
                if cnt:
                    mentions.append({"node_key": k, "node_name": n["name"], "count": cnt})
        mentions.sort(key=lambda m: -m["count"])
    total = len(terms)
    return {"matches": matches, "unmatched": unmatched[:200], "mentions": mentions[:40],
            "stats": {"terms": total, "matched_terms": len({m["term"] for m in matches}),
                      "entities_touched": len({m["node_key"] for m in matches} | {m["node_key"] for m in mentions})}}


class TelemetryService:
    def __init__(self, db):
        self.db = db
        self.files = FileRepository(db)
        self.graphs = GraphService(db)
        self.props = ProposalService(db)
        self.wfs = WorkflowService(db)

    def generate_proposal(self, user_id, domain):
        wf = self.wfs.require_completed(user_id, domain, 5, "Finalise the ontology (step 5) before generating the telemetry graph.")
        done = [f for f in self.files.list(user_id, wf["id"]) if f["status"] == "processed"]
        if not done:
            raise ValidationError("Upload and successfully process at least one file first.")
        number, state = self.graphs.working(user_id, wf, domain)
        raw, used, unmapped = [], [], []
        existing_sources = {n["name"] for n in state.nodes.values() if n["kind"] == "datasource"}
        for f in done:
            job = self.files.job_for_file(f["id"])
            mapping = (job or {}).get("result", {}) or {}
            mapping = mapping.get("mapping") or {}
            by_node = {}
            for m in mapping.get("matches", []):
                if m["node_key"] in state.nodes and m["confidence"] >= 0.6:
                    by_node.setdefault(m["node_key"], []).append(m)
            mention_nodes = {m["node_key"]: m["count"] for m in mapping.get("mentions", []) if m["node_key"] in state.nodes}
            if not by_node and not mention_nodes:
                unmapped.append(f["original_name"])
                continue
            name = f"Data: {f['original_name']}"
            if name in existing_sources:
                continue  # already represented in the graph
            meta = f["metadata"] or {}
            fields = sorted({m["term"] for ms in by_node.values() for m in ms})[:40]
            key = f"data-file-{f['id']}"
            raw.append({"op": "add_node", "payload": {"node_key": key, "name": name, "kind": "datasource", "attributes": fields,
                        "definition": f"Telemetry file “{f['original_name']}” ({f['ext'].upper()}, {f['size_bytes']:,} bytes) uploaded against graph KG{f['graph_version_number']}. "
                                      f"{len(by_node)} entit(ies) matched by field name; matching is lexical, not semantic."},
                        "rationale": "Represents the uploaded file as a data source in your graph."})
            used.append(f["original_name"])
            for nk, ms in by_node.items():
                raw.append({"op": "add_edge", "payload": {"source": key, "target": nk, "label": f"supplies {len(ms)} mapped field(s)"},
                            "rationale": "Fields: " + ", ".join(sorted({m['term'] for m in ms})[:8])})
            for nk, c in mention_nodes.items():
                if nk not in by_node:
                    raw.append({"op": "add_edge", "payload": {"source": key, "target": nk, "label": f"mentions ({c}×)"},
                                "rationale": "The entity name appears in the document text."})
        if not raw:
            if unmapped:
                raise ValidationError("None of the uploaded files could be matched to entities in your finalised graph, so there is nothing to add. "
                                      f"Unmatched files: {', '.join(unmapped[:5])}.")
            raise ConflictError("Every processed file is already represented in your graph.")
        title = f"Telemetry mapping from {len(used)} file(s)"
        summary = (f"Adds {len(used)} data-source node(s) linked to the entities their fields or text matched. "
                   + (f"{len(unmapped)} file(s) had no matches and are not included." if unmapped else ""))
        proposal, rejected = self.props.create(user_id, wf, domain, raw, title, summary, "telemetry")
        if not proposal:
            raise ConflictError("The telemetry changes could not be validated.", details=rejected)
        return {"proposal": proposal, "files_used": used, "files_unmapped": unmapped}
