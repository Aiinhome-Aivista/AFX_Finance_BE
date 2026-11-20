"""Verification guardrails for the data-derived knowledge graph. Pure pandas / stdlib: no DB, no Flask, no LLM.

Every relationship the LLM (or the deterministic proposer) suggests is checked against the real data here. Only claims the data
supports are kept, each with its evidence; the rest are returned as rejected with the reason. Formulas run through a restricted
arithmetic evaluator (no eval/exec), with divide-by-zero protection.
"""
import ast
import math
import re

from . import dataset_profiler as prof

MIN_JOIN_OVERLAP = 0.8      # share of the smaller side's distinct values found on the other side
MIN_FD_CONSISTENCY = 0.98   # share of child values that map to exactly one parent value
MAX_DIM_COLS = 12


def norm(s):
    return " ".join(re.findall(r"[a-z0-9]+", str(s).lower()))


# ----------------------------------------------------------------------------- joins
def verify_edge(tables, e):
    """e: {"from_table","from_column","to_table","to_column"} -> (evidence dict, None) or (None, reason)."""
    ta, ca, tb, cb = (e.get(k) for k in ("from_table", "from_column", "to_table", "to_column"))
    if ta == tb:
        return None, "a join must connect two different tables"
    for t, c in ((ta, ca), (tb, cb)):
        if t not in tables or c not in tables[t][1].columns:
            return None, f"unknown column {t}.{c}"
    a, b = tables[ta][1][ca].dropna().astype(str), tables[tb][1][cb].dropna().astype(str)
    if a.empty or b.empty:
        return None, "one side has no values"
    ua, ub = set(a.unique()), set(b.unique())
    overlap = len(ua & ub) / min(len(ua), len(ub))
    if overlap < MIN_JOIN_OVERLAP:
        return None, f"only {overlap:.0%} of values match (need {MIN_JOIN_OVERLAP:.0%})"
    a_unique, b_unique = len(ua) == len(a), len(ub) == len(b)
    card = {(True, True): "one-to-one", (True, False): "one-to-many", (False, True): "many-to-one", (False, False): "many-to-many"}[(a_unique, b_unique)]
    return {"overlap": round(overlap, 4), "cardinality": card, "left_distinct": len(ua), "right_distinct": len(ub)}, None


# ----------------------------------------------------------------------------- hierarchies
def _fd(df, child, parent):
    """Share of child values that map to exactly one parent value (functional dependency child -> parent)."""
    g = df[[child, parent]].dropna().drop_duplicates()
    if g.empty:
        return 0.0
    return float((g.groupby(child)[parent].nunique() == 1).mean())


def verify_hierarchy(tables, table, levels):
    """levels: top -> bottom column names, e.g. ["Region","Country","City"]. -> (evidence, None) or (None, reason)."""
    if table not in tables:
        return None, f"unknown table {table}"
    df = tables[table][1]
    if len(levels) < 2 or any(c not in df.columns for c in levels):
        return None, "needs at least two existing columns"
    steps = []
    for parent, child in zip(levels, levels[1:]):
        if df[parent].nunique() >= df[child].nunique():
            return None, f"{child} must have more distinct values than {parent}"
        c = _fd(df, child, parent)
        if c < MIN_FD_CONSISTENCY:
            return None, f"{child} does not belong to a single {parent} ({c:.0%} consistent)"
        steps.append({"parent": parent, "child": child, "consistency": round(c, 4)})
    return {"steps": steps}, None


def discover_hierarchies(tables):
    """Drill-down paths found directly in the data (functional dependencies), e.g. Region -> Country -> City."""
    out = []
    for tname, (summary, df) in tables.items():
        dims = [c for c, r in summary["roles"].items() if r == "dimension"][:MAX_DIM_COLS]
        dims.sort(key=lambda c: df[c].nunique())
        used = set()
        for top in dims:
            if top in used or df[top].nunique() < 2:
                continue
            chain = [top]
            for nxt in dims:
                if nxt in chain or nxt in used:
                    continue
                if df[nxt].nunique() > df[chain[-1]].nunique() and _fd(df, nxt, chain[-1]) >= MIN_FD_CONSISTENCY:
                    chain.append(nxt)
            if len(chain) >= 2:
                used.update(chain)
                out.append({"table": tname, "levels": chain})
    return out


# ----------------------------------------------------------------------------- formulas
_BRACKET = re.compile(r"\[([^\[\]]+)\]")


class FormulaError(ValueError):
    pass


def formula_columns(expr):
    return list(dict.fromkeys(m.strip() for m in _BRACKET.findall(str(expr))))


def evaluate(expr, values):
    """Evaluate +,-,*,/ over numbers and [Column] references using `values` {column: float}. Raises FormulaError, never executes code."""
    names = {}

    def sub(m):
        col = m.group(1).strip()
        names[f"_v{len(names)}"] = col
        return f"_v{len(names) - 1}"
    py = _BRACKET.sub(sub, str(expr))
    try:
        tree = ast.parse(py.strip(), mode="eval")
    except SyntaxError:
        raise FormulaError("not a valid arithmetic expression")

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id in names:
            v = values.get(names[n.id])
            if v is None or (isinstance(v, float) and math.isnan(v)):
                raise FormulaError(f"no value for [{names[n.id]}]")
            return float(v)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = walk(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = walk(n.left), walk(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if b == 0:
                raise FormulaError("division by zero")
            return a / b
        raise FormulaError("only + - * / numbers and [Column] references are allowed")
    out = walk(tree)
    if not math.isfinite(out):
        raise FormulaError("result is not a finite number")
    return out


def column_totals(df, roles, cols):
    """Aggregate value per referenced column: sum for additive measures, mean for rates / prices / percentages."""
    out = {}
    for c in cols:
        s = df[c].dropna()
        out[c] = float(s.mean() if prof.AVG_RE.search(c) else s.sum()) if len(s) else None
    return out


def verify_metric(tables, m):
    """m: {"name","formula","description"?, "table"?} -> (verified metric dict, None) or (None, reason)."""
    name, formula = str(m.get("name") or "").strip(), str(m.get("formula") or "").strip()
    if not name or not formula:
        return None, "missing name or formula"
    cols = formula_columns(formula)
    if not cols:
        return None, "the formula references no [Column]"
    cands = [m["table"]] if m.get("table") in tables else list(tables)
    for t in cands:
        summary, df = tables[t]
        if not all(c in summary["roles"] and summary["roles"][c] == "measure" for c in cols):
            continue
        try:
            value = evaluate(formula, column_totals(df, summary["roles"], cols))
        except FormulaError as e:
            return None, str(e)
        return {"name": name, "formula": formula, "description": str(m.get("description") or "")[:200], "table": t,
                "columns": cols, "value": round(value, 6)}, None
    missing = [c for c in cols if not any(c in tables[t][0]["roles"] and tables[t][0]["roles"][c] == "measure" for t in tables)]
    return None, f"column(s) not found as numeric measures: {', '.join(missing) or ', '.join(cols)}"


# ----------------------------------------------------------------------------- synonyms
def verify_synonyms(tables, items):
    """Keep term -> column mappings whose column exists and whose term points to exactly one column. Returns (kept, rejected)."""
    by_term, rejected = {}, []
    for it in items:
        term, col, t = norm(it.get("term")), it.get("column"), it.get("table")
        tbl = t if t in tables else next((x for x in tables if col in tables[x][1].columns), None)
        if not term or tbl is None or col not in tables[tbl][1].columns:
            rejected.append({"item": it, "reason": "term or column missing"})
            continue
        by_term.setdefault(term, set()).add((tbl, col))
    kept = []
    for term, targets in by_term.items():
        if len(targets) != 1:
            rejected.append({"item": {"term": term}, "reason": "ambiguous: points to more than one column"})
            continue
        (tbl, col), = targets
        if norm(col) == term:
            continue  # identical to the column name; nothing to add
        kept.append({"term": term, "column": col, "table": tbl})
    return kept, rejected
