"""The main query input card."""

from __future__ import annotations

import streamlit as st

from . import runtime
from .components import toast

EXAMPLES = {
    "Hello (minimum DAG · 2 nodes)":
        "Say hello.",
    "Shannon Wikipedia (auto-critic · 4 nodes)":
        "Fetch the Claude Shannon Wikipedia page and tell me his birth date, "
        "death date, and three key contributions to information theory.",
    "Parallel cities (fan-out · 7 nodes)":
        "Find the current populations of Tokyo, Delhi, and São Paulo, then "
        "tell me which is largest and by what percentage over the smallest.",
    "Graceful failure (no tool dispatched)":
        "Read /nonexistent/path.txt and tell me what's in it.",
    "Lagos / Cairo / Kinshasa (Query K — resume demo)":
        "For Lagos, Cairo, and Kinshasa, find current populations and growth "
        "rates and tell me which is growing fastest.",
    "Financial planner (RAG · personal finance)":
        "Should I prepay my home loan or invest the surplus in equity mutual "
        "funds? What does it depend on?",
    "Equity strategist (RAG · trading transcripts)":
        "Explain the 'Three Times in Three Years' strategy. What's the position "
        "size, which stocks does it apply to, and what are the entry rules?",
    "Multi-stock fan-out (equity_strategist × 3)":
        "Score these three stocks on the Three-Times-in-Three-Years filter "
        "from the class: HDFCBANK, INFY, and RELIANCE. For each, state whether "
        "it's NSE-listed and whether the class's rule would allow entry.",
}


def render(ss) -> tuple[bool, bool]:  # noqa: ANN001
    """Render the query card. Returns (run_clicked, stop_clicked)."""
    st.markdown('<div class="card">', unsafe_allow_html=True)

    # preset → seeds the textarea on selection change
    if "_last_preset" not in ss:
        ss._last_preset = list(EXAMPLES.keys())[2]
        ss.query_text = EXAMPLES[ss._last_preset]

    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.markdown(
            '<div class="metric-label">Quick start</div>',
            unsafe_allow_html=True,
        )
        preset = st.selectbox(
            "preset", list(EXAMPLES.keys()),
            index=list(EXAMPLES.keys()).index(ss._last_preset),
            label_visibility="collapsed",
        )
        if preset != ss._last_preset:
            ss._last_preset = preset
            ss.query_text = EXAMPLES[preset]
            st.rerun()
    with top_right:
        st.markdown(
            '<div class="metric-label">Session</div>',
            unsafe_allow_html=True,
        )
        st.code(ss.sid or "—", language="text")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    st.markdown(
        '<div class="metric-label">Query</div>',
        unsafe_allow_html=True,
    )
    ss.query_text = st.text_area(
        "query", value=ss.query_text, height=110,
        label_visibility="collapsed",
        placeholder="What should the agent do?",
    )

    # buttons
    bl, br = st.columns([1, 4])
    with bl:
        run_clicked = st.button(
            "▶  Run query", type="primary",
            width="stretch",
            disabled=runtime.is_running(ss.proc) or not ss.query_text.strip(),
        )
    with br:
        bcol1, bcol2 = st.columns([1, 4])
        with bcol1:
            stop_clicked = st.button(
                "■  Stop", width="stretch",
                disabled=not runtime.is_running(ss.proc),
            )

    st.markdown("</div>", unsafe_allow_html=True)
    return run_clicked, stop_clicked
