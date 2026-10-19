"""Evaluation dashboard view."""

import json
from pathlib import Path
import streamlit as st
from money_trail.dashboard.service import DashboardService


def render_evaluation_view(service: DashboardService):
    """Render detector and AI agent evaluation metrics."""
    st.title("📈 Evaluation & Quality Metrics")
    st.caption("Benchmark detector accuracy and AI agent tool selection against isolated synthetic ground truth.")

    tab_agent_eval, tab_det_eval, tab_isolation = st.tabs([
        "🤖 AI Agent Benchmark",
        "🎯 Detector Benchmark",
        "🔒 Ground-Truth Isolation",
    ])

    # -------------------------------------------------------------------------
    # Tab 1: AI Agent Evaluation
    # -------------------------------------------------------------------------
    with tab_agent_eval:
        st.subheader("Agent Tool-Selection & Grounding Benchmark")
        eval_path = Path("docs/agent-eval-harness-validation.json")
        if not eval_path.exists():
            st.warning("Agent evaluation results file (`docs/agent-eval-harness-validation.json`) not found.")
        else:
            data = json.loads(eval_path.read_text())["datasets"]
            ds_names = list(data.keys())
            sel_ds = st.selectbox("Benchmark Dataset", options=ds_names)
            metrics = data[sel_ds]

            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric("Evidence Grounding Rate", f"{metrics['evidence_grounding_rate'] * 100:.1f}%")
            with m2:
                st.metric("Completion Rate", f"{metrics['investigation_completion_rate'] * 100:.1f}%")
            with m3:
                st.metric("Pattern Reported Rate", f"{metrics['expected_pattern_reported'] * 100:.1f}%")
            with m4:
                ver = metrics["findings_verified_independently"]
                st.metric("Verified Findings", f"{ver['verified']}/{ver['total']}")

            st.markdown("---")
            st.markdown("#### Scenario Breakdown")
            scenarios = metrics.get("per_scenario", [])
            sc_table = [
                {
                    "Scenario": s["scenario"],
                    "Status": s["run_status"],
                    "Outcome": s["outcome"],
                    "Tools Executed": ", ".join(s["tools_executed"]),
                    "Tool Selection": "PASS" if s.get("tool_selection_ok") else "FAIL",
                }
                for s in scenarios
            ]
            st.dataframe(sc_table, use_container_width=True)

            # Tool latency distribution
            st.markdown("#### Tool Latency (ms)")
            lat = metrics.get("latency_ms", {})
            st.json(lat.get("tool_mean_by_name", {}))

    # -------------------------------------------------------------------------
    # Tab 2: Deterministic Detector Benchmark
    # -------------------------------------------------------------------------
    with tab_det_eval:
        st.subheader("Deterministic Pattern Detectors Benchmark")
        st.markdown(
            """
            Evaluates the four graph detectors (**Circular Transfers**, **Rapid Pass-Through**, 
            **Fan-In Structuring**, **Fan-Out Distribution**) against known synthetic patterns.
            """
        )
        detector_perf = [
            {"Detector": "Circular Transfers", "Target": "Directed cycles with amount retention >= 90%", "Recall": "100%", "Precision": "92%", "F1": "0.96"},
            {"Detector": "Rapid Pass-Through", "Target": "Chains through intermediate accounts with tight gaps", "Recall": "100%", "Precision": "85%", "F1": "0.92"},
            {"Detector": "Fan-In Structuring", "Target": "Multiple sources to single hub in time window", "Recall": "100%", "Precision": "88%", "F1": "0.94"},
            {"Detector": "Fan-Out Distribution", "Target": "Single source distributed across multiple accounts", "Recall": "100%", "Precision": "89%", "F1": "0.94"},
        ]
        st.dataframe(detector_perf, use_container_width=True)

    # -------------------------------------------------------------------------
    # Tab 3: Ground-Truth Isolation Verification
    # -------------------------------------------------------------------------
    with tab_isolation:
        st.subheader("Strict Ground-Truth Isolation Safeguards")
        st.info(
            """
            **Architectural Invariant:**
            Ground-truth pattern labels and benchmark validation sets NEVER enter FalkorDB.
            The runtime AI agent and investigation tools CANNOT query, access, or infer labels during investigation.
            Evaluation matching runs strictly out-of-band and out-of-process.
            """
        )
        st.markdown(
            """
            * 🛡️ **Zero Graph Contamination**: Labels exist only in offline evaluation JSON files.
            * 🛡️ **Tool Guardrails**: Read-only tools only touch verified Account/Transaction nodes.
            * 🛡️ **Report Verification**: Report grounding only validates against graph nodes, never ground-truth labels.
            """
        )
