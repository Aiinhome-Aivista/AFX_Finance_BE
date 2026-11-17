"""Deterministic dashboard analytics over the full records of a published model's uploaded files.

Ported in spirit from D-Agent's dynamic profiler / core-KPI approach: every number on the dashboard is computed here
from the data with an explicit definition. An LLM may only *narrate* these facts (see analyst_dashboard_service); it never
produces a metric. Pure functions, no DB or Flask access.
"""
import io
import re
from pathlib import Path

ROW_CAP = 500_000
MAX_CATEGORIES = 10
MEASURE_HINTS = ("amount", "value", "revenue", "sales", "cost", "price", "total", "qty", "quantity", "weight", "purity",
                 "profit", "margin", "balance", "debit", "credit", "spend", "budget", "units", "count", "score")
DIMENSION_HINTS = ("name", "customer", "vendor", "supplier", "category", "type", "region", "zone", "department", "product",
                   "item", "account", "status", "group", "segment", "branch", "city", "state", "country", "party")
ID_RE = re.compile(r"(^|[_\s])(id|code|no|number|num|ref|phone|mobile|zip|pin|pincode|sku|invoice|order)($|[_\s])|(_id|_no|_code)$", re.I)
AVG_RE = re.compile(r"pct|percent|ratio|rate|price|margin|score|index|avg|average", re.I)  # not additive: average, never sum
DATE_HINT_RE = re.compile(r"date|time|month|year|period|dt$", re.I)
TABULAR_EXT = {"csv", "xlsx"}


# ----------------------------------------------------------------------------- loading
def _read_csv(path: Path):
    import pandas as pd
    from .parsers import detect_delimiter
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sep = detect_delimiter(text[:200_000])
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False, on_bad_lines="skip", nrows=ROW_CAP)


def load_tables(path: Path, ext: str, display_name: str):
    """Yield (table_name, DataFrame of strings) for a csv/xlsx file. Other file types are not tabular."""
    import pandas as pd
    if ext == "csv":
        yield display_name, _read_csv(path)
    elif ext == "xlsx":
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False, engine="openpyxl", nrows=ROW_CAP)
        for name, df in sheets.items():
            df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed:")]]
            yield (f"{display_name} / {name}" if len(sheets) > 1 else display_name), df


# ----------------------------------------------------------------------------- typing
def _clean_numeric(s):
    import pandas as pd
    t = s.astype(str).str.strip().str.replace(r"[,\s₹$€£]", "", regex=True).str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    t = t.str.replace(r"%$", "", regex=True)
    return pd.to_numeric(t, errors="coerce")


def _clean_dates(s):
    import pandas as pd
    try:
        return pd.to_datetime(s, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return pd.Series([pd.NaT] * len(s), index=s.index)


def classify_columns(df):
    """Return {column: {role, series}} where role in measure|dimension|date|identifier|text|empty and series is typed."""
    import pandas as pd
    out = {}
    n = len(df)
    for col in df.columns:
        name = str(col)
        s = df[col].astype(str).str.strip()
        s = s.where(s != "", other=None)
        nn = s.dropna()
        if nn.empty:
            out[name] = {"role": "empty", "series": s}
            continue
        sample = nn.head(3000)
        num_ratio = _clean_numeric(sample).notna().mean()
        uniq = nn.nunique()
        uniq_ratio = uniq / max(len(nn), 1)
        if num_ratio >= 0.9:
            nums = _clean_numeric(s)
            is_int = (nums.dropna() % 1 == 0).all()
            if ID_RE.search(name) or (is_int and uniq_ratio > 0.95 and len(nn) > 20 and not any(h in name.lower() for h in MEASURE_HINTS)):
                out[name] = {"role": "identifier", "series": s}
            elif DATE_HINT_RE.search(name) and nums.dropna().between(1900, 2100).all() and is_int:
                out[name] = {"role": "dimension", "series": s}  # a bare year / month number: group by it, don't sum it
            else:
                out[name] = {"role": "measure", "series": nums}
            continue
        looks_dated = bool(DATE_HINT_RE.search(name)) or sample.str.contains(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", regex=True).mean() > 0.8
        if looks_dated:
            dts = _clean_dates(s)
            if dts.head(3000).notna().mean() >= 0.9:
                out[name] = {"role": "date", "series": dts}
                continue
        if ID_RE.search(name) and uniq_ratio > 0.9:
            out[name] = {"role": "identifier", "series": s}
        elif 2 <= uniq <= max(MAX_CATEGORIES * 5, int(n * 0.2)) and (uniq_ratio < 0.6 or (uniq < n and any(h in name.lower() for h in DIMENSION_HINTS))):
            out[name] = {"role": "dimension", "series": s}
        else:
            out[name] = {"role": "text", "series": s}
    return out


# ----------------------------------------------------------------------------- formatting
def fmt_number(v, decimals=2):
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.{decimals}f}"


def _score_measure(name, series):
    n = name.lower()
    hint = sum(1 for h in MEASURE_HINTS if h in n)
    nums = series.dropna()
    return (hint, float(nums.abs().sum()) if len(nums) else 0.0)


def _score_dimension(name, series):
    n = name.lower()
    hint = sum(1 for h in DIMENSION_HINTS if h in n)
    uniq = series.dropna().nunique()
    good_card = 1 if 3 <= uniq <= 60 else 0
    return (hint + good_card, -abs(uniq - 12))


def _pretty(name):
    return re.sub(r"\s+", " ", str(name).replace("_", " ")).strip()


# ----------------------------------------------------------------------------- profiling
def profile_table(name, file_ref, df):
    """Profile one table. Returns the table summary plus the typed columns for dashboard building."""
    cols = classify_columns(df)
    roles = {c: v["role"] for c, v in cols.items()}
    summary = {
        "name": name, "file": file_ref, "rows": int(len(df)), "columns": int(len(df.columns)),
        "row_cap_applied": len(df) >= ROW_CAP,
        "measures": [c for c, r in roles.items() if r == "measure"],
        "dimensions": [c for c, r in roles.items() if r == "dimension"],
        "dates": [c for c, r in roles.items() if r == "date"],
        "identifiers": [c for c, r in roles.items() if r == "identifier"],
    }
    return summary, cols


def _date_range(series):
    d = series.dropna()
    if d.empty:
        return None
    return {"from": d.min().date().isoformat(), "to": d.max().date().isoformat()}


def build_dashboard(tables):
    """tables: list of (summary, typed_columns, ) -> kpis, charts, facts. Picks the richest table as the primary one."""
    import pandas as pd
    if not tables:
        return {"kpis": [], "charts": [], "tables": [], "primary_table": None, "date_range": None, "facts": {}}

    def richness(t):
        s = t[0]
        return (len(s["measures"]) > 0, s["rows"] * (1 + len(s["measures"])))

    primary_summary, primary_cols = max(tables, key=richness)
    pname = primary_summary["name"]
    kpis, charts = [], []

    total_rows = sum(t[0]["rows"] for t in tables)
    kpis.append({"id": "total_records", "label": f"Total records ({_pretty(pname)})" if len(tables) > 1 else "Total records",
                 "value": primary_summary["rows"], "display": fmt_number(primary_summary["rows"]),
                 "definition": f"Number of data rows in {pname}." + (f" {total_rows:,} rows across all {len(tables)} tables." if len(tables) > 1 else ""),
                 "source": {"table": pname}})

    measures = sorted(primary_summary["measures"], key=lambda c: _score_measure(c, primary_cols[c]["series"]), reverse=True)
    measures = [m for m in measures if not AVG_RE.search(m)] or measures  # percentages/prices/rates are not summed or ranked by total
    dims = sorted(primary_summary["dimensions"], key=lambda c: _score_dimension(c, primary_cols[c]["series"]), reverse=True)
    dates = primary_summary["dates"]
    df = pd.DataFrame({c: v["series"] for c, v in primary_cols.items()})

    for m in measures[:3]:
        total = float(df[m].sum(skipna=True))
        kpis.append({"id": f"sum_{m}", "label": f"Total {_pretty(m)}", "value": total, "display": fmt_number(total),
                     "definition": f"Sum of '{m}' over {df[m].notna().sum():,} non-empty rows in {pname}.",
                     "source": {"table": pname, "column": m}})
    if measures:
        m = measures[0]
        avg = df[m].mean(skipna=True)
        if avg == avg:
            kpis.append({"id": f"avg_{m}", "label": f"Average {_pretty(m)}", "value": float(avg), "display": fmt_number(avg),
                         "definition": f"Mean of '{m}' over non-empty rows in {pname}.", "source": {"table": pname, "column": m}})

    if dims:
        d0 = dims[0]
        if measures:
            g = df.groupby(d0, dropna=True)[measures[0]].sum().sort_values(ascending=False)
            if len(g):
                kpis.append({"id": f"top_{d0}", "label": f"Top {_pretty(d0)}", "value": str(g.index[0]), "display": str(g.index[0]),
                             "definition": f"'{d0}' with the highest sum of '{measures[0]}' ({fmt_number(g.iloc[0])}).",
                             "source": {"table": pname, "column": d0}})
        kpis.append({"id": f"distinct_{d0}", "label": f"Distinct {_pretty(d0)}", "value": int(df[d0].nunique()), "display": fmt_number(df[d0].nunique()),
                     "definition": f"Number of distinct non-empty values of '{d0}'.", "source": {"table": pname, "column": d0}})

    date_range = None
    if dates:
        date_range = _date_range(primary_cols[dates[0]]["series"])
        if date_range:
            kpis.append({"id": "date_range", "label": "Data period", "value": f"{date_range['from']} – {date_range['to']}",
                         "display": f"{date_range['from']} – {date_range['to']}",
                         "definition": f"Earliest and latest '{dates[0]}' in {pname}.", "source": {"table": pname, "column": dates[0]}})

    # ---- charts
    if dims and measures:
        d0, m0 = dims[0], measures[0]
        g = df.groupby(d0, dropna=True)[m0].sum().sort_values(ascending=False).head(MAX_CATEGORIES)
        charts.append({"id": f"top_{d0}_by_{m0}", "type": "bar", "title": f"Top {len(g)} {_pretty(d0)} by {_pretty(m0)}",
                       "subtitle": f"Sum of {m0} per {d0}", "x_label": _pretty(d0), "y_label": _pretty(m0),
                       "data": [{"label": str(k), "value": round(float(v), 2)} for k, v in g.items()],
                       "definition": f"Sum of '{m0}' grouped by '{d0}', top {len(g)}.", "source": {"table": pname}})
    if dates and measures:
        dcol, m0 = dates[0], measures[0]
        t = df[[dcol, m0]].dropna(subset=[dcol])
        if len(t):
            t = t.assign(_p=t[dcol].dt.to_period("M")).groupby("_p")[m0].sum().sort_index()
            if len(t) >= 2:
                charts.append({"id": f"monthly_{m0}", "type": "line", "title": f"Monthly {_pretty(m0)} trend",
                               "subtitle": f"Sum of {m0} by month of {dcol}", "x_label": "Month", "y_label": _pretty(m0),
                               "data": [{"label": str(p), "value": round(float(v), 2)} for p, v in t.items()][-36:],
                               "definition": f"Sum of '{m0}' per calendar month of '{dcol}'.", "source": {"table": pname}})
    elif dates:
        dcol = dates[0]
        t = df[dcol].dropna().dt.to_period("M").value_counts().sort_index()
        if len(t) >= 2:
            charts.append({"id": f"monthly_records", "type": "line", "title": "Monthly record volume",
                           "subtitle": f"Rows per month of {dcol}", "x_label": "Month", "y_label": "Records",
                           "data": [{"label": str(p), "value": int(v)} for p, v in t.items()][-36:],
                           "definition": f"Row count per calendar month of '{dcol}'.", "source": {"table": pname}})
    pie_dim = dims[1] if len(dims) > 1 else (dims[0] if dims else None)
    if pie_dim:
        if measures:
            g = df.groupby(pie_dim, dropna=True)[measures[0]].sum().sort_values(ascending=False)
            basis = f"sum of {measures[0]}"
        else:
            g = df[pie_dim].value_counts()
            basis = "record count"
        g = g[g > 0]
        if len(g) >= 2:
            top = g.head(6)
            rest = float(g.iloc[6:].sum())
            data = [{"label": str(k), "value": round(float(v), 2)} for k, v in top.items()]
            if rest > 0:
                data.append({"label": "Other", "value": round(rest, 2)})
            charts.append({"id": f"share_{pie_dim}", "type": "pie", "title": f"{_pretty(pie_dim)} share", "subtitle": f"By {basis}",
                           "data": data, "definition": f"{basis.capitalize()} grouped by '{pie_dim}'; the 6 largest, remainder as Other.",
                           "source": {"table": pname}})
    if len(measures) > 1 and dims:
        d0, m1 = dims[0], measures[1]
        g = df.groupby(d0, dropna=True)[m1].sum().sort_values(ascending=False).head(MAX_CATEGORIES)
        if len(g) >= 2 and g.abs().sum() > 0:
            charts.append({"id": f"top_{d0}_by_{m1}", "type": "bar", "title": f"Top {len(g)} {_pretty(d0)} by {_pretty(m1)}",
                           "subtitle": f"Sum of {m1} per {d0}", "x_label": _pretty(d0), "y_label": _pretty(m1),
                           "data": [{"label": str(k), "value": round(float(v), 2)} for k, v in g.items()],
                           "definition": f"Sum of '{m1}' grouped by '{d0}', top {len(g)}.", "source": {"table": pname}})

    facts = {
        "tables": [{k: t[0][k] for k in ("name", "rows", "columns", "measures", "dimensions", "dates")} for t in tables],
        "kpis": {k["label"]: k["display"] for k in kpis},
        "charts": [{"title": c["title"], "top_points": c["data"][:5]} for c in charts],
        "date_range": date_range,
    }
    return {"kpis": kpis, "charts": charts, "tables": [t[0] for t in tables], "primary_table": pname,
            "date_range": date_range, "facts": facts}
