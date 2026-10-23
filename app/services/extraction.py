"""Rule-based business-knowledge extraction (the no-LLM fallback).

Turns a client's free-text answer to a KG0-grounded question into candidate graph operations. It is deliberately
conservative: it only proposes changes it can tie to the wording of the answer; the operations are then validated by
graph_ops before the user ever sees them, and nothing is applied without approval.
"""
import re

from .graph_ops import norm, slugify

SAME_RE = re.compile(r"^\s*(same|same as (?:the )?(?:reference|graph|above)|no change|nothing|none|n/?a|as is|identical|"
                     r"we use the same|exactly (?:the )?same|that'?s (?:right|correct|fine)|all of (?:them|these)|all|yes|correct|ok(?:ay)?|fine|good|sure|right)\b[\s.!]*$", re.I)
SKIP_RE = re.compile(r"^\s*(skip|pass|not sure|unsure|don'?t know|do not know|dunno|no idea|idk|later|\?+)[\s.!]*$", re.I)
CALL_RE = re.compile(r"(?:we |they |it(?: is|'s) |internally |people |our team |staff )?(?:call(?:ed)?|name[d]?|refer(?:red)? to(?: it| them)? as|know[n]?(?: it| them)? as)"
                     r"\s+(?:it |them |this |that )?(?:as |: ?)?[\"“'‘]?(?P<n>[A-Za-z0-9][A-Za-z0-9 &/\-_.]{1,58}?)[\"”'’]?\s*(?:[.,;!]|$| in | at | because | but | and )", re.I)
NEW_ENTITY_RE = re.compile(r"(?i:we (?:also )?(?:have|use|track|run|maintain|keep)|there (?:is|are)|additionally|plus|also)\s+(?i:an? |the |our |some )?"
                           r"(?P<n>[A-Z][A-Za-z0-9&/\- ]{2,50}?)(?:\s+(?:that|which|for|to|and|in|at|feeds|supplies|sends|drives)\b|\s*[.,;:]|\s*$)")
REL_VERBS = [  # (regex, label, direction relative to the question's node)
    (r"\b(?:fed by|receives? (?:data )?from|comes? from|sourced? from|depends? on|driven by|uses?)\b", "depends on", "in"),
    (r"\b(?:rolls? up to|reports? to|consolidates? into)\b", "rolls up to", "out"),
    (r"\b(?:feeds?|supplies|sends?(?: data)? to|goes? (?:in)?to|flows? into|drives|populates)\b", "feeds", "out"),
    (r"\b(?:links? to|connected to|ties? (?:in)?to|maps? to|joins?)\b", "links to", "out"),
]
STOP_ATTR = {"and", "or", "also", "we", "the", "etc", "yes", "no", "all", "some", "most", "none", "same", "fine", "good", "great", "sure", "maybe",
             "ok", "okay", "depends", "hmm", "hm", "um", "uh", "er", "well", "so", "yeah", "yep", "nope", "nah", "lol", "perhaps", "mostly", "roughly", "thanks", "thank you", "right", "correct", "i think", "not really"}


def _words(s):
    return re.findall(r"[A-Za-z0-9]+", s)


def _split_items(text):
    parts = re.split(r"[,;\n•\u2022]|\band\b|\bplus\b|\bas well as\b|\balso\b|\bwith\b", text)
    items = []
    for p in parts:
        p = re.sub(r"^(?:we(?: also)?\s+(?:track|use|capture|record|keep|add|have)|extra|additional|add|track|use|also|plus|the)\s*:?\s*", "", p.strip(" .:-–—\"'()[]"), flags=re.I).strip(" .:-")
        if p and 1 <= len(_words(p)) <= 5 and len(p) <= 60 and p.lower() not in STOP_ATTR:
            items.append(p)
    return items


def _attr_match(item, attrs):
    ni = norm(item)
    for a in attrs:
        na = norm(re.sub(r"\(.*?\)", "", a))
        if na and (ni == na or (len(ni) >= 4 and (ni in na or na in ni))):
            return a
    return None


def _mentions(state, text, exclude_key):
    low = text.lower()
    found = []
    for k, n in state.nodes.items():
        if k == exclude_key:
            continue
        for cand in {n["name"], n["name"].split(" / ")[0], re.sub(r"\s*\(.*?\)", "", n["name"]), n.get("ref_name") or ""}:
            c = cand.strip().lower()
            if len(c) >= 5 and re.search(r"(?<![a-z0-9])" + re.escape(c) + r"(?![a-z0-9])", low):
                found.append((low.index(c), k))
                break
    return [k for _, k in sorted(found)]


def _verb_label(text):
    for rx, label, direction in REL_VERBS:
        if re.search(rx, text, re.I):
            return label, direction
    return "relates to", "out"


def _node_ref(state, key):
    return key if key in state.nodes else state.find(key)


def extract(topic, node_key, state, text):
    """-> dict(ops=[...], followup=str|None, ack=str, skipped=bool)"""
    text = (text or "").strip()
    node = state.nodes.get(node_key)
    if node is None and node_key:
        resolved_key = state.find(node_key)
        if resolved_key:
            node_key = resolved_key
            node = state.nodes.get(node_key)
    res = {"ops": [], "followup": None, "ack": "", "skipped": False}
    if not text or SKIP_RE.match(text):
        res.update(skipped=True, ack="No problem — I'll skip that one.")
        return res
    if node is None:
        res["ack"] = "Noted."
        return res
    same = bool(SAME_RE.match(text))
    name = node["name"]

    if topic == "naming":
        if same:
            res["ack"] = f"Great — “{name}” stays as named in the reference model."
            return res
        m = CALL_RE.search(text)
        new = m.group("n").strip(" .") if m else None
        if not new and len(_words(text)) <= 6 and not re.search(r"\b(is|are|was|do|does|we|they|it)\b", text, re.I):
            new = text.strip(" .\"'“”‘’")
        if not new:
            res["followup"] = f"Just to be precise: what exact name does your team use for {name}?"
            res["ack"] = "I couldn't pick out a specific name."
            return res
        if norm(new) != norm(name):
            res["ops"].append({"op": "update_node", "payload": {"node_key": node_key, "name": new},
                               "rationale": f"Your business calls “{name}” → “{new}”."})
            res["ack"] = f"Got it — I'll propose renaming “{name}” to “{new}” in your graph."
        else:
            res["ack"] = "That matches the reference name."
        return res

    if topic == "attributes":
        if same:
            res["ack"] = f"Understood — you use the reference attributes of “{name}”."
            return res
        add, remove = [], []
        rm = re.search(r"(?:don'?t|do not|never|no longer|not)\s+(?:track|use|capture|have|record|keep)\s+([^.;]+)", text, re.I)
        body = text
        if rm:
            for it in _split_items(rm.group(1)):
                hit = _attr_match(it, node["attributes"])
                if hit:
                    remove.append(hit)
            body = text.replace(rm.group(0), " ")
        for it in _split_items(body):
            if not _attr_match(it, node["attributes"]):
                add.append(it)
        payload = {"node_key": node_key}
        if add: payload["add_attributes"] = add
        if remove: payload["remove_attributes"] = remove
        if add or remove:
            res["ops"].append({"op": "update_node", "payload": payload, "rationale": "Attributes your business tracks differ from the reference set."})
            bits = ([f"{len(add)} new attribute(s) ({', '.join(add[:4])})"] if add else []) + ([f"{len(remove)} attribute(s) you don't use"] if remove else [])
            res["ack"] = "Noted — " + " and ".join(bits) + "."
        else:
            res["ack"] = "Everything you mentioned is already in the reference attributes."
            if len(_words(text)) <= 2:
                res["followup"] = f"Which specific fields do you capture for {name}? A comma-separated list is perfect."
        return res

    if topic == "relationships":
        if same:
            res["ack"] = "Understood — the reference relationships hold for you."
            return res
        label, direction = _verb_label(text)
        for k in _mentions(state, text, node_key):
            if state.edge_between(node_key, k) or state.edge_between(k, node_key):
                continue
            s, t = (node_key, k) if direction == "out" else (k, node_key)
            res["ops"].append({"op": "add_edge", "payload": {"source": s, "target": t, "label": label},
                               "rationale": f"You described a link between “{name}” and “{state.nodes[k]['name']}” that the reference graph lacks."})
        for m in NEW_ENTITY_RE.finditer(text):
            ent = m.group("n").strip(" .")
            if ent and not state.find(ent) and 3 <= len(ent) <= 50:
                key = slugify(ent, "client-")
                res["ops"].append({"op": "add_node", "payload": {"node_key": key, "name": ent, "definition": f"Client entity related to {name}, described by the client."},
                                   "rationale": "A client-specific entity you mentioned."})
                s, t = (node_key, key) if direction == "out" else (key, node_key)
                res["ops"].append({"op": "add_edge", "payload": {"source": s, "target": t, "label": label}, "rationale": "Connects the new entity."})
        if res["ops"]:
            res["ack"] = f"Thanks — I found {sum(1 for o in res['ops'] if o['op']=='add_edge')} relationship(s) and {sum(1 for o in res['ops'] if o['op']=='add_node')} new entit(ies) to propose."
        else:
            res["ack"] = "I didn't find a link that's missing from the reference graph."
            if len(_words(text)) <= 3:
                res["followup"] = f"Which other entities does {name} connect to? Use names from the graph, or describe a new one."
        return res

    if topic == "rules":
        if same:
            res["ack"] = f"Understood — you use the reference rule for “{name}”."
            return res
        formulas = [ln.strip(" -•*\t") for ln in re.split(r"[\n;]", text) if "=" in ln and len(ln.strip()) < 240]
        payload = {"node_key": node_key}
        if formulas:
            payload["add_formulas"] = formulas[:4]
        else:
            payload["append_note"] = f"Client rule: {text[:500]}"
        res["ops"].append({"op": "update_node", "payload": payload, "rationale": "Your calculation / rule differs from the reference."})
        res["ack"] = "Recorded your own formula." if formulas else "Recorded this as a client business rule on the entity."
        return res

    if topic == "missing":
        if same or re.match(r"^(no|nothing|none|nope)\b", text, re.I):
            res["ack"] = "Good — then the graph covers your process for this area."
            return res
        items = [i for i in _split_items(re.sub(r"^(?:we (?:also )?(?:have|use|track)|there (?:is|are)|yes[,:]?)\s*", "", text, flags=re.I)) if len(i) >= 3]
        for it in items[:5]:
            ent = it[0].upper() + it[1:]
            if state.find(ent):
                continue
            key = slugify(ent, "client-")
            res["ops"].append({"op": "add_node", "payload": {"node_key": key, "name": ent, "definition": "Client-described entity missing from the reference graph."},
                               "rationale": "You said this entity is missing from the reference graph."})
            res["ops"].append({"op": "add_edge", "payload": {"source": node_key, "target": key, "label": "relates to"}, "rationale": "Anchors it next to the entity we were discussing."})
        res["ack"] = (f"I'll propose {len(items[:5])} new entit(ies)." if res["ops"] else "I didn't catch a specific entity name.")
        if not res["ops"] and len(_words(text)) <= 2:
            res["followup"] = "Which entity or concept is missing? A short name is enough."
        return res

    # experience
    if same or re.match(r"^(no|nothing|none|nope)\b", text, re.I):
        res["ack"] = "Thanks — nothing to add there."
        return res
    short = re.sub(r"\s*\(.*?\)", "", name.split(" / ")[0])
    exp_name = f"Experience: {short}"
    key = slugify(exp_name, "exp-")
    n = 2
    while key in state.nodes:
        key, exp_name = slugify(f"{exp_name}-{n}", "exp-"), f"Experience: {short} ({n})"
        n += 1
    res["ops"].append({"op": "add_node", "payload": {"node_key": key, "name": exp_name, "kind": "experience", "definition": text[:1800]},
                       "rationale": "Your first-hand business experience, captured as a node."})
    res["ops"].append({"op": "add_edge", "payload": {"source": key, "target": node_key, "label": "client experience on"}, "rationale": "Links the experience to the entity."})
    res["ack"] = "Valuable — I'll propose adding this experience as a node linked to the entity."
    return res
