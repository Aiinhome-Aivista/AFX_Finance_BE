"""Evidence for INSIGHT / HYBRID answers. Every item is {"source", "text", "citation"}.

Phase 1 provider: facts computed with pandas from the model's own tables (works with no documents and no vector store).
Document passages (ChromaDB) plug in later through the same item shape, so the chat and synthesizer do not change.
Nothing here is written by an LLM.
"""
import re

from . import dataset_profiler as prof

STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are", "was", "were", "what", "which", "who", "how", "why", "did", "does",
        "me", "my", "our", "and", "or", "with", "by", "this", "that", "it", "do", "show", "tell", "give", "about", "data", "has", "have"}
MAX_FACTS = 5


def _tok(s):
    return {t for t in re.findall(r"[a-z0-9]+", str(s).lower()) if t not in STOP and len(t) > 1}


def data_facts(tables):
    """All fact items for the tables: schema, totals, top contributors, and the monthly pattern."""
    f = prof.fmt_number
    out = []
    for tname, (summary, df) in tables.items():
        roles = summary["roles"]
        meas = [c for c, r in roles.items() if r == "measure"]
        add = [c for c in meas if not prof.AVG_RE.search(c)] or meas
        dims = [c for c, r in roles.items() if r == "dimension"]
        dates = [c for c, r in roles.items() if r == "date"]
        cite = f"data: {tname}"
        out.append({"source": "data", "citation": cite, "text":
                    f"{tname} has {summary['rows']:,} records. Measures: {', '.join(meas) or 'none'}. Categories: {', '.join(dims) or 'none'}."})
        for m in add[:4]:
            s = df[m].dropna()
            if len(s):
                out.append({"source": "data", "citation": cite,
                            "text": f"{m}: total {f(s.sum())}, average {f(s.mean())}, minimum {f(s.min())}, maximum {f(s.max())}."})
        for d in dims[:4]:
            for m in add[:3]:
                g = df.groupby(d)[m].sum().sort_values(ascending=False)
                if len(g) >= 2 and g.sum():
                    top = ", ".join(f"{k} {f(v)} ({v / g.sum():.0%})" for k, v in g.head(5).items())
                    out.append({"source": "data", "citation": cite, "text":
                                f"{m} by {d}, highest first: {top}. Lowest: {g.index[-1]} {f(g.iloc[-1])}."})
        for dcol in dates[:1]:
            for m in add[:3]:
                g = df.dropna(subset=[dcol]).groupby(df[dcol].dt.to_period("M"))[m].sum()
                if len(g) >= 2:
                    last, prev = g.iloc[-1], g.iloc[-2]
                    chg = f"{(last - prev) / abs(prev):+.1%}" if prev else "n/a"
                    out.append({"source": "data", "citation": cite, "text":
                                f"Monthly {m}: latest month {g.index[-1]} {f(last)} vs {g.index[-2]} {f(prev)} ({chg}); "
                                f"peak {g.idxmax()} {f(g.max())}; lowest {g.idxmin()} {f(g.min())}."})
    return out


def retrieve(question, tables, documents=None, k=MAX_FACTS, overview=False):
    """Top-k evidence for a question: document passages first (when any exist), then the best-matching data facts."""
    q = _tok(question)
    facts = data_facts(tables)

    def score(item):
        return len(q & _tok(item["text"]))
    ranked = [x for x in sorted(facts, key=score, reverse=True) if score(x) > 0] or (facts[:3] if overview else [])
    docs = list(documents or [])[:k]
    return docs + ranked[:max(k - len(docs), 2)]
