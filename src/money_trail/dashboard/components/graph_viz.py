"""Interactive Graph Explorer visualization component.

Renders physics-based interactive graph networks using vis.js via HTML components,
complete with directional edges, distinct entity styling, and an accessible
tabular alternative for screen readers and tabular inspection.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
import streamlit as st
import streamlit.components.v1 as components


def render_interactive_graph(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    height: Any = 500,
    highlight_nodes: Optional[List[str]] = None,
) -> str:
    """Render an interactive, physics-driven graph canvas with pan, zoom, and tooltips."""
    if not nodes:
        msg = "No graph nodes to display in the current scope."
        st.info(msg)
        return msg

    h = int(str(height).replace("px", "")) if height else 500
    highlight_set = set(highlight_nodes or [])

    # Format nodes for vis.js
    vis_nodes = []
    for n in nodes:
        nid = n["id"]
        ntype = n.get("type", "Node")
        is_center = n.get("is_center", False)
        is_highlighted = nid in highlight_set

        # Node styling based on entity type
        if ntype == "Account":
            color = "#10B981" if is_highlighted else "#06B6D4"  # emerald / cyan
            shape = "dot"
            size = 28 if is_center else 20
        elif ntype == "Transaction":
            color = "#F59E0B" if is_center else ("#EF4444" if is_highlighted else "#F97316")  # amber / orange / red
            shape = "diamond" if is_center else "square"
            size = 32 if is_center else 22
        elif ntype == "Finding":
            color = "#8B5CF6"  # purple
            shape = "triangle"
            size = 24
        elif ntype == "Evidence":
            color = "#3B82F6"  # blue
            shape = "hexagon"
            size = 22
        elif ntype == "Memory":
            color = "#EC4899"  # pink
            shape = "star"
            size = 22
        else:
            color = "#64748B"  # slate
            shape = "ellipse"
            size = 18

        label = n.get("label", nid)
        title = n.get("title", f"<b>{ntype}</b><br>{nid}")

        vis_nodes.append({
            "id": nid,
            "label": label,
            "title": title,
            "shape": shape,
            "size": size,
            "color": {
                "background": color,
                "border": "#FFFFFF",
                "highlight": {"background": "#F43F5E", "border": "#FFFFFF"},
            },
            "font": {"color": "#E2E8F0", "face": "system-ui, sans-serif", "size": 12},
            "borderWidth": 2 if is_center or is_highlighted else 1,
        })

    # Format edges for vis.js
    vis_edges = []
    for e in edges:
        vis_edges.append({
            "from": e["from"],
            "to": e["to"],
            "label": e.get("label", ""),
            "arrows": "to",
            "color": {"color": "#64748B", "highlight": "#F43F5E"},
            "font": {"color": "#94A3B8", "size": 10, "align": "middle"},
            "smooth": {"type": "curvedCW", "roundness": 0.15},
        })

    html_payload = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
      <style type="text/css">
        body {{
          margin: 0;
          padding: 0;
          background: #0F172A;
          color: #E2E8F0;
          font-family: system-ui, -apple-system, sans-serif;
        }}
        #network-canvas {{
          width: 100%;
          height: {height}px;
          border: 1px solid #334155;
          border-radius: 8px;
          background: radial-gradient(circle, #1E293B 10%, #0F172A 90%);
        }}
      </style>
    </head>
    <body>
      <div id="network-canvas"></div>
      <script type="text/javascript">
        var container = document.getElementById('network-canvas');
        var data = {{
          nodes: new vis.DataSet({json.dumps(vis_nodes)}),
          edges: new vis.DataSet({json.dumps(vis_edges)})
        }};
        var options = {{
          physics: {{
            barnesHut: {{
              gravitationalConstant: -2500,
              centralGravity: 0.3,
              springLength: 95,
              springConstant: 0.04
            }},
            minVelocity: 0.75
          }},
          interaction: {{
            hover: true,
            tooltipDelay: 100,
            navigationButtons: true,
            keyboard: true
          }}
        }};
        var network = new vis.Network(container, data, options);
      </script>
    </body>
    </html>
    """

    components.html(html_payload, height=h + 15)

    # Accessible tabular alternative
    with st.expander("📊 Accessible Tabular Graph View (Nodes & Edges)"):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Graph Nodes**")
            table_nodes = [{"ID": n["id"], "Type": n.get("type", "Unknown")} for n in nodes]
            st.dataframe(table_nodes, use_container_width=True)
        with col2:
            st.markdown("**Directed Transfer Edges**")
            table_edges = [{"Source": e["from"], "Target": e["to"], "Relationship": e.get("label", "")} for e in edges]
            st.dataframe(table_edges, use_container_width=True)

    return html_payload
