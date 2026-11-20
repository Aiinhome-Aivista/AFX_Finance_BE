"""Query-time use of the verified knowledge graph by the analyst chat: business synonyms and metric formulas.

A question that names a verified metric ("gross margin by region") is answered by running that metric's formula on the data with
pandas through the restricted evaluator; the LLM never computes it. Pure functions, no DB or Flask access.
"""
import re

from . import dataset_profiler as prof
from . import kgraph_verifier as ver

RATIO_NAME = re.compile(r"margin|rate|ratio|share|percent|pct|yield|utili[sz]ation|%", re.I)
MAX_ROWS = 20


def resolve_synonym(phrase, graph, table, roles, want):
    """Column for a business term ("sales" -> Gross_Sales_INR) from the verified synonyms of this table, or None. Longest term wins."""
    p = f" {ver.norm(phrase)} "
    best = None
    for s in graph.get("synonyms", []):
        if s["table"] == table and roles.get(s["column"]) in want and f" {s['term']} " in p:
            if best is None or len(s["term"]) > len(best["term"]):
                best = s
    return best["column"] if best else None


def find_metric(question, graph):
    q = f" {ver.norm(question)} "
    hits = [m for m in graph.get("metrics", []) if m.get("status") != "rejected" and f" {ver.norm(m['name'])} " in q]
    return max(hits, key=lambda m: len(m["name"])) if hits else None


def is_ratio(metric):
    return "/" in metric["formula"] and bool(RATIO_NAME.search(metric["name"]))


def _fmt(metric):
    return (lambda v: f"{v:.1%}") if is_ratio(metric) else prof.fmt_number


def compute(metric, tables, group=None, bucket=None):
    """Run the metric formula (overall, or per group / per month). Returns a result shaped like AnalystChatService.execute()."""
    summary, df = tables[metric["table"]]
    roles, name = summary["roles"], metric["name"]
    cols = metric["columns"]
    if not group:
        v = ver.evaluate(metric["formula"], ver.column_totals(df, roles, cols))
        return {"columns": [name], "rows": [[round(v, 6)]], "matched_rows": int(len(df)), "total_groups": 1,
                "group_columns": [], "metric_columns": [name], "is_time": False}
    key = df[group].dt.to_period("M").astype("string") if bucket else df[group]
    label = f"{group} ({bucket})" if bucket else group
    rows = []
    for k, sub in df.groupby(key, dropna=True):
        try:
            rows.append([str(k), round(ver.evaluate(metric["formula"], ver.column_totals(sub, roles, cols)), 6)])
        except ver.FormulaError:
            continue  # a group where the formula is undefined (e.g. zero denominator) is skipped, never guessed
    rows.sort(key=(lambda r: r[0]) if bucket else (lambda r: -r[1]))
    return {"columns": [label, name], "rows": rows[:MAX_ROWS if not bucket else 36], "matched_rows": int(len(df)), "total_groups": len(rows),
            "group_columns": [label], "metric_columns": [name], "is_time": bool(bucket)}


def headline(metric, result):
    f, name = _fmt(metric), metric["name"]
    formula = re.sub(r"[\[\]]", "", metric["formula"])
    rows = result["rows"]
    if not rows:
        return f"{name} cannot be calculated for that grouping (its denominator is zero)."
    if not result["group_columns"]:
        return f"{name} is {f(rows[0][0])}, calculated as {formula} over {result['matched_rows']:,} records."
    order = "in time order" if result["is_time"] else "highest first"
    body = "; ".join(f"{r[0]} {f(r[1])}" for r in rows[:6])
    more = f" Showing 6 of {result['total_groups']:,} (full table below)." if result["total_groups"] > 6 else ""
    return f"{name} by {result['group_columns'][0]}, {order}: {body}.{more}"


def answer(question, tables, graph, best_col):
    """If the question names a verified metric, compute it. Returns {"spec","result","headline","fmt","evidence"} or None."""
    m = find_metric(question, graph)
    if not m or m["table"] not in tables:
        return None
    if m.get("status") != "approved":  # a proposed definition is never calculated until the Data Architect approves it
        return {"pending": m}
    summary, df = tables[m["table"]]
    q = question.lower()
    group, bucket = None, None
    date_col = next((c for c, r in summary["roles"].items() if r == "date"), None)
    if date_col and re.search(r"\b(monthly|by month|per month|each month|month by month|trend|over time)\b", q):
        group, bucket = date_col, "month"
    else:
        mt = re.search(r"\b(?:by|per|for each|across)\s+(.+)$", q)
        if mt:
            group = best_col(mt.group(1).strip(" ?."), summary, ("dimension", "identifier"))
    try:
        result = compute(m, tables, group, bucket)
    except ver.FormulaError:
        return None
    spec = {"table": m["table"], "filters": [], "group_by": [{"column": group, "bucket": bucket}] if group else [],
            "metrics": [{"fn": "formula", "column": None, "as": m["name"], "formula": m["formula"]}],
            "order_by": {"by": m["name"], "desc": True}, "limit": MAX_ROWS}
    plain = re.sub(r"[\[\]]", "", m["formula"])
    return {"spec": spec, "result": result, "headline": headline(m, result), "fmt": _fmt(m),
            "evidence": {"source": "graph", "citation": "knowledge graph",
                         "text": f"{m['name']} = {plain} (verified on the data: {_fmt(m)(m['value'])} overall)."}}
