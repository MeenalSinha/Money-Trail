"""Interactive Graph Explorer view."""

import streamlit as st
from money_trail.dashboard.components.graph_viz import render_interactive_graph
from money_trail.dashboard.service import DashboardService


def render_graph_explorer_view(service: DashboardService):
    """Render interactive graph traversal and pattern visualization explorer."""
    st.title("🌐 Interactive Graph Explorer")
    st.caption("Visually trace connected accounts, fund flows, and detected transfer patterns in FalkorDB.")

    col_ds, col_tx, col_hops = st.columns([1, 2, 1])
    datasets = service.list_datasets()

    with col_ds:
        sel_ds = st.selectbox("Dataset", options=["All"] + datasets, key="graph_exp_ds")
    with col_tx:
        # Suggest transactions or allow manual entry
        ds_arg = None if sel_ds == "All" else sel_ds
        recent_txs = service.search_transactions(dataset=ds_arg, limit=15)
        tx_options = [t["transaction_id"] for t in recent_txs]
        selected_tx = st.selectbox(
            "Transaction Anchor",
            options=tx_options if tx_options else [""],
            help="Select an existing transaction or search by typing.",
        )
    with col_hops:
        max_hops = st.slider("Traversal Depth", 1, 4, 2, help="Hops of Account-Transaction expansion.")

    st.markdown("---")

    if not selected_tx:
        st.info("No transaction selected. Ingest a dataset or select an existing transaction above.")
        return

    # Fetch subgraph from FalkorDB
    with st.spinner("Traversing FalkorDB transaction graph..."):
        try:
            g_data = service.get_transaction_graph_network(selected_tx, max_hops=max_hops, max_nodes=75)
        except Exception as exc:
            st.error(f"Failed to query graph: {exc}")
            return

    # Graph Legend & Summary
    l_c1, l_c2, l_c3 = st.columns([2, 1, 1])
    with l_c1:
        st.markdown(
            """
            **Legend:** 
            🔷 **Account** (Cyan dot) • 
            🔶 **Transaction** (Orange square) • 
            ⭐ **Anchor Transaction** (Amber diamond)
            """
        )
    with l_c2:
        st.caption(f"**Nodes:** {len(g_data['nodes'])}")
    with l_c3:
        st.caption(f"**Edges:** {len(g_data['edges'])}")

    # Render interactive Vis.js graph
    render_interactive_graph(
        g_data["nodes"],
        g_data["edges"],
        height=550,
        highlight_nodes=[selected_tx],
    )

    # Detailed node inspection
    st.markdown("### 🔍 Node & Transaction Inspector")
    center_node = next((n for n in g_data["nodes"] if n["id"] == selected_tx), None)
    if center_node and "properties" in center_node:
        props = center_node["properties"]
        st.json({
            "Transaction ID": selected_tx,
            "Amount": f"{props.get('amount')} {props.get('currency', 'USD')}",
            "Timestamp (epoch ms)": props.get("timestamp"),
            "Dataset": props.get("dataset"),
            "Channel": props.get("channel"),
            "Reference": props.get("reference"),
        })
