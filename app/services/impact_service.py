"""Dynamic Business Impact synthesis for knowledge-graph version diffs.

Generates structured, multi-dimensional business impact assessments for every
added, modified, or removed entity directly from:
  - exact structural diffs (definition clause shifts, renames, attributes, formulas, notes)
  - semantic lenses across financial, operational, control, and planning dimensions
  - real upstream/downstream graph topology and added/removed relationships
  - captured client rationales and telemetry evidence from change proposals
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re

from .graph_ops import GraphState, norm

_ACRONYMS = {
    "abc", "td", "foar", "cogs", "cogm", "ebitda", "ebit", "gl", "erp", "bi",
    "zbb", "doa", "sod", "dsd", "sku", "kpi", "ifrs", "gaap", "roi", "roic",
    "pnl", "fpa", "ap", "ar", "tax", "fx", "etl", "api", "sql",
}

_LENSES = [
    {
        "id": "cost_allocation",
        "tag": "Costing & Allocation Mechanics",
        "tone": "indigo",
        "keywords": {
            "cost", "costing", "allocat", "overhead", "absorption", "foar", "abc",
            "td-abc", "ladder", "indirect", "direct", "pool", "shared service",
            "unit economics", "prime cost", "works cost", "cogm", "cogs", "recovery",
            "driver", "job/batch/process",
        },
        "financial": (
            "recalibrates unit-cost build-up, overhead absorption recovery rates, and "
            "cost-pool distribution across shared services and operating cost centers"
        ),
    },
    {
        "id": "profitability_margin",
        "tag": "Margin & Revenue Protection",
        "tone": "emerald",
        "keywords": {
            "margin", "profit", "profitability", "gross", "net", "rebate", "discount",
            "revenue", "price", "pricing", "contribution", "ebitda", "leakage",
            "pocket", "cost-to-serve", "costtoserve", "channel", "segment", "true margin",
        },
        "financial": (
            "directly governs gross-to-net revenue bridges, pocket-margin visibility, "
            "and true customer, product, or channel profitability rankings"
        ),
    },
    {
        "id": "operations_supply",
        "tag": "Operations & Fulfillment Economics",
        "tone": "teal",
        "keywords": {
            "warehouse", "freight", "logistics", "supply", "inventory", "sku", "order",
            "picking", "packing", "fulfillment", "delivery", "dsd", "cross-dock",
            "throughput", "capacity", "idle", "variance", "volume", "shipment", "plant",
        },
        "financial": (
            "links physical supply-chain consumption—warehousing, picking/packing, "
            "freight legs, and capacity utilization—directly to financial P&L lines"
        ),
    },
    {
        "id": "hierarchy_master",
        "tag": "Entity & Reporting Hierarchy",
        "tone": "cyan",
        "keywords": {
            "organization", "entity", "hierarchy", "center", "cost center",
            "profit center", "investment center", "revenue center", "coa", "chart of accounts",
            "ledger", "consolidation", "intercompany", "elimination", "statutory",
            "reporting", "spine", "taxonomy", "master",
        },
        "financial": (
            "structures management and statutory roll-ups across legal entities, "
            "responsibility centers, and consolidated general-ledger hierarchies"
        ),
    },
    {
        "id": "planning_scenario",
        "tag": "Budgeting, ZBB & Scenarios",
        "tone": "blue",
        "keywords": {
            "budget", "zbb", "zero-based", "forecast", "scenario", "simulation",
            "sensitivity", "version", "threshold", "target", "plan", "rolling",
            "driver-based", "assumption", "outlook",
        },
        "financial": (
            "reshapes budget gating, sensitivity modeling, and forward-looking "
            "variance accountability across planning cycles"
        ),
    },
    {
        "id": "governance_control",
        "tag": "Internal Controls & Compliance",
        "tone": "amber",
        "keywords": {
            "control", "audit", "doa", "sod", "policy", "approval", "compliance",
            "risk", "reconcil", "gaap", "ifrs", "statutory", "governance", "tax",
            "guardrail", "limit", "segregation",
        },
        "financial": (
            "strengthens or alters audit traceability, reconciliation controls, "
            "delegation-of-authority checkpoints, and statutory compliance posture"
        ),
    },
    {
        "id": "incentive_kpi",
        "tag": "Performance & Incentive Alignment",
        "tone": "purple",
        "keywords": {
            "bonus", "compensation", "incentive", "kpi", "benchmark", "scorecard",
            "payout", "commission", "performance", "reward",
        },
        "financial": (
            "anchors executive scorecards and variable compensation mechanics to "
            "verified financial profitability and cost-efficiency outcomes"
        ),
    },
]


def _humanize_identifier(raw: str) -> tuple[str, str | None]:
    """Turn camelCase/snake_case/bracketed tokens into readable label + optional detail."""
    text = (raw or "").strip()
    detail = None
    bracket = re.search(r"\[([^\]]+)\]\s*$", text)
    if bracket:
        detail = bracket.group(1).strip()
        text = text[: bracket.start()].strip()

    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    spaced = re.sub(r"[_\-]+", " ", spaced)
    spaced = re.sub(r"\s+", " ", spaced).strip()

    words = []
    for w in spaced.split(" "):
        if not w:
            continue
        low = w.lower()
        if low in _ACRONYMS:
            words.append(low.upper())
        elif w.isupper() and len(w) <= 5:
            words.append(w)
        else:
            words.append(w[0].upper() + w[1:])
    return (" ".join(words) or raw), detail


def _format_attr_phrase(raw: str) -> str:
    label, detail = _humanize_identifier(raw)
    return f"{label} ({detail})" if detail else label


def _clean_domain_label(domain: dict | str | None) -> str:
    if isinstance(domain, dict):
        name = domain.get("name") or domain.get("slug") or "Finance"
    else:
        name = domain or "Finance"
    return re.sub(r"^F\d+[:\s\-]*", "", str(name)).strip() or "Finance"


def _extract_definition_delta(from_def: str, to_def: str) -> dict:
    """Identify the specific clause/phrase changed between two definition strings."""
    a_str = (from_def or "").strip()
    b_str = (to_def or "").strip()
    if not a_str and not b_str:
        return {"kind": "none"}
    if not a_str:
        return {"kind": "new", "added": b_str}
    if not b_str:
        return {"kind": "cleared", "removed": a_str}
    if a_str == b_str:
        return {"kind": "none"}

    a_tokens = a_str.split()
    b_tokens = b_str.split()
    matcher = SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
    ratio = matcher.ratio()

    # Merge nearby non-equal opcodes separated by tiny accidental equal matches (<= 3 tokens)
    raw_ops = matcher.get_opcodes()
    merged_ops: list[list] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(raw_ops):
        if (
            tag == "equal"
            and idx + 1 < len(raw_ops)
            and merged_ops
            and merged_ops[-1][0] != "equal"
            and (i2 - i1) <= 3
            and not any(tok.endswith((".", ";")) for tok in a_tokens[i1:i2])
        ):
            prev = merged_ops[-1]
            prev[0] = "replace"
            prev[2] = i2
            prev[4] = j2
        elif tag != "equal" and merged_ops and merged_ops[-1][0] != "equal":
            prev = merged_ops[-1]
            prev[0] = "replace"
            prev[2] = i2
            prev[4] = j2
        else:
            merged_ops.append([tag, i1, i2, j1, j2])

    replacements = []
    additions = []
    removals = []
    for tag, i1, i2, j1, j2 in merged_ops:
        if tag == "equal":
            continue
        old_chunk = " ".join(a_tokens[i1:i2]).strip(" ,;.")
        new_chunk = " ".join(b_tokens[j1:j2]).strip(" ,;.")
        if old_chunk and new_chunk:
            replacements.append((old_chunk, new_chunk))
        elif new_chunk:
            additions.append(new_chunk)
        elif old_chunk:
            removals.append(old_chunk)

    return {
        "kind": "rewrite" if ratio < 0.35 else "refined",
        "ratio": round(ratio, 2),
        "replacements": replacements[:3],
        "additions": additions[:3],
        "removals": removals[:3],
        "from": a_str,
        "to": b_str,
    }


def _select_lenses(text_blob: str, group_name: str, kind: str) -> tuple[list[dict], str, str]:
    low = (text_blob or "").lower()
    scored = []
    for lens in _LENSES:
        hits = sum(1 for kw in lens["keywords"] if kw in low)
        if hits:
            scored.append((hits, lens))
    scored.sort(key=lambda item: -item[0])
    matched = [item[1] for item in scored[:2]]

    if kind == "datasource":
        tag = f"Telemetry Feed · {group_name}" if group_name else "Telemetry Data Integration"
        tone = "teal"
    elif kind == "experience":
        tag = f"Operating Nuance · {group_name}" if group_name else "Client Operating Nuance"
        tone = "purple"
    elif group_name and group_name.strip().lower() not in {"client-specific entities", "core", "shared"}:
        tag = group_name.strip()
        tone = matched[0]["tone"] if matched else "slate"
    elif matched:
        tag = matched[0]["tag"]
        tone = matched[0]["tone"]
    else:
        tag = "Enterprise Operating Rule"
        tone = "slate"

    return matched, tag, tone


def _edges_for_node(key: str, edges: list[dict], node_map: dict[str, str]) -> list[str]:
    out = []
    for e in edges or []:
        s, t, lbl = e.get("source"), e.get("target"), e.get("label") or "relates to"
        if s == key:
            target_name = node_map.get(t, t)
            out.append(f"{target_name} ({lbl})")
        elif t == key:
            source_name = node_map.get(s, s)
            out.append(f"from {source_name} ({lbl})")
    return out


def _join_preview(items: list[str], limit: int = 4) -> str:
    if not items:
        return ""
    if len(items) <= limit:
        return ", ".join(items)
    rest = len(items) - limit
    return f"{', '.join(items[:limit])}, and {rest} more"


def _gather_rationales(
    key: str,
    name: str,
    old_name: str | None,
    evidence: list,
    notes: str,
    proposal_context: dict | None,
) -> list[str]:
    collected: list[str] = []
    seen_norm: set[str] = set()

    def _add(txt: str):
        clean = re.sub(r"\s+", " ", (txt or "").strip())
        if not clean:
            return
        nk = norm(clean[:120])
        if nk and nk not in seen_norm:
            seen_norm.add(nk)
            collected.append(clean)

    ctx = proposal_context or {}
    candidate_keys = {k for k in (key, norm(key), norm(name), norm(old_name or "")) if k}
    for ck in candidate_keys:
        for entry in ctx.get(ck, []):
            _add(entry)

    if notes:
        for part in notes.split("\n"):
            p = part.strip()
            if p:
                _add(f"Model note: {p}")

    ev_list = [evidence] if isinstance(evidence, str) else (evidence or [])
    for ev in ev_list:
        if isinstance(ev, dict):
            src = ev.get("source") or ev.get("file") or ev.get("title") or ""
            snippet = ev.get("snippet") or ev.get("text") or ev.get("reason") or ""
            if src and snippet:
                _add(f"Telemetry ({src}): {snippet}")
            elif src or snippet:
                _add(f"Telemetry: {src or snippet}")
        elif isinstance(ev, str) and ev.strip():
            _add(f"Evidence: {ev.strip()}")

    return collected[:3]


def _build_ripple_text(
    node_name: str,
    upstream: list[dict],
    downstream: list[dict],
    touched_added_edges: list[str],
    touched_removed_edges: list[str],
    change_kind: str,
) -> str:
    up_labels = [f"{u['name']} [{u['label']}]" for u in (upstream or [])]
    down_labels = [f"{d['name']} [{d['label']}]" for d in (downstream or [])]

    parts = []
    if up_labels:
        parts.append(f"Inputs feeding into {node_name} originate from {_join_preview(up_labels, 3)}.")
    if down_labels:
        if change_kind == "removed":
            parts.append(
                f"Removing {node_name} leaves {len(down_labels)} downstream dependent(s) unanchored: "
                f"{_join_preview(down_labels, 4)}."
            )
        else:
            parts.append(
                f"Updates to {node_name} cascade directly into {_join_preview(down_labels, 4)}, "
                f"re-weighting downstream calculations and reporting roll-ups."
            )
    if touched_added_edges:
        parts.append(f"New graph connections introduced: {_join_preview(touched_added_edges, 3)}.")
    if touched_removed_edges:
        parts.append(f"Severed graph connections: {_join_preview(touched_removed_edges, 3)}.")

    if not parts:
        return (
            f"{node_name} currently operates as a self-contained concept in this subgraph with "
            f"no direct inbound or outbound entity links."
        )
    return " ".join(parts)


def _impact_for_modified(
    item: dict,
    domain_label: str,
    node_map: dict[str, str],
    edges_added: list[dict],
    edges_removed: list[dict],
    edges_modified: list[dict],
    sb_state: GraphState,
    proposal_context: dict | None,
) -> dict:
    key = item["key"]
    name = item["name"]
    ch = item.get("changes") or {}
    attrs_added = item.get("attributes_added") or []
    attrs_removed = item.get("attributes_removed") or []
    formulas_added = item.get("formulas_added") or []
    formulas_removed = item.get("formulas_removed") or []
    upstream = item.get("upstream") or []
    downstream = item.get("downstream") or []
    group_name = item.get("group_name") or ""
    kind = item.get("kind") or "core"
    origin = item.get("origin") or "reference"

    live_node = sb_state.nodes.get(key) or {}
    current_def = (ch.get("definition") or {}).get("to") or live_node.get("definition") or ""
    old_def = (ch.get("definition") or {}).get("from") or ""
    def_delta = _extract_definition_delta(old_def, current_def)
    notes_to = (ch.get("notes") or {}).get("to") or live_node.get("notes") or ""
    old_name = (ch.get("name") or {}).get("from")

    text_blob = " ".join([
        name, old_name or "", group_name, domain_label, current_def,
        " ".join(attrs_added), " ".join(attrs_removed),
        " ".join(formulas_added), " ".join(formulas_removed),
        notes_to,
    ])
    lenses, tag, tone = _select_lenses(text_blob, group_name, kind)

    # 1. Operational & Structural Shift
    op_parts = []
    if old_name:
        op_parts.append(f"Renamed from “{old_name}” to “{name}” to match enterprise terminology.")
    if def_delta["kind"] == "refined" and (def_delta["replacements"] or def_delta["additions"] or def_delta["removals"]):
        clauses = []
        for old_c, new_c in def_delta["replacements"]:
            clauses.append(f"replaced “{old_c}” with “{new_c}”")
        for add_c in def_delta["additions"]:
            clauses.append(f"added “{add_c}”")
        for rem_c in def_delta["removals"]:
            clauses.append(f"dropped “{rem_c}”")
        op_parts.append(
            f"Recalibrated the operating definition for {name}: {'; '.join(clauses)}."
        )
    elif def_delta["kind"] == "rewrite":
        op_parts.append(
            f"Redefined core operating scope for {name} to establish custom enterprise rules: {def_delta['to']}"
        )

    parsed_added = [_format_attr_phrase(a) for a in attrs_added]
    parsed_removed = [_format_attr_phrase(a) for a in attrs_removed]
    if parsed_added:
        op_parts.append(f"Introduced {len(parsed_added)} custom parameter(s)/driver(s): {_join_preview(parsed_added, 5)}.")
    if parsed_removed:
        op_parts.append(f"Retired {len(parsed_removed)} baseline attribute(s): {_join_preview(parsed_removed, 4)}.")
    if formulas_added:
        op_parts.append(f"Added {len(formulas_added)} calculation rule(s): {_join_preview(formulas_added, 3)}.")
    if formulas_removed:
        op_parts.append(f"Removed {len(formulas_removed)} baseline formula(s): {_join_preview(formulas_removed, 3)}.")
    if not op_parts:
        op_parts.append(f"Updated structural metadata and operating notes for {name}.")
    operational_effect = " ".join(op_parts)

    # 2. Financial & Analytical Impact
    fin_parts = []
    if lenses:
        lens_clauses = [l["financial"] for l in lenses]
        fin_parts.append(f"Within {domain_label}, this entity {' and '.join(lens_clauses)}.")
    else:
        fin_parts.append(f"Within {domain_label}, {name} governs core enterprise classification and execution fidelity.")

    if def_delta.get("replacements"):
        _, new_c = def_delta["replacements"][0]
        fin_parts.append(
            f"Transitioning execution logic toward “{new_c}” changes how transactions, cost pools, or "
            f"balances accumulate from source postings into management reporting lines."
        )
    if parsed_added:
        fin_parts.append(
            f"Tracking {_join_preview(parsed_added, 3)} provides finer attribution granularity and eliminates "
            f"unallocated averages across periods and business slices."
        )
    if formulas_added or formulas_removed:
        fin_parts.append(
            "Updated formula logic alters automated KPI aggregation, period-close variance computations, "
            "and downstream sensitivity runs."
        )
    if parsed_removed:
        fin_parts.append(
            f"Dropping {_join_preview(parsed_removed, 3)} narrows baseline tracking dimensions, so reports "
            f"relying on those fields must source values from alternative drivers."
        )
    financial_control = " ".join(fin_parts)

    # 3. Graph Ripple & Dependencies
    touched_added = _edges_for_node(key, edges_added, node_map)
    touched_removed = _edges_for_node(key, edges_removed, node_map)
    # Check relabelled edges too
    for em in edges_modified or []:
        if em.get("source") == key:
            touched_added.append(f"relabelled → {node_map.get(em['target'], em['target'])} ({em['from']} → {em['to']})")
        elif em.get("target") == key:
            touched_added.append(f"relabelled ← {node_map.get(em['source'], em['source'])} ({em['from']} → {em['to']})")

    downstream_ripple = _build_ripple_text(
        name, upstream, downstream, touched_added, touched_removed, "modified"
    )

    # 4. Governance & Risk
    risk_parts = []
    is_high = bool(formulas_added or formulas_removed or attrs_removed or def_delta["kind"] != "none")
    severity = "high" if is_high else "medium"
    if def_delta["kind"] != "none" or formulas_added or formulas_removed:
        risk_parts.append(
            "Period-over-period comparability risk: restate or annotate historical baselines before comparing KPIs "
            "across pre- and post-calibration closes."
        )
    if parsed_added:
        risk_parts.append(
            "Verify ERP/BI data pipelines populate every newly added parameter cleanly to avoid null roll-ups."
        )
    if parsed_removed or formulas_removed:
        risk_parts.append(
            "Regression check required: confirm downstream BI extracts or statutory schedules do not reference removed fields."
        )
    if not risk_parts:
        risk_parts.append(
            f"Validate {name} naming and attribute alignment with finance controllership before locking the Digital Twin."
        )
    governance_risk = " ".join(risk_parts)

    # Missing vs Model Template analysis (attributes/formulas/stages/edges omitted in Current Model)
    missing_items = []
    for old_c, new_c in def_delta.get("replacements") or []:
        missing_items.append(f"Baseline definition clause replaced: “{old_c}” → “{new_c}”")
    for rem_c in def_delta.get("removals") or []:
        missing_items.append(f"Removed definition clause: “{rem_c}”")
    for a in parsed_removed:
        missing_items.append(f"Missing baseline attribute: {a}")
    for f_rem in formulas_removed:
        missing_items.append(f"Missing baseline formula: {f_rem}")
    for e_rem in touched_removed:
        missing_items.append(f"Removed relationship {e_rem}")

    missing_from_baseline = None
    if missing_items:
        conseq_parts = []
        if def_delta.get("replacements") or def_delta.get("removals"):
            conseq_parts.append(
                f"Standard baseline semantics or cost/reporting stages on “{name}” no longer match the reference template, "
                f"which can break automated benchmarking against industry GAAP/statutory schedules unless mapped cleanly."
            )
        if parsed_removed:
            conseq_parts.append(
                f"Removing {_join_preview(parsed_removed, 4)} eliminates those baseline slicing dimensions—any downstream report "
                f"or dashboard expecting them will show blind spots or require fallback defaults."
            )
        if formulas_removed:
            conseq_parts.append(
                f"Dropping {_join_preview(formulas_removed, 3)} stops those reference calculations from running inside the twin."
            )
        if touched_removed:
            conseq_parts.append(
                f"Severed relationship(s) ({_join_preview(touched_removed, 3)}) cut baseline data/control handoffs."
            )
        missing_from_baseline = {
            "has_missing": True,
            "title": "Baseline Elements Missing or Replaced in Current Model",
            "summary": (
                f"{len(missing_items)} baseline element(s) present in the standard {domain_label} template are missing "
                f"or replaced on “{name}” in your Current Model."
            ),
            "missing_items": missing_items[:6],
            "business_consequences": " ".join(conseq_parts),
        }

    # Actionable, Descriptive Suggestions
    up_names = _join_preview([u["name"] for u in upstream], 3)
    down_names = _join_preview([d["name"] for d in downstream], 3)
    first_rep = (def_delta.get("replacements") or [None])[0]
    lens_phrase = lenses[0]["tag"].lower() if lenses else f"{domain_label} operations"

    action_steps = []
    if def_delta.get("replacements") or def_delta.get("removals"):
        if first_rep:
            action_steps.append(
                f"Accounting Cross-Walk & Mapping: Build a formal stage-by-stage reconciliation mapping the baseline logic "
                f"(“{first_rep[0]}”) to your custom operating flow (“{first_rep[1]}”) so management P&L lines still bridge cleanly to statutory/GAAP schedules."
            )
        else:
            action_steps.append(
                f"Accounting Cross-Walk & Mapping: Document how the revised operating scope on “{name}” maps back to standard "
                f"{domain_label} reporting buckets for external audit and year-over-year benchmarking."
            )
    if parsed_removed or formulas_removed:
        dropped = _join_preview(parsed_removed + formulas_removed, 3)
        action_steps.append(
            f"Retire & Regression-Check Removed Fields: Audit all downstream BI dashboards, close packets, and ERP extracts to verify none depend on "
            f"removed baseline element(s) ({dropped}), or configure explicit fallback calculations."
        )
    if touched_removed:
        action_steps.append(
            f"Validate Removed Graph Links: Review severed connection(s) ({_join_preview(touched_removed, 2)}) with domain owners and either "
            f"restore them in the Chatbot or document the alternate control path."
        )
    if parsed_added:
        action_steps.append(
            f"ERP / Telemetry Pipeline Alignment: Assign data-source ownership and GL mapping rules for newly added parameter(s) "
            f"({_join_preview(parsed_added, 3)}) so automated feeds populate every slice with zero unallocated nulls."
        )
    if formulas_added:
        action_steps.append(
            f"Parallel Close Formula Validation: Execute {len(formulas_added)} new rule(s) ({_join_preview(formulas_added, 2)}) in parallel "
            f"against the last closed period’s actuals to verify variance thresholds and absorption accuracy."
        )
    if downstream or upstream:
        lineage_parts = []
        if upstream:
            lineage_parts.append(f"upstream feeds from {up_names}")
        if downstream:
            lineage_parts.append(f"downstream roll-ups into {down_names}")
        action_steps.append(
            f"End-to-End Lineage & Roll-Up Review: Trace {' and '.join(lineage_parts)} to confirm driver weights, "
            f"cost-pool splits, and allocation multipliers behave consistently across all operating centers."
        )
    action_steps.append(
        f"Historical Baseline & Governance Sign-Off: Annotate or restate prior-period KPIs affected by “{name}” before comparing trends "
        f"post-calibration, and capture Finance Controllership approval prior to locking the Digital Twin."
    )

    rationale_parts = [
        f"Calibrating “{name}” directly reshapes {lens_phrase} within {domain_label}."
    ]
    if first_rep:
        rationale_parts.append(
            f"Because the baseline sequence (“{first_rep[0]}”) was recalibrated to “{first_rep[1]}”, downstream aggregations, management reporting lines, "
            f"and variance attributions will behave differently from off-the-shelf industry templates."
        )
    elif parsed_added or formulas_added:
        rationale_parts.append(
            f"Introducing custom attributes or rules on “{name}” sharpens granularity, provided source ERP/telemetry mappings and period-close controls are verified upfront."
        )
    if downstream:
        rationale_parts.append(
            f"Any mismatch here cascades into {len(downstream)} downstream entity link(s) ({down_names}), so confirming data lineage and cross-walk rules now prevents close-cycle rework."
        )

    verification_checks = []
    if first_rep:
        verification_checks.append(
            f"Verify GL account & cost-center buckets map cleanly into “{first_rep[1]}” without orphan balances."
        )
    if parsed_added:
        verification_checks.append(
            f"Confirm 100% source-system coverage for {_join_preview(parsed_added, 3)} across active reporting entities."
        )
    if downstream:
        verification_checks.append(
            f"Reconcile simulated roll-ups on {down_names} against latest audited actuals within acceptable tolerance."
        )
    if not verification_checks:
        verification_checks.append(
            f"Confirm “{name}” definitions, attributes, and formulas reflect approved finance policy."
        )

    suggestions = {
        "headline": (
            f"Verify Missing / Replaced Baseline Elements on “{name}”"
            if missing_items
            else f"Validate Custom Calibration for “{name}”"
        ),
        "priority": (
            "High Priority — Omission & Comparability Check"
            if missing_items or formulas_added
            else "Recommended — Schema Verification"
        ),
        "rationale": " ".join(rationale_parts),
        "action_steps": action_steps[:5],
        "verification_checks": verification_checks[:3],
        "expected_outcome": (
            f"Completing these checks ensures “{name}” delivers audit-ready reconciliation, prevents blind spots across "
            f"{down_names or 'downstream reports'}, and keeps YoY financial comparisons defensible."
        ),
    }

    # 5. Rationale / Evidence
    rationales = _gather_rationales(
        key, name, old_name, item.get("evidence") or live_node.get("evidence") or [],
        notes_to, proposal_context,
    )
    rationale = " | ".join(rationales) if rationales else ""

    # Summary (rich yet tight 2-sentence overview)
    change_counts = []
    if old_name:
        change_counts.append("entity rename")
    if def_delta["kind"] != "none":
        change_counts.append("definition recalibration")
    if parsed_added:
        change_counts.append(f"+{len(parsed_added)} attribute(s)")
    if parsed_removed:
        change_counts.append(f"-{len(parsed_removed)} attribute(s)")
    if formulas_added or formulas_removed:
        change_counts.append(f"{len(formulas_added) + len(formulas_removed)} formula change(s)")
    change_label = ", ".join(change_counts) or "metadata update"
    downstream_note = f" affecting {len(downstream)} downstream entity links" if downstream else ""
    summary = (
        f"Applied {change_label} on {name} within {domain_label}{downstream_note}. "
        f"{financial_control.split('. ')[0]}."
    )

    pillars = [
        {"label": "Operational & Structural Shift", "text": operational_effect},
        {"label": "Financial & Analytical Effect", "text": financial_control},
        {"label": "Graph Ripple & Dependencies", "text": downstream_ripple},
        {"label": "Governance & Risk Consideration", "text": governance_risk},
    ]
    if missing_from_baseline:
        pillars.append({"label": "Missing vs Model Template", "text": missing_from_baseline["business_consequences"]})
    if rationale:
        pillars.append({"label": "Captured Rationale & Evidence", "text": rationale})

    narrative = " ".join(p["text"] for p in pillars)
    return {
        "tag": tag,
        "tone": tone,
        "severity": severity,
        "origin": origin,
        "summary": summary,
        "operational_effect": operational_effect,
        "financial_control": financial_control,
        "downstream_ripple": downstream_ripple,
        "governance_risk": governance_risk,
        "missing_from_baseline": missing_from_baseline,
        "suggestions": suggestions,
        "rationale": rationale,
        "pillars": pillars,
        "narrative": narrative,
    }


def _impact_for_added(
    item: dict,
    domain_label: str,
    node_map: dict[str, str],
    edges_added: list[dict],
    proposal_context: dict | None,
) -> dict:
    key = item["key"]
    name = item["name"]
    definition = item.get("definition") or ""
    attrs = item.get("attributes") or []
    formulas = item.get("formulas") or []
    notes = item.get("notes") or ""
    upstream = item.get("upstream") or []
    downstream = item.get("downstream") or []
    group_name = item.get("group_name") or ""
    kind = item.get("kind") or "entity"
    origin = item.get("origin") or "user"

    text_blob = " ".join([name, group_name, domain_label, definition, " ".join(attrs), " ".join(formulas), notes])
    lenses, tag, tone = _select_lenses(text_blob, group_name, kind)

    parsed_attrs = [_format_attr_phrase(a) for a in attrs]
    origin_desc = (
        "telemetry schema discovery" if origin == "telemetry" or kind == "datasource"
        else "client discovery nuance"
    )

    op_parts = [f"Introduced new enterprise entity “{name}” via {origin_desc}, expanding beyond the standard {domain_label} reference template."]
    if definition and "no reference definition" not in definition.lower():
        op_parts.append(f"Defined role: {definition}")
    if parsed_attrs:
        op_parts.append(f"Captures {len(parsed_attrs)} dedicated attribute(s): {_join_preview(parsed_attrs, 5)}.")
    if formulas:
        op_parts.append(f"Embeds {len(formulas)} computation rule(s): {_join_preview(formulas, 3)}.")
    operational_effect = " ".join(op_parts)

    fin_parts = []
    if lenses:
        fin_parts.append(f"In {domain_label}, {name} {' and '.join(l['financial'] for l in lenses)}.")
    elif kind == "datasource":
        fin_parts.append(
            f"Supplies grounded operational telemetry into {domain_label}, bridging source-system records with semantic finance metrics."
        )
    else:
        fin_parts.append(
            f"Closes an enterprise-specific blind spot in {domain_label} that off-the-shelf industry baselines fail to model."
        )
    if formulas or parsed_attrs:
        fin_parts.append(
            "Enables explicit tracking, filtering, and variance attribution instead of burying these mechanics inside generic catch-all accounts."
        )
    financial_control = " ".join(fin_parts)

    touched_added = _edges_for_node(key, edges_added, node_map)
    downstream_ripple = _build_ripple_text(name, upstream, downstream, touched_added, [], "added")

    risk_parts = [
        f"Assign system-of-record ownership and GL/ERP mapping for “{name}” so telemetry refreshes stay synchronized."
    ]
    if not upstream and not downstream:
        risk_parts.append("Currently isolated without graph edges — link it to core backbone entities before final sign-off.")
    elif formulas:
        risk_parts.append("Reconcile newly introduced formulas against audited historical period numbers.")
    governance_risk = " ".join(risk_parts)

    rationales = _gather_rationales(key, name, None, item.get("evidence") or [], notes, proposal_context)
    rationale = " | ".join(rationales) if rationales else ""

    summary = (
        f"New client entity “{name}” added via {origin_desc} ({len(parsed_attrs)} attribute(s), "
        f"{len(formulas)} formula(s), {len(upstream) + len(downstream)} graph connection(s))."
    )

    add_steps = [
        f"Source-of-Record Ownership: Assign an ERP, GL, or operational telemetry owner for “{name}” and define refresh cadence so ongoing loads stay synchronized.",
    ]
    if parsed_attrs:
        add_steps.append(
            f"Schema & ETL Field Mapping: Map source tables to populate {len(parsed_attrs)} custom attribute(s) ({_join_preview(parsed_attrs, 4)}) with explicit null-handling rules."
        )
    if formulas:
        add_steps.append(
            f"Historical Period Reconciliation: Benchmark {len(formulas)} new rule(s) ({_join_preview(formulas, 2)}) against audited historical period numbers to verify accuracy."
        )
    if not upstream and not downstream:
        add_steps.append(
            f"Graph Backbone Integration: Use the Domain Chatbot to link “{name}” to core spine or profitability nodes so it participates in automated domain roll-ups."
        )
    else:
        add_steps.append(
            f"Neighbor Lineage Validation: Confirm edge semantics connecting “{name}” with "
            f"{_join_preview([n['name'] for n in (upstream + downstream)], 3)} reflect real accounting and operational flows."
        )
    add_steps.append(
        f"Controllership Governance Review: Confirm “{name}” does not duplicate an existing reference entity and document business intent for audit readiness."
    )

    add_checks = [
        f"Verify system-of-record extraction is scheduled and tested for “{name}”.",
        (
            f"Confirm downstream/upstream roll-ups with {_join_preview([n['name'] for n in (upstream + downstream)], 3)} reconcile cleanly."
            if (upstream or downstream)
            else f"Attach at least one backbone or target relationship to avoid an isolated node."
        ),
    ]

    suggestions = {
        "headline": f"Integrate & Govern New Enterprise Entity “{name}”",
        "priority": "Medium Priority — Nuance Validation",
        "rationale": (
            f"“{name}” extends the standard {domain_label} baseline via {origin_desc} to capture client-specific mechanics not modeled in generic templates. "
            f"Establishing clear source ownership, formula reconciliation, and graph connectivity ensures custom KPIs feed reliably into executive reporting."
        ),
        "action_steps": add_steps[:5],
        "verification_checks": add_checks,
        "expected_outcome": (
            f"Governing “{name}” prevents isolated data silos and turns client-specific operating nuances into auditable, automated Digital Twin metrics."
        ),
    }

    pillars = [
        {"label": "Operational & Structural Shift", "text": operational_effect},
        {"label": "Financial & Analytical Effect", "text": financial_control},
        {"label": "Graph Ripple & Dependencies", "text": downstream_ripple},
        {"label": "Governance & Risk Consideration", "text": governance_risk},
    ]
    if rationale:
        pillars.append({"label": "Captured Rationale & Evidence", "text": rationale})

    return {
        "tag": tag,
        "tone": tone,
        "severity": "medium",
        "origin": origin,
        "summary": summary,
        "operational_effect": operational_effect,
        "financial_control": financial_control,
        "downstream_ripple": downstream_ripple,
        "governance_risk": governance_risk,
        "missing_from_baseline": None,
        "suggestions": suggestions,
        "rationale": rationale,
        "pillars": pillars,
        "narrative": " ".join(p["text"] for p in pillars),
    }


def _impact_for_removed(
    item: dict,
    domain_label: str,
    node_map: dict[str, str],
    edges_removed: list[dict],
    proposal_context: dict | None,
) -> dict:
    key = item["key"]
    name = item["name"]
    definition = item.get("definition") or ""
    attrs = item.get("attributes") or []
    formulas = item.get("formulas") or []
    notes = item.get("notes") or ""
    upstream = item.get("upstream") or []
    downstream = item.get("downstream") or []
    group_name = item.get("group_name") or ""
    origin = item.get("origin") or "reference"

    text_blob = " ".join([name, group_name, domain_label, definition, " ".join(attrs), " ".join(formulas), notes])
    lenses, tag, _ = _select_lenses(text_blob, group_name, item.get("kind") or "core")

    parsed_attrs = [_format_attr_phrase(a) for a in attrs]
    op_parts = [
        f"Excluded baseline entity “{name}” from the active {domain_label} Digital Twin."
    ]
    if definition:
        op_parts.append(f"Previously governed: {definition}")
    if parsed_attrs:
        op_parts.append(f"Discontinued {len(parsed_attrs)} attribute(s): {_join_preview(parsed_attrs, 4)}.")
    if formulas:
        op_parts.append(f"Retired {len(formulas)} formula(s): {_join_preview(formulas, 3)}.")
    operational_effect = " ".join(op_parts)

    fin_parts = []
    if lenses:
        fin_parts.append(
            f"Eliminates structured visibility where this baseline normally {' and '.join(l['financial'] for l in lenses)}."
        )
    else:
        fin_parts.append(
            f"Removes a standard structural checkpoint from {domain_label}, shifting any related adjustments or reconciliations to external workflows."
        )
    financial_control = " ".join(fin_parts)

    touched_removed = _edges_for_node(key, edges_removed, node_map)
    downstream_ripple = _build_ripple_text(name, upstream, downstream, [], touched_removed, "removed")

    total_links = len(upstream) + len(downstream)
    severity = "high" if (origin == "reference" or total_links > 0 or formulas) else "medium"
    risk_parts = [
        f"Omission & audit check: verify your enterprise genuinely operates without “{name}” "
        f"({total_links} baseline dependency link(s) severed)."
    ]
    if origin == "reference":
        risk_parts.append(
            "Removing a standard reference entity can create reconciliation gaps or blind spots unless covered by another node."
        )
    governance_risk = " ".join(risk_parts)

    # Detailed Missing-vs-Model breakdown & actionable remediation suggestions
    missing_items = []
    if definition:
        missing_items.append(f"Baseline domain concept: {definition}")
    if parsed_attrs:
        missing_items.append(f"Lost tracking attribute(s): {_join_preview(parsed_attrs, 5)}")
    if formulas:
        missing_items.append(f"Lost calculation formula(s): {_join_preview(formulas, 3)}")
    if upstream:
        missing_items.append(
            f"Disconnected upstream feed(s): {_join_preview([u['name'] + ' [' + u['label'] + ']' for u in upstream], 3)}"
        )
    if downstream:
        missing_items.append(
            f"Unanchored downstream consumer(s): {_join_preview([d['name'] + ' [' + d['label'] + ']' for d in downstream], 3)}"
        )
    if not missing_items:
        missing_items.append(f"Core baseline entity “{name}” omitted from Current Model.")

    conseq_parts = []
    if lenses:
        conseq_parts.append(
            f"Because “{name}” is present in the standard {domain_label} template but missing in your Current Model, "
            f"your enterprise loses structured coverage where this baseline normally {' and '.join(l['financial'] for l in lenses)}."
        )
    else:
        conseq_parts.append(
            f"Because “{name}” is present in the standard {domain_label} template but missing in your Current Model, "
            f"processes governed by this entity lose formal tracking inside the Digital Twin."
        )
    if formulas:
        conseq_parts.append(
            f"Specifically, {len(formulas)} standard calculation rule(s) ({_join_preview(formulas, 2)}) will stop executing, "
            f"creating potential KPI or period-close gaps."
        )
    if downstream:
        conseq_parts.append(
            f"Downstream dependencies—{_join_preview([d['name'] for d in downstream], 4)}—no longer receive direct "
            f"lineage from “{name}”, risking incomplete roll-ups or manual spreadsheet workarounds."
        )
    missing_from_baseline = {
        "has_missing": True,
        "title": "What Is Missing vs Model Template & Business Consequence",
        "summary": (
            f"“{name}” existed in the standard {domain_label} Model Template but is absent from your Current Model "
            f"({total_links} link(s) and {len(formulas)} formula(s) affected)."
        ),
        "missing_items": missing_items[:5],
        "business_consequences": " ".join(conseq_parts),
    }

    rem_steps = [
        f"Evaluate Restoration via Chatbot: Click “Keep & Restore Entity” (or prompt the Domain Chatbot) to reinstate “{name}” if your enterprise still relies on its tracking"
        + (f" ({_join_preview(parsed_attrs, 3)})" if parsed_attrs else "")
        + ".",
    ]
    if downstream or upstream:
        connected = _join_preview([x["name"] for x in (upstream + downstream)], 4)
        rem_steps.append(
            f"Bridge Severed Graph Dependencies: If intentionally omitted, explicitly remap connected neighbors ({connected}) so {domain_label} calculations and lineage do not break."
        )
    if formulas:
        rem_steps.append(
            f"Confirm External ERP Handling: Verify that retired formula(s) ({_join_preview(formulas, 2)}) are either executed outside the twin or superseded by another entity."
        )
    rem_steps.append(
        f"Audit & Statutory Sign-Off: Document written confirmation from finance controllership/audit leads that excluding “{name}” leaves no statutory reconciliation or compliance gap."
    )

    rem_checks = [
        f"Confirm no closing checklist, statutory report, or BI extract queries “{name}”.",
        (
            f"Verify downstream consumers ({_join_preview([d['name'] for d in downstream], 3)}) receive required inputs via an alternate path."
            if downstream
            else f"Verify compensating internal controls cover the scope of “{name}”."
        ),
    ]

    suggestions = {
        "headline": f"Restore “{name}” or Verify Compensating Controls",
        "priority": "High Priority — Baseline Omission Risk" if severity == "high" else "Control Verification Needed",
        "rationale": (
            f"Because “{name}” is a standard {domain_label} reference component missing from your Current Model ({total_links} dependency link(s), "
            f"{len(parsed_attrs)} attribute(s), and {len(formulas)} formula(s) affected), confirming intentional exclusion vs accidental omission is critical before locking."
        ),
        "action_steps": rem_steps[:5],
        "verification_checks": rem_checks,
        "expected_outcome": (
            f"Either restoring “{name}” or documenting compensating controls closes silent reconciliation blind spots across your governed Digital Twin."
        ),
    }

    rationales = _gather_rationales(key, name, None, item.get("evidence") or [], notes, proposal_context)
    rationale = " | ".join(rationales) if rationales else ""

    summary = (
        f"Baseline entity “{name}” is present in the Model Template but missing in your Current Model, severing "
        f"{total_links} dependency link(s), {len(parsed_attrs)} attribute(s), and {len(formulas)} formula(s)."
    )

    pillars = [
        {"label": "Missing vs Model Template", "text": missing_from_baseline["business_consequences"]},
        {"label": "Operational & Structural Shift", "text": operational_effect},
        {"label": "Financial & Analytical Effect", "text": financial_control},
        {"label": "Graph Ripple & Dependencies", "text": downstream_ripple},
        {"label": "Governance & Risk Consideration", "text": governance_risk},
    ]
    if rationale:
        pillars.append({"label": "Captured Rationale & Evidence", "text": rationale})

    return {
        "tag": f"{tag} · Removed" if tag else "Removed Control",
        "tone": "red",
        "severity": severity,
        "origin": origin,
        "summary": summary,
        "operational_effect": operational_effect,
        "financial_control": financial_control,
        "downstream_ripple": downstream_ripple,
        "governance_risk": governance_risk,
        "missing_from_baseline": missing_from_baseline,
        "suggestions": suggestions,
        "rationale": rationale,
        "pillars": pillars,
        "narrative": " ".join(p["text"] for p in pillars),
    }


def build_proposal_context(proposals_with_items: list[tuple[dict, list[dict]]]) -> dict[str, list[str]]:
    """Index approved/pending proposal item rationales by node key and normalized entity name."""
    ctx: dict[str, list[str]] = {}
    for prop, items in proposals_with_items or []:
        if (prop.get("status") or "").lower() == "rejected":
            continue
        source_label = prop.get("source") or "change"
        for it in items or []:
            if (it.get("status") or "").lower() == "rejected":
                continue
            rationale = (it.get("rationale") or "").strip()
            desc = (it.get("description") or "").strip()
            text = rationale or desc
            if not text:
                continue
            prefix = "Telemetry gap rationale" if source_label == "telemetry" else "Discovery rationale"
            entry = f"{prefix}: {text}"
            payload = it.get("payload") or {}
            for field in ("node_key", "name", "source", "target"):
                val = payload.get(field)
                if not val:
                    continue
                for k in {str(val), norm(str(val))}:
                    if k:
                        ctx.setdefault(k, []).append(entry)
    return ctx


def enrich_diff_business_impact(
    diff: dict,
    sa: GraphState,
    sb: GraphState,
    domain: dict | str | None = None,
    proposal_context: dict[str, list[str]] | None = None,
) -> dict:
    """Attach a dynamic `business_impact` assessment to every added, removed, and modified node."""
    domain_label = _clean_domain_label(domain)
    node_map = {k: n.get("name") or k for k, n in sa.nodes.items()}
    node_map.update({k: n.get("name") or k for k, n in sb.nodes.items()})

    edges_added = diff.get("edges_added") or []
    edges_removed = diff.get("edges_removed") or []
    edges_modified = diff.get("edges_modified") or []

    for m in diff.get("nodes_modified") or []:
        m["business_impact"] = _impact_for_modified(
            m, domain_label, node_map, edges_added, edges_removed, edges_modified, sb, proposal_context
        )

    for a in diff.get("nodes_added") or []:
        a["business_impact"] = _impact_for_added(
            a, domain_label, node_map, edges_added, proposal_context
        )

    for r in diff.get("nodes_removed") or []:
        r["business_impact"] = _impact_for_removed(
            r, domain_label, node_map, edges_removed, proposal_context
        )

    for er in edges_removed:
        sname = node_map.get(er.get("source"), er.get("source"))
        tname = node_map.get(er.get("target"), er.get("target"))
        lbl = er.get("label") or "relates to"
        er["source_name"] = sname
        er["target_name"] = tname
        er["business_impact"] = {
            "impact": (
                f"In the standard {domain_label} Model Template, “{sname}” connected to “{tname}” ({lbl}). "
                f"With this relationship missing in the Current Model, automated handoff or validation from “{sname}” "
                f"into “{tname}” is disconnected unless covered by another path."
            ),
            "suggestion": (
                f"Confirm whether “{tname}” still depends on “{sname}” ({lbl}); restore the link in the Chatbot or map an alternate upstream input."
            ),
        }

    for ea in edges_added:
        ea["source_name"] = node_map.get(ea.get("source"), ea.get("source"))
        ea["target_name"] = node_map.get(ea.get("target"), ea.get("target"))

    for em in edges_modified:
        sname = node_map.get(em.get("source"), em.get("source"))
        tname = node_map.get(em.get("target"), em.get("target"))
        em["source_name"] = sname
        em["target_name"] = tname
        em["business_impact"] = {
            "impact": (
                f"Relationship semantics between “{sname}” and “{tname}” shifted from “{em.get('from')}” to “{em.get('to')}”, "
                f"altering how downstream rules interpret this dependency."
            ),
            "suggestion": f"Verify downstream rules on “{tname}” align with the updated “{em.get('to')}” relationship.",
        }

    return diff
