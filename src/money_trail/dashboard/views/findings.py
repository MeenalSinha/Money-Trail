"""Findings and Evidence Explorer view."""

import streamlit as st
from money_trail.dashboard.components.graph_viz import render_interactive_graph
from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.service import DashboardService


def render_findings_view(service: DashboardService):
    """Render the findings and evidence exploration dashboard."""
    st.title("🔎 Findings & Evidence Explorer")
    st.caption("Inspect structural detections, analytical claims, and supporting graph evidence records.")

    # Fetch findings across graph
    fnd_rows = service.conn.graph.ro_query(
        "MATCH (i:Investigation)-[:HAS_FINDING]->(f:Finding) "
        "RETURN f.finding_id, f.kind, f.statement, f.created_at, i.investigation_id "
        "ORDER BY f.created_at DESC LIMIT 100"
    ).result_set

    if not fnd_rows:
        st.info("No findings currently recorded in FalkorDB. Run an investigation to discover patterns.")
        return

    # Filter controls
    col_k, col_s = st.columns([1, 2])
    with col_k:
        kinds = sorted(list(set(r[1] for r in fnd_rows)))
        sel_kind = st.selectbox("Finding Classification", options=["All"] + kinds)
    with col_s:
        search_query = st.text_input("Filter Statements", placeholder="e.g. circular, pass_through, accounts...")

    filtered = []
    for r in fnd_rows:
        fid, kind, stmt, created_at, inv_id = r
        if sel_kind != "All" and kind != sel_kind:
            continue
        if search_query and search_query.lower() not in stmt.lower():
            continue
        filtered.append({
            "finding_id": fid,
            "kind": kind,
            "statement": stmt,
            "created_at": created_at,
            "investigation_id": inv_id,
        })

    st.markdown(f"**Showing {len(filtered)} findings:**")
    st.markdown("---")

    for fnd in filtered:
        with st.expander(f"[{fnd['kind'].upper()}] {fnd['statement'][:90]}..."):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**Statement:** {fnd['statement']}")
                st.caption(f"Finding ID: `{fnd['finding_id']}` • Investigation: `{fnd['investigation_id']}`")
            with c2:
                st.markdown(status_badge(fnd["kind"]), unsafe_allow_html=True)
                st.caption(f"Created: {fnd['created_at']}")

            # Check supporting evidence
            ev_rows = service.conn.graph.ro_query(
                "MATCH (f:Finding {finding_id: $fid})-[:SUPPORTED_BY]->(e:Evidence) "
                "RETURN e.evidence_id, e.produced_by_tool, e.query_ref, e.description",
                {"fid": fnd["finding_id"]},
            ).result_set

            if ev_rows:
                st.markdown("##### 📜 Supporting Evidence")
                for eid, tool, qref, desc in ev_rows:
                    st.info(f"**Evidence `{eid}`** ({tool}): {desc}")

                    # Referenced Transactions
                    tx_rows = service.conn.graph.ro_query(
                        "MATCH (e:Evidence {evidence_id: $eid})-[:REFERENCES]->(t:Transaction) "
                        "RETURN t.transaction_id, t.amount, t.currency, t.timestamp",
                        {"eid": eid},
                    ).result_set
                    if tx_rows:
                        st.markdown("**Referenced Transactions in Evidence:**")
                        tx_table = [
                            {"Transaction ID": t[0], "Amount": f"{t[1]} {t[2]}", "Timestamp": t[3]}
                            for t in tx_rows
                        ]
                        st.dataframe(tx_table, use_container_width=True)

            # Pattern Subgraph preview
            if st.button("🌐 Visualize Supporting Subgraph", key=f"btn_subgraph_{fnd['finding_id']}"):
                subgraph = service.get_pattern_subgraph(fnd["finding_id"])
                if subgraph:
                    st.markdown("###### Supporting Pattern Subgraph")
                    render_interactive_graph(subgraph["nodes"], subgraph["edges"], height=350)
                else:
                    st.warning("Could not construct pattern subgraph for this finding.")
