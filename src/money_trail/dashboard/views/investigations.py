"""Investigation workspace view."""

import json
import streamlit as st

from money_trail.dashboard.components.graph_viz import render_interactive_graph
from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.service import DashboardService


def render_investigations_view(service: DashboardService):
    """Render the complete investigation workspace."""
    st.title("🔍 Investigation Workspace")
    st.caption("Browse, execute, and inspect evidence-grounded financial investigations.")

    tab_browse, tab_new, tab_detail = st.tabs(["📁 Browse Cases", "🚀 New Investigation", "🔬 Case Workspace"])

    # -------------------------------------------------------------------------
    # Tab 1: Browse Existing Cases
    # -------------------------------------------------------------------------
    with tab_browse:
        st.subheader("Persisted Investigations in FalkorDB")
        filter_col1, filter_col2 = st.columns([2, 1])
        with filter_col1:
            status_filter = st.selectbox(
                "Filter Status",
                options=["All", "open", "in_progress", "closed"],
                index=0,
                key="inv_browse_status",
            )
        with filter_col2:
            limit = st.slider("Max Results", 10, 100, 25, key="inv_browse_limit")

        st_arg = None if status_filter == "All" else status_filter
        cases = service.list_investigations(status=st_arg, limit=limit)

        if not cases:
            st.info("No investigations found matching the criteria. Start a new investigation in the next tab.")
        else:
            for case in cases:
                with st.expander(f"Case: {case['investigation_id']} — {case['title']}"):
                    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                    with c1:
                        st.markdown(f"**Anchor:** `{case['anchor_id'] or 'None'}`")
                    with c2:
                        st.markdown(f"**Findings:** {case['findings_count']}")
                    with c3:
                        st.markdown(status_badge(case["status"]), unsafe_allow_html=True)
                    with c4:
                        if st.button("Open Case", key=f"btn_open_{case['investigation_id']}"):
                            st.session_state["selected_investigation_id"] = case["investigation_id"]
                            st.rerun()

    # -------------------------------------------------------------------------
    # Tab 2: Start New Investigation
    # -------------------------------------------------------------------------
    with tab_new:
        st.subheader("Initiate a Graph-Backed Investigation")
        datasets = service.list_datasets()

        with st.form("new_investigation_form"):
            col_ds, col_tx = st.columns([1, 2])
            with col_ds:
                selected_ds = st.selectbox("Dataset Namespace", options=datasets if datasets else ["default"])
            with col_tx:
                anchor_id = st.text_input(
                    "Anchor Transaction ID",
                    placeholder="e.g. synthetic-demo:TXN000000018",
                    help="Must reference an existing transaction in FalkorDB.",
                )

            question = st.text_area(
                "Investigative Question",
                value="Do connected transfers show structural layering, circular cycles, or rapid pass-through?",
            )

            persist_results = st.checkbox("Persist Findings, Memories & Tasks in FalkorDB", value=True)
            submit_btn = st.form_submit_button("Start Coordinated Multi-Agent Investigation")

        if submit_btn:
            if not anchor_id or not anchor_id.strip():
                st.error("Please enter a valid Transaction Anchor ID.")
            else:
                with st.spinner("Executing multi-agent investigation across FalkorDB graph..."):
                    try:
                        res = service.start_investigation(
                            anchor_id=anchor_id.strip(),
                            dataset=selected_ds,
                            question=question,
                            persist=persist_results,
                        )
                        st.success(f"Investigation completed successfully! (Status: {res['status']})")
                        st.session_state["selected_investigation_id"] = res["investigation_id"]
                        st.json({
                            "session_id": res["session_id"],
                            "investigation_id": res["investigation_id"],
                            "steps_executed": res["steps_executed"],
                            "tasks_created": len(res.get("tasks", [])),
                            "handoffs_count": len(res.get("handoffs", [])),
                        })
                        st.info("Switching to Case Workspace...")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Investigation failed: {exc}")

    # -------------------------------------------------------------------------
    # Tab 3: Detailed Case Workspace
    # -------------------------------------------------------------------------
    with tab_detail:
        active_inv_id = st.session_state.get("selected_investigation_id")
        if not active_inv_id:
            st.info("Select an investigation from 'Browse Cases' or start a new one to view its workspace.")
            # Quick select dropdown
            recent = service.list_investigations(limit=20)
            if recent:
                chosen = st.selectbox("Or choose a recent case to load:", options=[r["investigation_id"] for r in recent])
                if st.button("Load Selected Case"):
                    st.session_state["selected_investigation_id"] = chosen
                    st.rerun()
            return

        inv_data = service.get_investigation_workspace(active_inv_id)
        if not inv_data:
            st.error(f"Investigation '{active_inv_id}' not found in FalkorDB.")
            return

        # Case Header
        h_col1, h_col2, h_col3 = st.columns([3, 1, 1])
        with h_col1:
            st.markdown(f"### 📋 {inv_data['title'] or active_inv_id}")
            st.caption(f"ID: `{active_inv_id}` • Anchor: `{inv_data['anchor']['transaction_id'] if inv_data['anchor'] else 'None'}`")
        with h_col2:
            st.markdown(f"Status: {status_badge(inv_data['status'])}", unsafe_allow_html=True)
        with h_col3:
            # Report Export
            report_json = json.dumps(inv_data, indent=2)
            st.download_button(
                "📥 Export Report",
                data=report_json,
                file_name=f"investigation_{active_inv_id}.json",
                mime="application/json",
            )

        st.markdown("---")

        sub_summary, sub_findings, sub_path, sub_timeline, sub_tasks = st.tabs([
            "📊 Summary",
            "🔎 Findings & Evidence",
            "🌐 Transaction Path",
            "⏳ Agent Timeline & Handoffs",
            "📌 Tasks & Blockers",
        ])

        # Summary Tab
        with sub_summary:
            st.markdown("#### Executive Investigation Summary")
            anchor = inv_data["anchor"]
            if anchor:
                st.markdown(
                    f"**Anchor Transaction:** `{anchor['transaction_id']}` • "
                    f"**Amount:** {anchor['amount']} {anchor['currency']} • "
                    f"**Timestamp:** {anchor['timestamp']} • "
                    f"**Dataset:** `{anchor.get('dataset')}`"
                )

            st.markdown(f"**Verified Findings:** {len(inv_data['findings'])}")
            st.markdown(f"**Participating Sessions:** {len(inv_data['sessions'])}")
            st.markdown(f"**Agent Handoffs:** {len(inv_data['handoffs'])}")

            if inv_data["memories"]:
                st.markdown("##### Grounded Graph Memories")
                for m in inv_data["memories"]:
                    st.info(f"💡 [{m['memory_type'].upper()}] {m['statement']}")

        # Findings & Evidence Tab
        with sub_findings:
            st.markdown("#### Detected Patterns & Evidence")
            findings = inv_data["findings"]
            if not findings:
                st.info("No findings recorded for this investigation.")
            else:
                for fnd in findings:
                    with st.container():
                        st.markdown(
                            f"**{fnd['kind'].upper()}:** {fnd['statement']} &nbsp; {status_badge(fnd.get('kind', 'verified'))}",
                            unsafe_allow_html=True,
                        )
                        st.caption(f"Finding ID: `{fnd['finding_id']}` • Created: {fnd['created_at']}")
                        ev_ids = fnd.get("evidence_ids", [])
                        if ev_ids:
                            st.caption(f"Supporting Evidence IDs: {', '.join(ev_ids)}")
                        st.divider()

        # Transaction Path Tab
        with sub_path:
            st.markdown("#### Graph Neighborhood & Transaction Paths")
            anchor_tx = inv_data["anchor"]["transaction_id"] if inv_data["anchor"] else None
            if anchor_tx:
                g_data = service.get_transaction_graph_network(anchor_tx, max_hops=2)
                render_interactive_graph(g_data["nodes"], g_data["edges"], height=450)
            else:
                st.info("No anchor transaction available to construct graph visualization.")

        # Timeline & Handoffs Tab
        with sub_timeline:
            st.markdown("#### Multi-Agent Execution & Handoff History")
            handoffs = inv_data["handoffs"]
            if not handoffs:
                st.info("No handoffs recorded.")
            else:
                for h in handoffs:
                    st.markdown(
                        f"**{h['source_agent_id']}** ➔ **{h['target_role']}** &nbsp; {status_badge(h['status'])}",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"Handoff ID: `{h['handoff_id']}` • Reason: {h['reason']}")
                    if h.get("evidence_refs"):
                        st.caption(f"Evidence References: `{h['evidence_refs']}`")
                    st.divider()

        # Tasks & Blockers Tab
        with sub_tasks:
            st.markdown("#### Coordinated Tasks & Recovery")
            tasks = inv_data["tasks"]
            if not tasks:
                st.info("No tasks recorded for this investigation.")
            else:
                for t in tasks:
                    t_col1, t_col2, t_col3 = st.columns([3, 1, 1])
                    with t_col1:
                        st.markdown(f"**{t['task_type']}** (`{t['task_id']}`)")
                        st.caption(f"Assigned Role: `{t.get('assigned_role')}` • Deps: {t.get('dependencies') or 'None'}")
                    with t_col2:
                        st.markdown(status_badge(t["status"]), unsafe_allow_html=True)
                    with t_col3:
                        st.caption(f"Attempts: {t.get('attempt_count', 0)}")
                    st.divider()

            # Resumption if interrupted
            interrupted_sessions = [s for s in inv_data["sessions"] if s["status"] == "interrupted"]
            if interrupted_sessions:
                st.warning(f"Session `{interrupted_sessions[0]['session_id']}` was interrupted.")
                if st.button("🔄 Resume Interrupted Investigation"):
                    with st.spinner("Resuming from FalkorDB state..."):
                        res_resumed = service.resume_investigation(interrupted_sessions[0]["session_id"])
                        st.success(f"Investigation resumed and updated to: {res_resumed['status']}")
                        st.rerun()
