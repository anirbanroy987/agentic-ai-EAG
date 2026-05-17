"""
Streamlit demo for the SchemeContext multi-agent advisor.

Run from the project root (so `src` is importable):

    streamlit run app.py

Requires the LLM Gateway V2 to be running (default http://localhost:8100).

This is a thin presentation layer: it calls the exact same
`generate_recommendation()` the CLI uses, renders the same
`render_markdown()` output, and shows the 8 pipeline stages live via the
orchestrator's optional `progress` callback.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import streamlit as st

from src.gateway_client import GatewayError
from src.orchestrator import generate_recommendation, render_markdown

DEMO_QUERY = (
    "I'm a 32-year-old farmer in Bihar, pincode 800001. I have 1 acre of "
    "ancestral land. Family of 5 including 2 school-going kids. Annual "
    "income around 1.2 lakh. My wife wants to start a small dairy business. "
    "We live in a kachha 2-room house. No bank account problems but never "
    "applied for any government scheme."
)

STAGES = [
    "Profile Parser",
    "State Resolver",
    "Scheme Matcher",
    "Eligibility Checkers (parallel)",
    "Macro Contextualizer (e-Sankhyiki MCP)",
    "Application Drafters (parallel)",
    "Priority Ranker",
    "Verifier",
]

GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "http://localhost:8100")

st.set_page_config(page_title="SchemeContext", page_icon="🪪", layout="wide")


def gateway_status() -> tuple[bool, str]:
    """Return (reachable, detail). The pipeline hard-depends on the gateway."""
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/providers", timeout=4.0)
        r.raise_for_status()
        data = r.json()
        provs = ", ".join(data.get("providers", [])) or "none"
        return True, f"providers: {provs}"
    except Exception as e:  # noqa: BLE001 — surface any reachability failure
        return False, str(e)


def render_progress(placeholder, current: int, done: bool = False) -> None:
    """Render the 8-stage checklist. `current` is the 1-based running stage."""
    lines = []
    for i, name in enumerate(STAGES, start=1):
        if done or i < current:
            lines.append(f"- ✅ **{i}/8** {name}")
        elif i == current:
            lines.append(f"- 🔄 **{i}/8** {name} …")
        else:
            lines.append(f"- ⬜ **{i}/8** {name}")
    placeholder.markdown("\n".join(lines))


# ── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("SchemeContext")
    st.caption(
        "Multi-agent advisor for Indian government schemes, grounded in "
        "MoSPI e-Sankhyiki macro data."
    )
    ok, detail = gateway_status()
    if ok:
        st.success(f"Gateway reachable\n\n`{GATEWAY_URL}`\n\n{detail}")
    else:
        st.error(
            f"Gateway NOT reachable at `{GATEWAY_URL}`.\n\n"
            f"Start it first:\n"
            f"`llm_gatewayV2/.venv/Scripts/python.exe main.py`\n\n"
            f"Detail: {detail}"
        )
    st.divider()
    st.caption(
        "A full run is ~16 LLM calls. On the Gemini free tier it serializes "
        "through rate-limit cooldowns and takes several minutes."
    )

# ── Main ────────────────────────────────────────────────────────────────────
st.title("🪪 SchemeContext — scheme advisor")

query = st.text_area(
    "Describe the citizen (free text):",
    value=DEMO_QUERY,
    height=140,
)

run = st.button("Generate recommendation", type="primary", disabled=not ok)

if run:
    if not query.strip():
        st.warning("Enter a description first.")
        st.stop()

    st.subheader("Pipeline progress")
    prog_box = st.empty()
    bar = st.progress(0.0)
    render_progress(prog_box, current=1)

    def on_stage(n: int, name: str) -> None:
        render_progress(prog_box, current=n)
        bar.progress((n - 1) / len(STAGES))

    def _leaves(exc: BaseException) -> list[BaseException]:
        """Flatten (possibly nested) ExceptionGroups into concrete leaves.

        The parallel stages run under asyncio.TaskGroup, which re-raises any
        child failure wrapped in an ExceptionGroup. Unwrap it so the real
        cause (usually a GatewayError) is shown, not "1 sub-exception".
        """
        if isinstance(exc, BaseExceptionGroup):
            out: list[BaseException] = []
            for sub in exc.exceptions:
                out.extend(_leaves(sub))
            return out
        return [exc]

    try:
        with st.spinner("Running the 8-agent pipeline…"):
            recommendation, verdict, trace = asyncio.run(
                generate_recommendation(query, progress=on_stage)
            )
    except Exception as e:  # noqa: BLE001 — surface every failure in-UI
        bar.empty()
        leaves = _leaves(e)
        gw = next((x for x in leaves if isinstance(x, GatewayError)), None)
        if gw is not None:
            st.error(
                "The LLM Gateway failed mid-pipeline. The most common cause "
                "on the free tier is **exhausted Gemini quota (HTTP 429)** — "
                "wait for the quota window to reset, or add more provider "
                "keys to `Session-5 Planning/.env` and restart the gateway "
                "so the pipeline can fail over to another provider.\n\n"
                f"```\n{gw}\n```"
            )
        else:
            st.error("The pipeline failed:")
            for leaf in leaves:
                st.exception(leaf)
        st.stop()

    render_progress(prog_box, current=len(STAGES), done=True)
    bar.progress(1.0)
    st.session_state["result"] = (
        render_markdown(recommendation, query),
        recommendation.model_dump_json(indent=2),
        verdict.model_dump_json(indent=2),
        trace.model_dump_json(indent=2),
        trace.summary(),
        verdict.final_verdict,
        len(verdict.issues_found),
    )

# ── Results (kept across reruns so download buttons don't lose them) ─────────
if "result" in st.session_state:
    md, rec_json, verdict_json, trace_json, summary, final_verdict, n_issues = (
        st.session_state["result"]
    )

    badge = {"approved": "✅", "revise": "⚠️", "rejected": "❌"}.get(
        final_verdict, "ℹ️"
    )
    st.subheader("Result")
    st.markdown(
        f"**Verifier verdict:** {badge} `{final_verdict}` · "
        f"**issues found:** {n_issues}"
    )

    st.markdown("---")
    st.markdown(md)

    with st.expander("Pipeline telemetry (trace summary)"):
        st.json(summary)

    c1, c2, c3 = st.columns(3)
    c1.download_button(
        "recommendation.md", md, file_name="recommendation.md"
    )
    c1.download_button(
        "recommendation.json", rec_json, file_name="recommendation.json"
    )
    c2.download_button(
        "verdict.json", verdict_json, file_name="verdict.json"
    )
    c3.download_button(
        "trace.json", trace_json, file_name="trace.json"
    )
