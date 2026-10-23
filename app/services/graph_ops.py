"""Pure graph logic (no DB): working-graph state, change-operation validation/application, diffing, graph validation.

Every operation acts on a user-owned GraphState copy. KG0 is never an input that can be mutated: reference nodes are
*copied* into a GraphState and the copy carries origin='reference' so provenance is preserved.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import networkx as nx

OPS = ("add_node", "update_node", "remove_node", "add_edge", "remove_edge", "update_edge")
MAX_ITEMS_PER_PROPOSAL = 60
LIM = {"name": 120, "definition": 2000, "label": 120, "attr": 120, "formula": 240, "note": 1200, "attrs": 40, "formulas": 12}
KIND_STYLE = {  # appearance of client-created nodes
    "entity": ("client", "Client-specific entities", "#E08A1E"),
    "experience": ("client-experience", "Client business experience", "#D946EF"),
    "datasource": ("telemetry", "Telemetry data sources", "#14B8A6"),
}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(v, limit):
    if v is None:
        return ""
    s = _CTRL.sub("", str(v))
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:limit]


def slugify(s, prefix=""):
    base = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "item"
    return f"{prefix}{base}"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@dataclass
class GraphState:
    nodes: dict = field(default_factory=dict)   # key -> node dict (insertion ordered)
    edges: dict = field(default_factory=dict)   # (source, target, label) -> edge dict

    def copy(self):
        return GraphState(copy.deepcopy(self.nodes), copy.deepcopy(self.edges))

    @classmethod
    def from_lists(cls, nodes, edges):
        return cls({n["key"]: n for n in nodes}, {(e["source"], e["target"], e["label"]): e for e in edges})

    def node_list(self):
        return list(self.nodes.values())

    def edge_list(self):
        return list(self.edges.values())

    def reset_change_states(self):
        for n in self.nodes.values():
            n["change_state"] = "unchanged"
        for e in self.edges.values():
            e["change_state"] = "unchanged"

    def find(self, ref):
        """Resolve a node reference by key, display name, normalized string, or stem/alias."""
        if not ref:
            return None
        if ref in self.nodes:
            return ref
        r = norm(ref)
        if not r:
            return None
        # 1. Exact key normalized or exact name normalized
        for k, n in self.nodes.items():
            if norm(k) == r or norm(n.get("name", "")) == r:
                return k
        # 2. Substring / containment match
        for k, n in self.nodes.items():
            nk = norm(k)
            nn = norm(n.get("name", ""))
            if r in nk or nk in r or r in nn or nn in r:
                return k
        # 3. Stem matching (e.g. planversion vs planningversions, zbb vs zerobasedbudgetingzbb)
        stem = r.replace("ing", "").rstrip("s")
        if len(stem) >= 4:
            for k, n in self.nodes.items():
                k_stem = norm(k).replace("ing", "").rstrip("s")
                n_stem = norm(n.get("name", "")).replace("ing", "").rstrip("s")
                if stem in k_stem or k_stem in stem or stem in n_stem or n_stem in stem:
                    return k
        return None

    def edge_between(self, a, b):
        for (s, t, _l), e in self.edges.items():
            if s == a and t == b:
                return e
        return None


def reference_node_to_state(rn, role="core"):
    """Copy a KG0 row into a user-graph node dict (the copy is independent of KG0)."""
    return {
        "key": rn["node_key"], "name": rn["name"], "ref_name": rn["name"], "group_key": rn["group_key"],
        "group_name": rn["group_name"], "color": rn["color"], "kind": rn["kind"], "role": role,
        "definition": rn["definition"], "attributes": list(rn["attributes"]), "formulas": list(rn["formulas"]),
        "evidence": rn.get("evidence"), "notes": None, "origin": "reference", "change_state": "unchanged",
        "added_in_version": 0,
    }


def reference_edge_to_state(re_):
    return {"source": re_["source_key"], "target": re_["target_key"], "label": re_["label"], "origin": "reference",
            "change_state": "unchanged", "added_in_version": 0, "cross_domain": bool(re_.get("cross_domain"))}


# ----------------------------------------------------------------------------- validation
class OpResult:
    def __init__(self, payload=None, errors=None, warnings=None):
        self.payload, self.errors, self.warnings = payload, errors or [], warnings or []

    @property
    def ok(self):
        return not self.errors


def _str_list(v, item_limit, max_n):
    out = []
    for x in (v or []):
        t = clean_text(x, item_limit)
        if t and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out[:max_n]


def _unique_key(state, base):
    key, i = base, 2
    while key in state.nodes:
        key = f"{base}-{i}"
        i += 1
    return key


def validate_op(state: GraphState, op: str, payload: dict) -> OpResult:
    """Validate one operation against `state` and return a *cleaned* payload (keys resolved)."""
    if op not in OPS:
        return OpResult(errors=[f"Unknown operation '{op}'."])
    if not isinstance(payload, dict):
        return OpResult(errors=["Payload must be an object."])
    errs, warns, p = [], [], {}

    if op == "add_node":
        name = clean_text(payload.get("name"), LIM["name"])
        if not name:
            return OpResult(errors=["A node needs a name."])
        if any(norm(n["name"]) == norm(name) for n in state.nodes.values()):
            return OpResult(errors=[f"An entity named '{name}' already exists in your graph."])
        kind = payload.get("kind", "entity")
        if kind not in KIND_STYLE:
            kind = "entity"
        prefix = {"entity": "client-", "experience": "exp-", "datasource": "data-"}[kind]
        requested = clean_text(payload.get("node_key"), 64)
        base = slugify(requested or name, prefix if not (requested or "").startswith(prefix) else "")
        p = {"node_key": _unique_key(state, base), "name": name, "kind": kind,
             "definition": clean_text(payload.get("definition"), LIM["definition"]) or "Client-defined entity (no reference definition).",
             "attributes": _str_list(payload.get("attributes"), LIM["attr"], LIM["attrs"]),
             "formulas": _str_list(payload.get("formulas"), LIM["formula"], LIM["formulas"])}
        return OpResult(p, [], warns)

    if op == "update_node":
        key = state.find(clean_text(payload.get("node_key"), 160))
        if key is None:
            return OpResult(errors=["The entity to update does not exist in your graph."])
        n = state.nodes[key]
        p = {"node_key": key}
        new_name = clean_text(payload.get("name"), LIM["name"])
        if new_name and norm(new_name) != norm(n["name"]):
            if any(norm(o["name"]) == norm(new_name) for k, o in state.nodes.items() if k != key):
                return OpResult(errors=[f"Another entity is already called '{new_name}'."])
            p["name"] = new_name
        if payload.get("definition"):
            d = clean_text(payload["definition"], LIM["definition"])
            if d and d != n["definition"]:
                p["definition"] = d
        add_a = [a for a in _str_list(payload.get("add_attributes"), LIM["attr"], LIM["attrs"])
                 if norm(a) not in {norm(x) for x in n["attributes"]}]
        if add_a:
            p["add_attributes"] = add_a
        rem_a = [a for a in _str_list(payload.get("remove_attributes"), LIM["attr"], LIM["attrs"])
                 if norm(a) in {norm(x) for x in n["attributes"]}]
        if rem_a:
            p["remove_attributes"] = rem_a
        add_f = [f for f in _str_list(payload.get("add_formulas"), LIM["formula"], LIM["formulas"])
                 if norm(f) not in {norm(x) for x in n["formulas"]}]
        if add_f:
            p["add_formulas"] = add_f
        note = clean_text(payload.get("append_note"), LIM["note"])
        if note:
            p["append_note"] = note
        if len(p) == 1:
            return OpResult(errors=["This change would not alter the entity."])
        if len(n["attributes"]) + len(add_a) > 80:
            return OpResult(errors=["Too many attributes on one entity."])
        return OpResult(p, [], warns)

    if op == "remove_node":
        key = state.find(clean_text(payload.get("node_key"), 160))
        if key is None:
            return OpResult(errors=["The entity to remove does not exist in your graph."])
        n = state.nodes[key]
        if n["origin"] == "reference":
            warns.append("Removes a reference entity from YOUR customised graph (KG0 itself is unchanged).")
        deg = sum(1 for (s, t, _l) in state.edges if key in (s, t))
        if deg:
            warns.append(f"Also removes {deg} relationship(s) attached to it.")
        return OpResult({"node_key": key}, [], warns)

    if op in ("add_edge", "remove_edge", "update_edge"):
        s = state.find(clean_text(payload.get("source"), 160))
        t = state.find(clean_text(payload.get("target"), 160))
        if s is None or t is None:
            return OpResult(errors=["Both ends of a relationship must be existing entities."])
        if s == t:
            return OpResult(errors=["A relationship cannot connect an entity to itself."])
        label = clean_text(payload.get("label"), LIM["label"])
        if op == "add_edge":
            if state.edge_between(s, t):
                ex = state.edge_between(s, t)
                return OpResult(errors=[f"'{state.nodes[s]['name']}' → '{state.nodes[t]['name']}' already exists ('{ex['label']}')."])
            return OpResult({"source": s, "target": t, "label": label or "relates to"}, [], warns)
        ex = None
        want = clean_text(payload.get("old_label") if op == "update_edge" else payload.get("label"), LIM["label"])
        for (es, et, el), e in state.edges.items():
            if es == s and et == t and (not want or el == want):
                ex = e
                break
        if ex is None:
            return OpResult(errors=["That relationship does not exist."])
        if op == "remove_edge":
            return OpResult({"source": s, "target": t, "label": ex["label"]}, [], warns)
        if not label or label == ex["label"]:
            return OpResult(errors=["The new label must differ from the current one."])
        return OpResult({"source": s, "target": t, "old_label": ex["label"], "label": label}, [], warns)
    return OpResult(errors=["Unsupported operation."])


def apply_op(state: GraphState, op: str, p: dict, version_number: int, origin: str = "user"):
    """Apply a *validated* (cleaned) payload to `state` in place."""
    if op == "add_node":
        group, group_name, color = KIND_STYLE[p["kind"]]
        state.nodes[p["node_key"]] = {
            "key": p["node_key"], "name": p["name"], "ref_name": None, "group_key": group, "group_name": group_name,
            "color": color, "kind": p["kind"], "role": "client", "definition": p["definition"],
            "attributes": list(p["attributes"]), "formulas": list(p["formulas"]), "evidence": None, "notes": None,
            "origin": origin, "change_state": "added", "added_in_version": version_number}
    elif op == "update_node":
        n = state.nodes[p["node_key"]]
        if "name" in p:
            n["name"] = p["name"]
        if "definition" in p:
            n["definition"] = p["definition"]
        n["attributes"] = [a for a in n["attributes"] if norm(a) not in {norm(x) for x in p.get("remove_attributes", [])}]
        n["attributes"] += p.get("add_attributes", [])
        n["formulas"] += p.get("add_formulas", [])
        if "append_note" in p:
            n["notes"] = (n["notes"] + "\n" if n.get("notes") else "") + p["append_note"]
        if n["change_state"] != "added":
            n["change_state"] = "modified"
        n.setdefault("modified_in_version", version_number)
        n["modified_in_version"] = version_number
    elif op == "remove_node":
        state.nodes.pop(p["node_key"], None)
        for k in [k for k in state.edges if p["node_key"] in (k[0], k[1])]:
            state.edges.pop(k)
    elif op == "add_edge":
        state.edges[(p["source"], p["target"], p["label"])] = {
            "source": p["source"], "target": p["target"], "label": p["label"], "origin": origin,
            "change_state": "added", "added_in_version": version_number,
            "cross_domain": False}
    elif op == "remove_edge":
        state.edges.pop((p["source"], p["target"], p["label"]), None)
    elif op == "update_edge":
        old = state.edges.pop((p["source"], p["target"], p["old_label"]))
        old["label"], old["change_state"] = p["label"], "modified"
        state.edges[(p["source"], p["target"], p["label"])] = old


def describe_op(state: GraphState, op: str, p: dict) -> str:
    nm = lambda k: state.nodes[k]["name"] if k in state.nodes else k  # noqa: E731
    if op == "add_node":
        return f"Add {p['kind'] if p['kind'] != 'entity' else 'entity'} “{p['name']}”" + (f" with {len(p['attributes'])} attribute(s)" if p["attributes"] else "")
    if op == "update_node":
        bits = []
        if "name" in p: bits.append(f"rename to “{p['name']}”")
        if "add_attributes" in p: bits.append("add attributes: " + ", ".join(p["add_attributes"][:6]))
        if "remove_attributes" in p: bits.append("remove attributes: " + ", ".join(p["remove_attributes"][:6]))
        if "add_formulas" in p: bits.append(f"add {len(p['add_formulas'])} formula(s)")
        if "definition" in p: bits.append("revise definition")
        if "append_note" in p: bits.append("add a client business rule note")
        return f"Update “{nm(p['node_key'])}”: " + "; ".join(bits)
    if op == "remove_node":
        return f"Remove “{nm(p['node_key'])}” from your graph"
    if op == "add_edge":
        return f"Add relationship “{nm(p['source'])}” —{p['label']}→ “{nm(p['target'])}”"
    if op == "remove_edge":
        return f"Remove relationship “{nm(p['source'])}” —{p['label']}→ “{nm(p['target'])}”"
    return f"Relabel “{nm(p['source'])}” → “{nm(p['target'])}” from '{p['old_label']}' to '{p['label']}'"


def validate_sequence(state: GraphState, raw_items: list, version_number: int, origin: str = "user"):
    """Validate items in order on a scratch copy so later items may reference nodes added earlier.
    Returns (accepted, rejected, scratch_state): accepted=[{op,payload,description,warnings,rationale}]"""
    scratch = state.copy()
    accepted, rejected = [], []
    for raw in raw_items[:MAX_ITEMS_PER_PROPOSAL]:
        op, payload = raw.get("op"), raw.get("payload") or {}
        res = validate_op(scratch, op, payload)
        if not res.ok:
            rejected.append({"op": op, "payload": payload, "errors": res.errors})
            continue
        desc = describe_op(scratch, op, res.payload)
        apply_op(scratch, op, res.payload, version_number, origin)
        accepted.append({"op": op, "payload": res.payload, "description": desc, "warnings": res.warnings,
                         "rationale": clean_text(raw.get("rationale"), 600) or None})
    if len(raw_items) > MAX_ITEMS_PER_PROPOSAL:
        rejected.append({"op": "-", "payload": {}, "errors": [f"Only the first {MAX_ITEMS_PER_PROPOSAL} changes are considered per proposal."]})
    return accepted, rejected, scratch


# ----------------------------------------------------------------------------- diff
def _node_sig(n):
    return {"name": n["name"], "definition": n["definition"], "attributes": sorted(n["attributes"]),
            "formulas": sorted(n["formulas"]), "notes": n.get("notes") or ""}


def diff_graphs(a: GraphState, b: GraphState) -> dict:
    added = [b.nodes[k] for k in b.nodes if k not in a.nodes]
    removed = [a.nodes[k] for k in a.nodes if k not in b.nodes]
    modified = []
    for k in b.nodes:
        if k in a.nodes:
            sa, sb = _node_sig(a.nodes[k]), _node_sig(b.nodes[k])
            ch = {f: {"from": sa[f], "to": sb[f]} for f in sa if sa[f] != sb[f]}
            if ch:
                modified.append({"key": k, "name": b.nodes[k]["name"], "changes": ch,
                                 "attributes_added": sorted(set(sb["attributes"]) - set(sa["attributes"])),
                                 "attributes_removed": sorted(set(sa["attributes"]) - set(sb["attributes"]))})
    pa = {(s, t): e for (s, t, _l), e in a.edges.items()}
    pb = {(s, t): e for (s, t, _l), e in b.edges.items()}
    e_added = [e for p, e in pb.items() if p not in pa]
    e_removed = [e for p, e in pa.items() if p not in pb]
    e_mod = [{"source": p[0], "target": p[1], "from": pa[p]["label"], "to": e["label"]}
             for p, e in pb.items() if p in pa and pa[p]["label"] != e["label"]]
    slim = lambda n: {k: n[k] for k in ("key", "name", "kind", "origin", "group_name", "color", "definition")}  # noqa: E731
    return {
        "nodes_added": [slim(n) for n in added], "nodes_removed": [slim(n) for n in removed], "nodes_modified": modified,
        "edges_added": e_added, "edges_removed": e_removed, "edges_modified": e_mod,
        "counts": {"nodes_added": len(added), "nodes_removed": len(removed), "nodes_modified": len(modified),
                   "edges_added": len(e_added), "edges_removed": len(e_removed), "edges_modified": len(e_mod),
                   "nodes_a": len(a.nodes), "nodes_b": len(b.nodes), "edges_a": len(a.edges), "edges_b": len(b.edges)},
    }


# ----------------------------------------------------------------------------- graph validation (finalization)
def validate_graph(state: GraphState, reference_core_keys: set) -> dict:
    errors, warnings = [], []
    names = {}
    for n in state.nodes.values():
        names.setdefault(norm(n["name"]), []).append(n["name"])
        if not n["name"].strip():
            errors.append({"code": "empty_name", "message": f"Entity {n['key']} has no name."})
    for k, v in names.items():
        if len(v) > 1:
            errors.append({"code": "duplicate_name", "message": f"Duplicate entity name: {v[0]}."})
    for (s, t, l) in state.edges:
        if s not in state.nodes or t not in state.nodes:
            errors.append({"code": "dangling_edge", "message": f"Relationship '{l}' points to a missing entity."})
    if len(state.nodes) < 2:
        errors.append({"code": "too_small", "message": "The graph needs at least two entities."})

    g = nx.Graph()
    g.add_nodes_from(state.nodes)
    g.add_edges_from((s, t) for (s, t, _l) in state.edges if s in state.nodes and t in state.nodes)
    for k in nx.isolates(g):
        n = state.nodes[k]
        warnings.append({"code": "isolated_entity", "node": k, "message": f"“{n['name']}” has no relationships."})
    comps = nx.number_connected_components(g) if len(g) else 0
    if comps > 1:
        warnings.append({"code": "disconnected", "message": f"The graph has {comps} disconnected parts."})
    for n in state.nodes.values():
        if n["origin"] != "reference" and n["kind"] == "entity" and n["definition"].startswith("Client-defined entity"):
            warnings.append({"code": "no_definition", "node": n["key"], "message": f"“{n['name']}” has no definition yet."})
        if n["origin"] != "reference" and not n["attributes"] and n["kind"] == "entity":
            warnings.append({"code": "no_attributes", "node": n["key"], "message": f"“{n['name']}” has no attributes, so telemetry cannot be mapped to it."})
    missing_core = [k for k in reference_core_keys if k not in state.nodes]
    if missing_core:
        warnings.append({"code": "reference_entities_removed", "message":
                         f"{len(missing_core)} reference entities are not part of your graph: " + ", ".join(sorted(missing_core)[:8])})
    generic = sum(1 for e in state.edges.values() if e["label"].lower() == "relates to")
    if generic:
        warnings.append({"code": "generic_labels", "message": f"{generic} relationship(s) still use the generic label 'relates to'."})
    return {"errors": errors, "warnings": warnings, "stats": {"nodes": len(state.nodes), "edges": len(state.edges), "components": comps}}
