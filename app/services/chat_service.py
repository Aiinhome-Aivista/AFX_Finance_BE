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
        return {"queue": [], "max_questions": None}

    # ------------------------------------------------------------------ snapshot
    def _msg(self, m):
        return {"id": m["id"], "role": m["role"], "kind": m["kind"], "content": m["content"], "meta": m["meta"], "created_at": m["created_at"]}

    def snapshot(self, user_id, wf, conv, new_from_id=None, updated_graph=None):
        msgs = [self._msg(m) for m in self.convs.messages(conv["id"])]
        qs = self.convs.questions(conv["id"])
        open_q = next((q for q in reversed(qs) if q["status"] == "asked"), None)
        answered = sum(1 for q in qs if q["status"] in ("answered", "skipped") and not q["is_followup"])
        last_msg = msgs[-1] if msgs else {}
        awaiting_decision = bool((last_msg.get("meta") or {}).get("awaiting_session_decision"))
        has_completion_flag = any((m.get("meta") or {}).get("plan_complete") for m in msgs)
        plan_complete = bool(open_q is None and not awaiting_decision and has_completion_flag)
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
                "answered": answered,
                "planned": None,
                "plan_complete": plan_complete,
                "awaiting_session_decision": awaiting_decision,
            },
            "awaiting_session_decision": awaiting_decision,
            "pending_change_count": pending_ops,
            "engine": self.engine,
            "llm_enabled": bool(self.llm),
            "finalized": bool(wf["finalized_version_number"])
        }
        if updated_graph is not None:
            res["graph"] = updated_graph
            res["graph_updated"] = True
        return res

    def _generate_dynamic_question(self, domain, state, qs, force=False):
        """Generates the next contextual finance interview question using the LLM and the active graph state,
        or dynamically decides to complete when the domain is sufficiently tailored."""
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
            f"Your mission is to conduct a contextual discovery interview with a finance executive to adapt "
            f"and tailor their live knowledge graph to how their actual business operates.\n\n"
            f"ACTIVE KNOWLEDGE GRAPH CONTEXT ({g_ctx['entity_count']} entities, {g_ctx['relationship_count']} relationships):\n"
            f"{g_ctx['entities_text']}\n\n"
            f"ACTIVE RELATIONSHIPS:\n{g_ctx['relationships_text']}\n\n"
            f"EVALUATION & INTERVIEW RULES:\n"
            f"1. You are NOT bound to a fixed number of questions (there is no 10-question requirement).\n"
            f"2. Dynamically evaluate whether the entities, relationships, attributes, and formulas for “{domain['name']}” are already sufficiently tailored and clear based on the active graph and previous dialogue.\n"
            f"3. {'The user has explicitly asked to continue exploring deeper nuances. Find an unexplored entity, connection, or rule and ask a question. Do NOT complete.' if force else 'If the domain context is sufficiently understood or the core customizations have been addressed, mark the interview complete (complete: true).'}\n"
            f"4. If a further question is needed to clarify an important unexplored aspect of this domain (e.g. custom organizational hierarchies, specific gross-to-net deductions, variance tracking, or calculation formulas):\n"
            f"   - Ground the question deeply in the active entities, attributes, formulas, or connections shown above.\n"
            f"   - Pick an entity or process area that hasn't been thoroughly explored yet.\n"
            f"   - NEVER repeat previously asked questions or concepts.\n"
            f"   - Ask exactly ONE clear, friendly, and practical question for the user to answer."
        )

        user_prompt = {
            "domain": domain["name"],
            "questions_answered_so_far": question_idx - 1,
            "previously_asked_questions": asked_questions[-12:],
            "previous_dialogue_summary": past_qa[-6:],
            "instructions": (
                "Decide if another question is needed for this domain. Return JSON in one of these two forms:\n"
                "If another question is needed:\n"
                "{\"complete\": false, \"question\": \"...\", \"target_node_key\": \"key of entity being explored\", \"topic\": \"naming|attributes|relationships|rules|missing|experience\"}\n"
                "If the domain context is sufficiently complete and no further questions are needed:\n"
                "{\"complete\": true, \"reason\": \"...\"}"
            )
        }

        if self.llm:
            try:
                out = self.llm.complete_json(sys_prompt, json.dumps(user_prompt, ensure_ascii=False), max_tokens=600)
                if out.get("complete") is True and question_idx > 1 and not force:
                    log.info("LLM concluded domain interview for %s after %d questions: %s", domain["name"], question_idx - 1, out.get("reason"))
                    return None, None, None

                q_text = clean_text(out.get("question"), 600)
                node_key = out.get("target_node_key") or ""
                if node_key not in state.nodes:
                    node_key = state.find(node_key) or (list(state.nodes.keys())[0] if state.nodes else "domain")
                topic = out.get("topic") or "attributes"
                if q_text and len(q_text) >= 15:
                    return q_text, node_key, topic
                elif out.get("complete") is True and not force:
                    return None, None, None
            except Exception as e:
                log.warning("LLM dynamic question generation failed: %s", e)

        # Fallback question based on unvisited nodes in graph (only if <= 5 questions answered or force is True)
        if question_idx > 5 and not force:
            return None, None, None

        candidates = [k for k in state.nodes if k not in {q.get("node_key") for q in qs}] or list(state.nodes.keys())
        target = candidates[0] if candidates else list(state.nodes.keys())[0]
        fb_topics = ["attributes", "relationships", "rules", "naming"]
        chosen_topic = fb_topics[(question_idx - 1) % len(fb_topics)]
        fb_q = self._question_text(domain, state, target, chosen_topic)
        return fb_q, target, chosen_topic

    def _ask_next(self, user_id, wf, domain, conv, force=False):
        """Ask the next dynamic question grounded in active graph. Returns True if a question was asked."""
        _, state = self.graphs.working(user_id, wf, domain)
        qs = self.convs.questions(conv["id"])
        answered = sum(1 for q in qs if q["status"] in ("answered", "skipped") and not q["is_followup"])
        if answered >= 25 and not force:
            return False

        seq = (qs[-1]["seq"] if qs else 0) + 1
        text, node_key, topic = self._generate_dynamic_question(domain, state, qs, force=force)
        if not text:
            return False

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
            u = self.db.one("SELECT display_name FROM users WHERE id = %s", (user_id,))
            full_name = u["display_name"] if u and u.get("display_name") else ""
            first_name = full_name.split()[0] if full_name else "there"
            hi = (f"Hello {first_name}! Welcome to “{domain['name']}”. "
                  "I'm here to help adapt this financial model to how your organization actually operates. "
                  "We'll explore your key processes and reporting structures together, "
                  "and your inputs will automatically refine and update the knowledge graph in real time.")
            self.convs.add_message(cid, "assistant", hi, "intro", {"engine": self.engine})
            self._ask_next(user_id, wf, domain, conv)
        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]))

    def get(self, user_id, domain):
        wf = self._wf(user_id, domain)
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            return {"conversation_id": None, "messages": [], "open_question": None, "progress": {"answered": 0, "planned": None, "plan_complete": False},
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

            # Combine any unproposed ops from previous questions that didn't apply earlier with current ops
            unproposed_qs = self.convs.unproposed(conv["id"])
            staged_ops = []
            staged_qids = []
            for uq in unproposed_qs:
                if uq["id"] != q["id"] and uq.get("extracted_ops"):
                    staged_qids.append(uq["id"])
                    for uo in uq["extracted_ops"]:
                        p = dict(uo.get("payload") or {})
                        if uo.get("op") in ("update_node", "remove_node") and "node_key" in p:
                            res_nk = state.find(p["node_key"])
                            if res_nk:
                                p["node_key"] = res_nk
                        staged_ops.append({"op": uo["op"], "payload": p, "rationale": uo.get("rationale")})

            candidate_ops = staged_ops + (analysis.get("ops") or [])

            # Ensure any new node in candidate_ops has at least one connecting edge
            new_node_names = set()
            for op in candidate_ops:
                if op.get("op") == "add_node":
                    n_name = (op.get("payload") or {}).get("name") or (op.get("payload") or {}).get("node_key")
                    if n_name:
                        new_node_names.add(n_name)

            # Remove any invalid self-loop edges generated by LLM (source == target)
            candidate_ops = [
                op for op in candidate_ops
                if not (op.get("op") == "add_edge" and (op.get("payload") or {}).get("source") and
                        (op.get("payload") or {}).get("source") == (op.get("payload") or {}).get("target"))
            ]

            if new_node_names:
                connected_nodes = set()
                for op in candidate_ops:
                    if op.get("op") == "add_edge":
                        s = (op.get("payload") or {}).get("source")
                        t = (op.get("payload") or {}).get("target")
                        if s and t and s != t:
                            if s in new_node_names:
                                connected_nodes.add(s)
                            if t in new_node_names:
                                connected_nodes.add(t)

                missing_edge_nodes = new_node_names - connected_nodes
                target_nk = (state.find(q.get("node_key")) if q.get("node_key") else None) or (list(state.nodes.keys())[0] if state.nodes else None)

                if target_nk:
                    for nn in missing_edge_nodes:
                        candidate_ops.append({
                            "op": "add_edge",
                            "payload": {"source": nn, "target": target_nk, "label": "relates to"},
                            "rationale": f"Connect new entity '{nn}' to active domain context '{target_nk}'"
                        })

            accepted, rejected, _ = validate_sequence(state, candidate_ops, number + 1)
            ops = [{"op": a["op"], "payload": a["payload"], "rationale": a["rationale"]} for a in accepted]
            status = "skipped" if analysis["skipped"] else "answered"
            self.convs.answer_question(q["id"], text, analysis.get("ops") or [], status)
            if status == "answered":
                self.convs.add_experience(user_id, wf["id"], q["id"], q["node_key"], q["topic"], text)

            # Apply accepted graph updates dynamically to the user's graph version
            if ops:
                try:
                    summary = f"Dynamic graph update from question #{q['seq']} answer: {text[:60]}"
                    new_version = self.graphs.create_version(
                        user_id, wf, domain, ops, "customization", None, summary, "user"
                    )
                    number, state = self.graphs.working(user_id, wf, domain)
                    updated_graph_data = self.graphs.graph(user_id, wf, domain, new_version)

                    # Mark applied question ops so pending_change_count clears automatically
                    self.convs.mark_proposed([q["id"]] + staged_qids)

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

            # Detailed breakdown of graph changes and duplicate detection
            added_nodes = []
            added_edges = []
            updated_nodes = []
            duplicate_nodes = list(analysis.get("duplicate_nodes") or [])
            duplicate_edges = []

            for a in accepted:
                op_type = a.get("op")
                p = a.get("payload") or {}
                if op_type == "add_node":
                    n_name = p.get("name") or p.get("node_key")
                    if n_name and n_name not in added_nodes:
                        added_nodes.append(n_name)
                elif op_type == "add_edge":
                    s_key = p.get("source")
                    t_key = p.get("target")
                    lbl = p.get("label") or "relates to"
                    s_name = state.nodes.get(s_key, {}).get("name", s_key) if s_key in state.nodes else s_key
                    t_name = state.nodes.get(t_key, {}).get("name", t_key) if t_key in state.nodes else t_key
                    if s_name and t_name:
                        added_edges.append(f"“{s_name}” → “{t_name}” ({lbl})")
                elif op_type == "update_node":
                    nk = p.get("node_key")
                    n_name = p.get("name") or (state.nodes.get(nk, {}).get("name", nk) if nk in state.nodes else nk)
                    details = []
                    if p.get("name") and p.get("name") != state.nodes.get(nk, {}).get("name"):
                        details.append(f"renamed to “{p['name']}”")
                    if p.get("add_attributes"):
                        attrs_str = ", ".join(p["add_attributes"][:3])
                        details.append(f"added attributes: {attrs_str}")
                    if p.get("append_note") or p.get("definition"):
                        details.append("updated definitions/rules")
                    detail_str = f" ({'; '.join(details)})" if details else ""
                    if n_name and n_name not in updated_nodes:
                        updated_nodes.append(f"“{n_name}”{detail_str}")

            for r in rejected:
                r_op = r.get("op")
                r_pl = r.get("payload") or {}
                errs = r.get("errors") or []
                err_text = " ".join(errs).lower()
                if r_op == "add_node" or "already exists" in err_text:
                    node_name = r_pl.get("name") or r_pl.get("node_key")
                    if not node_name:
                        import re
                        m = re.search(r"named ['\"]([^'\"]+)['\"]", " ".join(errs))
                        if m:
                            node_name = m.group(1)
                    if node_name and node_name not in duplicate_nodes:
                        duplicate_nodes.append(node_name)
                elif r_op == "add_edge":
                    s_k = r_pl.get("source")
                    t_k = r_pl.get("target")
                    s_n = state.nodes.get(s_k, {}).get("name", s_k) if s_k in state.nodes else s_k
                    t_n = state.nodes.get(t_k, {}).get("name", t_k) if t_k in state.nodes else t_k
                    lbl = r_pl.get("label") or "relates to"
                    duplicate_edges.append(f"“{s_n}” → “{t_n}” ({lbl})")

            # If target node of the question is already in graph, mark it as existing node confirmed
            target_node = state.nodes.get(q.get("node_key"))
            if target_node:
                t_name = target_node.get("name")
                if t_name and t_name not in added_nodes and t_name not in duplicate_nodes:
                    duplicate_nodes.append(t_name)

            base_ack = (analysis.get("ack") or "").strip()
            if base_ack:
                import re
                base_ack = re.sub(r"\s*\([^)]*(?:already exist|duplicate nodes|no duplicate|Dynamically updated|I reviewed)[^)]*\)", "", base_ack).strip()
            else:
                base_ack = "Noted and processed."

            change_details = []
            if added_nodes:
                change_details.append(f"✨ New Node Added: {', '.join([f'“{n}”' for n in added_nodes])}")
            if added_edges:
                change_details.append(f"🔗 New Relationship Built: {', '.join(added_edges)}")
            if updated_nodes:
                change_details.append(f"📝 Entity Updated: {', '.join(updated_nodes)}")
            if duplicate_nodes:
                change_details.append(f"⚠️ Existing Node Detected (Duplicate avoided): {', '.join([f'“{n}”' for n in duplicate_nodes])}")
            if duplicate_edges:
                change_details.append(f"⚠️ Existing Relationship Detected (Duplicate avoided): {', '.join(duplicate_edges)}")

            if change_details:
                ack = base_ack + "\n\n" + "\n".join(change_details)
            elif ops:
                ack = base_ack + f"\n\n• Knowledge graph updated ({len(ops)} change{'s' if len(ops) != 1 else ''} applied)."
            else:
                ack = base_ack

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
                    msgs = self.convs.messages(conv["id"])
                    last_msg = msgs[-1] if msgs else {}
                    already_deciding = bool((last_msg.get("meta") or {}).get("awaiting_session_decision"))
                    already_complete = any((m.get("meta") or {}).get("plan_complete") for m in msgs)
                    if not already_deciding and not already_complete:
                        self.convs.add_message(
                            conv["id"],
                            "assistant",
                            f"We've covered the core processes for “{domain['name']}”. Do you want to continue exploring further nuances or proceed to review?",
                            "decision",
                            {"awaiting_session_decision": True}
                        )

        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id, updated_graph=updated_graph_data)

    def decide_session(self, user_id, domain, action):
        """Handle user choice to continue exploring or proceed to review."""
        wf = self._wf(user_id, domain)
        conv = self.convs.get(user_id, wf["id"])
        if not conv:
            return self.get(user_id, domain)

        action = (action or "").strip().lower()
        last_id = self.convs.messages(conv["id"])[-1]["id"] if self.convs.messages(conv["id"]) else 0

        with self.db.transaction():
            if action == "continue":
                self.convs.add_message(conv["id"], "user", "Continue exploring", "answer")
                asked = self._ask_next(user_id, wf, domain, conv, force=True)
                if not asked:
                    _, state = self.graphs.working(user_id, wf, domain)
                    qs = self.convs.questions(conv["id"])
                    seq = (qs[-1]["seq"] if qs else 0) + 1
                    target = list(state.nodes.keys())[0] if state.nodes else "domain"
                    node_name = state.nodes[target]["name"] if target in state.nodes else target
                    q_text = f"What specific exceptions, custom accounting treatments, or business rules apply to “{node_name}” in your setup?"
                    qid = self.convs.add_question(conv["id"], seq, target, "rules", q_text)
                    self.convs.add_message(conv["id"], "assistant", q_text, "question",
                                           {"question_id": qid, "node_key": target, "topic": "rules"})
            else:
                self.convs.add_message(conv["id"], "user", "Proceed to review", "answer")
                answered_count = sum(1 for q_item in self.convs.questions(conv["id"]) if q_item["status"] in ("answered", "skipped") and not q_item["is_followup"])
                self.convs.add_message(
                    conv["id"],
                    "assistant",
                    f"Domain Discovery Complete! Based on our dialogue, your Knowledge Graph for “{domain['name']}” has been tailored with your business nuances ({answered_count} question{'s' if answered_count != 1 else ''} explored). You can now review the proposed graph changes.",
                    "system",
                    {"plan_complete": True}
                )

        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id)

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
                        "add_edge": {"source": "new node name or existing entity key/name", "target": "existing entity key/name or new node name", "label": "str e.g. relates to, feeds, categorizes, defines"},
                        "remove_edge": {"source": "key", "target": "key"},
                        "remove_node": {"node_key": "key"}
                    },
                    "DUPLICATE_PREVENTION_RULES": [
                        "Check ACTIVE_GRAPH_NODES first. If an entity with the same or similar name already exists, DO NOT add a new node. Use update_node with its existing key instead.",
                        "Check ACTIVE_GRAPH_EDGES first. If an edge already exists between source and target, DO NOT add a duplicate edge.",
                        "If the user says 'same', 'nothing', 'no change', or confirms existing behavior, return ops: [] and an affirmative acknowledgement.",
                        "If the user introduces or mentions a DISTINCT new entity, calculation model, or table (not already in ACTIVE_GRAPH_NODES), you MUST generate an 'add_node' operation with a descriptive definition and attributes, and an 'add_edge' operation linking it to the relevant active entity."
                    ],
                    "INSTRUCTIONS": (
                        "Analyze the client's answer against the active graph. Return JSON:\n"
                        "{\n"
                        "  \"acknowledgement\": \"Short sentence summarizing what was understood and changed\",\n"
                        "  \"duplicate_entities_found\": [\"names of any entities mentioned by user that already exist in ACTIVE_GRAPH_NODES so no duplicate node is added\"],\n"
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
                        payload = dict(o.get("payload") or {})
                        op_type = o.get("op")
                        if op_type in ("update_node", "remove_node"):
                            nk = payload.get("node_key") or q.get("node_key")
                            resolved = state.find(nk) or (state.find(q.get("node_key")) if q.get("node_key") else None)
                            if resolved:
                                payload["node_key"] = resolved

                            if op_type == "update_node":
                                raw_attrs = payload.pop("attributes", None)
                                if raw_attrs:
                                    if isinstance(raw_attrs, dict):
                                        new_attrs = [str(k) for k in raw_attrs.keys()]
                                        notes_from_dict = [f"{k}: {v}" for k, v in raw_attrs.items() if v]
                                        if notes_from_dict:
                                            payload["append_note"] = (payload.get("append_note") or "") + ("\n" if payload.get("append_note") else "") + "; ".join(notes_from_dict)
                                    elif isinstance(raw_attrs, list):
                                        new_attrs = [str(x) for x in raw_attrs]
                                    else:
                                        new_attrs = []
                                    payload.setdefault("add_attributes", []).extend(new_attrs)

                                if not payload.get("add_attributes") and not payload.get("append_note") and text:
                                    payload["append_note"] = text[:300]
                        elif op_type in ("add_edge", "remove_edge"):
                            src_ref = payload.get("source") or q.get("node_key")
                            tgt_ref = payload.get("target")
                            src = state.find(src_ref) or (state.find(q.get("node_key")) if q.get("node_key") else None)
                            tgt = state.find(tgt_ref) if tgt_ref else None
                            if src and tgt_ref and not tgt:
                                tgt_name = clean_text(tgt_ref, 120)
                                tgt_key = slugify(tgt_name, "client-")
                                clean_ops.append({
                                    "op": "add_node",
                                    "payload": {
                                        "name": tgt_name,
                                        "node_key": tgt_key,
                                        "kind": "entity",
                                        "definition": f"Client entity: {tgt_name}.",
                                        "attributes": [],
                                        "formulas": []
                                    },
                                    "rationale": f"Auto-created entity '{tgt_name}' to complete relationship."
                                })
                                tgt = tgt_key
                            if src and tgt:
                                payload["source"] = src
                                payload["target"] = tgt
                        clean_ops.append({
                            "op": op_type,
                            "payload": payload,
                            "rationale": o.get("rationale") or "Derived dynamically from user interview response."
                        })
                return {
                    "ops": clean_ops,
                    "followup": clean_text(out.get("followup"), 400) or None,
                    "ack": clean_text(out.get("acknowledgement"), 400),
                    "duplicate_nodes": [clean_text(x, 120) for x in (out.get("duplicate_entities_found") or []) if clean_text(x, 120)],
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
            f"You are the Finance Knowledge Graph Assistant for “{domain['name']}”.\n"
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
        updated_graph_data = None
        has_graph_intent = any(w in text.lower() for w in ("add", "create", "new entity", "new node", "insert", "link", "connect", "delete", "remove", "rename", "update entity", "entity named", "node named"))

        if self.llm and has_graph_intent:
            try:
                graph_payload = {
                    "DOMAIN": domain["name"],
                    "ACTIVE_GRAPH_NODES": [{"key": k, "name": n["name"], "attributes": n.get("attributes", [])} for k, n in list(state.nodes.items())[:80]],
                    "ACTIVE_GRAPH_EDGES": [{"source": s, "target": t, "label": l} for (s, t, l) in list(state.edges.keys())[:80]],
                    "USER_REQUEST": text,
                    "ALLOWED_OPS": {
                        "add_node": {"name": "str", "definition": "str?", "attributes": "[str]?", "kind": "entity|experience", "node_key": "str?"},
                        "update_node": {"node_key": "str", "name": "str?", "add_attributes": "[str]?", "remove_attributes": "[str]?", "add_formulas": "[str]?", "append_note": "str?", "definition": "str?"},
                        "add_edge": {"source": "existing key|name", "target": "existing key|name", "label": "str"},
                        "remove_edge": {"source": "key", "target": "key"},
                        "remove_node": {"node_key": "key"}
                    },
                    "INSTRUCTIONS": (
                        "Analyze if the user is requesting to add, update, link, or remove entities/nodes in the Knowledge Graph. Return JSON:\n"
                        "{\n"
                        "  \"reply\": \"Clear response to user confirming what was created/updated.\",\n"
                        "  \"ops\": [\n"
                        "     {\"op\": \"...\", \"payload\": {...}, \"rationale\": \"...\"}\n"
                        "  ]\n"
                        "}"
                    )
                }
                out = self.llm.complete_json(
                    f"You are the Finance Knowledge Graph Assistant for “{domain['name']}”. Extract structured graph operations from the user request.",
                    json.dumps(graph_payload, ensure_ascii=False),
                    max_tokens=850
                )
                if isinstance(out, dict):
                    raw_ops = out.get("ops") or []
                    reply_text = out.get("reply") or out.get("acknowledgement") or out.get("answer")
                    if raw_ops:
                        # Remove any invalid self-loop edges generated by LLM (source == target)
                        raw_ops = [
                            ro for ro in raw_ops
                            if not (ro.get("op") == "add_edge" and (ro.get("payload") or {}).get("source") and
                                    (ro.get("payload") or {}).get("source") == (ro.get("payload") or {}).get("target"))
                        ]

                        # Auto-connect edge if add_node has no edge
                        new_node_names = set()
                        for ro in raw_ops:
                            if ro.get("op") == "add_node":
                                nn = (ro.get("payload") or {}).get("name") or (ro.get("payload") or {}).get("node_key")
                                if nn:
                                    new_node_names.add(nn)
                        if new_node_names:
                            connected = set()
                            for ro in raw_ops:
                                if ro.get("op") == "add_edge":
                                    s = (ro.get("payload") or {}).get("source")
                                    t = (ro.get("payload") or {}).get("target")
                                    if s and t and s != t:
                                        if s in new_node_names:
                                            connected.add(s)
                                        if t in new_node_names:
                                            connected.add(t)
                            fallback_t = (state.find(node_key) if node_key else None) or (list(state.nodes.keys())[0] if state.nodes else None)
                            if fallback_t:
                                for nn in (new_node_names - connected):
                                    raw_ops.append({
                                        "op": "add_edge",
                                        "payload": {"source": nn, "target": fallback_t, "label": "relates to"},
                                        "rationale": f"Connect new entity '{nn}' to active graph context '{fallback_t}'"
                                    })

                        accepted, rejected, _ = validate_sequence(state, raw_ops, number + 1)
                        ops = [{"op": a["op"], "payload": a["payload"], "rationale": a["rationale"]} for a in accepted]
                        if ops:
                            try:
                                summary = f"Chat graph update: {text[:60]}"
                                new_ver = self.graphs.create_version(
                                    user_id, wf, domain, ops, "customization", None, summary, "user"
                                )
                                updated_graph_data = self.graphs.graph(user_id, wf, domain, new_ver)
                                try:
                                    from flask import current_app
                                    from .html_graph_service import HTMLGraphService
                                    graph_dir = current_app.config["APP_CONFIG"].GRAPH_DIR
                                    html_url = HTMLGraphService.save_html(domain["slug"], domain, updated_graph_data.get("nodes", []), updated_graph_data.get("edges", []), output_dir=graph_dir)
                                    updated_graph_data["html_url"] = html_url
                                except Exception:
                                    pass

                                added_nodes_list = [p.get("name") or p.get("node_key") for a in accepted if a.get("op") == "add_node" for p in [a.get("payload") or {}]]
                                added_edges_list = [f"“{p.get('source')}” → “{p.get('target')}”" for a in accepted if a.get("op") == "add_edge" for p in [a.get("payload") or {}]]
                                extra_lines = []
                                if added_nodes_list:
                                    extra_lines.append(f"✨ New Node Added: {', '.join([f'“{n}”' for n in added_nodes_list])}")
                                if added_edges_list:
                                    extra_lines.append(f"🔗 New Relationship Built: {', '.join(added_edges_list)}")
                                if extra_lines:
                                    answer = (reply_text or "Updated your knowledge graph.") + "\n\n" + "\n".join(extra_lines)
                                else:
                                    answer = reply_text
                                engine = self.engine
                            except Exception as ex:
                                log.warning("Chat graph update execution failed: %s", ex)
                    elif reply_text:
                        answer = reply_text
                        engine = self.engine
            except Exception as e:
                log.warning("LLM chat message graph extraction failed: %s", e)

        if not answer and self.llm:
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

        return self.snapshot(user_id, wf, self.convs.get(user_id, wf["id"]), new_from_id=last_id, updated_graph=updated_graph_data)

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
