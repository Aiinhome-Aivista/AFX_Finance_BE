"""Model Eval — pre-commit validation of Business Architect inputs before they reach the K graph template.

[MODEL-EVAL | Created 2026-11-28 11:29 IST (+05:30)]

Why this exists
---------------
In the Business Input stage the architect's free-text answers are turned (by the LLM or the rule-based extractor) into
graph operations and applied to the client's copy of the Finance / FMCG K graph template. `graph_ops.validate_sequence`
only guarantees *structural* validity (the node exists, the name is not an exact duplicate, ...). It does not judge
whether the change is *good*: relevant to the template, well defined, not a near-duplicate, free of personal data,
mathematically sane, and not damaging to graph integrity.

Model Eval fills that gap. It runs inside the existing Business Input chatbox
[MODEL-EVAL | updated 2026-11-28 12:05 IST — no separate window]: every chatbot answer / chat edit is evaluated
before `GraphService.create_version`. Ops that FAIL are held back (not applied); everything else is applied as before.
The verdict, held changes, reasons and suggested fixes are written into the chatbot's reply. Set
MODEL_EVAL_ENFORCE=false for report-only mode (issues reported, nothing held).

The service is pure (no DB, no Flask) so it is fully unit-testable. Each run is recorded for audit by
`repositories/model_evals.py`.

Report shape (abridged)
-----------------------
{
  "template": {"key": "finance", "name": ...}, "verdict": "pass|warn|fail|blocked", "score": 0-100,
  "dimensions": {"relevance": {...}, "quality": {...}, "consistency": {...}, "safety": {...}, "integrity": {...}},
  "findings": [{"code", "severity": "info|warn|fail", "dimension", "message", "suggestion", "op_index"}],
  "ops": [{"index", "op", "description", "status": "approved|held|structural_reject", "reasons": [...]}],
  "impact": {"before": {...}, "after": {...}, "delta": {...}},
  "summary": {"ops_total", "ops_approved", "ops_held", "fails", "warns", "infos"}, "enforced": bool
}
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .graph_ops import GraphState, apply_op, describe_op, norm, validate_graph, validate_op
from .guardrail_service import DOMAIN_TAXONOMY, DomainGuardrailService

# ----------------------------------------------------------------------------------------------------------------------
# Template profiles  [MODEL-EVAL | 2026-11-28 11:29 IST]
# Each K graph template (Finance, FMCG) carries its own vocabulary and sub-domains. Relevance of a new entity is judged
# against the active template's vocabulary + the active domain's keywords + the words already used in the client graph.
# ----------------------------------------------------------------------------------------------------------------------
_FINANCE_CORE_VOCAB = {
    "account", "accounts", "ledger", "gl", "general ledger", "journal", "invoice", "payment", "revenue", "expense",
    "cost center", "cost centre", "profit center", "profit centre", "legal entity", "currency", "period", "fiscal",
    "accrual", "allocation", "budget", "actuals", "variance", "margin", "ebit", "ebitda", "cash", "asset", "liability",
    "equity", "tax", "audit", "approval", "price", "volume", "rate", "customer", "supplier", "vendor", "product", "sku",
    "brand", "channel", "region", "market", "kpi", "report", "forecast", "plan", "scenario", "driver", "rebate",
    "discount", "promotion", "trade", "receivable", "payable", "inventory", "capex", "opex", "cogs", "headcount",
    "payroll", "depreciation", "amortization", "amortisation", "provision", "reserve", "consolidation",
    "intercompany", "dividend", "interest", "loan", "debt", "bank", "fx", "hedge", "treasury", "valuation", "roi",
    "npv", "irr", "wacc", "ratio", "erp", "master data", "hierarchy", "chart of accounts", "coa", "p&l", "pnl",
    "balance sheet", "cash flow", "statement", "close", "reconciliation", "posting", "billing", "credit", "debit",
    "cost", "costing", "profit", "profitability", "spend", "target", "version", "calendar", "cycle", "control",
    "entity", "segment", "business unit", "allocation key", "overhead", "gross", "net", "sales",
}
for _meta in DOMAIN_TAXONOMY.values():
    _FINANCE_CORE_VOCAB |= set(_meta["allowed_keywords"])

_FMCG_SUBDOMAINS: Dict[str, Dict[str, Any]] = {
    "fmcg-demand-supply-planning": {
        "name": "Demand & Supply Planning (S&OP / IBP)",
        "keywords": {"demand plan", "demand planning", "demand sensing", "forecast accuracy", "forecast bias", "mape",
                     "s&op", "ibp", "supply plan", "production plan", "mrp", "safety stock", "days of cover",
                     "consensus forecast", "baseline forecast", "statistical forecast"},
    },
    "fmcg-trade-promotion-rgm": {
        "name": "Trade Promotion & Revenue Growth Management",
        "keywords": {"trade promotion", "tpm", "tpo", "promo", "promotion", "scheme", "claim", "rgm",
                     "revenue growth management", "price pack architecture", "ppa", "mix", "trade spend", "uplift",
                     "baseline volume", "incremental volume", "promo roi", "list price", "net price"},
    },
    "fmcg-route-to-market": {
        "name": "Route-to-Market & Distribution",
        "keywords": {"route-to-market", "rtm", "distributor", "stockist", "wholesaler", "beat", "beat plan",
                     "van sales", "salesman", "numeric distribution", "weighted distribution", "general trade",
                     "modern trade", "e-commerce", "secondary sales", "primary sales", "dms"},
    },
    "fmcg-retail-execution": {
        "name": "Retail Execution & Perfect Store",
        "keywords": {"outlet", "store", "retailer", "planogram", "shelf share", "share of shelf",
                     "on-shelf availability", "osa", "out-of-stock", "oos", "merchandiser", "perfect store", "pos",
                     "sell-out", "tertiary sales", "retail audit", "shopper", "display"},
    },
    "fmcg-manufacturing-procurement": {
        "name": "Manufacturing & Procurement",
        "keywords": {"plant", "line", "oee", "yield", "wastage", "bom", "bill of materials", "batch", "raw material",
                     "packaging material", "co-packer", "procurement", "purchase order", "supplier", "vendor",
                     "conversion cost", "capacity"},
    },
    "fmcg-logistics-distribution": {
        "name": "Logistics, Warehousing & Fulfilment",
        "keywords": {"warehouse", "depot", "cfa", "dc", "distribution center", "distribution centre", "logistics",
                     "freight", "transport", "otif", "fill rate", "case fill rate", "lead time", "service level",
                     "shelf life", "expiry", "fefo", "fifo", "pallet", "case", "returns", "damages"},
    },
}
_FMCG_CORE_VOCAB = {
    "sku", "brand", "category", "sub-category", "pack size", "pack", "consumer", "shopper", "market share",
    "retail audit", "nielsen", "price", "margin", "gross margin", "cogs", "inventory", "sales", "volume", "value",
    "channel", "customer", "region", "territory", "kpi", "forecast", "target", "promotion", "trade", "product",
    "portfolio", "innovation", "launch", "listing", "delisting", "assortment",
}
for _sd in _FMCG_SUBDOMAINS.values():
    _FMCG_CORE_VOCAB |= _sd["keywords"]

TEMPLATE_PROFILES: Dict[str, Dict[str, Any]] = {
    "finance": {
        "key": "finance",
        "name": "Finance K Graph Template",
        "description": "Enterprise finance & EPM ontology (F1–F8): planning, reporting/G2N, costing, working capital, "
                       "consolidation, valuation, risk & controls, EPM platforms.",
        "vocabulary": _FINANCE_CORE_VOCAB,
        "sub_domains": {m["slug"]: {"name": m["name"], "keywords": set(m["allowed_keywords"])}
                        for m in DOMAIN_TAXONOMY.values()},
    },
    "fmcg": {
        "key": "fmcg",
        "name": "FMCG K Graph Template",
        "description": "Fast-moving consumer goods: demand/supply planning, trade promotion & RGM, route-to-market, "
                       "retail execution, manufacturing & procurement, logistics.",
        "vocabulary": _FMCG_CORE_VOCAB,
        "sub_domains": _FMCG_SUBDOMAINS,
    },
}

# ----------------------------------------------------------------------------------------------------------------------
# Rule tables  [MODEL-EVAL | 2026-11-28 11:29 IST]
# ----------------------------------------------------------------------------------------------------------------------
GENERIC_NAMES = {
    "data", "info", "information", "thing", "things", "stuff", "item", "items", "misc", "miscellaneous", "other",
    "others", "new", "new entity", "new node", "entity", "node", "test", "testing", "temp", "tmp", "sample", "table",
    "unknown", "tbd", "na", "n/a", "none", "general", "details", "value", "values", "field", "object", "record",
}
GENERIC_EDGE_LABELS = {"relates to", "related to", "relates", "related", "link", "links", "linked", "connects",
                       "connected to", "has", "is", "associated with", "association", ""}
PLACEHOLDER_DEFINITION_RE = re.compile(r"^(client-defined entity|client entity:)", re.I)
HEDGE_RE = re.compile(r"\b(maybe|not sure|i think|i guess|probably|perhaps|sort of|kind of|roughly|it depends|"
                      r"depends|sometimes|possibly|might be|could be)\b", re.I)
STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "by", "with", "at", "from", "is", "are", "be",
    "this", "that", "these", "those", "it", "its", "as", "we", "our", "us", "they", "their", "which", "who", "what",
    "when", "where", "how", "per", "each", "all", "any", "some", "into", "via", "used", "use", "uses", "using",
    "client", "entity", "defined", "based", "level", "type", "name", "id", "code", "date", "number", "no",
}
SENSITIVE_ATTRIBUTE_RE = re.compile(
    r"\b(ssn|social security|password|passwd|pin code|cvv|credit card|card number|aadhaar|aadhar|pan number|"
    r"passport|date of birth|dob|personal phone|personal email|home address|biometric|salary of)\b", re.I)

# Personal data / secret patterns that must never be written into a shared K graph.
PII_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "an e-mail address"),
    # Phone numbers must carry a separator or a '+' prefix so plain amounts (e.g. 2500000000) are not flagged.
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}(?:[\s-]?\(?\d{2,5}\)?){2,4}|\(?\d{3,5}\)?[\s-]\d{3,5}[\s-]\d{4,5})(?![\w.])"),
     "a phone number"),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "a payment card number"),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?=[A-Z0-9]*\d{4})[A-Z0-9]{11,30}\b"), "an IBAN / bank account number"),
    ("pan_in", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "an Indian PAN"),
    # Aadhaar is written in 4-4-4 groups; unspaced 12-digit runs are more likely amounts.
    ("aadhaar", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"), "an Aadhaar number"),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,})\b"),
     "an API key / secret"),
    ("password", re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.I), "a password"),
]

SEVERITY_PENALTY = {"fail": 35, "warn": 12, "info": 3}
DIMENSIONS = {
    # key: (label, weight, description)
    "relevance": ("Template & domain relevance", 0.25, "Does the input belong to the active K graph template and domain?"),
    "quality": ("Definition quality", 0.25, "Naming, definitions, attributes and formulas are precise and usable."),
    "consistency": ("Consistency & de-duplication", 0.20, "No near-duplicates, conflicting renames or redundant links."),
    "safety": ("Data safety", 0.15, "No personal data, secrets or sensitive attributes enter the shared graph."),
    "integrity": ("Graph integrity", 0.15, "The change keeps the graph connected, valid and governed."),
}
PASS_THRESHOLD = 80.0
FAIL_THRESHOLD = 55.0
NEAR_DUP_FAIL = 0.90
NEAR_DUP_WARN = 0.78
HUB_DEGREE = 3


def enforce_enabled() -> bool:
    """MODEL_EVAL_ENFORCE (default true). When false the gate runs in shadow mode: it reports but holds nothing."""
    return str(os.environ.get("MODEL_EVAL_ENFORCE", "true")).strip().lower() not in ("0", "false", "no", "off")


# ----------------------------------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------------------------------
def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9&]+", (text or "").lower()) if t not in STOPWORDS and len(t) > 1]


def _vocab_hits(text: str, vocab: set) -> List[str]:
    """Vocabulary terms present in `text` (multi-word terms by phrase, single words by token)."""
    low = (text or "").lower()
    toks = set(re.findall(r"[a-z0-9&]+", low))
    hits = []
    for term in vocab:
        if " " in term or "-" in term or "&" in term:
            if term in low:
                hits.append(term)
        elif term in toks or (term + "s") in toks:
            hits.append(term)
    return sorted(set(hits))


def _similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # plural / singular variants ("Rebate Agreement" vs "Rebate Agreements")
    if na.rstrip("s") == nb.rstrip("s"):
        return 0.97
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(_tokens(a)), set(_tokens(b))
    jac = (len(ta & tb) / len(ta | tb)) if (ta and tb) else 0.0
    return max(seq, jac)


def _iter_strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _iter_strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _iter_strings(x)


def _luhn_ok(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if len(d) < 13:
        return False
    s = 0
    for i, x in enumerate(reversed(d)):
        if i % 2:
            x *= 2
            if x > 9:
                x -= 9
        s += x
    return s % 10 == 0


def detect_sensitive(text: str) -> List[Dict[str, str]]:
    """Return [{kind, label, sample}] for personal data / secrets found in `text`. Samples are masked."""
    found, seen = [], set()
    for kind, rx, label in PII_PATTERNS:
        for m in rx.finditer(text or ""):
            val = m.group(0)
            if kind == "card" and not _luhn_ok(val):
                continue
            if kind == "phone":
                digits = re.sub(r"\D", "", val)
                # ignore years, amounts and short numeric codes that look phone-ish
                if len(digits) < 10 or len(digits) > 13:
                    continue
            if kind in seen:
                continue
            seen.add(kind)
            masked = val[:2] + "…" + val[-2:] if len(val) > 6 else "…"
            found.append({"kind": kind, "label": label, "sample": masked})
    return found


def check_formula(formula: str) -> List[Tuple[str, str, str]]:
    """Return [(severity, code, message)] for a single formula string."""
    out = []
    f = (formula or "").strip()
    if not f:
        return out
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for ch in f:
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in pairs.values():
            if not stack or stack.pop() != ch:
                stack = ["!"]
                break
    if stack:
        out.append(("fail", "formula_unbalanced", f"Formula “{f[:80]}” has unbalanced brackets."))
    if re.search(r"/\s*0(?:\.0+)?(?![\d.])", f):
        out.append(("fail", "formula_div_zero", f"Formula “{f[:80]}” divides by zero."))
    if re.search(r"[+\-*/^]\s*[*/^]", f) or re.search(r"[+*/^]\s*$", f):
        out.append(("fail", "formula_bad_operator", f"Formula “{f[:80]}” has a dangling or doubled operator."))
    if not re.search(r"[=+\-*/^×÷]|\b(sum|avg|average|min|max|count|if|ratio|of)\b", f, re.I):
        out.append(("warn", "formula_not_expression",
                    f"“{f[:80]}” does not read like a calculation (no operator, '=' or function)."))
    return out


# ----------------------------------------------------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------------------------------------------------
class ModelEvalService:
    """Stateless evaluator. All public methods are static/class methods."""

    # ---------------------------------------------------------------- template resolution
    @staticmethod
    def resolve_template(domain_slug: Optional[str], explicit: Optional[str] = None) -> str:
        """`explicit` (finance|fmcg) wins; otherwise FMCG domain slugs map to fmcg and everything else to finance."""
        if explicit and str(explicit).strip().lower() in TEMPLATE_PROFILES:
            return str(explicit).strip().lower()
        s = str(domain_slug or "").strip().lower().replace("_", "-")
        if s.startswith("fmcg") or s.startswith("supply-chain") or s in _FMCG_SUBDOMAINS:
            return "fmcg"
        return "finance"

    # ---------------------------------------------------------------- input-level checks
    @classmethod
    def _input_findings(cls, text: str, template_key: str, domain_slug: Optional[str],
                        state: Optional[GraphState]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        clean = (text or "").strip()
        if not clean:
            return out

        # 1) Personal data / secrets -> blocks the whole input
        for hit in detect_sensitive(clean):
            out.append(cls._f("fail", "sensitive_data", "safety",
                              f"The input appears to contain {hit['label']} ({hit['sample']}).",
                              "Remove personal data and secrets; describe the business rule instead of the record.",
                              blocking=True))

        # 2) Domain boundary (re-uses the existing hard-coded guardrail; cross-finance-domain redirects are not
        #    meaningful for the FMCG template, so they are ignored there)
        active = [{"name": n["name"]} for n in state.nodes.values()] if state and state.nodes else None
        g = DomainGuardrailService.validate_domain_input(domain_slug or "", clean, active_graph_nodes=active)
        reason = g.get("reason", "")
        if not g.get("is_valid"):
            if template_key == "fmcg" and reason.startswith("cross_domain_redirect"):
                pass
            elif reason.startswith("cross_domain_redirect"):
                out.append(cls._f("warn", "cross_domain", "relevance", g.get("suggested_guidance") or
                                  "The input looks like it belongs to another domain.",
                                  "Switch to the matching domain or rephrase in this domain's terms."))
            else:
                out.append(cls._f("fail", "out_of_scope", "relevance",
                                  g.get("suggested_guidance") or "The input is outside the template's scope.",
                                  "Describe a business process, entity, rule or KPI of this domain.", blocking=True))

        # 3) Template vocabulary fit (input-level)
        own = _vocab_hits(clean, TEMPLATE_PROFILES[template_key]["vocabulary"])
        other_key = "fmcg" if template_key == "finance" else "finance"
        other = _vocab_hits(clean, TEMPLATE_PROFILES[other_key]["vocabulary"])
        if len(clean.split()) >= 6 and not own and len(other) >= 2:
            out.append(cls._f("warn", "template_mismatch", "relevance",
                              f"The input uses {TEMPLATE_PROFILES[other_key]['name']} vocabulary "
                              f"({', '.join(other[:4])}) but none from the active {TEMPLATE_PROFILES[template_key]['name']}.",
                              "Confirm you are enhancing the right template."))

        # 4) Specificity / confidence
        hedges = sorted({m.group(0).lower() for m in HEDGE_RE.finditer(clean)})
        if hedges:
            out.append(cls._f("warn", "low_confidence_language", "quality",
                              f"Hedging language detected ({', '.join(hedges[:3])}); the K graph will record it as fact.",
                              "State the rule as it is actually applied, or skip until it is confirmed."))

        # 5) Numeric sanity
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", clean):
            if float(m.group(1)) > 100 and re.search(r"\b(margin|discount|rebate|rate|share|accuracy|fill|otif|"
                                                      r"utili[sz]ation|probability)\b", clean, re.I):
                out.append(cls._f("warn", "implausible_percentage", "quality",
                                  f"{m.group(0)} is above 100% for a bounded ratio.",
                                  "Check the figure; margins, discounts and service levels are usually ≤ 100%."))
                break
        for m in re.finditer(r"\b(dso|dpo|dio|days of cover|lead time)\b[^.\d]{0,20}(\d{3,})\s*days?", clean, re.I):
            if int(m.group(2)) > 365:
                out.append(cls._f("warn", "implausible_days", "quality",
                                  f"{m.group(1).upper()} of {m.group(2)} days exceeds a year.",
                                  "Confirm the unit (days vs. hours) and the figure."))
                break
        return out

    # ---------------------------------------------------------------- op-level checks
    @classmethod
    def _relevance_for(cls, text: str, template_key: str, domain_slug: Optional[str], graph_vocab: set) -> Dict[str, Any]:
        prof = TEMPLATE_PROFILES[template_key]
        hits = set(_vocab_hits(text, prof["vocabulary"]))
        sub = prof["sub_domains"].get(str(domain_slug or "").replace("_", "-"))
        if sub:
            hits |= set(_vocab_hits(text, sub["keywords"]))
        graph_hits = set(_tokens(text)) & graph_vocab
        other_key = "fmcg" if template_key == "finance" else "finance"
        other_hits = set(_vocab_hits(text, TEMPLATE_PROFILES[other_key]["vocabulary"])) - hits
        return {"template_hits": sorted(hits), "graph_hits": sorted(graph_hits), "other_hits": sorted(other_hits)}

    @classmethod
    def _op_findings(cls, idx: int, op: str, p: Dict[str, Any], scratch: GraphState, template_key: str,
                     domain_slug: Optional[str], graph_vocab: set, rationale: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        F = lambda sev, code, dim, msg, sug="": out.append(cls._f(sev, code, dim, msg, sug, op_index=idx))  # noqa: E731

        # sensitive data hidden inside an op payload (e.g. an LLM-copied note)
        payload_text = " ".join(_iter_strings(p))
        for hit in detect_sensitive(payload_text):
            F("fail", "sensitive_data_in_change", "safety", f"This change would store {hit['label']} in the graph.",
              "Remove the value from the note/definition.")

        if op == "add_node":
            name = p.get("name", "")
            if norm(name) in {norm(x) for x in GENERIC_NAMES} or len(norm(name)) < 3:
                F("fail", "generic_name", "quality", f"“{name}” is too generic to be a governed entity.",
                  "Use the business name your team uses, e.g. 'Distributor Claim' or 'Rebate Agreement'.")
            elif name.isupper() and len(name) > 6 and " " in name:
                F("info", "naming_convention", "quality", f"“{name}” is all caps; the template uses Title Case.",
                  "Rename to Title Case.")
            elif name == name.lower() and len(name) > 3:
                F("info", "naming_convention", "quality", f"“{name}” is lower case; the template uses Title Case.",
                  "Rename to Title Case.")
            # near-duplicates (graph_ops only catches exact normalised duplicates)
            best_key, best_sim = None, 0.0
            for k, n in scratch.nodes.items():
                s = max(_similarity(name, n.get("name", "")), _similarity(name, n.get("ref_name") or ""))
                if s > best_sim:
                    best_key, best_sim = k, s
            if best_key and best_sim >= NEAR_DUP_FAIL:
                F("fail", "near_duplicate", "consistency",
                  f"“{name}” is a near-duplicate of existing entity “{scratch.nodes[best_key]['name']}” "
                  f"(similarity {best_sim:.2f}).",
                  f"Update “{scratch.nodes[best_key]['name']}” (add attributes / a note) instead of adding a new entity.")
            elif best_key and best_sim >= NEAR_DUP_WARN:
                F("warn", "possible_duplicate", "consistency",
                  f"“{name}” is similar to “{scratch.nodes[best_key]['name']}” (similarity {best_sim:.2f}).",
                  "Confirm these are genuinely different concepts.")
            # definition
            d = (p.get("definition") or "").strip()
            if not d or PLACEHOLDER_DEFINITION_RE.match(d):
                F("warn", "missing_definition", "quality", f"“{name}” has no business definition.",
                  "Add one sentence describing what it is and who owns it.")
            elif len(d) < 25:
                F("warn", "thin_definition", "quality", f"The definition of “{name}” is very short ({len(d)} chars).",
                  "Expand the definition so analysts can interpret mapped data.")
            elif set(_tokens(d)) <= set(_tokens(name)):
                F("warn", "circular_definition", "quality", f"The definition of “{name}” only repeats its name.",
                  "Describe purpose, grain and owner instead of restating the name.")
            # attributes
            attrs = p.get("attributes") or []
            if p.get("kind", "entity") == "entity" and not attrs:
                F("warn", "no_attributes", "quality", f"“{name}” has no attributes, so telemetry cannot be mapped to it.",
                  "List the fields your systems capture for it.")
            for a in attrs:
                if SENSITIVE_ATTRIBUTE_RE.search(a):
                    F("warn", "sensitive_attribute", "safety", f"Attribute “{a}” on “{name}” may hold personal data.",
                      "Model it as a masked/derived attribute or leave it out of the shared graph.")
            for f in p.get("formulas") or []:
                for sev, code, msg in check_formula(f):
                    F(sev, code, "quality", msg, "Correct the expression before adding it.")
            # relevance
            rel = cls._relevance_for(" ".join([name, d, " ".join(attrs)]), template_key, domain_slug, graph_vocab)
            if not rel["template_hits"] and not rel["graph_hits"]:
                if len(rel["other_hits"]) >= 1:
                    other = "FMCG" if template_key == "finance" else "Finance"
                    F("warn", "template_mismatch", "relevance",
                      f"“{name}” matches the {other} template vocabulary ({', '.join(rel['other_hits'][:3])}) "
                      f"rather than the active one.", "Check that the entity belongs in this template.")
                else:
                    F("warn", "low_relevance", "relevance",
                      f"“{name}” shares no vocabulary with the template, the domain or the current graph.",
                      "Rename using domain terms or describe how it connects to existing entities.")

        elif op == "update_node":
            key = p.get("node_key")
            n = scratch.nodes.get(key) or {}
            if "name" in p:
                for k, o in scratch.nodes.items():
                    if k != key and _similarity(p["name"], o.get("name", "")) >= NEAR_DUP_FAIL:
                        F("fail", "rename_collision", "consistency",
                          f"Renaming “{n.get('name')}” to “{p['name']}” collides with “{o['name']}”.",
                          "Pick a distinct name or merge the two entities.")
                        break
                if n.get("origin") == "reference":
                    F("info", "reference_alias", "consistency",
                      f"“{n.get('name')}” is a template entity; the template name is kept as its reference alias.")
            if "definition" in p:
                if len(p["definition"]) < 25:
                    F("warn", "thin_definition", "quality", "The replacement definition is very short.",
                      "Keep the governed definition or expand the new one.")
                if n.get("origin") == "reference":
                    F("warn", "governed_definition_override", "integrity",
                      f"This overrides the governed template definition of “{n.get('name')}”.",
                      "Prefer appending a client note so the template definition is preserved.")
            if p.get("remove_attributes") and n.get("origin") == "reference":
                F("warn", "reference_attribute_removed", "integrity",
                  f"Removes template attribute(s) {', '.join(p['remove_attributes'][:4])} from “{n.get('name')}”.",
                  "Telemetry mapped to those attributes will no longer resolve.")
            for a in p.get("add_attributes") or []:
                if SENSITIVE_ATTRIBUTE_RE.search(a):
                    F("warn", "sensitive_attribute", "safety", f"Attribute “{a}” may hold personal data.",
                      "Model it as a masked/derived attribute or leave it out of the shared graph.")
            for f in p.get("add_formulas") or []:
                for sev, code, msg in check_formula(f):
                    F(sev, code, "quality", msg, "Correct the expression before adding it.")

        elif op == "remove_node":
            key = p.get("node_key")
            n = scratch.nodes.get(key) or {}
            deg = sum(1 for (s, t, _l) in scratch.edges if key in (s, t))
            if n.get("origin") == "reference" and deg >= HUB_DEGREE:
                F("fail", "reference_hub_removal", "integrity",
                  f"Removing template hub entity “{n.get('name')}” would drop {deg} relationships.",
                  "Hub entities of the template are held for explicit review — state the exclusion as a note instead.")
            elif n.get("origin") == "reference":
                F("warn", "reference_removal", "integrity",
                  f"Removes template entity “{n.get('name')}” from your graph.")

        elif op in ("add_edge", "update_edge"):
            label = (p.get("label") or "").strip().lower()
            s_name = scratch.nodes.get(p.get("source"), {}).get("name", p.get("source"))
            t_name = scratch.nodes.get(p.get("target"), {}).get("name", p.get("target"))
            if label in GENERIC_EDGE_LABELS:
                sev = "info" if "connect new entity" in (rationale or "").lower() else "warn"
                F(sev, "generic_relationship", "quality",
                  f"“{s_name}” → “{t_name}” uses the generic label '{label or 'relates to'}'.",
                  "Use a business verb such as 'feeds', 'rolls up to', 'is approved by', 'allocates to'.")
            elif len(label.split()) > 6:
                F("info", "verbose_relationship", "quality", f"Relationship label '{label}' is long.",
                  "Keep labels to a short verb phrase.")
            if op == "add_edge":
                for (s, t, l) in scratch.edges:
                    if s == p.get("target") and t == p.get("source") and l.strip().lower() == label:
                        F("warn", "reverse_duplicate", "consistency",
                          f"The reverse relationship “{t_name}” —{label}→ “{s_name}” already exists.",
                          "Keep a single direction to avoid circular traversal.")
                        break

        elif op == "remove_edge":
            e = scratch.edges.get((p.get("source"), p.get("target"), p.get("label")))
            if e and e.get("origin") == "reference":
                F("warn", "reference_edge_removal", "integrity",
                  f"Removes template relationship '{p.get('label')}'.")
        return out

    # ---------------------------------------------------------------- the gate
    @classmethod
    def evaluate(cls, text: str, state: GraphState, raw_ops: List[Dict[str, Any]], domain: Optional[Dict[str, Any]] = None,
                 template_key: Optional[str] = None, reference_core_keys: Optional[set] = None,
                 version_number: int = 1, enforce: Optional[bool] = None, source: str = "answer",
                 llm: Any = None) -> Dict[str, Any]:
        """Evaluate an architect input and its candidate graph ops.

        Returns a report that also carries `approved_ops` (raw ops safe to hand to `validate_sequence` /
        `create_version`) and `held_ops`. Ops that fail *structural* validation are passed through untouched so the
        existing duplicate/rejection reporting in ChatService keeps working exactly as before.
        """
        domain = domain or {}
        slug = domain.get("slug") or domain.get("key") or ""
        tkey = cls.resolve_template(slug, template_key)
        enforce = enforce_enabled() if enforce is None else bool(enforce)
        state = state or GraphState()
        raw_ops = [o for o in (raw_ops or []) if isinstance(o, dict)]

        graph_vocab = set()
        for n in state.nodes.values():
            graph_vocab |= set(_tokens(n.get("name", "")))
            for a in (n.get("attributes") or [])[:20]:
                graph_vocab |= set(_tokens(a))

        findings = cls._input_findings(text, tkey, slug, state)
        input_blocked = any(f.get("blocking") for f in findings)

        scratch = state.copy()
        op_rows: List[Dict[str, Any]] = []
        approved, held = [], []
        held_refs: set = set()   # normalised names/keys of held new nodes (to catch dependants)

        for idx, raw in enumerate(raw_ops):
            op, payload = raw.get("op"), raw.get("payload") or {}
            refs = {norm(str(payload.get(k))) for k in ("source", "target", "node_key") if payload.get(k)}
            # Dependants of a held new entity are held too. This is checked *before* validate_op because
            # GraphState.find() fuzzy-matches and could silently re-attach the edge to a similar existing entity.
            if op != "add_node" and refs & held_refs:
                op_rows.append({"index": idx, "op": op, "description": f"{op} (depends on a held change)",
                                "status": "held", "reasons": ["Depends on a change that Model Eval held back."]})
                held.append(raw)
                continue
            res = validate_op(scratch, op, payload)
            if not res.ok:
                # structural rejection: let the existing pipeline report it (duplicates etc.)
                op_rows.append({"index": idx, "op": op, "description": f"{op}: {'; '.join(res.errors)[:200]}",
                                "status": "structural_reject", "reasons": res.errors})
                approved.append(raw)
                continue

            op_f = cls._op_findings(idx, op, res.payload, scratch, tkey, slug, graph_vocab, raw.get("rationale") or "")
            findings.extend(op_f)
            desc = describe_op(scratch, op, res.payload)
            fails = [f for f in op_f if f["severity"] == "fail"]
            if (fails or input_blocked) and enforce:
                reasons = [f["message"] for f in fails] or ["The input was blocked by Model Eval."]
                op_rows.append({"index": idx, "op": op, "description": desc, "status": "held", "reasons": reasons})
                held.append(raw)
                if op == "add_node":
                    held_refs |= {norm(res.payload.get("name", "")), norm(res.payload.get("node_key", "")),
                                  norm(str(payload.get("name") or "")), norm(str(payload.get("node_key") or ""))} - {""}
                continue
            op_rows.append({"index": idx, "op": op, "description": desc, "status": "approved",
                            "reasons": [f["message"] for f in op_f if f["severity"] != "info"]})
            approved.append(raw)
            apply_op(scratch, op, res.payload, version_number, "user")

        # ---- graph impact simulation (before vs after the approved ops)
        core = reference_core_keys or set()
        before_v = validate_graph(state, core)
        after_v = validate_graph(scratch, core)
        impact = cls._impact(state, scratch, before_v, after_v)
        if impact["delta"]["new_errors"]:
            for e in impact["delta"]["new_errors"]:
                findings.append(cls._f("fail", f"graph_{e['code']}", "integrity", e["message"],
                                       "Adjust the change so the graph stays valid."))
        if impact["delta"]["isolated"] > 0:
            findings.append(cls._f("warn", "new_isolated_entities", "integrity",
                                   f"{impact['delta']['isolated']} entity(ies) would have no relationships.",
                                   "Say how the new entity connects to the existing process."))
        if impact["delta"]["components"] > 0:
            findings.append(cls._f("warn", "graph_fragmented", "integrity",
                                   f"The graph would split into {impact['after']['components']} disconnected parts.",
                                   "Link the new part to the existing model."))

        # ---- optional LLM judge (advisory only; never holds an op on its own)
        judge = None
        if llm is not None and (text or "").strip():
            judge = cls._llm_judge(llm, text, tkey, domain, raw_ops)
            if judge and judge.get("scores"):
                low = [k for k, v in judge["scores"].items() if isinstance(v, (int, float)) and v <= 2]
                if low:
                    findings.append(cls._f("warn", "llm_judge_low", "quality",
                                           f"LLM judge rated {', '.join(low)} as low. {judge.get('rationale', '')[:200]}",
                                           judge.get("suggestion") or ""))

        dims = cls._score_dimensions(findings)
        score = round(sum(dims[k]["score"] * DIMENSIONS[k][1] for k in DIMENSIONS), 1)
        n_fail = sum(1 for f in findings if f["severity"] == "fail")
        n_warn = sum(1 for f in findings if f["severity"] == "warn")
        if input_blocked:
            verdict = "blocked"
        elif n_fail or score < FAIL_THRESHOLD or (raw_ops and held and not [r for r in op_rows if r["status"] == "approved"]):
            verdict = "fail"
        elif n_warn or score < PASS_THRESHOLD:
            verdict = "warn"
        else:
            verdict = "pass"
        # [MODEL-EVAL | 2026-11-28 12:05 IST] keep the score consistent with the verdict shown in the chatbox:
        # one hard failure only dents one weighted dimension, so a FAIL/BLOCKED input is capped below the fail line.
        if verdict in ("fail", "blocked"):
            score = min(score, round(FAIL_THRESHOLD - 1, 1))

        prof = TEMPLATE_PROFILES[tkey]
        return {
            "template": {"key": tkey, "name": prof["name"]},
            "domain": {"slug": slug, "name": domain.get("name")},
            "source": source,
            "enforced": enforce,
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "input_excerpt": (text or "")[:280],
            "verdict": verdict,
            "score": score,
            "dimensions": dims,
            "findings": [{k: v for k, v in f.items() if k != "blocking"} for f in findings],
            "ops": op_rows,
            "impact": impact,
            "llm_judge": judge,
            "summary": {
                "ops_total": len(raw_ops),
                "ops_approved": sum(1 for r in op_rows if r["status"] == "approved"),
                "ops_held": sum(1 for r in op_rows if r["status"] == "held"),
                "ops_structural_reject": sum(1 for r in op_rows if r["status"] == "structural_reject"),
                "fails": n_fail, "warns": n_warn, "infos": sum(1 for f in findings if f["severity"] == "info"),
            },
            # consumed by the caller; stripped from API payloads via `public()`
            "approved_ops": approved,
            "held_ops": held,
        }

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _f(severity, code, dimension, message, suggestion="", op_index=None, blocking=False):
        f = {"severity": severity, "code": code, "dimension": dimension, "message": message, "suggestion": suggestion,
             "op_index": op_index}
        if blocking:
            f["blocking"] = True
        return f

    @staticmethod
    def _score_dimensions(findings):
        dims = {}
        for k, (label, weight, desc) in DIMENSIONS.items():
            pen = sum(SEVERITY_PENALTY[f["severity"]] for f in findings if f["dimension"] == k)
            dims[k] = {"label": label, "weight": weight, "score": float(max(0, 100 - pen)),
                       "findings": sum(1 for f in findings if f["dimension"] == k)}
        return dims

    @staticmethod
    def _impact(before: GraphState, after: GraphState, bv: Dict[str, Any], av: Dict[str, Any]) -> Dict[str, Any]:
        def iso(v):
            return sum(1 for w in v["warnings"] if w["code"] == "isolated_entity")

        def gen(v):
            for w in v["warnings"]:
                if w["code"] == "generic_labels":
                    m = re.match(r"(\d+)", w["message"])
                    return int(m.group(1)) if m else 0
            return 0

        before_err = {(e["code"], e["message"]) for e in bv["errors"]}
        new_errors = [e for e in av["errors"] if (e["code"], e["message"]) not in before_err and e["code"] != "too_small"]
        b = {"nodes": len(before.nodes), "edges": len(before.edges), "components": bv["stats"]["components"],
             "isolated": iso(bv), "generic_labels": gen(bv)}
        a = {"nodes": len(after.nodes), "edges": len(after.edges), "components": av["stats"]["components"],
             "isolated": iso(av), "generic_labels": gen(av)}
        return {"before": b, "after": a,
                "delta": {"nodes": a["nodes"] - b["nodes"], "edges": a["edges"] - b["edges"],
                          "components": a["components"] - b["components"], "isolated": a["isolated"] - b["isolated"],
                          "generic_labels": a["generic_labels"] - b["generic_labels"], "new_errors": new_errors}}

    @staticmethod
    def _llm_judge(llm, text, tkey, domain, raw_ops):
        """Advisory rubric from the configured LLM. Any failure returns None (rules remain authoritative)."""
        try:
            prof = TEMPLATE_PROFILES[tkey]
            system = ("You are a strict model-governance reviewer for an enterprise knowledge graph. You evaluate a "
                      "business architect's input before it is added to the graph template. Be concise and objective.")
            user = json.dumps({
                "TEMPLATE": prof["name"], "TEMPLATE_SCOPE": prof["description"],
                "DOMAIN": (domain or {}).get("name"), "ARCHITECT_INPUT": text[:2000],
                "PROPOSED_GRAPH_CHANGES": raw_ops[:20],
                "RUBRIC": {"domain_fit": "1-5", "clarity": "1-5", "specificity": "1-5", "graph_consistency": "1-5"},
                "OUTPUT": {"scores": {"domain_fit": 0, "clarity": 0, "specificity": 0, "graph_consistency": 0},
                           "rationale": "one sentence", "suggestion": "one sentence"},
            }, ensure_ascii=False)
            out = llm.complete_json(system, user, 400)
            scores = {k: max(1, min(5, int(v))) for k, v in (out.get("scores") or {}).items()
                      if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit())}
            if not scores:
                return None
            return {"engine": getattr(llm, "name", "llm"), "scores": scores,
                    "rationale": str(out.get("rationale") or "")[:400], "suggestion": str(out.get("suggestion") or "")[:300]}
        except Exception:  # noqa: BLE001 — the judge is advisory; never break the gate
            return None

    @staticmethod
    def public(report: Dict[str, Any]) -> Dict[str, Any]:
        """API-safe copy of a report (drops the raw op lists the gate hands back to the caller)."""
        return {k: v for k, v in (report or {}).items() if k not in ("approved_ops", "held_ops")}

    @staticmethod
    def compact(report: Dict[str, Any]) -> Dict[str, Any]:
        """Small summary stored on chat messages."""
        r = report or {}
        return {"verdict": r.get("verdict"), "score": r.get("score"), "template": (r.get("template") or {}).get("key"),
                "enforced": r.get("enforced"), "summary": r.get("summary"),
                "held": [{"description": o["description"], "reasons": o["reasons"][:2]}
                         for o in r.get("ops", []) if o["status"] == "held"][:5],
                "top_findings": [f["message"] for f in r.get("findings", []) if f["severity"] in ("fail", "warn")][:4]}

    @staticmethod
    def ack_lines(report: Dict[str, Any]) -> str:
        """Plain-text lines appended to the chatbot acknowledgement."""
        r = report or {}
        s = r.get("summary") or {}
        # No graph change and nothing blocking (e.g. "skip", "same") -> keep the chat quiet.
        if not s.get("ops_total") and not s.get("fails"):
            return ""
        # [MODEL-EVAL | 2026-11-28 12:40 IST] Plain-language chat output for non-technical users.
        # No scores, codes or jargon in the chat: one simple headline, then at most a few everyday-language points.
        # Similar points are merged ("…for “A” and “B”"). The technical wording is still kept in the stored report.
        findings = r.get("findings", [])
        held_idx = {o["index"] for o in r.get("ops", []) if o["status"] == "held"}
        dependants = sum(1 for o in r.get("ops", []) if o["status"] == "held"
                         and o["description"].endswith("(depends on a held change)"))
        # skip warnings about items that were not added anyway — they only add noise
        findings = [f for f in findings if f["severity"] == "fail" or f.get("op_index") not in held_idx]
        # count only the "real" held items; skipped links to them get a single note below
        added, held = s.get("ops_approved", 0), max(0, s.get("ops_held", 0) - dependants)
        verdict = r.get("verdict")

        if verdict == "blocked":
            head = "Quick check: nothing was saved."
        elif held:
            head = (f"Quick check: {held} change{'s were' if held != 1 else ' was'} not added"
                    + (f" ({added} other{'s were' if added != 1 else ' was'} added)." if added else "."))
        elif verdict == "warn":
            head = f"Quick check: done — {added} change{'s' if added != 1 else ''} added. A few things to look at:"
        else:
            head = f"Quick check: all good — {added} change{'s' if added != 1 else ''} added."
        lines = [head]

        # fails first (they explain what was not added), then warnings; info is never shown in the chat
        shown = [f for f in findings if f["severity"] == "fail"] + [f for f in findings if f["severity"] == "warn"]
        groups: Dict[str, Dict[str, Any]] = {}
        for f in shown:
            names = re.findall(r"“([^”]+)”", f.get("message") or "")
            g = groups.setdefault(f["code"], {"f": f, "names": [], "others": []})
            if names and names[0] not in g["names"]:
                g["names"].append(names[0])
            if len(names) > 1 and names[1] not in g["others"]:
                g["others"].append(names[1])
        for code, g in list(groups.items())[:4]:
            text = ModelEvalService._plain(code, g["f"], g["names"], g["others"])
            if text:
                lines.append(f"- {text}")
        if dependants:
            lines.append("- Links to anything that was not added were skipped too.")

        if not r.get("enforced") and s.get("fails"):
            lines.append("(The check is in report-only mode, so your changes were saved anyway.)")
        if held or verdict == "blocked":
            lines.append("Just fix this and send your answer again.")
        return "\n".join(lines)

    @staticmethod
    def _plain(code: str, f: Dict[str, Any], names: List[str], others: List[str]) -> str:
        """[MODEL-EVAL | 2026-11-28 12:40 IST] One everyday-language sentence per kind of finding."""
        def q(xs):
            xs = [f"“{x}”" for x in xs]
            return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else "this item"
        n, o, msg = q(names), q(others), f.get("message") or ""
        if code in ("sensitive_data", "sensitive_data_in_change"):
            m = re.search(r"(?:contain|store) (.+?)(?: \(| in the graph|\.)", msg)
            what = m.group(1) if m else "personal details"
            return f"Your message includes {what}. For privacy we don't save personal details. Please remove it."
        table = {
            "near_duplicate": f"{n} is already in the model as {o}. We didn't add it twice — tell us what to add to {o} instead.",
            "possible_duplicate": f"{n} sounds a lot like {o}. Please make sure they are really different things.",
            "rename_collision": "The new name is already used by something else in the model. Please pick a different name.",
            "generic_name": f"The name {n} is too vague. Please use the name your team actually uses.",
            "missing_definition": f"{n} has no description yet. Add one short sentence saying what it is.",
            "thin_definition": f"The description of {n} is very short. A bit more detail will help others understand it.",
            "circular_definition": f"The description of {n} only repeats its name. Say what it is used for.",
            "no_attributes": f"{n} has no details listed yet (like ID, owner or amount), so data can't be linked to it.",
            "sensitive_attribute": f"A detail on {n} could hold personal information. It's safer to leave it out.",
            "formula_unbalanced": "A formula has a missing bracket. Please check it.",
            "formula_div_zero": "A formula divides by zero, which can't be calculated. Please check it.",
            "formula_bad_operator": "A formula has an extra or missing symbol (like + or /). Please check it.",
            "formula_not_expression": "Something given as a formula doesn't look like a calculation.",
            "template_mismatch": (f"{n} seems to belong to a different business area than this model."
                                  if names else "Your answer seems to be about a different business area than this model."),
            "low_relevance": f"It's not clear how {n} relates to this part of the business. Please say how it connects.",
            "cross_domain": "This sounds like it belongs to a different finance area. You may want to switch areas.",
            "out_of_scope": "This doesn't seem to be about this part of the business.",
            "governed_definition_override": (f"You replaced the standard description of {n}. That's fine if intended — "
                                             "otherwise just add your detail as an extra note."),
            "reference_attribute_removed": f"Some standard details were removed from {n}. Data linked to them will stop showing.",
            "reference_hub_removal": (f"{n} is a key part of the standard model, so we kept it. "
                                      "If you don't use it, just say so and we'll note that instead."),
            "reference_removal": f"{n} (a standard item) was removed from your model.",
            "reference_edge_removal": "A standard link between two items was removed.",
            "generic_relationship": ("Some links only say “relates to”. Saying how things connect "
                                     "(for example “feeds” or “rolls up to”) makes the model clearer."),
            "reverse_duplicate": "This link already exists the other way round, so it isn't needed.",
            "low_confidence_language": "You sounded unsure (for example “maybe”). We saved it as fact — please correct it later if needed.",
            "implausible_percentage": "A percentage above 100% looks unusual here. Please double-check the number.",
            "implausible_days": "A number of days longer than a year looks unusual. Please double-check it.",
            "new_isolated_entities": "Something new isn't connected to anything yet. Tell us how it links to the rest.",
            "graph_fragmented": "This would leave part of the model cut off from the rest.",
            "llm_judge_low": "Your answer was a little unclear. Adding a bit more detail will help.",
        }
        if code.startswith("graph_"):
            return "This change would break the structure of the model, so it wasn't added."
        return table.get(code, "")
