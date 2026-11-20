"""3-way intent router for the analyst chat. Pure and deterministic (no LLM, no I/O).

AGGREGATION  math on the data (totals, averages, rankings, variances, trends)        -> pandas only
INSIGHT      qualitative / document questions with no number to compute              -> retrieval only
HYBRID       a number must be computed first, then explained ("why did West drop?")  -> pandas + retrieval

Guard: a question containing math keywords can never be INSIGHT, so it is never answered from retrieved text alone.
"""
import re
from dataclasses import dataclass

AGGREGATION, INSIGHT, HYBRID = "AGGREGATION", "INSIGHT", "HYBRID"

MATH_RE = re.compile(
    r"\b(sum|total|totals|average|avg|mean|median|count|how many|how much|number of|top|bottom|best|worst|highest|lowest|"
    r"maximum|minimum|max|min|most|least|rank\w*|variance|cagr|growth|trend|compare|comparison|share|ratio|margin)\b|%|\bper\b|\bby\b",
    re.I)
WHY_RE = re.compile(r"\b(why|reason|reasons|cause|caused|causes|driver|drivers|explain|behind|because|what happened|due to|root cause)\b", re.I)
CHANGE_RE = re.compile(r"\b(drop|dropped|fall|fell|decline\w*|decrease\w*|increase\w*|rise|rose|grew|growth|spike\w*|dip\w*|change\w*|down|up)\b", re.I)
DOC_RE = re.compile(r"\b(policy|policies|memo|document|documents|note|notes|footnote|summar\w*|overview|describe|recommend\w*|risk\w*|"
                    r"outlook|commentary|insight\w*|observation\w*|takeaway\w*|tell me about|what stands out|key findings?)\b", re.I)


@dataclass(frozen=True)
class Route:
    intent: str
    reason: str


def route(question, known_terms=()):
    """Classify a question. known_terms: column names / category values of the model, used to recognise "why did <metric> ..."."""
    q = str(question or "").lower()
    math, why, doc, change = bool(MATH_RE.search(q)), bool(WHY_RE.search(q)), bool(DOC_RE.search(q)), bool(CHANGE_RE.search(q))
    mentions_data = any(t and t.lower() in q for t in known_terms)

    if why and (math or mentions_data or change):
        return Route(HYBRID, "a 'why' question about a figure: compute the change, then explain it")
    if math and doc:
        return Route(HYBRID, "asks for both a calculation and a qualitative explanation")
    if math:
        return Route(AGGREGATION, "math keyword: answered by exact computation, never by retrieval")
    if why or doc:
        return Route(INSIGHT, "qualitative question")
    if mentions_data:
        return Route(AGGREGATION, "names a column or value of the data")
    return Route(INSIGHT, "no calculation requested")
