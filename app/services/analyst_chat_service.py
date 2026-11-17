"""Model-scoped, read-only chat for the Analyst dashboard.

Pipeline (D-Agent style "ask your data", hardened):
  question -> PLAN (LLM or rules) -> VALIDATE against the real schema -> EXECUTE with pandas -> ANSWER from the result.

The LLM never writes SQL or code. It only proposes a small JSON query spec (table, filters, group_by, metrics, order, limit)
which is validated column-by-column against the published model's own files and then run in-process, so a question can only
ever read that model's data. Numbers in the final wording must come from the computed result, or a deterministic answer
is returned instead.
"""
import json
import logging
import re
import threading
from collections import OrderedDict

from ..errors import ValidationError
from . import dataset_profiler as prof
from .analyst_dashboard_service import AnalystDashboardService

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
FNS = {"sum", "avg", "min", "max", "count", "nunique"}
NUMERIC_FNS = {"sum", "avg", "min", "max"}
OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "contains"}
BUCKETS = {"month": "M", "year": "Y", "quarter": "Q"}

_CACHE = OrderedDict()  # fingerprint -> {table: (summary, typed DataFrame)}; keeps the last few models in memory
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 3

PLAN_SYSTEM = (
    "You translate a business question into a JSON query over the tables described in SCHEMA. "
    "You cannot run code or SQL. Use ONLY table and column names that appear in SCHEMA, exactly as spelled. "
    "Return ONE JSON object:\n"
    '{"table": str, "filters": [{"column": str, "op": one of "=","!=",">",">=","<","<=","in","contains", "value": any}], '
    '"group_by": [str | {"column": str, "bucket": "month"|"quarter"|"year"}], '
    '"metrics": [{"fn": one of "sum","avg","min","max","count","nunique", "column": str|null, "as": str}], '
    '"order_by": {"by": str, "desc": bool}, "limit": int}\n'
    "Rules: sum/avg/min/max only on measure columns; count with column null counts rows; use group_by for 'by/per/top/each' questions; "
    "order_by.by must be the 'as' name of a metric (never a category column): desc for highest/top, asc for lowest. For 'top N' set limit N; for a time trend group by a date column with a bucket; match category values using the example values in SCHEMA. "
    'If the question cannot be answered from SCHEMA, return {"unanswerable": "short reason"}.'
)


class AnalystChatService:
    def __init__(self, db, cfg, llm=None):
        self.dash = AnalystDashboardService(db, cfg, llm)
        self.llm = llm

    # ------------------------------------------------------------------ data
    def _tables(self, wf):
        files, _ = self.dash._files(wf)
        fp = self.dash._fingerprint(wf, files)
        with _CACHE_LOCK:
            if fp in _CACHE:
                _CACHE.move_to_end(fp)
                return _CACHE[fp]
        import pandas as pd
        tables = {}
        for f in files:
            if f["ext"] not in prof.TABULAR_EXT:
                continue
            path = self.dash._path(f)
            if not path.exists():
                continue
            try:
                for tname, df in prof.load_tables(path, f["ext"], f["original_name"]):
                    if len(df.columns) == 0 or len(df) == 0:
                        continue
                    summary, cols = prof.profile_table(tname, f["id"], df)
                    typed = pd.DataFrame({c: v["series"] for c, v in cols.items()})
                    summary["roles"] = {c: v["role"] for c, v in cols.items()}
                    tables[tname] = (summary, typed)
            except Exception:
                log.exception("chat: could not load file %s", f["id"])
        with _CACHE_LOCK:
            _CACHE[fp] = tables
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        return tables

    @staticmethod
    def _schema(tables):
        out = []
        for name, (s, df) in tables.items():
            cols = []
            for c, role in s["roles"].items():
                if role in ("empty", "text"):
                    continue
                item = {"name": c, "role": role}
                if role == "dimension":
                    item["example_values"] = [str(v) for v in df[c].dropna().value_counts().head(12).index]
                elif role == "date":
                    r = df[c].dropna()
                    if len(r):
                        item["range"] = f"{r.min().date()} to {r.max().date()}"
                cols.append(item)
            out.append({"table": name, "rows": s["rows"], "columns": cols})
        return out

    # ------------------------------------------------------------------ validate
    @staticmethod
    def _col(name, s, roles, what):
        if not isinstance(name, str) or name not in roles:
            raise ValidationError(f"Unknown column '{name}' in {what}.")
        return name

    def validate(self, spec, tables):
        if not isinstance(spec, dict):
            raise ValidationError("The query plan was not an object.")
        tname = spec.get("table")
        if tname not in tables:
            if len(tables) == 1:
                tname = next(iter(tables))
            else:
                raise ValidationError(f"Unknown table '{tname}'.")
        summary, _ = tables[tname]
        roles = summary["roles"]
        clean = {"table": tname, "filters": [], "group_by": [], "metrics": []}

        for f in (spec.get("filters") or [])[:8]:
            col, op = self._col(f.get("column"), summary, roles, "filter"), f.get("op", "=")
            if op not in OPS:
                raise ValidationError(f"Unsupported filter operator '{op}'.")
            if roles[col] in ("empty", "text") and op not in ("=", "!=", "contains", "in"):
                raise ValidationError(f"Column '{col}' cannot be compared with '{op}'.")
            if "value" not in f:
                raise ValidationError("A filter is missing its value.")
            clean["filters"].append({"column": col, "op": op, "value": f["value"]})

        for g in (spec.get("group_by") or [])[:3]:
            if isinstance(g, str):
                g = {"column": g}
            col = self._col(g.get("column"), summary, roles, "group_by")
            bucket = g.get("bucket")
            if isinstance(bucket, str) and bucket.strip().lower() in ("", "null", "none", "any", "n/a", "na"):
                bucket = None
            if bucket is not None:
                if bucket not in BUCKETS or roles[col] != "date":
                    raise ValidationError(f"Cannot bucket '{col}' by '{bucket}'.")
            elif roles[col] in ("measure", "empty"):
                raise ValidationError(f"Cannot group by the numeric column '{col}'.")
            clean["group_by"].append({"column": col, "bucket": bucket})

        used = set()
        for m in (spec.get("metrics") or [])[:6]:
            fn = str(m.get("fn", "")).lower()
            if fn not in FNS:
                raise ValidationError(f"Unsupported metric '{fn}'.")
            col = m.get("column")
            if col is None:
                if fn != "count":
                    raise ValidationError(f"Metric '{fn}' needs a column.")
            else:
                self._col(col, summary, roles, "metric")
                if fn in NUMERIC_FNS and roles[col] != "measure":
                    raise ValidationError(f"'{fn}' only works on numeric columns; '{col}' is {roles[col]}.")
            label = re.sub(r"\s+", " ", str(m.get("as") or (f"{fn} of {col}" if col else "records"))).strip()[:60]
            while label in used:
                label += "_"
            used.add(label)
            clean["metrics"].append({"fn": fn, "column": col, "as": label})
        if not clean["metrics"]:
            clean["metrics"] = [{"fn": "count", "column": None, "as": "records"}]

        ob = spec.get("order_by") or {}
        valid_by = {m["as"] for m in clean["metrics"]} | {g["column"] for g in clean["group_by"]}
        clean["order_by"] = {"by": ob.get("by") if ob.get("by") in valid_by else None, "desc": bool(ob.get("desc", True))}
        try:
            clean["limit"] = max(1, min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        except (TypeError, ValueError):
            clean["limit"] = DEFAULT_LIMIT
        return clean


    # ------------------------------------------------------------------ grounding of an (untrusted) LLM plan
    _LOW = re.compile(r"\b(lowest|least|minimum|min|smallest|worst|bottom|fewest|weakest)\b")
    _HIGH = re.compile(r"\b(highest|most|maximum|max|largest|best|top|biggest|greatest|strongest)\b")

    def ground(self, question, spec):
        """Make a schema-valid plan faithful to the question: drop invented filters, take ranking direction from the words."""
        q, notes = question.lower(), []
        kept = []
        for f in spec["filters"]:
            vals = f["value"] if isinstance(f["value"], list) else [f["value"]]
            if all(str(v).strip().lower() in q for v in vals):
                kept.append(f)
            else:
                notes.append(f"ignored filter {f['column']} {f['op']} {f['value']!r} (not asked for)")
        spec["filters"] = kept
        by = spec["order_by"]["by"]
        metric_names = {m["as"] for m in spec["metrics"]}
        wants_low, wants_high = bool(self._LOW.search(q)), bool(self._HIGH.search(q))
        if spec["group_by"] and by in metric_names and wants_low != wants_high:
            spec["order_by"]["desc"] = wants_high
        if re.match(r"\s*(which|who)\b", q) and not spec["group_by"]:
            raise ValidationError("a 'which ...' question needs a grouping, but the plan had none")
        m = re.search(r"\b(?:top|bottom|first|lowest|highest)\s+(\d+)\b", q)
        if m:
            spec["limit"] = max(1, min(int(m.group(1)), MAX_LIMIT))
        elif re.match(r"\s*(which|who)\b", q) and spec["group_by"] and wants_low != wants_high:
            spec["limit"] = 1
        return spec, notes

    # ------------------------------------------------------------------ execute
    @staticmethod
    def _filter(df, roles, f):
        import pandas as pd
        s, op, v, role = df[f["column"]], f["op"], f["value"], roles[f["column"]]
        if role == "measure":
            vals = [float(x) for x in (v if isinstance(v, list) else [v])]
            if op == "in":
                return s.isin(vals)
            return {"=": s == vals[0], "!=": s != vals[0], ">": s > vals[0], ">=": s >= vals[0], "<": s < vals[0], "<=": s <= vals[0]}.get(op, s.notna())
        if role == "date":
            ts = pd.to_datetime(v, errors="coerce", format="mixed")
            if pd.isna(ts):
                raise ValidationError(f"'{v}' is not a valid date for '{f['column']}'.")
            return {"=": s.dt.normalize() == ts.normalize(), "!=": s.dt.normalize() != ts.normalize(), ">": s > ts, ">=": s >= ts,
                    "<": s < ts, "<=": s <= ts}.get(op, s.notna())
        low = s.astype("string").str.lower()
        if op == "in":
            return low.isin([str(x).lower() for x in (v if isinstance(v, list) else [v])])
        if op == "contains":
            return low.str.contains(str(v).lower(), regex=False, na=False)
        if op in ("=", "!="):
            m = low == str(v).lower()
            return m if op == "=" else ~m
        raise ValidationError(f"Operator '{op}' is not valid for '{f['column']}'.")

    def execute(self, spec, tables):
        import pandas as pd
        summary, df = tables[spec["table"]]
        roles = summary["roles"]
        mask = pd.Series(True, index=df.index)
        for f in spec["filters"]:
            mask &= self._filter(df, roles, f).fillna(False)
        sub = df[mask]
        matched = int(len(sub))

        keys, key_names = [], []
        for g in spec["group_by"]:
            s = sub[g["column"]]
            if g["bucket"]:
                s = s.dt.to_period(BUCKETS[g["bucket"]]).astype("string")
                name = f"{g['column']} ({g['bucket']})"
            else:
                name = g["column"]
            keys.append(s.rename(name))
            key_names.append(name)

        def agg(frame_or_group, m, grouped):
            col, fn = m["column"], m["fn"]
            if col is None:
                return frame_or_group.size() if grouped else len(sub)
            ser = frame_or_group[col]
            return getattr(ser, {"avg": "mean", "nunique": "nunique"}.get(fn, fn))()

        if keys:
            grp = sub.groupby(keys, dropna=True)
            res = pd.DataFrame({m["as"]: agg(grp, m, True) for m in spec["metrics"]}).reset_index()
            total_groups = int(len(res))
        else:
            res = pd.DataFrame([{m["as"]: agg(sub, m, False) for m in spec["metrics"]}])
            total_groups = 1

        by = spec["order_by"]["by"]
        is_time = any(g["bucket"] or roles[g["column"]] == "date" for g in spec["group_by"])
        if keys:
            metric_names = {m["as"] for m in spec["metrics"]}
            if by in metric_names:  # ranking ("top", "highest", "lowest") always follows a metric
                res = res.sort_values(by, ascending=not spec["order_by"]["desc"], kind="stable")
            elif is_time:  # trends read chronologically
                res = res.sort_values(key_names, kind="stable")
            else:  # ordering a category by its label is never what an analyst means; rank by the first metric
                res = res.sort_values(spec["metrics"][0]["as"], ascending=False, kind="stable")
        res = res.head(spec["limit"])
        rows = [[self._cell(v) for v in r] for r in res.itertuples(index=False, name=None)]
        return {"columns": [str(c) for c in res.columns], "rows": rows, "matched_rows": matched, "total_groups": total_groups,
                "group_columns": key_names, "metric_columns": [m["as"] for m in spec["metrics"]], "is_time": is_time}

    @staticmethod
    def _cell(v):
        import pandas as pd
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return None
        if isinstance(v, pd.Timestamp):
            return v.strftime("%Y-%m-%d")
        if hasattr(v, "item"):
            v = v.item()
        if isinstance(v, float):
            return round(v, 2)
        return v if isinstance(v, (int, str, bool)) else str(v)

    # ------------------------------------------------------------------ planners
    def _llm_plan(self, question, schema, history):
        ctx = [{"role": h["role"], "content": str(h["content"])[:300]} for h in history[-4:]]
        user = json.dumps({"SCHEMA": schema, "RECENT_CONVERSATION": ctx, "QUESTION": question}, default=str)
        return self.llm.complete_json(PLAN_SYSTEM, user, max_tokens=700)

    @staticmethod
    def _tokens(s):
        return set(re.findall(r"[a-z0-9]+", str(s).lower()))

    def _best_col(self, phrase, summary, want):
        """Fuzzy-match a phrase to a column of the given roles: all phrase words must appear; shortest column name wins."""
        stop = {"the", "of", "a", "an", "total", "my", "our", "all"}
        pt = self._tokens(phrase) - stop
        if not pt:
            return None
        best, key = None, None
        for c, role in summary["roles"].items():
            if role not in want:
                continue
            ct = self._tokens(c)
            cover = len(pt & ct) / len(pt)
            if cover < 1.0 and not (cover >= 0.5 and len(pt) > 2):
                continue
            k = (cover, -len(ct))
            if key is None or k > key:
                best, key = c, k
        return best

    @staticmethod
    def _fn_for(col, default="sum"):
        return "avg" if (default == "sum" and prof.AVG_RE.search(col)) else default

    def _rule_plan(self, question, tables):
        """Deterministic planner for the common question shapes. Returns None when the question is not one of them."""
        q = re.sub(r"^(please |can you |tell me |show me |what is |what's |what are )+", "", question.lower().strip().rstrip("?")).strip()
        tname = max(tables, key=lambda t: tables[t][0]["rows"])
        summary = tables[tname][0]
        meas, dims = ("measure",), ("dimension", "identifier")

        if re.search(r"\bhow many (rows|records|entries|transactions)\b", q) or q in ("count", "total records"):
            return {"table": tname, "metrics": [{"fn": "count", "column": None, "as": "records"}]}

        m = re.match(r"(?:which|what)\s+(.+?)\s+(?:has|have|had|is|are|with|gives?|generates?)\s+(?:the\s+)?"
                     r"(highest|lowest|most|least|maximum|minimum|best|worst|largest|smallest)\s+(?:average\s+|avg\s+|total\s+)?(.+)", q)
        if m:
            d, me = self._best_col(m.group(1), summary, dims), self._best_col(m.group(3), summary, meas)
            if d and me:
                fn = "avg" if re.search(r"\b(average|avg)\b", q) else self._fn_for(me)
                low = m.group(2) in ("lowest", "least", "minimum", "worst", "smallest")
                name = f"{fn} {me}"
                return {"table": tname, "group_by": [d], "metrics": [{"fn": fn, "column": me, "as": name}],
                        "order_by": {"by": name, "desc": not low}, "limit": 1}

        m = re.search(r"(top|bottom)\s+(\d+)?\s*(.+?)\s+(?:by|on|in terms of)\s+(.+)", q)
        if m:
            d, me = self._best_col(m.group(3), summary, dims), self._best_col(m.group(4), summary, meas)
            if d and me:
                fn = self._fn_for(me)
                return {"table": tname, "group_by": [d], "metrics": [{"fn": fn, "column": me, "as": f"{fn} {me}"}],
                        "order_by": {"by": f"{fn} {me}", "desc": m.group(1) == "top"}, "limit": int(m.group(2) or 10)}

        m = re.search(r"(trend|over time|month by month|monthly|by month|per month|each month)", q)
        if m:
            dcol = next((c for c, r in summary["roles"].items() if r == "date"), None)
            tail = re.sub(r"\b(how|did|has|have|does|the|trend|over time|month by month|monthly|by month|per month|each month|of|in|for|changed?)\b", " ", q)
            me = self._best_col(tail, summary, meas) or next((c for c, r in summary["roles"].items() if r == "measure"), None)
            if dcol and me:
                fn = self._fn_for(me)
                return {"table": tname, "group_by": [{"column": dcol, "bucket": "month"}],
                        "metrics": [{"fn": fn, "column": me, "as": f"{fn} {me}"}], "limit": 36}

        m = re.search(r"(total|sum|average|avg|mean|max|maximum|highest|min|minimum|lowest)\s+(?:of\s+)?(.+?)(?:\s+(?:by|per|for each)\s+(.+))?$", q)
        if m:
            fn = {"total": "sum", "sum": "sum", "average": "avg", "avg": "avg", "mean": "avg", "max": "max", "maximum": "max",
                  "highest": "max", "min": "min", "minimum": "min", "lowest": "min"}[m.group(1)]
            me = self._best_col(m.group(2), summary, meas)
            if me:
                fn = self._fn_for(me, fn)
                plan = {"table": tname, "metrics": [{"fn": fn, "column": me, "as": f"{fn} {me}"}]}
                if m.group(3):
                    d = self._best_col(m.group(3), summary, dims)
                    if not d:
                        return None
                    plan.update({"group_by": [d], "order_by": {"by": plan["metrics"][0]["as"], "desc": True}})
                return plan
        return None

    # ------------------------------------------------------------------ answer
    @staticmethod
    def _fmt(v):
        return prof.fmt_number(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)

    @staticmethod
    def _label(name):
        t = re.sub(r"\s+", " ", str(name).replace("_", " ")).strip()
        t = re.sub(r"^(sum|avg|min|max) ", lambda m: {"sum": "total ", "avg": "average ", "min": "minimum ", "max": "maximum "}[m.group(1)], t)
        return re.sub(r"\s+(inr)$", "", t)

    def _det_answer(self, r, spec):
        if not r["rows"]:
            return "No records match that question." if r["matched_rows"] == 0 else "No results."
        gcols, mcols = r["group_columns"], r["metric_columns"]
        if not gcols:
            row = r["rows"][0]
            body = "; ".join(f"{self._label(c)}: {self._fmt(v)}" for c, v in zip(r["columns"], row))
            return f"{body} (over {r['matched_rows']:,} matching records)."
        gi = len(gcols)
        ob = spec["order_by"]
        top_metric = spec["metrics"][0]["as"]
        direction = "highest" if (ob.get("by") in (None, top_metric) and ob.get("desc", True)) else "lowest"
        if r["is_time"]:
            direction = "chronological"

        def line(row):
            return f"{' / '.join(str(x) for x in row[:gi])}: " + ", ".join(f"{self._label(c)} {self._fmt(v)}" for c, v in zip(mcols, row[gi:]))

        if len(r["rows"]) == 1 and r["total_groups"] > 1:
            row = r["rows"][0]
            return (f"{' / '.join(str(x) for x in row[:gi])} has the {direction} {self._label(mcols[0])}: {self._fmt(row[gi])} "
                    f"(out of {r['total_groups']:,} {self._label(gcols[0])} values).")
        head = "; ".join(line(row) for row in r["rows"][:6])
        order = "in time order" if direction == "chronological" else f"{direction} first"
        more = f" Showing {min(6, len(r['rows']))} of {r['total_groups']:,} (full table below)." if r["total_groups"] > 6 else ""
        return f"By {self._label(gcols[0])}, {order}: {head}.{more}"

    @staticmethod
    def _chart(result):
        g, m = result["group_columns"], result["metric_columns"]
        if len(g) != 1 or not m or len(result["rows"]) < 2:
            return None
        data = [{"label": str(r[0]), "value": r[1]} for r in result["rows"] if isinstance(r[1], (int, float))]
        if len(data) < 2:
            return None
        is_time = bool(result.get("is_time"))
        return {"type": "line" if is_time else "bar", "title": f"{m[0]} by {g[0]}", "subtitle": "", "data": data}

    # ------------------------------------------------------------------ main
    def ask(self, user_id, role, workflow_id, question, history=None):
        question = re.sub(r"\s+", " ", str(question or "")).strip()
        if not question:
            raise ValidationError("Ask a question about the data.")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationError(f"Keep the question under {MAX_QUESTION_CHARS} characters.")
        history = [h for h in (history or []) if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]

        wf, _ = self.dash.resolve_model(user_id, role, workflow_id)  # server-side access check, same as the dashboard
        tables = self._tables(wf)
        if not tables:
            return {"answer": "There is no readable tabular data for this published model yet, so I can't answer data questions. "
                              "Ask the Data Architect to upload the CSV/Excel file again.", "unanswerable": True, "follow_ups": [], "source": "rules"}
        schema = self._schema(tables)

        spec, plan_src, plan_err, notes = None, "rules", None, []
        rp = self._rule_plan(question, tables)
        if rp:
            try:
                spec = self.validate(rp, tables)
            except ValidationError as e:
                plan_err = str(e)
        if spec is None and self.llm:
            try:
                raw = self._llm_plan(question, schema, history)
                if raw.get("unanswerable"):
                    return {"answer": f"I can't answer that from this model's data: {raw['unanswerable']}", "unanswerable": True,
                            "follow_ups": self._suggestions(tables), "source": "llm"}
                spec, notes = self.ground(question, self.validate(raw, tables))
                plan_src = "llm"
            except Exception as e:  # untrusted/unusable plan or LLM down: say so, never guess
                plan_err = str(e)
                log.info("chat plan via LLM failed (%s)", e)
        if spec is None:
            return {"answer": "I couldn't turn that into a reliable query over this model's data"
                              + (f" ({plan_err})" if plan_err else "") + ". Try naming the columns, e.g. one of the suggestions below.",
                    "unanswerable": True, "follow_ups": self._suggestions(tables), "source": plan_src}

        try:
            result = self.execute(spec, tables)
        except ValidationError:
            raise
        except Exception:
            log.exception("chat execution failed")
            return {"answer": "That query could not be run on this data. Try rephrasing it.", "unanswerable": True,
                    "follow_ups": self._suggestions(tables), "source": plan_src}
        return {"answer": self._det_answer(result, spec), "unanswerable": False,
                "table": {"columns": result["columns"], "rows": result["rows"]},
                "chart": self._chart(result), "spec": spec, "matched_rows": result["matched_rows"], "notes": notes,
                "follow_ups": self._suggestions(tables, spec), "source": plan_src}

    @staticmethod
    def _suggestions(tables, spec=None):
        s, _ = (tables[spec["table"]] if spec else max(tables.values(), key=lambda t: t[0]["rows"]))
        roles = s["roles"]
        additive = [c for c, r in roles.items() if r == "measure" and not prof.AVG_RE.search(c)]
        meas = additive or [c for c, r in roles.items() if r == "measure"]
        dims = [c for c, r in roles.items() if r == "dimension"]

        def pretty(c):
            return re.sub(r"\s+", " ", c.replace("_", " "))

        used_m = {m["column"] for m in spec["metrics"]} if spec else set()
        used_g = {g["column"] for g in spec["group_by"]} if spec else set()
        out = []
        m0 = next((m for m in meas if m not in used_m), meas[0] if meas else None)
        d0 = next((d for d in dims if d not in used_g), dims[0] if dims else None)
        if m0 and d0:
            out.append(f"Top 5 {pretty(d0)} by {pretty(m0)}")
        if m0 and any(r == "date" for r in roles.values()):
            out.append(f"How did {pretty(m0)} trend month by month?")
        if m0:
            out.append(f"What is the total {pretty(m0)}?")
        out.append("How many records are there?")
        return out[:3]
