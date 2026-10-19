"""Memory and Session Explorer view."""

import streamlit as st
from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.service import DashboardService
from money_trail.memory.models import MemoryScope, MemoryType


def render_memory_view(service: DashboardService):
    """Render persistent graph memory, provenance chains, and session history."""
    st.title("🧠 Persistent Graph Memory & Sessions")
    st.caption("Inspect episodic investigation events, evidence-grounded semantic facts, and provenance chains in FalkorDB.")

    tab_mems, tab_prov, tab_sess = st.tabs(["💡 Persistent Memories", "🔗 Provenance Chains", "⏱️ Sessions History"])

    # -------------------------------------------------------------------------
    # Tab 1: Persistent Memories
    # -------------------------------------------------------------------------
    with tab_mems:
        st.subheader("FalkorDB Memory Nodes (`Memory`)")

        col_s, col_t = st.columns([1, 1])
        with col_s:
            sel_scope = st.selectbox("Memory Scope", options=["All", "investigation", "dataset"])
        with col_t:
            sel_type = st.selectbox("Memory Type", options=["All", "episodic", "semantic"])

        where = ["1=1"]
        params = {}
        if sel_scope != "All":
            where.append("m.scope = $scope")
            params["scope"] = sel_scope
        if sel_type != "All":
            where.append("m.memory_type = $mtype")
            params["mtype"] = sel_type

        cypher = f"""
        MATCH (m:Memory)
        WHERE {' AND '.join(where)}
        OPTIONAL MATCH (m)-[:GROUNDED_BY]->(e:Evidence)
        OPTIONAL MATCH (m)-[:SUPERSEDES]->(old_m:Memory)
        OPTIONAL MATCH (m)-[:CONTRADICTS]->(opp_m:Memory)
        RETURN m.memory_id, m.memory_type, m.scope, m.statement, m.status, m.created_at,
               collect(DISTINCT e.evidence_id), collect(DISTINCT old_m.memory_id), collect(DISTINCT opp_m.memory_id)
        ORDER BY m.created_at DESC LIMIT 50
        """
        rows = service.conn.graph.ro_query(cypher, params).result_set

        if not rows:
            st.info("No memory nodes found matching the criteria.")
        else:
            for mid, mtype, scope, stmt, st_val, created_at, ev_ids, supersedes, contradicts in rows:
                with st.expander(f"[{mtype.upper()}] {stmt[:80]}... — {st_val.upper()}"):
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        st.markdown(f"**Statement:** {stmt}")
                        st.caption(f"Memory ID: `{mid}` • Scope: `{scope}`")
                    with c2:
                        st.markdown(status_badge(st_val), unsafe_allow_html=True)
                        st.caption(f"Created: {created_at}")

                    valid_ev = [e for e in ev_ids if e]
                    if valid_ev:
                        st.markdown(f"**Grounded By Evidence:** `{', '.join(valid_ev)}`")

                    valid_super = [s for s in supersedes if s]
                    if valid_super:
                        st.warning(f"**Supersedes Obsolete Memory:** `{', '.join(valid_super)}`")

                    valid_contra = [c for c in contradicts if c]
                    if valid_contra:
                        st.error(f"**Contradicts Opposing Claim:** `{', '.join(valid_contra)}`")

    # -------------------------------------------------------------------------
    # Tab 2: Provenance Chains
    # -------------------------------------------------------------------------
    with tab_prov:
        st.subheader("Evidence Provenance Tracer")
        st.caption("Trace: Investigation ➔ Verified Finding ➔ Evidence ➔ Transaction ➔ Persistent Memory")

        # Fetch semantic memories with evidence
        sem_rows = service.conn.graph.ro_query(
            "MATCH (m:Memory)-[:GROUNDED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction) "
            "OPTIONAL MATCH (f:Finding)-[:SUPPORTED_BY]->(e) "
            "OPTIONAL MATCH (i:Investigation)-[:HAS_FINDING]->(f) "
            "RETURN m.memory_id, m.statement, e.evidence_id, e.produced_by_tool, t.transaction_id, f.finding_id, i.investigation_id "
            "LIMIT 25"
        ).result_set

        if not sem_rows:
            st.info("No evidence-grounded semantic memories found yet. Run an investigation to curate memories.")
        else:
            for mid, stmt, eid, tool, tid, fid, inv_id in sem_rows:
                with st.container():
                    st.markdown(f"💡 **Memory:** `{stmt}`")
                    st.markdown(
                        f"""
                        ```text
                        Investigation: [{inv_id or 'inv_ref'}]
                               │
                               ▼
                        Finding:       [{fid or 'fnd_ref'}]
                               │
                               ▼
                        Evidence:      [{eid}] (Produced by: {tool})
                               │
                               ▼
                        Transaction:   [{tid}]
                               │
                               ▼
                        Memory:        [{mid}]
                        ```
                        """
                    )
                    st.divider()

    # -------------------------------------------------------------------------
    # Tab 3: Session History
    # -------------------------------------------------------------------------
    with tab_sess:
        st.subheader("Persistent Agent Sessions (`AgentSession`)")

        sess_rows = service.conn.graph.ro_query(
            "MATCH (s:AgentSession) "
            "OPTIONAL MATCH (i:Investigation)-[:HAS_SESSION]->(s) "
            "OPTIONAL MATCH (s)-[:RECORDED]->(m:Memory) "
            "RETURN s.session_id, s.status, s.started_at, s.completed_at, i.investigation_id, count(m) "
            "ORDER BY s.started_at DESC LIMIT 50"
        ).result_set

        if not sess_rows:
            st.info("No sessions recorded yet.")
        else:
            for sid, st_val, started, completed, inv_id, mcount in sess_rows:
                with st.container():
                    c1, c2, c3 = st.columns([3, 1, 1])
                    with c1:
                        st.markdown(f"**Session:** `{sid}`")
                        st.caption(f"Investigation: `{inv_id or 'None'}` • Started: {started}")
                    with c2:
                        st.markdown(status_badge(st_val), unsafe_allow_html=True)
                    with c3:
                        st.caption(f"{mcount} memories recorded")
                    st.divider()
