"""Money Trail — Main Interactive Dashboard Entrypoint.

Streamlit application providing a complete, cohesive financial-investigation workspace
powered by FalkorDB graph memory and multi-agent coordination.
"""
from __future__ import annotations

import streamlit as st

from money_trail.config import load_settings
from money_trail.dashboard.components.styles import apply_theme
from money_trail.dashboard.service import DashboardService
from money_trail.dashboard.views.coordination import render_coordination_view
from money_trail.dashboard.views.evaluation import render_evaluation_view
from money_trail.dashboard.views.findings import render_findings_view
from money_trail.dashboard.views.graph_explorer import render_graph_explorer_view
from money_trail.dashboard.views.investigations import render_investigations_view
from money_trail.dashboard.views.memory import render_memory_view
from money_trail.dashboard.views.overview import render_overview_view
from money_trail.dashboard.views.system_status import render_system_status_view
from money_trail.database import FalkorDBConnection


@st.cache_resource
def get_dashboard_service() -> DashboardService:
    """Initialize and cache the application connection and service facade."""
    settings = load_settings()
    conn = FalkorDBConnection(settings)
    return DashboardService(conn)


def main():
    """Main dashboard application router."""
    st.set_page_config(
        page_title="Money Trail — Graph Investigation Workspace",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Apply modern custom theme
    apply_theme()

    # Sidebar Header
    st.sidebar.markdown(
        """
        <div style="padding-bottom: 1rem;">
            <h2 style="margin: 0; color: #38BDF8;">🛡️ Money Trail</h2>
            <span style="font-size: 0.8rem; color: #94A3B8;">FalkorDB Graph Hacks 2026</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Navigation Menu
    PAGES = {
        "🛡️ Overview": render_overview_view,
        "🔍 Investigations": render_investigations_view,
        "🌐 Graph Explorer": render_graph_explorer_view,
        "🔎 Findings & Evidence": render_findings_view,
        "🤝 Agent Coordination": render_coordination_view,
        "🧠 Memory & Sessions": render_memory_view,
        "📈 Evaluation": render_evaluation_view,
        "⚙️ System Status": render_system_status_view,
    }

    selected_page_name = st.sidebar.radio(
        "Navigation",
        options=list(PAGES.keys()),
        index=0,
    )

    st.sidebar.markdown("---")
    st.sidebar.caption("Connected to FalkorDB • Schema v2")

    # Initialize Service
    try:
        service = get_dashboard_service()
    except Exception as exc:
        st.error(f"Failed to connect to FalkorDB: {exc}")
        st.info("Check your `.env` configuration and confirm the FalkorDB server is running.")
        return

    # Render selected view
    view_func = PAGES[selected_page_name]
    view_func(service)


if __name__ == "__main__":
    main()
