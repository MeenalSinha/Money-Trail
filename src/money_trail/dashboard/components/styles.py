"""Custom CSS and visual design tokens for the Money Trail dashboard."""

import streamlit as st


def apply_theme():
    """Inject modern, sleek dark-mode styles and typography for financial investigations."""
    custom_css = """
    <style>
      /* Main theme overrides */
      .stApp {
        background-color: #0B0F19;
        color: #E2E8F0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      }

      /* Sidebar styling */
      section[data-testid="stSidebar"] {
        background-color: #0F172A;
        border-right: 1px solid #1E293B;
      }

      /* Card container */
      .mt-card {
        background: #1E293B;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 16px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
      }
      .mt-card-header {
        font-size: 0.95rem;
        font-weight: 600;
        color: #94A3B8;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 8px;
      }
      .mt-card-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #F8FAFC;
      }

      /* Status Badges */
      .mt-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.8rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      .mt-badge-open { background: #0284C7; color: #E0F2FE; }
      .mt-badge-completed { background: #059669; color: #D1FAE5; }
      .mt-badge-verified { background: #10B981; color: #ECFDF5; }
      .mt-badge-running { background: #D97706; color: #FEF3C7; }
      .mt-badge-blocked { background: #7C3AED; color: #EDE9FE; }
      .mt-badge-failed { background: #E11D48; color: #FFE4E6; }
      .mt-badge-superseded { background: #475569; color: #CBD5E1; }
      .mt-badge-unverified { background: #D97706; color: #FFFBEB; }

      /* Headers */
      h1, h2, h3 {
        color: #F1F5F9 !important;
        font-weight: 700 !important;
      }
    </style>
    """
    st.markdown(custom_css, unsafe_allow_html=True)


def status_badge(status: str) -> str:
    """Return an HTML badge element for a given status string."""
    s = str(status).strip().lower()
    badge_cls = "mt-badge-open"
    if s in ("completed", "closed", "active"):
        badge_cls = "mt-badge-completed"
    elif s in ("verified", "observation"):
        badge_cls = "mt-badge-verified"
    elif s in ("running", "ready", "transferred"):
        badge_cls = "mt-badge-running"
    elif s in ("blocked", "hypothesis"):
        badge_cls = "mt-badge-blocked"
    elif s in ("failed", "rejected", "invalidated"):
        badge_cls = "mt-badge-failed"
    elif s in ("superseded", "cancelled"):
        badge_cls = "mt-badge-superseded"
    elif s in ("unverified", "pending", "initiated"):
        badge_cls = "mt-badge-unverified"

    return f'<span class="mt-badge {badge_cls}">{status}</span>'
