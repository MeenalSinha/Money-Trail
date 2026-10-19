"""Agent activity and task coordination view."""

import streamlit as st
from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.service import DashboardService


def render_coordination_view(service: DashboardService):
    """Render agent activity, task coordination DAG, and handoff timeline."""
    st.title("🤝 Agent Activity & Task Coordination")
    st.caption("Inspect specialized agent roles, dependency-aware task graphs, and auditable handoffs in FalkorDB.")

    tab_tasks, tab_handoffs, tab_roles = st.tabs(["📌 Tasks & Dependencies", "🔄 Handoff Timeline", "👥 Specialized Roles"])

    # -------------------------------------------------------------------------
    # Tab 1: Tasks & Dependencies
    # -------------------------------------------------------------------------
    with tab_tasks:
        st.subheader("Durable Agent Tasks (`AgentTask`)")

        # Query all tasks from FalkorDB
        task_rows = service.conn.graph.ro_query(
            "MATCH (t:AgentTask) "
            "OPTIONAL MATCH (t)-[:DEPENDS_ON]->(d:AgentTask) "
            "RETURN t.task_id, t.task_type, t.status, t.assigned_role, t.investigation_id, collect(d.task_id), t.result_summary "
            "ORDER BY t.created_at DESC LIMIT 50"
        ).result_set

        if not task_rows:
            st.info("No agent tasks recorded in FalkorDB yet.")
        else:
            status_counts = {}
            for r in task_rows:
                st_val = r[2]
                status_counts[st_val] = status_counts.get(st_val, 0) + 1

            # Summary badges
            st.markdown(
                " • ".join([f"{status_badge(k)}: **{v}**" for k, v in status_counts.items()]),
                unsafe_allow_html=True,
            )
            st.markdown("---")

            for tid, ttype, st_val, role, inv_id, deps, result_sum in task_rows:
                with st.expander(f"Task: {ttype} (`{tid}`) — Status: {st_val.upper()}"):
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1:
                        st.markdown(f"**Task Type:** `{ttype}`")
                        st.markdown(f"**Assigned Role:** `{role or 'Unassigned'}`")
                        st.caption(f"Investigation: `{inv_id}`")
                    with c2:
                        st.markdown(status_badge(st_val), unsafe_allow_html=True)
                    with c3:
                        deps_list = [d for d in deps if d]
                        st.markdown(f"**Dependencies:** {len(deps_list)}")
                        if deps_list:
                            st.caption(", ".join(deps_list))

                    if result_sum:
                        st.info(f"**Result:** {result_sum}")

    # -------------------------------------------------------------------------
    # Tab 2: Handoff Timeline
    # -------------------------------------------------------------------------
    with tab_handoffs:
        st.subheader("Auditable Agent Handoffs (`AgentHandoff`)")

        handoff_rows = service.conn.graph.ro_query(
            "MATCH (h:AgentHandoff) "
            "RETURN h.handoff_id, h.source_agent_id, h.target_role, h.status, h.reason, h.investigation_id, h.task_id, h.created_at "
            "ORDER BY h.created_at DESC LIMIT 50"
        ).result_set

        if not handoff_rows:
            st.info("No handoffs recorded yet.")
        else:
            for hid, src, tgt, st_val, reason, inv_id, tid, ts in handoff_rows:
                with st.container():
                    st.markdown(
                        f"**{src}** ➔ **{tgt}** &nbsp; {status_badge(st_val)}",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**Reason:** {reason}")
                    st.caption(f"Handoff ID: `{hid}` • Task ID: `{tid}` • Investigation: `{inv_id}` • Time: {ts}")
                    st.divider()

    # -------------------------------------------------------------------------
    # Tab 3: Specialized Roles
    # -------------------------------------------------------------------------
    with tab_roles:
        st.subheader("Specialized Agent Roles Registry")
        roles_info = [
            {
                "Role": "Coordinator / Planner",
                "Identifier": "coordinator",
                "Responsibilities": "Loads graph memory, constructs bounded task DAGs, checks task readiness in FalkorDB, schedules execution.",
                "Permissions": "Task creation & scheduling; no direct Cypher invention.",
            },
            {
                "Role": "Graph Investigator",
                "Identifier": "investigator",
                "Responsibilities": "Executes read-only graph queries, traces account transfer paths, runs deterministic detectors (circular, pass-through, fan-in, fan-out).",
                "Permissions": "Phase 4 tool execution; read-only graph access.",
            },
            {
                "Role": "Evidence Auditor",
                "Identifier": "auditor",
                "Responsibilities": "Independently audits findings against FalkorDB, verifies transaction existence and amounts, rejects unsupported claims.",
                "Permissions": "Evidence verification & finding validation.",
            },
            {
                "Role": "Memory Curator",
                "Identifier": "memory_curator",
                "Responsibilities": "Enforces anti-poisoning safeguards, records episodic memory, promotes verified findings to semantic memory with provenance.",
                "Permissions": "Episodic & semantic graph memory management; supersession & contradictions.",
            },
        ]
        st.dataframe(roles_info, use_container_width=True)
