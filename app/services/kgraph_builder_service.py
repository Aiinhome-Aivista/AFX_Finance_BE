"""Automated, verified knowledge graph of an uploaded dataset (Module B).

  profile the tables -> propose (LLM + deterministic rules) -> VERIFY every claim against the data with pandas -> persist + sync

* The LLM only proposes: joins, business synonyms and metric formulas. Nothing it says is kept unless kgraph_verifier confirms it
  from the data; rejected proposals are recorded with the reason. Hierarchies are discovered from the data itself.
* Stored in MySQL (reliable copy) and mirrored to ArangoDB (graph store) in shared collections, every document carrying
  `workflow_id` so one tenant's graph is never mixed with another's. Arango being down never fails a build or a chat.
"""
import hashlib
import logging
import re
import threading
import time

from ..repositories.knowledge_graphs import KnowledgeGraphRepository
from . import dataset_profiler as prof
from . import kgraph_verifier as ver

log = logging.getLogger(__name__)

NODES, EDGES, META = "data_kg_nodes", "data_kg_edges", "data_kg_meta"
ARANGO_COOLDOWN_S = 120
_ARANGO_DOWN_UNTIL = 0.0
_MEM = {}                 # workflow_id -> (fingerprint, graph)
_BUILDING = set()         # workflow ids being built right now (one build at a time per workflow)
_LOCK = threading.Lock()

ABBREV = {"amt": "amount", "qty": "quantity", "rev": "revenue", "cogs": "cost of goods sold", "pct": "percent", "avg": "average",
          "inr": "", "usd": "", "id": "id", "no": "number", "dt": "date"}

PROPOSE_SYSTEM = (
    "You are a finance data modeller. Propose a knowledge graph for the DATA below as ONE JSON object with keys "
    '"edges" (joins between two different tables: from_table, from_column, to_table, to_column), '
    '"synonyms" (everyday business words for a column: term, column), '
    '"metrics" (business formulas: name, formula, description). '
    "Rules: use ONLY table and column names exactly as listed; in formulas write each column in square brackets like [Gross Sales] and use "
    "only + - * / and numbers on numeric measure columns; never invent a column. Every proposal will be checked against the data and "
    "dropped if it is wrong. Output only the JSON object."
)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:40] or "x"


def _key(wf, kind, *parts):
    raw = "|".join(str(p) for p in parts)
    return f"wf{int(wf)}_{kind}_{_slug(parts[-1])}_{hashlib.md5(raw.encode()).hexdigest()[:8]}"


class KGraphBuilderService:
    def __init__(self, db, cfg, llm=None, arango=None):
        self.db, self.cfg, self.llm = db, cfg, llm
        self._arango = arango
        self.repo = KnowledgeGraphRepository(db)

    # ------------------------------------------------------------------ proposals
    @staticmethod
    def profile_text(tables):
        """Compact plain-text description of the data (small models follow this far better than nested JSON)."""
        f = prof.fmt_number
        lines = []
        for tname, (s, df) in tables.items():
            roles = s["roles"]
            lines.append(f'TABLE "{tname}" - {s["rows"]:,} rows')
            meas = [f'{c} (sum {f(df[c].sum())})' for c, r in roles.items() if r == "measure"]
            dims = [f'{c} ({df[c].nunique()} values, e.g. {", ".join(map(str, df[c].dropna().value_counts().head(4).index))})'
                    for c, r in roles.items() if r == "dimension"]
            if meas:
                lines.append("Numeric measures: " + "; ".join(meas))
            if dims:
                lines.append("Category columns: " + "; ".join(dims))
            ids = [c for c, r in roles.items() if r == "identifier"]
            if ids:
                lines.append("Identifier columns: " + ", ".join(ids))
            dates = [c for c, r in roles.items() if r == "date"]
            if dates:
                lines.append("Date columns: " + ", ".join(dates))
        return "\n".join(lines)

    @staticmethod
    def _example(tables):
        tname, (s, _) = next(iter(tables.items()))
        meas = [c for c, r in s["roles"].items() if r == "measure"]
        dims = [c for c, r in s["roles"].items() if r == "dimension"]
        ex = {"edges": [], "synonyms": [], "metrics": []}
        if meas:
            ex["synonyms"].append({"term": "sales", "column": meas[0]})
        if len(meas) >= 2:
            ex["metrics"].append({"name": "Net share", "formula": f"([{meas[0]}] - [{meas[1]}]) / [{meas[0]}]", "description": f"{meas[0]} less {meas[1]} as a share of {meas[0]}"})
        if dims:
            ex["synonyms"].append({"term": "area", "column": dims[0]})
        import json
        return json.dumps(ex)

    def _llm_proposals(self, tables):
        if not self.llm:
            return {}
        user = (f"DATA\n{self.profile_text(tables)}\n\nEXAMPLE OF THE OUTPUT FORMAT (shape only; make your own choices from DATA):\n"
                f"{self._example(tables)}\n\nTASK: Propose edges, synonyms and metrics for the DATA above. Output only the JSON object.")
        try:
            out = self.llm.complete_json(PROPOSE_SYSTEM, user, max_tokens=1500)
            return out if isinstance(out, dict) else {}
        except Exception as e:  # LLM down / prose instead of JSON: the deterministic proposals still apply
            log.info("graph proposal via LLM unavailable: %s", e)
            return {}

    @staticmethod
    def _det_edges(tables):
        out = []
        names = list(tables)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ra, rb = tables[a][0]["roles"], tables[b][0]["roles"]
                for ca, role_a in ra.items():
                    for cb, role_b in rb.items():
                        if ver.norm(ca) == ver.norm(cb) and role_a in ("identifier", "dimension") and role_b in ("identifier", "dimension"):
                            out.append({"from_table": a, "from_column": ca, "to_table": b, "to_column": cb})
        return out

    @staticmethod
    def _det_synonyms(tables):
        out = []
        for tname, (s, _) in tables.items():
            for c, role in s["roles"].items():
                if role in ("empty", "text"):
                    continue
                words = [w for w in (ABBREV.get(t, t) for t in ver.norm(c).split()) if w]
                for term in {" ".join(words), " ".join(w for w in words if w not in ("total", "amount", "value"))}:
                    out.append({"term": term, "column": c, "table": tname})
        return out

    @staticmethod
    def _det_metrics(tables):
        """Common finance ratios, offered only when the matching columns exist (they are verified like any other proposal)."""
        out = []
        for tname, (s, _) in tables.items():
            meas = [c for c, r in s["roles"].items() if r == "measure" and not prof.AVG_RE.search(c)]
            find = lambda rx: next((c for c in meas if re.search(rx, c, re.I)), None)  # noqa: E731
            gross, net = find(r"gross.*(sales|revenue)|^(sales|revenue)"), find(r"net.*(revenue|sales)")
            cost, disc, qty = find(r"cogs|cost"), find(r"discount"), find(r"qty|quantity|units")
            if gross and cost:
                out.append({"name": "Gross Margin", "formula": f"([{gross}] - [{cost}]) / [{gross}]", "table": tname,
                            "description": f"({gross} - {cost}) / {gross}"})
            if gross and disc:
                out.append({"name": "Discount Rate", "formula": f"[{disc}] / [{gross}]", "table": tname, "description": f"{disc} as a share of {gross}"})
            if (net or gross) and qty:
                top = net or gross
                out.append({"name": "Average Revenue per Unit", "formula": f"[{top}] / [{qty}]", "table": tname, "description": f"{top} per {qty}"})
        return out

    # ------------------------------------------------------------------ build
    def build(self, workflow_id, tables, fingerprint):
        """Propose + verify + persist + sync. Returns the verified graph (also cached in memory)."""
        proposals = self._llm_proposals(tables)
        rejected = []

        edges, seen = [], set()
        for e in (proposals.get("edges") or []) + self._det_edges(tables):
            if not isinstance(e, dict):
                continue
            k = (e.get("from_table"), e.get("from_column"), e.get("to_table"), e.get("to_column"))
            if k in seen:
                continue
            seen.add(k)
            ev, why = ver.verify_edge(tables, e)
            (edges.append({**{x: e[x] for x in ("from_table", "from_column", "to_table", "to_column")}, **ev}) if ev else
             rejected.append({"kind": "edge", "item": e, "reason": why}))

        hierarchies = []
        for h in ver.discover_hierarchies(tables):
            ev, why = ver.verify_hierarchy(tables, h["table"], h["levels"])
            (hierarchies.append({**h, **ev}) if ev else rejected.append({"kind": "hierarchy", "item": h, "reason": why}))

        syn_items = [s for s in (proposals.get("synonyms") or []) if isinstance(s, dict)] + self._det_synonyms(tables)
        synonyms, syn_rej = ver.verify_synonyms(tables, syn_items)
        rejected += [{"kind": "synonym", **r} for r in syn_rej]

        metrics, mseen = [], set()
        for m in [x for x in (proposals.get("metrics") or []) if isinstance(x, dict)] + self._det_metrics(tables):
            if ver.norm(m.get("name")) in mseen:
                continue
            mseen.add(ver.norm(m.get("name")))
            ok, why = ver.verify_metric(tables, m)
            (metrics.append(ok) if ok else rejected.append({"kind": "metric", "item": m, "reason": why}))

        graph = {
            "workflow_id": int(workflow_id), "fingerprint": fingerprint, "generated_by": "llm+data" if proposals else "data",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fact_tables": [{"table": t, "rows": s["rows"], "measures": [c for c, r in s["roles"].items() if r == "measure"]}
                            for t, (s, _) in tables.items() if any(r == "measure" for r in s["roles"].values()) and s["rows"] > 0],
            "dimensions": [{"table": t, "column": c, "distinct": int(df[c].nunique())}
                           for t, (s, df) in tables.items() for c, r in s["roles"].items() if r == "dimension"],
            "dates": [{"table": t, "column": c} for t, (s, _) in tables.items() for c, r in s["roles"].items() if r == "date"],
            "edges": edges, "hierarchies": hierarchies, "synonyms": synonyms, "metrics": metrics, "rejected": rejected[:50],
        }
        synced = self._sync_arango(graph)
        try:
            self.repo.save(workflow_id, fingerprint, graph, synced)
        except Exception:
            log.exception("could not store the knowledge graph for workflow %s", workflow_id)
        with _LOCK:
            _MEM[int(workflow_id)] = (fingerprint, graph)
        return graph

    # ------------------------------------------------------------------ ArangoDB
    def arango(self):
        if self._arango is None:
            from ..arango_db import get_arango
            self._arango = get_arango(self.cfg if getattr(self.cfg, "ARANGO_URL", "") else None)
        return self._arango

    @staticmethod
    def arango_docs(graph):
        """Graph -> (node docs, edge docs). Every document carries workflow_id; keys are namespaced per workflow."""
        wf, nodes, edges = graph["workflow_id"], {}, []

        def node(kind, table, column=None, **extra):
            k = _key(wf, kind, table, column or table)
            nodes[k] = {"_key": k, "workflow_id": wf, "kind": kind, "table": table, "column": column,
                        "label": column or table, **extra}
            return f"{NODES}/{k}"

        for ft in graph["fact_tables"]:
            t = node("fact_table", ft["table"], rows=ft["rows"])
            for c in ft["measures"]:
                edges.append({"_from": t, "_to": node("measure", ft["table"], c), "type": "has_measure"})
        for d in graph["dimensions"]:
            edges.append({"_from": node("table", d["table"]), "_to": node("dimension", d["table"], d["column"], distinct=d["distinct"]), "type": "has_dimension"})
        for e in graph["edges"]:
            edges.append({"_from": node("table", e["from_table"]), "_to": node("table", e["to_table"]), "type": "joins",
                          "on": [e["from_column"], e["to_column"]], "overlap": e["overlap"], "cardinality": e["cardinality"]})
        for h in graph["hierarchies"]:
            for s in h["steps"]:
                edges.append({"_from": node("dimension", h["table"], s["child"]), "_to": node("dimension", h["table"], s["parent"]),
                              "type": "rolls_up_to", "consistency": s["consistency"]})
        for s in graph["synonyms"]:
            edges.append({"_from": node("term", s["table"], s["term"]), "_to": node("column", s["table"], s["column"]), "type": "synonym_of"})
        for m in graph["metrics"]:
            mk = node("metric", m["table"], m["name"], formula=m["formula"], value=m["value"])
            for c in m["columns"]:
                edges.append({"_from": mk, "_to": node("measure", m["table"], c), "type": "derived_from"})
        for i, ed in enumerate(edges):
            ed["workflow_id"] = wf
            ed["_key"] = _key(wf, "e", ed["_from"], ed["_to"], ed["type"], i)
        return list(nodes.values()), edges

    def _sync_arango(self, graph):
        global _ARANGO_DOWN_UNTIL
        if time.time() < _ARANGO_DOWN_UNTIL:
            return False
        try:
            a, wf = self.arango(), graph["workflow_id"]
            a.ensure_collection(NODES)
            a.ensure_collection(EDGES, is_edge=True)
            a.ensure_collection(META)
            for c in (NODES, EDGES, META):  # replace only THIS workflow's documents
                a.query(f"FOR d IN {c} FILTER d.workflow_id == @wf REMOVE d IN {c}", {"wf": wf})
            nodes, edges = self.arango_docs(graph)
            a.import_documents(NODES, nodes)
            a.import_documents(EDGES, edges)
            a.import_documents(META, [{"_key": f"wf{wf}", "workflow_id": wf, "fingerprint": graph["fingerprint"], "graph": graph}])
            return True
        except Exception as e:
            _ARANGO_DOWN_UNTIL = time.time() + ARANGO_COOLDOWN_S
            log.warning("ArangoDB sync skipped (%s: %s); the graph is kept in MySQL", type(e).__name__, e)
            return False

    # ------------------------------------------------------------------ read
    def load(self, workflow_id, fingerprint):
        """The verified graph for this exact data version, or None. Memory -> ArangoDB -> MySQL; a stale graph is never returned."""
        global _ARANGO_DOWN_UNTIL
        with _LOCK:
            hit = _MEM.get(int(workflow_id))
        if hit and hit[0] == fingerprint:
            return hit[1]
        graph = None
        if time.time() >= _ARANGO_DOWN_UNTIL:
            try:
                rows = self.arango().query(f"FOR d IN {META} FILTER d._key == @k RETURN d", {"k": f"wf{int(workflow_id)}"})
                if rows and rows[0].get("workflow_id") == int(workflow_id) and rows[0].get("fingerprint") == fingerprint:
                    graph = rows[0]["graph"]
            except Exception as e:
                _ARANGO_DOWN_UNTIL = time.time() + ARANGO_COOLDOWN_S
                log.info("ArangoDB read skipped (%s); using MySQL copy", type(e).__name__)
        if graph is None:
            try:
                row = self.repo.get(workflow_id)
                if row and row[0] == fingerprint:
                    graph = row[1]
            except Exception:
                graph = None
        if graph is not None:
            with _LOCK:
                _MEM[int(workflow_id)] = (fingerprint, graph)
        return graph

    def build_in_background(self, workflow_id, tables_fn, fingerprint):
        """Build once per workflow without blocking the caller. tables_fn() -> typed tables (called inside the worker)."""
        from ..db import Session, open_session
        if not isinstance(self.db, Session):
            return False  # a worker needs its own real DB session
        with _LOCK:
            if int(workflow_id) in _BUILDING:
                return False
            _BUILDING.add(int(workflow_id))

        def work():
            db = None
            try:
                db = open_session(self.cfg)
                KGraphBuilderService(db, self.cfg, self.llm, self._arango).build(workflow_id, tables_fn(db), fingerprint)
            except Exception:
                log.exception("background knowledge-graph build failed for workflow %s", workflow_id)
            finally:
                with _LOCK:
                    _BUILDING.discard(int(workflow_id))
                if db is not None:
                    db.close()
        threading.Thread(target=work, daemon=True, name=f"kgraph-{workflow_id}").start()
        return True
