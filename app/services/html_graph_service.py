"""HTML Graph Generator Service.

Generates self-contained, interactive HTML Knowledge Graph visualizations using Vis.js.
Saves the generated HTML into the configured graphs/ folder for dynamic rendering
inside iframes on the frontend.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{domain_name} — Knowledge Graph</title>
  <script type="text/javascript" src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body, html {{
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: radial-gradient(circle at 50% 50%, #FFFFFF 0%, #F1F5F9 100%);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      color: #0F172A;
      user-select: none;
    }}
    #network-container {{
      width: 100%;
      height: 100%;
      position: absolute;
      top: 0;
      left: 0;
    }}
    /* Top Toolbar */
    .toolbar {{
      position: absolute;
      top: 16px;
      right: 16px;
      display: flex;
      gap: 8px;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(226, 232, 240, 0.9);
      border-radius: 12px;
      padding: 6px;
      z-index: 20;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
    }}
    .tool-btn {{
      background: transparent;
      border: 1px solid transparent;
      color: #475569;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }}
    .tool-btn:hover {{
      background: rgba(241, 245, 249, 0.95);
      color: #0F172A;
      border-color: #CBD5E1;
    }}
    .tool-btn.active {{
      background: rgba(59, 130, 246, 0.12);
      color: #2563EB;
      border-color: rgba(59, 130, 246, 0.35);
    }}
    /* Floating Domain Badge */
    .domain-badge {{
      position: absolute;
      top: 16px;
      left: 16px;
      display: flex;
      align-items: center;
      gap: 10px;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(226, 232, 240, 0.9);
      border-radius: 12px;
      padding: 8px 14px;
      z-index: 20;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
    }}
    .domain-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: {domain_color};
      box-shadow: 0 0 8px {domain_color};
    }}
    .domain-title {{
      font-size: 13px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #0F172A;
    }}
    .domain-stats {{
      font-size: 11px;
      color: #64748B;
      margin-left: 4px;
    }}
    /* Legend */
    .legend {{
      position: absolute;
      bottom: 16px;
      left: 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      max-width: 60%;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(226, 232, 240, 0.9);
      border-radius: 12px;
      padding: 8px 12px;
      z-index: 20;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
    }}
    .legend-item {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      color: #334155;
      font-weight: 500;
    }}
    .legend-color {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }}
    /* Node Info Overlay */
    .inspector {{
      position: absolute;
      bottom: 16px;
      right: 16px;
      width: 320px;
      max-height: 280px;
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(16px);
      border: 1px solid rgba(226, 232, 240, 0.95);
      border-radius: 14px;
      padding: 14px;
      z-index: 20;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.12);
      display: none;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
      transition: opacity 0.2s ease;
    }}
    .inspector.open {{
      display: flex;
    }}
    .inspector-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
      border-bottom: 1px solid #E2E8F0;
      padding-bottom: 8px;
    }}
    .inspector-title {{
      font-size: 14px;
      font-weight: 700;
      color: #0F172A;
    }}
    .inspector-badge {{
      display: inline-block;
      font-size: 10px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 20px;
      background: #EFF6FF;
      color: #1D4ED8;
      border: 1px solid #DBEAFE;
    }}
    .inspector-close {{
      background: transparent;
      border: none;
      color: #94A3B8;
      font-size: 16px;
      cursor: pointer;
      line-height: 1;
    }}
    .inspector-close:hover {{
      color: #0F172A;
    }}
    .inspector-def {{
      font-size: 11px;
      line-height: 1.5;
      color: #475569;
    }}
    .inspector-section {{
      margin-top: 4px;
    }}
    .inspector-subtitle {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748B;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .tag-container {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }}
    .tag {{
      background: #F1F5F9;
      border: 1px solid #E2E8F0;
      border-radius: 6px;
      padding: 2px 6px;
      font-size: 10px;
      color: #334155;
      font-family: 'JetBrains Mono', monospace;
    }}
    .formula-tag {{
      background: #ECFDF5;
      border: 1px solid #A7F3D0;
      border-radius: 6px;
      padding: 4px 6px;
      font-size: 10px;
      color: #065F46;
      font-family: 'JetBrains Mono', monospace;
      width: 100%;
      margin-bottom: 4px;
    }}
  </style>
</head>
<body>
  <div id="network-container"></div>

  <!-- Inspector Card -->
  <div class="inspector" id="inspector">
    <div class="inspector-header">
      <div>
        <div class="inspector-title" id="insp-name">Entity</div>
        <div class="inspector-badge" id="insp-group">Group</div>
      </div>
      <button class="inspector-close" id="insp-close">&times;</button>
    </div>
    <div class="inspector-def" id="insp-def"></div>
    <div class="inspector-section" id="insp-attrs-section">
      <div class="inspector-subtitle">Attributes</div>
      <div class="tag-container" id="insp-attrs"></div>
    </div>
    <div class="inspector-section" id="insp-formulas-section">
      <div class="inspector-subtitle">Key Calculation Formulas</div>
      <div id="insp-formulas"></div>
    </div>
  </div>

  <script type="text/javascript">
    const rawNodes = {nodes_json};
    const rawEdges = {edges_json};

    // Build Legend (if element exists)
    const legendEl = document.getElementById('legend');
    if (legendEl) {
      const groupMap = new Map();
      rawNodes.forEach(n => {{
        if (n.group_name && !groupMap.has(n.group_name)) {{
          groupMap.set(n.group_name, n.color || '{domain_color}');
        }}
      }});
      groupMap.forEach((color, name) => {{
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = `<span class="legend-color" style="background:${{color}};box-shadow:0 0 6px ${{color}}"></span><span>${{name}}</span>`;
        legendEl.appendChild(item);
      }});
    }

    // Transform Nodes for Vis.js (White / Light Theme)
    const visNodes = rawNodes.map(n => {{
      const nodeColor = n.color || '{domain_color}';
      return {{
        id: n.key,
        label: n.name,
        group: n.group_name,
        color: {{
          background: '#FFFFFF',
          border: nodeColor,
          highlight: {{
            background: '#F0F9FF',
            border: nodeColor
          }},
          hover: {{
            background: '#F8FAFC',
            border: nodeColor
          }}
        }},
        font: {{
          color: '#0F172A',
          size: 13,
          face: 'Inter, sans-serif',
          bold: true
        }},
        shape: 'box',
        margin: 12,
        borderWidth: 2,
        borderWidthSelected: 3,
        shadow: {{
          enabled: true,
          color: 'rgba(0, 0, 0, 0.08)',
          size: 8,
          x: 1,
          y: 3
        }},
        raw: n
      }};
    }});

    // Transform Edges for Vis.js (White / Light Theme)
    const visEdges = rawEdges.map((e, idx) => {{
      return {{
        id: 'e_' + idx,
        from: e.source,
        to: e.target,
        label: e.label || '',
        arrows: {{
          to: {{
            enabled: true,
            scaleFactor: 0.8
          }}
        }},
        color: {{
          color: '#94A3B8',
          highlight: '#2563EB',
          hover: '#64748B'
        }},
        font: {{
          color: '#475569',
          size: 11,
          face: 'Inter, sans-serif',
          align: 'middle',
          background: 'rgba(255, 255, 255, 0.95)'
        }},
        smooth: {{
          type: 'curvedCW',
          roundness: 0.2
        }},
        length: 220
      }};
    }});

    const container = document.getElementById('network-container');
    const nodesDataset = new vis.DataSet(visNodes);
    const edgesDataset = new vis.DataSet(visEdges);

    const data = {{
      nodes: nodesDataset,
      edges: edgesDataset
    }};

    const options = {{
      physics: {{
        enabled: true,
        solver: 'forceAtlas2Based',
        forceAtlas2Based: {{
          gravitationalConstant: -50,
          centralGravity: 0.01,
          springLength: 160,
          springConstant: 0.08,
          damping: 0.4
        }},
        stabilization: {{
          iterations: 160,
          updateInterval: 25
        }}
      }},
      interaction: {{
        hover: true,
        tooltipDelay: 150,
        selectable: true,
        multiselect: false,
        navigationButtons: false,
        zoomView: true,
        dragView: true
      }}
    }};

    const network = new vis.Network(container, data, options);

    // Inspector elements
    const inspector = document.getElementById('inspector');
    const inspName = document.getElementById('insp-name');
    const inspGroup = document.getElementById('insp-group');
    const inspDef = document.getElementById('insp-def');
    const inspAttrs = document.getElementById('insp-attrs');
    const inspAttrsSection = document.getElementById('insp-attrs-section');
    const inspFormulas = document.getElementById('insp-formulas');
    const inspFormulasSection = document.getElementById('insp-formulas-section');
    const inspClose = document.getElementById('insp-close');

    function showDetails(nodeRaw) {{
      if (!nodeRaw) {{
        inspector.classList.remove('open');
        return;
      }}
      inspName.textContent = nodeRaw.name || nodeRaw.key;
      inspGroup.textContent = nodeRaw.group_name || '{domain_name}';
      inspDef.textContent = nodeRaw.definition || 'No definition available.';

      const attrs = nodeRaw.attributes || [];
      if (attrs.length > 0) {{
        inspAttrsSection.style.display = 'block';
        inspAttrs.innerHTML = attrs.map(a => `<span class="tag">${{typeof a === 'string' ? a : (a.name || a.key)}}</span>`).join('');
      }} else {{
        inspAttrsSection.style.display = 'none';
      }}

      const formulas = nodeRaw.formulas || [];
      if (formulas.length > 0) {{
        inspFormulasSection.style.display = 'block';
        inspFormulas.innerHTML = formulas.map(f => `<div class="formula-tag">${{typeof f === 'string' ? f : (f.name || f.expression)}}</div>`).join('');
      }} else {{
        inspFormulasSection.style.display = 'none';
      }}

      inspector.classList.add('open');
    }}

    inspClose.onclick = () => {{
      inspector.classList.remove('open');
      network.unselectAll();
      try {{
        window.parent.postMessage({{ type: 'NODE_DESELECTED' }}, '*');
      }} catch(e) {{}}
    }};

    // Network Selection Events
    network.on('selectNode', function(params) {{
      if (params.nodes && params.nodes.length > 0) {{
        const nodeId = params.nodes[0];
        const vNode = nodesDataset.get(nodeId);
        if (vNode && vNode.raw) {{
          showDetails(vNode.raw);
          try {{
            window.parent.postMessage({{
              type: 'NODE_SELECTED',
              key: nodeId,
              name: vNode.label,
              node: vNode.raw
            }}, '*');
          }} catch(e) {{}}
        }}
      }}
    }});

    network.on('deselectNode', function(params) {{
      if (!params.nodes || params.nodes.length === 0) {{
        inspector.classList.remove('open');
        try {{
          window.parent.postMessage({{ type: 'NODE_DESELECTED' }}, '*');
        }} catch(e) {{}}
      }}
    }});

    // Toolbar handlers (if present)
    const fitBtn = document.getElementById('btn-fit');
    if (fitBtn) {
      fitBtn.onclick = () => {{
        network.fit({{ animation: {{ duration: 600, easingFunction: 'easeInOutQuad' }} }});
      }};
    }

    const zoomInBtn = document.getElementById('btn-zoom-in');
    if (zoomInBtn) {
      zoomInBtn.onclick = () => {{
        const scale = network.getScale() * 1.3;
        network.moveTo({{ scale: scale, animation: {{ duration: 300 }} }});
      }};
    }

    const zoomOutBtn = document.getElementById('btn-zoom-out');
    if (zoomOutBtn) {
      zoomOutBtn.onclick = () => {{
        const scale = network.getScale() * 0.7;
        network.moveTo({{ scale: scale, animation: {{ duration: 300 }} }});
      }};
    }

    let physicsEnabled = true;
    const physicsBtn = document.getElementById('btn-physics');
    if (physicsBtn) {
      physicsBtn.onclick = () => {{
        physicsEnabled = !physicsEnabled;
        network.setOptions({{ physics: {{ enabled: physicsEnabled }} }});
        physicsBtn.classList.toggle('active', physicsEnabled);
      }};
    }

    // Settle physics after initial stabilization
    network.once('stabilizationIterationsDone', function() {{
      network.fit({{ animation: {{ duration: 500 }} }});
      // Gently turn off continuous physics to save CPU
      setTimeout(() => {{
        network.setOptions({{ physics: {{ enabled: false }} }});
        if (physicsBtn) physicsBtn.classList.remove('active');
        physicsEnabled = false;
      }}, 1200);
    }});

    // Listen to messages from React Parent
    window.addEventListener('message', function(event) {{
      const data = event.data;
      if (!data) return;

      if (data.type === 'SELECT_NODE' && data.key) {{
        network.selectNodes([data.key]);
        network.focus(data.key, {{ scale: 1.15, animation: {{ duration: 600, easingFunction: 'easeInOutQuad' }} }});
        const vNode = nodesDataset.get(data.key);
        if (vNode && vNode.raw) {{
          showDetails(vNode.raw);
        }}
      }} else if (data.type === 'DESELECT_NODE') {{
        network.unselectAll();
        inspector.classList.remove('open');
      }} else if (data.type === 'FIT_GRAPH') {{
        network.fit({{ animation: {{ duration: 600 }} }});
      }}
    }});

    // Notify parent window that graph is ready
    window.parent.postMessage({{ type: 'GRAPH_READY', slug: '{domain_slug}' }}, '*');
  </script>
</body>
</html>
"""


class HTMLGraphService:
    @staticmethod
    def get_graph_path(output_dir: str, slug: str) -> Path:
        """Returns the Path to the generated HTML file."""
        clean_slug = slug.strip().replace(" ", "-").replace("%20", "-").lower()
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{clean_slug}.html"

    @classmethod
    def save_html(cls, slug: str, domain: dict, nodes: list, edges: list, output_dir: str = "./graphs") -> str:
        """Compiles nodes and edges into an interactive Vis.js HTML document and writes it to disk.

        Returns the relative API path for serving via Flask: e.g. '/api/graphs/{slug}.html'
        """
        clean_slug = slug.strip().replace(" ", "-").replace("%20", "-").lower()
        domain_name = domain.get("name") or clean_slug.replace("-", " ").title()
        domain_color = domain.get("color") or "#2563EB"

        html_content = HTML_TEMPLATE.format(
            domain_name=domain_name,
            domain_slug=clean_slug,
            domain_color=domain_color,
            node_count=len(nodes),
            edge_count=len(edges),
            nodes_json=json.dumps(nodes, default=str),
            edges_json=json.dumps(edges, default=str),
        )

        file_path = cls.get_graph_path(output_dir, clean_slug)
        file_path.write_text(html_content, encoding="utf-8")
        log.info("Saved Knowledge Graph HTML visualization to %s (%d nodes, %d edges)", file_path, len(nodes), len(edges))

        return f"/api/graphs/{clean_slug}.html"
