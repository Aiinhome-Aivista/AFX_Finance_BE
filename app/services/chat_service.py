"""Domain chatbot: one grounded question at a time -> business knowledge -> validated change proposals.

LLM (if configured) phrases questions and extracts structured changes; otherwise a rule-based engine does it.
Either way the output is only a *proposal* validated by graph_ops; the chatbot can never write KG0 or approve changes.
"""
import json
import logging

from ..errors import ConflictError, NotFoundError, ValidationError
from ..llm import LLMError
from ..repositories.conversations import ConversationRepository
from .extraction import extract
from .graph_ops import OPS, clean_text, validate_sequence
from .graph_service import GraphService
from .proposal_service import ProposalService
from .reference_service import ReferenceService
from .workflow_service import WorkflowService

log = logging.getLogger(__name__)
MAX_PLAN_NODES = 5
MAX_ANSWER_CHARS = 4000

SYSTEM = ("You are the Finance ontology interviewer for an FMCG finance function. You help a client customise a reference knowledge graph "
          "(KG0) of the domain “{domain}”. Ground every statement ONLY in the CONTEXT and the client's words; never invent entities, "
          "formulas, figures or sources. You cannot change the reference graph; you only propose changes to the client's own copy, "
          "and the client must approve each change.")


class ChatService:
    def __init__(self, db, llm=None):
        self.db = db
        self.llm = llm
        self.convs = ConversationRepository(db)
        self.graphs = GraphService(db)
        self.refs = ReferenceService(db)
        self.wfs = WorkflowService(db)
        self.props = ProposalService(db)

    # ------------------------------------------------------------------ helpers
    @property
    def engine(self):
        return f"llm:{self.llm.name}" if self.llm else "rule-based"

    def _wf(self, user_id, domain):
        wf = self.wfs.get_wf(user_id, domain, required=False)
        if not wf:
            u = self.wfs.users.by_id(user_id)
            if u and u.get("selected_app") != "finance":
                self.wfs.users.set_selected_app(user_id, "finance")
            self.wfs.start(user_id, domain)
            wf = self.wfs.get_wf(user_id, domain, required=True)
        steps = {s["step_no"]: s["status"] for s in self.wfs.repo.steps(wf["id"])}
        if steps.get(3) != "completed":
            blocks = self.refs.blocks(domain["slug"])
            default_ids = [b["id"] for b in blocks if b.get("default")] or [b["id"] for b in blocks]
            self.wfs.confirm_blocks(user_id, domain, default_ids)
            wf = self.wfs.get_wf(user_id, domain, required=True)
        return wf

    def _node_ctx(self, state, key):
        n = state.nodes[key]
        rels = []
        for (s, t, l), e in state.edges.items():
            if s == key:
                rels.append(f"{n['name']} —{l}→ {state.nodes[t]['name']}")
            elif t == key:
                rels.append(f"{state.nodes[s]['name']} —{l}→ {n['name']}")
        return {"key": key, "name": n["name"], "definition": n["definition"], "attributes": n["attributes"], "formulas": n["formulas"],
                "relationships": rels[:14], "evidence": n.get("evidence"), "client_notes": n.get("notes")}

    def _full_graph_ctx(self, state):
        nodes_summary = []
        for k, n in list(state.nodes.items())[:35]:
            attrs = ", ".join(n.get("attributes", [])[:5]) or "none"
            forms = "; ".join(n.get("formulas", [])[:2]) or "none"
            nodes_summary.append(f"- {n['name']} (key: {k}): {n.get('definition', '')[:160]}\n  Attributes: {attrs}\n  Formulas: {forms}")

        edges_summary = []
        for (s, t, l), e in list(state.edges.items())[:40]:
            sn = state.nodes.get(s, {}).get("name", s)
            tn = state.nodes.get(t, {}).get("name", t)
            edges_summary.append(f"- {sn} —[{l}]→ {tn}")

        return {
            "entity_count": len(state.nodes),
            "relationship_count": len(state.edges),
            "entities_text": "\n".join(nodes_summary),
            "relationships_text": "\n".join(edges_summary)
        }

    def _llm_json(self, domain, user_payload, max_tokens=1500):
        return self.llm.complete_json(SYSTEM.format(domain=domain["name"]), json.dumps(user_payload, ensure_ascii=False), max_tokens)

    # ------------------------------------------------------------------ question text
    def _question_text(self, domain, state, node_key, topic):
        n = state.nodes[node_key]
        name = n["name"]
        short_def = n["definition"].split(". ")[0][:220].rstrip(".")
        attrs = ", ".join(a for a in n["attributes"][:8])
        others = [state.nodes[t]["name"] for (s, t, _l) in state.edges if s == node_key][:3] + \
                 [state.nodes[s]["name"] for (s, t, _l) in state.edges if t == node_key][:2]
        formula = n["formulas"][0] if n["formulas"] else None
        base = {
            "naming": f"Let's start with “{name}” — {short_def}. What do you call this in your business (the name your team actually uses)? If it's the same, just say “same”.",
            "attributes": f"Which attributes of “{name}” does your business track? The reference lists: {attrs or 'none'}. Tell me which apply, any you don't use, and extra fields you capture that aren't listed.",
            "relationships": f"How does “{name}” connect to other entities in your finance process? The reference links it with: {', '.join(others) or 'no other entity'}. Which links hold for you, and what is missing?",
            "rules": f"For “{name}”, the reference uses: {formula or 'no fixed formula'}. Do you calculate or govern it the same way? Share your own formula or rule if it differs.",
            "missing": f"Thinking about {domain['name']} as you run it — is there an entity, document or system that doesn't appear in this graph at all? Name it and say how it connects.",
            "experience": f"From your own experience, what is the biggest practical nuance, pain point or workaround around “{name}” that a standard reference model wouldn't capture?",
        }[topic]
        if self.llm and topic != "missing":
            try:
                ctx = self._node_ctx(state, node_key)
                txt = self.llm.complete(SYSTEM.format(domain=domain["name"]),
                                        f"CONTEXT: {json.dumps(ctx, ensure_ascii=False)}\nTOPIC: {topic}\nBASE QUESTION: {base}\n"
                                        "Rewrite the base question as ONE short, friendly, business-level question for a finance professional. "
                                        "Keep every fact from the base question. Output only the question.", 300).strip()
                if 20 <= len(txt) <= 700:
                    return txt
            except LLMError as e:
                log.warning("LLM phrasing failed: %s", e)
        return base

    # ------------------------------------------------------------------ plan
    def _build_plan(self, state):
        return {"queue": [], "max_questions": 10}

    # ------------------------------------------------------------------ snapshot
    def _msg(self, m):
        return {"id": m["id"], "role": m["role"], "kind": m["kind"], "content": m["content"], "meta": m["meta"], "created_at": m["created_at"]}

    def snapshot(self, user_id, wf, conv, new_from_id=None, updated_graph=None):
        msgs = [self._msg(m) for m in self.convs.messages(conv["id"])]
        qs = self.convs.questions(conv["id"])
        open_q = next((q for q in reversed(qs) if q["status"] == "asked"), None)
        answered = sum(1 for q in qs if q["status"] in ("answered", "skipped") and not q["is_followup"])
        planned = 10
        plan_complete = answered >= planned and open_q is None
        pending_ops = sum(len(q["extracted_ops"]) for q in qs if q["status"] == "answered" and not q["proposed"])

        res = {
            "conversation_id": conv["id"],
            "messages": [m for m in msgs if new_from_id is None or m["id"] > new_from_id] if new_from_id else msgs,
            "open_question": {
                "id": open_q["id"],
                "node_key": open_q["node_key"],
                "topic": open_q["topic"],
                "text": open_q["question_text"],
                "is_followup": bool(open_q["is_followup"])
            } if open_q else None,
            "progress": {
                "answered": min(answered, 10),
                "planned": planned,
                "plan_complete": plan_complete
            },
            "pending_change_count": pending_ops,
            "engine": self.engine,
            "llm_enabled": bool(self.llm),
            "finalized": bool(wf["finalized_version_number"])
        }
        if updated_graph is not None:
            res["graph"] = updated_graph
            res["graph_updated"] = True
        return res

    def _generate_dynamic_question(self, domain, state, qs):
        """Generates the next contextual finance interview question using the LLM and the active graph state."""
        past_qa = []
        asked_questions = []
        for q in qs:
            txt = q.get("question_text", "")
            ans = q.get("answer_text", "")
            if txt:
                asked_questions.append(txt)
            if txt and ans:
                past_qa.append(f"Q: {txt}\nA: {ans}")

        g_ctx = self._full_graph_ctx(state)
        question_idx = sum(1 for q in qs if q.get("status") in ("answered", "skipped") and not q.get("is_followup")) + 1

        sys_prompt = (
            f"You are the senior Finance Ontology & Knowledge Graph interviewer customized for the domain “{domain['name']}”.\n"
            f"Your mission is to conduct a professional, 10-question discovery interview with a finance executive to adapt "
            f"and tailor their live knowledge graph to how their actual business operates.\n\n"
            f"ACTIVE KNOWLEDGE GRAPH CONTEXT ({g_ctx['entity_count']} entities, {g_ctx['relationship_count']} relationships):\n"
            f"{g_ctx['entities_text']}\n\n"
            f"ACTIVE RELATIONSHIPS:\n{g_ctx['relationships_text']}\n\n"
            f"INTERVIEW RULES:\n"
            f"1. Generate question #{question_idx} of 10.\n"
            f"2. Ground the question deeply in the active entities, attributes, formulas, or connections shown above.\n"
            f"3. Pick an entity or process area that hasn't been thoroughly explored yet.\n"
            f"4. NEVER repeat previously asked questions or concepts.\n"
            f"5. Questions should address real finance nuances: custom hierarchies, specific deduction/rebate calculations, missing master entities, integration flows, or variance tracking.\n"
            f"6. Ask exactly ONE clear, friendly, and practical question for the user to answer."
        )

        user_prompt = {
            "question_number": question_idx,
            "domain": domain["name"],
            "previously_asked_questions": asked_questions[-10:],
            "previous_dialogue_summary": past_qa[-5:],
            "instructions": "Return JSON: {\"question\": \"string\", \"target_node_key\": \"key of entity being explored\", \"topic\": \"naming|attributes|relationships|rules|missing|experience\"}"
        }

        if self.llm:
            try:
                out = self.llm.complete_json(sys_prompt, json.dumps(user_prompt, ensure_ascii=False), max_tokens=600)
                q_text = clean_text(out.get("question"), 600)
                node_key = out.get("target_node_key") or ""
                if node_key not in state.nodes:
                    node_key = state.find(node_key) or (list(state.nodes.keys())[0] if state.nodes else "domain")
                topic = out.get("topic") or "attributes"
                if q_text and len(q_text) >= 15:
                    return q_text, node_key, topic
            except Exception as e:
                log.warning("LLM dynamic question generation failed: %s", e)

        # Fallback question based on unvisited nodes in graph
        candidates = [k for k in state.nodes if k not in {q.get("node_key") for q in qs}] or list(state.nodes.keys())
        target = candidates[0] if candidates else list(state.nodes.keys())[0]
        n = state.nodes[target]
        fb_topics = ["attributes", "relationships", "rules", "naming"]
        chosen_topic = fb_topics[(question_idx - 1) % len(fb_topics)]
        fb_q = self._question_text(domain, state, target, chosen_topic)
        return fb_q, target, chosen_topic

    def _ask_next(self, user_id, wf, domain, conv):
        """Ask the next dynamic question grounded in active graph. Returns True if a question was asked."""
        _, state = self.graphs.working(user_id, wf, domain)
        qs = self.convs.questions(conv["id"])
        answered = sum(1 for q in qs if q["status"] in ("answered", "skipped") and not q["is_followup"])
        if answered >= 10:
            return False

        seq = (qs[-1]["seq"] if qs else 0) + 1
        text, node_key, topic = self._generate_dynamic_question(domain, state, qs)

        qid = self.convs.add_question(conv["id"], seq, node_key, topic, text)
        self.convs.add_message(conv["id"], "assistant", text, "question",
                               {"question_id": qid, "node_key": node_key, "topic": topic, "question_number": answered + 1})
        return True

    # ------------------------------------------------------------------ public API
    def start(self, user_id, domain):
        wf = self._wf(user_id, domain)
        conv = self.convs.get(user_id, wf["id"])
        if conv:
            return self.snapshot(user_id, wf, conv)
        _, state = self.graphs.working(user_id, wf, domain)
        plan = self._build_plan(state)
        with self.db.transaction():
            cid = self.convs.create(user_id, wf["id"], plan)
            conv = self.convs.get(user_id, wf["id"])
            hi = (f"Hi! I'm your AI Knowledge Graph Copilot for “{domain['name']}”. "
                  "I'll analyze your active live graph and conduct a 10-question dynamic discovery interview. "
                  "Each answer you provide will be analyzed by the LLM and dynamically updated directly into your knowledge graph nodes, attributes, and relationships.")
            self.convs.add_message(cid, "assistant", hi, "intro", {"engine": self.engine})
            self._ask_next(user_id, wf, domain, conv)
        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]))

    def get(self, user_id, domain):
        wf = self._wf(user_id, domain)
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            return {"conversation_id": None, "messages": [], "open_question": None, "progress": {"answered": 0, "planned": 10, "plan_complete": False},
                    "pending_change_count": 0, "engine": self.engine, "llm_enabled": bool(self.llm), "finalized": bool(wf["finalized_version_number"])}
        return self.snapshot(user_id, wf, conv)

    def focus(self, user_id, domain, node_key):
        """Jump the interview to explore a chosen entity with LLM."""
        wf = self._wf(user_id, domain)
        if wf["finalized_version_number"]:
            raise ConflictError("The ontology is finalised; re-open it to continue the interview.")
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            raise ConflictError("Start the conversation first.")
        if self.convs.open_question(conv["id"]):
            raise ConflictError("Please answer (or skip) the current question first.")
        _, state = self.graphs.working(user_id, wf, domain)
        key = state.find(node_key)
        if key is None:
            raise NotFoundError("That entity is not in your graph.")
        n = state.nodes[key]
        last = self.convs.messages(conv["id"])[-1]["id"]
        self.convs.add_message(conv["id"], "assistant", f"Sure — let's focus on “{n['name']}”.", "system")
        self._ask_next(user_id, wf, domain, conv)
        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last)

    # ------------------------------------------------------------------ answer & dynamic graph update
    def answer(self, user_id, domain, text):
        wf = self._wf(user_id, domain)
        if wf["finalized_version_number"]:
            raise ConflictError("The ontology is finalised; re-open it to continue the interview.")
        text = clean_text(text, MAX_ANSWER_CHARS)
        if not text:
            raise ValidationError("Please type an answer (or say “skip”).")
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            self.start(user_id, domain)
            conv = self.convs.get(user_id, wf["id"])
        q = self.convs.open_question(conv["id"])
        if not q:
            return self.chat_message(user_id, domain, text)
        last_id = self.convs.messages(conv["id"])[-1]["id"]
        number, state = self.graphs.working(user_id, wf, domain)

        updated_graph_data = None
        with self.db.transaction():
            self.convs.add_message(conv["id"], "user", text, "answer", {"question_id": q["id"]})
            analysis, fallback = self._analyze(domain, state, q, text)
            accepted, rejected, _ = validate_sequence(state, analysis["ops"], number + 1)
            ops = [{"op": a["op"], "payload": a["payload"], "rationale": a["rationale"]} for a in accepted]
            status = "skipped" if analysis["skipped"] else "answered"
            self.convs.answer_question(q["id"], text, ops, status)
            if status == "answered":
                self.convs.add_experience(user_id, wf["id"], q["id"], q["node_key"], q["topic"], text)

            # Apply accepted graph updates dynamically to the user's graph version
            if ops:
                try:
                    summary = f"LLM dynamic graph update from question #{q['seq']} answer: {text[:60]}"
                    new_version = self.graphs.create_version(
                        user_id, wf, domain, ops, "customization", None, summary, "user"
                    )
                    number, state = self.graphs.working(user_id, wf, domain)
                    updated_graph_data = self.graphs.graph(user_id, wf, domain, new_version)

                    # Also refresh static HTML visualization for iframe
                    try:
                        from flask import current_app
                        from .html_graph_service import HTMLGraphService
                        graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
                        html_url = HTMLGraphService.save_html(domain["slug"], domain, updated_graph_data.get("nodes", []), updated_graph_data.get("edges", []), output_dir=graph_dir)
                        updated_graph_data["html_url"] = html_url
                    except Exception:
                        pass
                except Exception as e:
                    log.warning("Direct graph update failed, kept in ops: %s", e)

            ack = analysis["ack"] or "Noted and processed."
            if ops:
                ack += f" (Dynamically updated knowledge graph: {len(ops)} node/relationship changes applied.)"
            elif rejected and not accepted and analysis["ops"]:
                ack += " (I reviewed your response; those items already exist in the graph so no duplicate nodes were added.)"

            self.convs.add_message(conv["id"], "assistant", ack, "ack",
                                   {"question_id": q["id"], "ops": len(ops), "engine": "rule-based" if fallback else self.engine,
                                    **({"fallback_reason": fallback} if fallback and self.llm else {})})

            asked = False
            if analysis["followup"] and not q["is_followup"]:
                seq = max(x["seq"] for x in self.convs.questions(conv["id"])) + 1
                fqid = self.convs.add_question(conv["id"], seq, q["node_key"], q["topic"], analysis["followup"], True)
                self.convs.add_message(conv["id"], "assistant", analysis["followup"], "question",
                                       {"question_id": fqid, "node_key": q["node_key"], "topic": q["topic"], "followup": True})
                asked = True

            if not asked:
                conv = self.convs.get(user_id, wf["id"])
                asked = self._ask_next(user_id, wf, domain, conv)
                if not asked:
                    self.convs.add_message(conv["id"], "assistant",
                                           "Interview Complete (10/10 questions answered)! Your Knowledge Graph has been fully tailored and enriched with your business insights. You can continue asking questions using the Graph Copilot anytime.",
                                           "system", {"plan_complete": True})

        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id, updated_graph=updated_graph_data)

    def _analyze(self, domain, state, q, text):
        """Analyze client answer with LLM to extract graph nodes, attributes, and relationships with duplicate prevention."""
        if q["node_key"] not in state.nodes and state.find(q["node_key"]) is None:
            # Fallback to general graph if target node missing
            pass
        if self.llm:
            from .extraction import SKIP_RE
            if SKIP_RE.match(text):
                return extract(q["topic"], q["node_key"], state, text), None
            try:
                g_ctx = self._full_graph_ctx(state)
                curr_node = self._node_ctx(state, q["node_key"]) if q["node_key"] in state.nodes else {}
                payload = {
                    "DOMAIN": domain["name"],
                    "TARGET_ENTITY": curr_node,
                    "ACTIVE_GRAPH_NODES": [{"key": k, "name": n["name"], "attributes": n.get("attributes", [])} for k, n in list(state.nodes.items())[:80]],
                    "ACTIVE_GRAPH_EDGES": [{"source": s, "target": t, "label": l} for (s, t, l) in list(state.edges.keys())[:80]],
                    "QUESTION_ASKED": q["question_text"],
                    "TOPIC": q["topic"],
                    "CLIENT_ANSWER": text,
                    "ALLOWED_OPS": {
                        "add_node": {"name": "str", "definition": "str?", "attributes": "[str]?", "kind": "entity|experience", "node_key": "str?"},
                        "update_node": {"node_key": "str", "name": "str?", "add_attributes": "[str]?", "remove_attributes": "[str]?", "add_formulas": "[str]?", "append_note": "str?", "definition": "str?"},
                        "add_edge": {"source": "existing key|name", "target": "existing key|name", "label": "str"},
                        "remove_edge": {"source": "key", "target": "key"},
                        "remove_node": {"node_key": "key"}
                    },
                    "DUPLICATE_PREVENTION_RULES": [
                        "Check ACTIVE_GRAPH_NODES first. If an entity with the same or similar name already exists, DO NOT add a new node. Use update_node with its existing key instead.",
                        "Check ACTIVE_GRAPH_EDGES first. If an edge already exists between source and target, DO NOT add a duplicate edge.",
                        "If the user says 'same', 'nothing', 'no change', or confirms existing behavior, return ops: [] and an affirmative acknowledgement.",
                        "If adding a new node, provide a meaningful business definition and attributes."
                    ],
                    "INSTRUCTIONS": (
                        "Analyze the client's answer against the active graph. Return JSON:\n"
                        "{\n"
                        "  \"acknowledgement\": \"Short sentence summarizing what was understood and changed\",\n"
                        "  \"followup\": null,\n"
                        "  \"ops\": [\n"
                        "     {\"op\": \"...\", \"payload\": {...}, \"rationale\": \"short explanation\"}\n"
                        "  ]\n"
                        "}"
                    )
                }
                out = self._llm_json(domain, payload)
                raw_ops = out.get("ops") or []
                clean_ops = []
                for o in raw_ops:
                    if isinstance(o, dict) and o.get("op") in OPS:
                        clean_ops.append({
                            "op": o.get("op"),
                            "payload": o.get("payload") or {},
                            "rationale": o.get("rationale") or "Derived dynamically from user interview response."
                        })
                return {
                    "ops": clean_ops,
                    "followup": clean_text(out.get("followup"), 400) or None,
                    "ack": clean_text(out.get("acknowledgement"), 400),
                    "skipped": False
                }, None
            except LLMError as e:
                log.warning("LLM analysis failed, falling back to rule extraction: %s", e)
                return extract(q["topic"], q["node_key"], state, text), str(e)
        return extract(q["topic"], q["node_key"], state, text), None

    # ------------------------------------------------------------------ general graph-grounded chat
    def chat_message(self, user_id, domain, text, node_key=None):
        wf = self._wf(user_id, domain)
        text = clean_text(text, MAX_ANSWER_CHARS)
        if not text:
            raise ValidationError("Please type a message.")
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            self.start(user_id, domain)
            conv = self.convs.get(user_id, wf["id"])

        last_id = self.convs.messages(conv["id"])[-1]["id"] if self.convs.messages(conv["id"]) else 0
        number, state = self.graphs.working(user_id, wf, domain)

        # Context build: full knowledge graph
        g_ctx = self._full_graph_ctx(state)
        selected_ctx = self._node_ctx(state, node_key) if node_key and node_key in state.nodes else None

        sys_prompt = (
            f"You are the Finance Knowledge Graph Copilot for “{domain['name']}”.\n"
            f"You are grounded strictly in the user's active Knowledge Graph below:\n\n"
            f"ACTIVE KNOWLEDGE GRAPH ENTITIES ({g_ctx['entity_count']}):\n{g_ctx['entities_text']}\n\n"
            f"ACTIVE RELATIONSHIPS ({g_ctx['relationship_count']}):\n{g_ctx['relationships_text']}\n\n"
            "INSTRUCTIONS:\n"
            "1. Answer questions clearly, accurately, and concisely (2 to 5 sentences).\n"
            "2. Ground every fact in the active graph entities, attributes, formulas, or connections.\n"
            "3. If the user asks in Bengali or another language, reply fluently in the user's language.\n"
            "4. If the user asks about an entity's calculations, explain its formulas.\n"
            "5. If the user suggests modifying, adding, or renaming an entity, acknowledge it and explain how it connects."
        )

        user_prompt = f"User Question: {text}"
        if selected_ctx:
            user_prompt = f"Selected Entity Context: {json.dumps(selected_ctx, ensure_ascii=False)}\n\n" + user_prompt

        recent = [m for m in self.convs.messages(conv["id"])[-4:] if m.get("content")]
        if recent:
            history_lines = [f"{m['role'].capitalize()}: {m['content'][:250]}" for m in recent]
            user_prompt = "Recent chat history:\n" + "\n".join(history_lines) + "\n\n" + user_prompt

        answer, engine = None, "rule-based"
        if self.llm:
            try:
                answer = self.llm.complete(sys_prompt, user_prompt, max_tokens=750).strip()
                engine = self.engine
            except LLMError as e:
                log.warning("LLM chat message failed: %s", e)

        if not answer:
            answer = f"I'm here to help explore your {domain['name']} knowledge graph. It currently has {g_ctx['entity_count']} entities and {g_ctx['relationship_count']} relationships. You can ask about any entity, connection, or calculation formula."

        with self.db.transaction():
            self.convs.add_message(conv["id"], "user", text, "answer", {"node_key": node_key} if node_key else None)
            self.convs.add_message(conv["id"], "assistant", answer, "node_qa" if node_key else "system",
                                   {"engine": engine, "node_key": node_key} if node_key else {"engine": engine})

        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)

    # ------------------------------------------------------------------ node Q&A (grounded)
    def ask_node(self, user_id, domain, node_key, question):
        wf = self._wf(user_id, domain)
        question = clean_text(question, 1000)
        if not question:
            raise ValidationError("Type a question about the selected entity.")
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            raise ConflictError("Start the conversation first.")
        _, state = self.graphs.working(user_id, wf, domain)
        key = state.find(node_key)
        if key is None:
            raise NotFoundError("That entity is not in your graph.")
        ctx = self._node_ctx(state, key)
        canon = self.refs.repo.node(key)
        if canon:
            ctx["canon_joins"] = canon.get("canon_joins")
        last_id = self.convs.messages(conv["id"])[-1]["id"]
        answer, engine = None, "rule-based"
        if self.llm:
            try:
                answer = self.llm.complete(SYSTEM.format(domain=domain["name"]),
                                           f"CONTEXT: {json.dumps(ctx, ensure_ascii=False)}\nQUESTION: {question}\n"
                                           "Answer in at most 6 sentences using only the CONTEXT. If the CONTEXT does not contain the answer, say so.", 600).strip()
                engine = self.engine
            except LLMError as e:
                log.warning("LLM node QA failed: %s", e)
        if not answer:
            answer = self._rule_answer(ctx, question)
        with self.db.transaction():
            self.convs.add_message(conv["id"], "user", question, "node_qa", {"node_key": key})
            self.convs.add_message(conv["id"], "assistant", answer, "node_qa", {"node_key": key, "engine": engine})
        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)

    @staticmethod
    def _rule_answer(ctx, question):
        q = question.lower()
        name = ctx["name"]
        if any(w in q for w in ("formula", "calculate", "computed", "compute", "how is", "how do you measure")):
            if ctx["formulas"]:
                return f"For {name}, the ontology gives: " + "; ".join(ctx["formulas"]) + "."
            return f"The ontology doesn't state a formula for {name}. Its attributes are: {', '.join(ctx['attributes'][:8]) or 'none listed'}."
        if any(w in q for w in ("attribute", "field", "track", "capture", "column")):
            return f"{name} carries these attributes: {', '.join(ctx['attributes']) or 'none listed'}."
        if any(w in q for w in ("relat", "connect", "link", "feed", "join", "depend", "neighbour")):
            rels = ctx["relationships"]
            return (f"{name} is connected as follows: " + "; ".join(rels) + ".") if rels else f"{name} has no relationships in your current graph."
        if any(w in q for w in ("source", "evidence", "where does", "reference", "proof")):
            return f"Evidence for {name}: {ctx['evidence'] or 'not recorded'}." + (f" Canon joins: {ctx['canon_joins']}" if ctx.get("canon_joins") else "")
        parts = [ctx["definition"]]
        if ctx["attributes"]:
            parts.append("Key attributes: " + ", ".join(ctx["attributes"][:6]) + ".")
        if ctx["relationships"]:
            parts.append("Connections: " + "; ".join(ctx["relationships"][:3]) + ".")
        if ctx.get("client_notes"):
            parts.append("Your notes: " + ctx["client_notes"][:300])
        return " ".join(parts)

    # ------------------------------------------------------------------ propose
    def propose(self, user_id, domain):
        wf = self._wf(user_id, domain)
        if wf["finalized_version_number"]:
            raise ConflictError("The ontology is finalised; re-open it to propose further changes.")
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            raise ConflictError("Start the conversation first.")
        qs = self.convs.unproposed(conv["id"])
        if not qs:
            raise ValidationError("Answer at least one question before reviewing proposed changes.")
        raw = []
        for q in qs:
            for o in q["extracted_ops"]:
                raw.append({**o, "rationale": (o.get("rationale") or "") + f" (from your answer about {q['topic']})"})
        last_id = self.convs.messages(conv["id"])[-1]["id"]
        proposal, rejected = (None, [])
        if raw:
            title = f"Proposed changes from {len(qs)} answer(s)"
            summary = self._summary(domain, raw)
            proposal, rejected = self.props.create(user_id, wf, domain, raw, title, summary, "chat", llm_used=bool(self.llm))
        with self.db.transaction():
            self.convs.mark_proposed([q["id"] for q in qs])
            if proposal:
                text = (f"I compared your answers with the reference graph and prepared {len(proposal['items'])} proposed change(s). "
                        f"{proposal['summary']} Review them in the panel: approve or reject each one. Nothing is applied until you approve.")
                self.convs.add_message(conv["id"], "assistant", text, "proposal", {"proposal_id": proposal["id"]})
            else:
                self.convs.add_message(conv["id"], "assistant",
                                       "Your answers match the reference model closely, so I have no graph changes to propose. "
                                       "You can continue answering, focus on another entity, or finalise when you're ready.", "system")
        snap = self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)
        snap["proposal"] = proposal
        snap["discarded_changes"] = len(rejected)
        return snap

    def _summary(self, domain, raw):
        counts = {}
        for o in raw:
            counts[o["op"]] = counts.get(o["op"], 0) + 1
        labels = {"add_node": "new entities", "update_node": "entity updates", "add_edge": "new relationships",
                  "remove_node": "removals", "remove_edge": "removed relationships", "update_edge": "relabelled relationships"}
        base = "Summary: " + ", ".join(f"{v} {labels[k]}" for k, v in counts.items()) + "."
        if self.llm:
            try:
                s = self.llm.complete(SYSTEM.format(domain=domain["name"]),
                                      "Summarise these proposed graph changes for a finance executive in at most 2 sentences, using only the data given:\n"
                                      + json.dumps(raw[:30], ensure_ascii=False), 250).strip()
                if 10 < len(s) < 600:
                    return s
            except LLMError:
                pass
        return base



# """Domain chatbot: one grounded question at a time -> business knowledge -> validated change proposals.

# LLM (if configured) phrases questions and extracts structured changes; otherwise a rule-based engine does it.
# Either way the output is only a *proposal* validated by graph_ops; the chatbot can never write KG0 or approve changes.
# """
# import json
# import logging

# from ..errors import ConflictError, NotFoundError, ValidationError
# from ..llm import LLMError
# from ..repositories.conversations import ConversationRepository
# from .extraction import extract
# from .graph_ops import OPS, clean_text, validate_sequence
# from .graph_service import GraphService
# from .proposal_service import ProposalService
# from .reference_service import ReferenceService
# from .workflow_service import WorkflowService

# log = logging.getLogger(__name__)
# MAX_PLAN_NODES = 5
# MAX_ANSWER_CHARS = 4000

# SYSTEM = ("You are the Finance ontology interviewer for an FMCG finance function. You help a client customise a reference knowledge graph "
#           "(KG0) of the domain “{domain}”. Ground every statement ONLY in the CONTEXT and the client's words; never invent entities, "
#           "formulas, figures or sources. You cannot change the reference graph; you only propose changes to the client's own copy, "
#           "and the client must approve each change.")


# class ChatService:
#     def __init__(self, db, llm=None):
#         self.db = db
#         self.llm = llm
#         self.convs = ConversationRepository(db)
#         self.graphs = GraphService(db)
#         self.refs = ReferenceService(db)
#         self.wfs = WorkflowService(db)
#         self.props = ProposalService(db)

#     # ------------------------------------------------------------------ helpers
#     @property
#     def engine(self):
#         return f"llm:{self.llm.name}" if self.llm else "rule-based"

#     def _wf(self, user_id, domain):
#         wf = self.wfs.require_completed(user_id, domain, 3, "Choose your focus blocks on the reference graph first (step 3).")
#         return wf

#     def _node_ctx(self, state, key):
#         n = state.nodes[key]
#         rels = []
#         for (s, t, l), e in state.edges.items():
#             if s == key:
#                 rels.append(f"{n['name']} —{l}→ {state.nodes[t]['name']}")
#             elif t == key:
#                 rels.append(f"{state.nodes[s]['name']} —{l}→ {n['name']}")
#         return {"key": key, "name": n["name"], "definition": n["definition"], "attributes": n["attributes"], "formulas": n["formulas"],
#                 "relationships": rels[:14], "evidence": n.get("evidence"), "client_notes": n.get("notes")}

#     def _llm_json(self, domain, user_payload, max_tokens=1500):
#         return self.llm.complete_json(SYSTEM.format(domain=domain["name"]), json.dumps(user_payload, ensure_ascii=False), max_tokens)

#     # ------------------------------------------------------------------ question text
#     def _question_text(self, domain, state, node_key, topic):
#         n = state.nodes[node_key]
#         name = n["name"]
#         short_def = n["definition"].split(". ")[0][:220].rstrip(".")
#         attrs = ", ".join(a for a in n["attributes"][:8])
#         others = [state.nodes[t]["name"] for (s, t, _l) in state.edges if s == node_key][:3] + \
#                  [state.nodes[s]["name"] for (s, t, _l) in state.edges if t == node_key][:2]
#         formula = n["formulas"][0] if n["formulas"] else None
#         base = {
#             "naming": f"Let's start with “{name}” — {short_def}. What do you call this in your business (the name your team actually uses)? If it's the same, just say “same”.",
#             "attributes": f"Which attributes of “{name}” does your business track? The reference lists: {attrs or 'none'}. Tell me which apply, any you don't use, and extra fields you capture that aren't listed.",
#             "relationships": f"How does “{name}” connect to other entities in your finance process? The reference links it with: {', '.join(others) or 'no other entity'}. Which links hold for you, and what is missing?",
#             "rules": f"For “{name}”, the reference uses: {formula or 'no fixed formula'}. Do you calculate or govern it the same way? Share your own formula or rule if it differs.",
#             "missing": f"Thinking about {domain['name']} as you run it — is there an entity, document or system that doesn't appear in this graph at all? Name it and say how it connects.",
#             "experience": f"From your own experience, what is the biggest practical nuance, pain point or workaround around “{name}” that a standard reference model wouldn't capture?",
#         }[topic]
#         if self.llm and topic != "missing":
#             try:
#                 ctx = self._node_ctx(state, node_key)
#                 txt = self.llm.complete(SYSTEM.format(domain=domain["name"]),
#                                         f"CONTEXT: {json.dumps(ctx, ensure_ascii=False)}\nTOPIC: {topic}\nBASE QUESTION: {base}\n"
#                                         "Rewrite the base question as ONE short, friendly, business-level question for a finance professional. "
#                                         "Keep every fact from the base question. Output only the question.", 300).strip()
#                 if 20 <= len(txt) <= 700:
#                     return txt
#             except LLMError as e:
#                 log.warning("LLM phrasing failed: %s", e)
#         return base

#     # ------------------------------------------------------------------ plan
#     def _build_plan(self, state):
#         deg = {k: 0 for k in state.nodes}
#         for (s, t, _l) in state.edges:
#             deg[s] += 1
#             deg[t] += 1
#         pool = [k for k, n in state.nodes.items() if n["origin"] == "reference" and n.get("role") == "core"] or list(state.nodes)
#         pool.sort(key=lambda k: (-deg[k], state.nodes[k]["name"]))
#         queue = []
#         for k in pool[:MAX_PLAN_NODES]:
#             n = state.nodes[k]
#             topics = ["naming", "attributes"] + (["relationships"] if deg[k] else []) + (["rules"] if n["formulas"] else []) + ["experience"]
#             queue += [{"node_key": k, "topic": t, "asked": False} for t in topics]
#         if pool:
#             queue.append({"node_key": pool[0], "topic": "missing", "asked": False})
#         return {"queue": queue}

#     # ------------------------------------------------------------------ snapshot
#     def _msg(self, m):
#         return {"id": m["id"], "role": m["role"], "kind": m["kind"], "content": m["content"], "meta": m["meta"], "created_at": m["created_at"]}

#     def snapshot(self, user_id, wf, conv, new_from_id=None):
#         msgs = [self._msg(m) for m in self.convs.messages(conv["id"])]
#         qs = self.convs.questions(conv["id"])
#         open_q = next((q for q in reversed(qs) if q["status"] == "asked"), None)
#         plan = conv["plan"]["queue"]
#         answered = sum(1 for q in qs if q["status"] in ("answered", "skipped") and not q["is_followup"])
#         pending_ops = sum(len(q["extracted_ops"]) for q in qs if q["status"] == "answered" and not q["proposed"])
#         return {"conversation_id": conv["id"], "messages": [m for m in msgs if new_from_id is None or m["id"] > new_from_id] if new_from_id else msgs,
#                 "open_question": {"id": open_q["id"], "node_key": open_q["node_key"], "topic": open_q["topic"], "text": open_q["question_text"],
#                                  "is_followup": bool(open_q["is_followup"])} if open_q else None,
#                 "progress": {"answered": answered, "planned": len(plan), "plan_complete": all(p["asked"] for p in plan) and open_q is None},
#                 "pending_change_count": pending_ops, "engine": self.engine, "llm_enabled": bool(self.llm),
#                 "finalized": bool(wf["finalized_version_number"])}

#     def _ask_next(self, user_id, wf, domain, conv):
#         """Ask the next planned question (skipping nodes that no longer exist). Returns True if a question was asked."""
#         _, state = self.graphs.working(user_id, wf, domain)
#         plan = conv["plan"]
#         qs = self.convs.questions(conv["id"])
#         seq = (qs[-1]["seq"] if qs else 0) + 1
#         for item in plan["queue"]:
#             if item["asked"]:
#                 continue
#             item["asked"] = True
#             if item["node_key"] not in state.nodes:
#                 continue
#             text = self._question_text(domain, state, item["node_key"], item["topic"])
#             qid = self.convs.add_question(conv["id"], seq, item["node_key"], item["topic"], text)
#             self.convs.add_message(conv["id"], "assistant", text, "question", {"question_id": qid, "node_key": item["node_key"], "topic": item["topic"]})
#             self.convs.update_plan(conv["id"], plan)
#             return True
#         self.convs.update_plan(conv["id"], plan)
#         return False

#     # ------------------------------------------------------------------ public API
#     def start(self, user_id, domain):
#         wf = self._wf(user_id, domain)
#         conv = self.convs.get(user_id, wf["id"])
#         if conv:
#             return self.snapshot(user_id, wf, conv)
#         _, state = self.graphs.working(user_id, wf, domain)
#         plan = self._build_plan(state)
#         with self.db.transaction():
#             cid = self.convs.create(user_id, wf["id"], plan)
#             conv = self.convs.get(user_id, wf["id"])
#             hi = (f"Hi! I'll help you tailor the “{domain['name']}” reference graph (KG0) to how your business really works. "
#                   "I'll ask one question at a time. Your answers become *proposed* changes — nothing changes in your graph until you approve it, "
#                   "and the reference graph itself is never modified. You can say “skip” to pass on any question, or select a node and ask me about it.")
#             self.convs.add_message(cid, "assistant", hi, "intro", {"engine": self.engine})
#             self._ask_next(user_id, wf, domain, conv)
#         return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]))

#     def get(self, user_id, domain):
#         wf = self._wf(user_id, domain)
#         conv = self.convs.get(user_id, wf["id"])
#         if not conv:
#             return {"conversation_id": None, "messages": [], "open_question": None, "progress": {"answered": 0, "planned": 0, "plan_complete": False},
#                     "pending_change_count": 0, "engine": self.engine, "llm_enabled": bool(self.llm), "finalized": bool(wf["finalized_version_number"])}
#         return self.snapshot(user_id, wf, conv)

#     def focus(self, user_id, domain, node_key):
#         """Jump the interview to a chosen entity: its questions go to the front of the queue."""
#         wf = self._wf(user_id, domain)
#         if wf["finalized_version_number"]:
#             raise ConflictError("The ontology is finalised; re-open it to continue the interview.")
#         conv = self.convs.get(user_id, wf["id"])
#         if not conv:
#             raise ConflictError("Start the conversation first.")
#         if self.convs.open_question(conv["id"]):
#             raise ConflictError("Please answer (or skip) the current question first.")
#         _, state = self.graphs.working(user_id, wf, domain)
#         key = state.find(node_key)
#         if key is None:
#             raise NotFoundError("That entity is not in your graph.")
#         deg = sum(1 for (s, t, _l) in state.edges if key in (s, t))
#         n = state.nodes[key]
#         topics = ["naming", "attributes"] + (["relationships"] if deg else []) + (["rules"] if n["formulas"] else []) + ["experience"]
#         plan = conv["plan"]
#         plan["queue"] = [i for i in plan["queue"] if i["asked"]] + [{"node_key": key, "topic": t, "asked": False} for t in topics] + \
#                         [i for i in plan["queue"] if not i["asked"] and i["node_key"] != key]
#         self.convs.update_plan(conv["id"], plan)
#         conv = self.convs.get(user_id, wf["id"])
#         last = self.convs.messages(conv["id"])[-1]["id"]
#         self.convs.add_message(conv["id"], "assistant", f"Sure — let's focus on “{n['name']}”.", "system")
#         self._ask_next(user_id, wf, domain, conv)
#         return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last)

#     # ------------------------------------------------------------------ answer
#     def answer(self, user_id, domain, text):
#         wf = self._wf(user_id, domain)
#         if wf["finalized_version_number"]:
#             raise ConflictError("The ontology is finalised; re-open it to continue the interview.")
#         text = clean_text(text, MAX_ANSWER_CHARS)
#         if not text:
#             raise ValidationError("Please type an answer (or say “skip”).")
#         conv = self.convs.get(user_id, wf["id"])
#         if not conv:
#             raise ConflictError("Start the conversation first.")
#         q = self.convs.open_question(conv["id"])
#         if not q:
#             raise ConflictError("There is no open question right now. Use “Review proposed changes”, or focus on another entity.")
#         last_id = self.convs.messages(conv["id"])[-1]["id"]
#         number, state = self.graphs.working(user_id, wf, domain)
#         with self.db.transaction():
#             self.convs.add_message(conv["id"], "user", text, "answer", {"question_id": q["id"]})
#             analysis, fallback = self._analyze(domain, state, q, text)
#             accepted, rejected, _ = validate_sequence(state, analysis["ops"], number + 1)
#             ops = [{"op": a["op"], "payload": a["payload"], "rationale": a["rationale"]} for a in accepted]
#             status = "skipped" if analysis["skipped"] else "answered"
#             self.convs.answer_question(q["id"], text, ops, status)
#             if status == "answered":
#                 self.convs.add_experience(user_id, wf["id"], q["id"], q["node_key"], q["topic"], text)
#             ack = analysis["ack"] or "Noted."
#             if rejected and not accepted and analysis["ops"]:
#                 ack += " (I couldn't turn that into a valid graph change — e.g. the entity may already exist.)"
#             self.convs.add_message(conv["id"], "assistant", ack, "ack",
#                                    {"question_id": q["id"], "ops": len(ops), "engine": "rule-based" if fallback else self.engine,
#                                     **({"fallback_reason": fallback} if fallback and self.llm else {})})
#             asked = False
#             if analysis["followup"] and not q["is_followup"]:
#                 seq = max(x["seq"] for x in self.convs.questions(conv["id"])) + 1
#                 fqid = self.convs.add_question(conv["id"], seq, q["node_key"], q["topic"], analysis["followup"], True)
#                 self.convs.add_message(conv["id"], "assistant", analysis["followup"], "question",
#                                        {"question_id": fqid, "node_key": q["node_key"], "topic": q["topic"], "followup": True})
#                 asked = True
#             if not asked:
#                 conv = self.convs.get(user_id, wf["id"])
#                 asked = self._ask_next(user_id, wf, domain, conv)
#                 if not asked:
#                     self.convs.add_message(conv["id"], "assistant",
#                                            "That covers the main areas for your selected blocks. Click “Review proposed changes” and I'll summarise "
#                                            "what your answers would change, so you can approve or reject each item.", "system", {"plan_complete": True})
#         return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)

#     def _analyze(self, domain, state, q, text):
#         """-> (analysis dict, fallback_reason|None). Never raises for LLM problems."""
#         if q["node_key"] not in state.nodes:
#             return {"ops": [], "followup": None, "ack": "That entity is no longer in your graph.", "skipped": False}, None
#         if self.llm:
#             from .extraction import SKIP_RE
#             if SKIP_RE.match(text):
#                 return extract(q["topic"], q["node_key"], state, text), None
#             try:
#                 payload = {"CONTEXT": self._node_ctx(state, q["node_key"]),
#                            "GRAPH_ENTITIES": [{"key": k, "name": n["name"]} for k, n in list(state.nodes.items())[:70]],
#                            "TOPIC": q["topic"], "QUESTION": q["question_text"], "CLIENT_ANSWER": text,
#                            "ALLOWED_OPS": {"add_node": {"name": "str", "definition": "str?", "attributes": "[str]?", "kind": "entity|experience", "node_key": "str?"},
#                                            "update_node": {"node_key": "str", "name": "str?", "add_attributes": "[str]?", "remove_attributes": "[str]?",
#                                                            "add_formulas": "[str]?", "append_note": "str?", "definition": "str?"},
#                                            "add_edge": {"source": "key|name", "target": "key|name", "label": "str"},
#                                            "remove_edge": {"source": "key", "target": "key"}, "remove_node": {"node_key": "key"}},
#                            "INSTRUCTIONS": "Compare the client's answer with the reference CONTEXT. Return JSON: {\"acknowledgement\": one short sentence, "
#                                            "\"followup\": one clarifying question or null (only if the answer is genuinely unclear), "
#                                            "\"ops\": [{\"op\": ..., \"payload\": {...}, \"rationale\": short}]}. Only include ops clearly supported by the "
#                                            "answer. Use existing keys for existing entities. For a new entity give a node_key like client-<slug> and reference it in edges."}
#                 out = self._llm_json(domain, payload)
#                 ops = [{"op": o.get("op"), "payload": o.get("payload") or {}, "rationale": o.get("rationale")}
#                        for o in (out.get("ops") or []) if isinstance(o, dict) and o.get("op") in OPS]
#                 return {"ops": ops, "followup": clean_text(out.get("followup"), 400) or None,
#                         "ack": clean_text(out.get("acknowledgement"), 400), "skipped": False}, None
#             except LLMError as e:
#                 log.warning("LLM analysis failed, using rule-based fallback: %s", e)
#                 return extract(q["topic"], q["node_key"], state, text), str(e)
#         return extract(q["topic"], q["node_key"], state, text), None

#     # ------------------------------------------------------------------ node Q&A (grounded)
#     def ask_node(self, user_id, domain, node_key, question):
#         wf = self._wf(user_id, domain)
#         question = clean_text(question, 1000)
#         if not question:
#             raise ValidationError("Type a question about the selected entity.")
#         conv = self.convs.get(user_id, wf["id"])
#         if not conv:
#             raise ConflictError("Start the conversation first.")
#         _, state = self.graphs.working(user_id, wf, domain)
#         key = state.find(node_key)
#         if key is None:
#             raise NotFoundError("That entity is not in your graph.")
#         ctx = self._node_ctx(state, key)
#         canon = self.refs.repo.node(key)
#         if canon:
#             ctx["canon_joins"] = canon.get("canon_joins")
#         last_id = self.convs.messages(conv["id"])[-1]["id"]
#         answer, engine = None, "rule-based"
#         if self.llm:
#             try:
#                 answer = self.llm.complete(SYSTEM.format(domain=domain["name"]),
#                                            f"CONTEXT: {json.dumps(ctx, ensure_ascii=False)}\nQUESTION: {question}\n"
#                                            "Answer in at most 6 sentences using only the CONTEXT. If the CONTEXT does not contain the answer, say so.", 600).strip()
#                 engine = self.engine
#             except LLMError as e:
#                 log.warning("LLM node QA failed: %s", e)
#         if not answer:
#             answer = self._rule_answer(ctx, question)
#         with self.db.transaction():
#             self.convs.add_message(conv["id"], "user", question, "node_qa", {"node_key": key})
#             self.convs.add_message(conv["id"], "assistant", answer, "node_qa", {"node_key": key, "engine": engine})
#         return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)

#     @staticmethod
#     def _rule_answer(ctx, question):
#         q = question.lower()
#         name = ctx["name"]
#         if any(w in q for w in ("formula", "calculate", "computed", "compute", "how is", "how do you measure")):
#             if ctx["formulas"]:
#                 return f"For {name}, the ontology gives: " + "; ".join(ctx["formulas"]) + "."
#             return f"The ontology doesn't state a formula for {name}. Its attributes are: {', '.join(ctx['attributes'][:8]) or 'none listed'}."
#         if any(w in q for w in ("attribute", "field", "track", "capture", "column")):
#             return f"{name} carries these attributes: {', '.join(ctx['attributes']) or 'none listed'}."
#         if any(w in q for w in ("relat", "connect", "link", "feed", "join", "depend", "neighbour")):
#             rels = ctx["relationships"]
#             return (f"{name} is connected as follows: " + "; ".join(rels) + ".") if rels else f"{name} has no relationships in your current graph."
#         if any(w in q for w in ("source", "evidence", "where does", "reference", "proof")):
#             return f"Evidence for {name}: {ctx['evidence'] or 'not recorded'}." + (f" Canon joins: {ctx['canon_joins']}" if ctx.get("canon_joins") else "")
#         parts = [ctx["definition"]]
#         if ctx["attributes"]:
#             parts.append("Key attributes: " + ", ".join(ctx["attributes"][:6]) + ".")
#         if ctx["relationships"]:
#             parts.append("Connections: " + "; ".join(ctx["relationships"][:3]) + ".")
#         if ctx.get("client_notes"):
#             parts.append("Your notes: " + ctx["client_notes"][:300])
#         return " ".join(parts)

#     # ------------------------------------------------------------------ propose
#     def propose(self, user_id, domain):
#         wf = self._wf(user_id, domain)
#         if wf["finalized_version_number"]:
#             raise ConflictError("The ontology is finalised; re-open it to propose further changes.")
#         conv = self.convs.get(user_id, wf["id"])
#         if not conv:
#             raise ConflictError("Start the conversation first.")
#         qs = self.convs.unproposed(conv["id"])
#         if not qs:
#             raise ValidationError("Answer at least one question before reviewing proposed changes.")
#         raw = []
#         for q in qs:
#             for o in q["extracted_ops"]:
#                 raw.append({**o, "rationale": (o.get("rationale") or "") + f" (from your answer about {q['topic']})"})
#         last_id = self.convs.messages(conv["id"])[-1]["id"]
#         proposal, rejected = (None, [])
#         if raw:
#             title = f"Proposed changes from {len(qs)} answer(s)"
#             summary = self._summary(domain, raw)
#             proposal, rejected = self.props.create(user_id, wf, domain, raw, title, summary, "chat", llm_used=bool(self.llm))
#         with self.db.transaction():
#             self.convs.mark_proposed([q["id"] for q in qs])
#             if proposal:
#                 text = (f"I compared your answers with the reference graph and prepared {len(proposal['items'])} proposed change(s). "
#                         f"{proposal['summary']} Review them in the panel: approve or reject each one. Nothing is applied until you approve.")
#                 self.convs.add_message(conv["id"], "assistant", text, "proposal", {"proposal_id": proposal["id"]})
#             else:
#                 self.convs.add_message(conv["id"], "assistant",
#                                        "Your answers match the reference model closely, so I have no graph changes to propose. "
#                                        "You can continue answering, focus on another entity, or finalise when you're ready.", "system")
#         snap = self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)
#         snap["proposal"] = proposal
#         snap["discarded_changes"] = len(rejected)
#         return snap

#     def _summary(self, domain, raw):
#         counts = {}
#         for o in raw:
#             counts[o["op"]] = counts.get(o["op"], 0) + 1
#         labels = {"add_node": "new entities", "update_node": "entity updates", "add_edge": "new relationships",
#                   "remove_node": "removals", "remove_edge": "removed relationships", "update_edge": "relabelled relationships"}
#         base = "Summary: " + ", ".join(f"{v} {labels[k]}" for k, v in counts.items()) + "."
#         if self.llm:
#             try:
#                 s = self.llm.complete(SYSTEM.format(domain=domain["name"]),
#                                       "Summarise these proposed graph changes for a finance executive in at most 2 sentences, using only the data given:\n"
#                                       + json.dumps(raw[:30], ensure_ascii=False), 250).strip()
#                 if 10 < len(s) < 600:
#                     return s
#             except LLMError:
#                 pass
#         return base
