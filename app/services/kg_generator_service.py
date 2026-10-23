"""LLM-powered Knowledge Graph generator for Finance domains.

Generates structured ontology nodes (entities, properties, formulas) and relationships (edges)
grounded in domain specifications and custom user business prompts.
"""
import json
import logging
import re

from ..errors import ValidationError
from ..llm import LLMError
from ..repositories.graphs import GraphRepository
from .graph_ops import clean_text, slugify
from .workflow_service import WorkflowService

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an Enterprise Finance Knowledge Graph Architect specializing in FMCG and Corporate Finance.
Your task is to generate an interconnected, production-ready Knowledge Graph for the domain: “{domain_name}”.

Generate 7 to 14 core entities (nodes) and 8 to 16 directed relationships (edges) that model how finance operations,
data structures, drivers, and calculations flow in this domain.

Each node MUST represent a distinct financial entity, metric set, master-data spine, or operational mechanism.
Each edge MUST represent a directed business relationship (e.g. "drives inputs", "reforecasts", "stages methodology", "produces versions", "consolidates into").

RESPOND ONLY WITH A SINGLE VALID JSON OBJECT matching this exact structure:
{{
  "nodes": [
    {{
      "key": "unique_lowercase_slug",
      "name": "Standard Business Name",
      "group_name": "Sub-domain or Group (e.g. Planning, Spine, Execution, Governance)",
      "definition": "Clear 1-2 sentence definition explaining its role in the finance function.",
      "attributes": ["attr1", "attr2", "attr3", "attr4"],
      "formulas": ["Key Calculation Formula e.g. Variance = Actual - Budget"]
    }}
  ],
  "edges": [
    {{
      "source": "source_node_key",
      "target": "target_node_key",
      "label": "verb phrase describing the relationship"
    }}
  ]
}}
"""

PALETTE = [
    "#2563EB",  # blue
    "#7C3AED",  # purple
    "#059669",  # emerald
    "#D97706",  # amber
    "#DC2626",  # red
    "#0891B2",  # cyan
    "#4F46E5",  # indigo
    "#DB2777",  # pink
]


class KGGeneratorService:
    def __init__(self, db, llm=None):
        self.db = db
        self.llm = llm
        self.graphs = GraphRepository(db) if db else None
        self.wfs = WorkflowService(db) if db else None

    def generate_graph(self, domain: dict, prompt: str = "", reference_nodes: list = None) -> dict:
        """Calls the LLM to generate a complete Knowledge Graph for the given domain."""
        if not self.llm:
            raise ValidationError("No LLM provider is configured. Please configure LLM_PROVIDER in .env.")

        domain_name = domain.get("name", "Finance Domain")
        domain_desc = domain.get("description", "")
        domain_color = domain.get("color", "#2563EB")

        user_content = f"Domain: {domain_name}\nDescription: {domain_desc}\n"
        if reference_nodes:
            sample_names = [n.get("name") for n in reference_nodes[:8] if isinstance(n, dict)]
            if sample_names:
                user_content += f"Reference Entities in this area: {', '.join(sample_names)}\n"
        if prompt and prompt.strip():
            user_content += f"Client Business Requirements / Nuances:\n{prompt.strip()}\n"
        else:
            user_content += "Create a comprehensive, cohesive enterprise graph covering data inputs, simulation/drivers, calendar cadence, versions, and governance."

        try:
            raw = self.llm.complete_json(
                SYSTEM_PROMPT.format(domain_name=domain_name),
                user_content,
                max_tokens=2800
            )
        except LLMError as e:
            log.error("LLM Knowledge Graph generation failed: %s", e)
            raise ValidationError(f"LLM failed to generate knowledge graph: {e}")

        raw_nodes = raw.get("nodes") or []
        raw_edges = raw.get("edges") or []

        if not raw_nodes:
            raise ValidationError("The LLM did not return any nodes for the knowledge graph.")

        # Process and normalize nodes
        nodes = []
        node_keys = set()
        group_colors = {}
        color_idx = 0

        for idx, n in enumerate(raw_nodes):
            if not isinstance(n, dict):
                continue
            name = clean_text(n.get("name"), 120) or f"Entity {idx+1}"
            raw_key = clean_text(n.get("key"), 60) or slugify(name)
            key = slugify(raw_key)

            # Ensure unique key
            base_key = key
            counter = 2
            while key in node_keys:
                key = f"{base_key}-{counter}"
                counter += 1
            node_keys.add(key)

            grp = clean_text(n.get("group_name"), 80) or domain_name
            if grp not in group_colors:
                group_colors[grp] = PALETTE[color_idx % len(PALETTE)]
                color_idx += 1

            attrs = [clean_text(a, 100) for a in (n.get("attributes") or []) if clean_text(a, 100)]
            formulas = [clean_text(f, 200) for f in (n.get("formulas") or []) if clean_text(f, 200)]
            defn = clean_text(n.get("definition"), 1500) or f"Entity representing {name} in {domain_name}."

            nodes.append({
                "key": key,
                "name": name,
                "group_key": slugify(grp),
                "group_name": grp,
                "color": group_colors[grp],
                "kind": "entity",
                "role": "core",
                "definition": defn,
                "attributes": attrs,
                "formulas": formulas,
                "origin": "user",
                "change_state": "unchanged"
            })

        # Process and normalize edges
        edges = []
        seen_edges = set()

        for e in raw_edges:
            if not isinstance(e, dict):
                continue
            s = slugify(clean_text(e.get("source"), 60))
            t = slugify(clean_text(e.get("target"), 60))
            lbl = clean_text(e.get("label"), 80) or "relates to"

            # Check if source or target need fuzzy match with node keys
            if s not in node_keys:
                match = next((k for k in node_keys if k in s or s in k), None)
                if match:
                    s = match
            if t not in node_keys:
                match = next((k for k in node_keys if k in t or t in k), None)
                if match:
                    t = match

            if s in node_keys and t in node_keys and s != t:
                edge_id = (s, t, lbl)
                if edge_id not in seen_edges:
                    seen_edges.add(edge_id)
                    edges.append({
                        "source": s,
                        "target": t,
                        "label": lbl,
                        "origin": "user",
                        "change_state": "unchanged",
                        "cross_domain": False,
                        "origin_graph": "llm_generated"
                    })

        return {
            "domain": {
                "slug": domain.get("slug", "finance"),
                "code": domain.get("code", "F1"),
                "name": domain_name,
                "color": domain_color
            },
            "version": {
                "number": 1,
                "label": "KG-AI (LLM Generated)",
                "kind": "llm_generated",
                "readonly": False
            },
            "nodes": nodes,
            "edges": edges,
            "prompt_used": prompt
        }

    def generate_and_persist(self, user_id, domain: dict, wf: dict, prompt: str = "") -> dict:
        """Generates a graph with LLM and atomically saves it as a new custom version."""
        res = self.generate_graph(domain, prompt)
        if not self.db or not self.graphs:
            return res

        nodes = res["nodes"]
        edges = res["edges"]

        with self.db.transaction():
            latest = self.graphs.latest(user_id, wf["id"])
            number = (latest["version_number"] if latest else 0) + 1
            parent = latest["version_number"] if latest else 0
            summary = f"AI Generated Knowledge Graph ({len(nodes)} entities, {len(edges)} relationships)"
            if prompt and prompt.strip():
                summary += f" based on: {prompt.strip()[:80]}"

            for n in nodes:
                n["added_in_version"] = number
                n["change_state"] = "added" if parent > 0 else "unchanged"
                n["origin"] = "user"
            for e in edges:
                e["added_in_version"] = number
                e["change_state"] = "added" if parent > 0 else "unchanged"
                e["origin"] = "user"

            vid = self.graphs.create_version(user_id, wf["id"], number, "customization", parent, None, summary)
            self.graphs.insert_nodes(vid, nodes)
            self.graphs.insert_edges(vid, edges)
            if self.wfs:
                self.wfs.on_version_created(wf, "customization", latest is None)

            res["version"] = {
                "number": number,
                "label": f"KG{number} (AI Generated)",
                "kind": "customization",
                "summary": summary,
                "readonly": False
            }

        # Add announcement to conversation so Chatbot is immediately grounded in the new graph
        try:
            from ..repositories.conversations import ConversationRepository
            convs = ConversationRepository(self.db)
            conv = convs.get(user_id, wf["id"])
            if not conv:
                from .chat_service import ChatService
                ChatService(self.db, self.llm).start(user_id, domain)
                conv = convs.get(user_id, wf["id"])
            if conv:
                notice = (
                    f"✨ Knowledge Graph for “{domain.get('name')}” has been generated by the LLM "
                    f"({len(nodes)} entities, {len(edges)} relationships).\n\n"
                    "I am your Knowledge Graph Assistant. You can explore these entities, their formulas, attributes, and connections through this interactive discovery interview."
                )
                convs.add_message(conv["id"], "assistant", notice, "intro", {"engine": f"llm:{self.llm.name}" if self.llm else "llm"})
        except Exception as err:
            log.warning("Could not append announcement to conversation: %s", err)

        return res

