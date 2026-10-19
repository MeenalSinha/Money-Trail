"""Overview dashboard view."""

import streamlit as st
from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.service import DashboardService


def render_overview_view(service: DashboardService):
    """Render the main operations overview dashboard."""
    st.title("🛡️ Money Trail — Executive Overview")
    st.caption("FalkorDB Graph Hacks 2026 • AI Financial Investigation & Multi-Agent Coordination")

    health = service.get_system_health()
    if not health["database_connected"]:
        st.error(f"⚠️ FalkorDB is unreachable at {health['endpoint']}. Please start the database container.")
        return

    # Dataset scope filter
    datasets = service.list_datasets()
    col_filter, col_status = st.columns([2, 1])
    with col_filter:
        selected_ds = st.selectbox(
            "Dataset Scope",
            options=["All Datasets"] + datasets,
            index=0,
            help="Filter graph metrics by specific ingested dataset namespace.",
        )
    with col_status:
        st.markdown(
            f"""
            <div style="padding-top: 1.8rem; font-size: 0.9rem;">
                🟢 <b>FalkorDB Connected</b> ({health['ping_latency_ms']} ms) • <b>Schema v{health['active_schema_version']}</b>
            </div>
            """,
            unsafe_allow_html=True,
        )

    ds_filter = None if selected_ds == "All Datasets" else selected_ds
    metrics = service.get_overview_metrics(dataset=ds_filter)

    st.markdown("---")

    # KPI Metric Cards
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Transactions", f"{metrics['transactions']:,}")
    with c2:
        st.metric("Accounts", f"{metrics['accounts']:,}")
    with c3:
        st.metric("Investigations", f"{metrics['investigations']['total']:,}", f"{metrics['investigations']['open']} open")
    with c4:
        st.metric("Findings", f"{metrics['findings']['total']:,}", f"{metrics['findings']['observation']} facts")
    with c5:
        st.metric("Graph Memories", f"{metrics['memories']:,}")
    with c6:
        st.metric("Agent Tasks", f"{metrics['tasks']['total']:,}", f"{metrics['tasks']['completed']} done")

    st.markdown("---")

    # Recent Investigations & Recent Handoffs in columns
    col_inv, col_coord = st.columns(2)

    with col_inv:
        st.subheader("📁 Recent Investigations")
        invs = service.list_investigations(limit=5)
        if not invs:
            st.info("No investigations recorded yet. Start one from the Investigations workspace.")
        else:
            for inv in invs:
                with st.container():
                    cols = st.columns([3, 1, 1])
                    with cols[0]:
                        st.markdown(f"**{inv['title']}**")
                        st.caption(f"ID: `{inv['investigation_id']}` • Anchor: `{inv.get('anchor_id') or 'N/A'}`")
                    with cols[1]:
                        st.markdown(status_badge(inv["status"]), unsafe_allow_html=True)
                    with cols[2]:
                        st.caption(f"{inv['findings_count']} findings")
                    st.divider()

    with col_coord:
        st.subheader("🤝 Recent Agent Handoffs")
        handoffs = service._coord_store.list_handoffs("")[0:5] if hasattr(service._coord_store, "list_handoffs") else []
        # Query recent handoffs directly
        handoff_rows = service.conn.graph.ro_query(
            "MATCH (h:AgentHandoff) RETURN h.handoff_id, h.source_agent_id, h.target_role, h.status, h.reason, h.investigation_id "
            "ORDER BY h.created_at DESC LIMIT 5"
        ).result_set

        if not handoff_rows:
            st.info("No agent handoffs recorded yet. Multi-agent runs persist transfers here.")
        else:
            for hid, src, tgt, st_val, reason, inv_ref in handoff_rows:
                with st.container():
                    st.markdown(f"**{src}** ➔ **{tgt}** &nbsp; {status_badge(st_val)}", unsafe_allow_html=True)
                    st.caption(f"Reason: {reason} • Inv: `{inv_ref}`")
                    st.divider()
