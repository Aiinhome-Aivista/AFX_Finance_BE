"""Pure unit tests for validation / apply / diff / graph validation."""
from app.services.graph_ops import GraphState, apply_op, diff_graphs, validate_graph, validate_op, validate_sequence


def node(k, name, attrs=(), origin="reference"):
    return {"key": k, "name": name, "ref_name": name, "group_key": "f1", "group_name": "g", "color": "#000", "kind": "domain", "role": "core",
            "definition": "d", "attributes": list(attrs), "formulas": [], "evidence": None, "notes": None, "origin": origin,
            "change_state": "unchanged", "added_in_version": 0}


def state():
    return GraphState.from_lists([node("a", "Alpha", ["x"]), node("b", "Beta")],
                                 [{"source": "a", "target": "b", "label": "feeds", "origin": "reference", "change_state": "unchanged", "added_in_version": 0}])


def test_duplicates_selfloops_unknown_rejected():
    s = state()
    assert not validate_op(s, "add_node", {"name": "alpha"}).ok                    # case-insensitive duplicate
    assert not validate_op(s, "add_edge", {"source": "a", "target": "a"}).ok
    assert not validate_op(s, "add_edge", {"source": "a", "target": "b", "label": "again"}).ok
    assert not validate_op(s, "add_edge", {"source": "a", "target": "zzz"}).ok
    assert not validate_op(s, "nuke", {}).ok
    assert not validate_op(s, "update_node", {"node_key": "a"}).ok                  # no-op
    assert not validate_op(s, "update_node", {"node_key": "a", "name": "Beta"}).ok  # name collision


def test_text_is_sanitised_and_bounded():
    r = validate_op(state(), "add_node", {"name": "  Gamma\x00\x07  ", "definition": "x" * 5000, "attributes": ["a"] * 100})
    assert r.ok and r.payload["name"] == "Gamma" and len(r.payload["definition"]) == 2000 and r.payload["attributes"] == ["a"]


def test_apply_marks_origin_and_change_state():
    s = state()
    acc, rej, out = validate_sequence(s, [{"op": "add_node", "payload": {"name": "Gamma"}},
                                           {"op": "add_edge", "payload": {"source": "Gamma", "target": "a", "label": "l"}},
                                           {"op": "update_node", "payload": {"node_key": "a", "add_attributes": ["y"]}}], 3)
    assert not rej and len(acc) == 3
    g = next(n for n in out.nodes.values() if n["name"] == "Gamma")
    assert g["origin"] == "user" and g["change_state"] == "added" and g["added_in_version"] == 3
    assert out.nodes["a"]["change_state"] == "modified" and s.nodes["a"]["change_state"] == "unchanged"   # input untouched


def test_remove_node_cascades_edges_and_warns():
    s = state()
    r = validate_op(s, "remove_node", {"node_key": "a"})
    assert r.ok and any("KG0 itself is unchanged" in w for w in r.warnings)
    apply_op(s, "remove_node", r.payload, 1)
    assert "a" not in s.nodes and not s.edges


def test_diff_and_graph_validation():
    a = state(); b = a.copy()
    apply_op(b, "update_node", {"node_key": "a", "add_attributes": ["y"]}, 1)
    apply_op(b, "remove_node", {"node_key": "b"}, 1)
    d = diff_graphs(a, b)
    assert d["counts"]["nodes_modified"] == 1 and d["counts"]["nodes_removed"] == 1 and d["counts"]["edges_removed"] == 1
    v = validate_graph(b, {"a", "b"})
    codes = {w["code"] for w in v["warnings"]}
    assert "reference_entities_removed" in codes and v["errors"][0]["code"] == "too_small"
