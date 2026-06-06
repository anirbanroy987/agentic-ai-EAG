"""The five result tabs.

Each tab is its own function so it can be unit-tested or swapped out.
"""

from __future__ import annotations

import json
import time
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from . import runtime
from .components import (badge, fmt_dur, fmt_time, metric_card, metric_row,
                         section_title)


# ── tab 1: Final answer ─────────────────────────────────────────────────────

def final_answer(nodes: list[dict[str, Any]]) -> None:
    formatters = [n for n in nodes
                  if n["skill"] == "formatter" and n["status"] == "complete"]
    if not formatters:
        if any(n["status"] == "running" for n in nodes):
            st.markdown(
                '<div class="card"><div class="muted">'
                'Waiting for the formatter to complete…'
                '</div></div>', unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div class="card"><div class="muted">'
                'No formatter output yet. Run a query to see the answer here.'
                '</div></div>', unsafe_allow_html=True,
            )
        return
    output = (formatters[-1].get("result") or {}).get("output", {})
    answer = output.get("final_answer") if isinstance(output, dict) else None
    if not answer:
        st.markdown(
            '<div class="card"><div class="muted">'
            'Formatter returned no final_answer field.'
            '</div></div>', unsafe_allow_html=True,
        )
        return

    # Answer pane: `.output-pane` is a fixed-max-height scroll container
    # with CSS overflow-anchor. Long outputs (10k+ words) scroll *inside*
    # the pane instead of pushing the page; the scroll position pins to
    # the bottom automatically as content grows.
    st.markdown(
        f'<div class="card">'
        f'<div class="metric-label" style="margin-bottom:10px">Answer</div>'
        f'<div class="output-pane" '
        f'style="font-size:1.02rem; line-height:1.65; white-space:pre-wrap">'
        f'{_escape(answer)}'
        f'</div></div>', unsafe_allow_html=True,
    )

    # Copy/download row. The download_button is a Streamlit primitive (one
    # full rerun on click) — fine, this is a deliberate user action. The
    # copy button is plain HTML+JS so it does NOT cause a rerun: clicking
    # it doesn't disturb the fragment's auto-refresh cycle.
    c1, c2, _ = st.columns([1, 1, 6])
    with c1:
        st.download_button(
            "⬇ Download .md",
            data=answer,
            file_name=f"answer_{formatters[-1].get('node_id','out')}.md",
            mime="text/markdown",
            width="stretch",
        )
    with c2:
        _copy_button(answer, key=f"copy_{formatters[-1].get('node_id','out')}")

    # citations / sources if the upstream emitted any
    src_nodes = [n for n in nodes if n["skill"] in
                 ("researcher", "retriever", "financial_adviser",
                  "equity_strategist")]
    for n in src_nodes:
        out = (n.get("result") or {}).get("output") or {}
        sources = (
            out.get("sources") or out.get("sources_used")
            or out.get("class_citations") or []
        )
        if not sources:
            continue
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        st.markdown(
            f'<div class="card card-tight">'
            f'<div class="metric-label" style="margin-bottom:8px">'
            f'Sources from {n["node_id"]} · {n["skill"]}'
            f'</div>', unsafe_allow_html=True,
        )
        for s in sources[:6]:
            if isinstance(s, dict):
                label = s.get("url") or s.get("source") or s.get("class") or "—"
                detail = (
                    s.get("title") or s.get("key_point")
                    or s.get("verbatim") or ""
                )
                detail_html = ""
                if detail:
                    detail_html = (
                        "<div class='muted' style='font-size:0.8rem; margin-top:2px'>"
                        f"{_escape(str(detail))[:200]}"
                        "</div>"
                    )
                st.markdown(
                    f"<div style='padding:6px 0; border-bottom:1px solid var(--border)'>"
                    f"<code style='font-size:0.8rem'>{_escape(str(label))[:120]}</code>"
                    f"{detail_html}"
                    f"</div>", unsafe_allow_html=True,
                )
        st.markdown('</div>', unsafe_allow_html=True)


# ── tab 2: Reasoning steps ──────────────────────────────────────────────────

def reasoning_steps(nodes: list[dict[str, Any]]) -> None:
    if not nodes:
        st.markdown(
            '<div class="card"><div class="muted">No nodes yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    # Planner rationale on top
    planners = [n for n in nodes
                if n["skill"] == "planner" and n["status"] == "complete"]
    if planners:
        p_out = (planners[0].get("result") or {}).get("output") or {}
        rationale = p_out.get("rationale", "")
        if rationale:
            st.markdown(
                f'<div class="card" style="margin-bottom:14px">'
                f'<div class="metric-label" style="margin-bottom:8px">'
                f'Planner rationale</div>'
                f'<div style="font-style:italic; color:var(--text)">'
                f'{_escape(rationale)}</div>'
                f'</div>', unsafe_allow_html=True,
            )

    # numbered step list — order by start time so parallel siblings interleave
    sorted_nodes = sorted(nodes, key=lambda n: n.get("started_at") or 0)
    for i, n in enumerate(sorted_nodes, 1):
        result = n.get("result") or {}
        meta_bits = [
            f"{badge(n['status'])}",
            f"elapsed {fmt_dur(result.get('elapsed_s'))}",
        ]
        if result.get("provider"):
            meta_bits.append(f"via {result['provider']}")
        inputs = n.get("inputs", [])
        if inputs:
            meta_bits.append(f"inputs: {', '.join(inputs)}")
        meta_html = "  ·  ".join(meta_bits)

        # extract a short rationale-line from the output
        out = result.get("output") or {}
        line = (
            out.get("rationale") or out.get("topic") or out.get("question")
            or out.get("summary") or ""
        )
        if isinstance(line, str) and len(line) > 280:
            line = line[:280] + "…"

        rationale_html = (
            f"<div class='step-rationale'>{_escape(line)}</div>"
            if line else ""
        )
        st.markdown(
            f"<div class='step-row'>"
            f"<div class='step-num'>{i}</div>"
            f"<div class='step-body'>"
            f"<div class='step-skill'>{n['node_id']}  ·  {n['skill']}</div>"
            f"<div class='step-meta'>{meta_html}</div>"
            f"{rationale_html}"
            f"</div></div>", unsafe_allow_html=True,
        )

    # node inspector
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    with st.expander("🔍 Inspect a node's prompt and raw output", expanded=False):
        ids = [n["node_id"] for n in sorted_nodes]
        if ids:
            picked = st.selectbox(
                "node", ids,
                format_func=lambda k: (
                    f"{k}  ·  "
                    f"{next(n['skill'] for n in nodes if n['node_id']==k)}  ·  "
                    f"{next(n['status'] for n in nodes if n['node_id']==k)}"
                ),
            )
            picked_node = next(n for n in nodes if n["node_id"] == picked)
            t1, t2, t3 = st.tabs(["Prompt sent", "Raw output", "Node JSON"])
            with t1:
                st.code(picked_node.get("prompt_sent")
                        or "(no prompt captured)", language="markdown")
            with t2:
                output = (picked_node.get("result") or {}).get("output", {})
                st.code(
                    json.dumps(output, indent=2, ensure_ascii=False),
                    language="json",
                )
            with t3:
                st.code(
                    json.dumps(picked_node, indent=2,
                               ensure_ascii=False, default=str),
                    language="json",
                )


# ── tab 3: Tool calls ───────────────────────────────────────────────────────

def tool_calls(nodes: list[dict[str, Any]]) -> None:
    """Aggregate per-node tool usage.

    Note: the orchestrator doesn't currently log individual tool-call frames
    to disk — they're consumed inside mcp_runner. We show what we have:
    which nodes had tool budgets, how long they ran (proxy for # of calls),
    and any tool-shaped fields the skill chose to emit in its output JSON
    (sources, urls, chunks).
    """
    if not nodes:
        st.markdown(
            '<div class="card"><div class="muted">No nodes yet.</div></div>',
            unsafe_allow_html=True,
        )
        return

    # which skills have tool budgets at all
    TOOL_SKILLS = {
        "researcher": ["web_search", "fetch_url"],
        "retriever": ["search_knowledge"],
        "financial_adviser": ["search_knowledge"],
        "equity_strategist": ["search_knowledge"],
    }
    tool_nodes = [n for n in nodes if n["skill"] in TOOL_SKILLS]
    if not tool_nodes:
        st.markdown(
            '<div class="card"><div class="muted">'
            "This run didn't dispatch any tools — every skill answered from "
            "context or its own LLM call.</div></div>",
            unsafe_allow_html=True,
        )
        return

    # summary card
    total_elapsed = sum(
        (n.get("result") or {}).get("elapsed_s", 0.0) or 0.0
        for n in tool_nodes
    )
    metric_row([
        ("Tool-using nodes", str(len(tool_nodes)), None, False),
        ("Total tool-loop wall time", fmt_dur(total_elapsed), None, False),
        ("Tools available globally",
         str(sum(len(v) for v in TOOL_SKILLS.values())), None, False),
    ])

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    for n in tool_nodes:
        result = n.get("result") or {}
        out = result.get("output") or {}
        tools = TOOL_SKILLS.get(n["skill"], [])
        sources = (
            out.get("sources") or out.get("sources_used")
            or out.get("class_citations") or out.get("chunks") or []
        )
        chips_html = "".join(
            f'<span class="chip">{t}</span>' for t in tools
        )
        nid = n["node_id"]
        skill_name = n["skill"]
        elapsed_str = fmt_dur(result.get("elapsed_s"))
        st.markdown(
            f'<div class="card card-tight" style="margin-bottom:10px">'
            f'<div style="display:flex; justify-content:space-between; '
            f'align-items:center; margin-bottom:8px">'
            f'<div>'
            f'<code style="font-size:0.78rem; color:var(--text-muted)">{nid}</code>'
            f'  <strong>{skill_name}</strong>  '
            f'<span class="muted" style="font-size:0.82rem">'
            f'elapsed {elapsed_str}</span></div>'
            f'<div>{badge(n["status"])}</div>'
            f'</div>'
            f'<div style="font-size:0.82rem">'
            f'Tools allowed: {chips_html}'
            f'  ·  Items returned: <code>{len(sources)}</code>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.caption(
        "Detailed per-call traces (which `web_search` was issued, which "
        "URLs were fetched) are consumed inside `mcp_runner.py` and not "
        "currently persisted. To see them, enable verbose logging on the "
        "gateway or re-run with `flow.py` stdout piped to a file."
    )


# ── tab 4: Execution logs ───────────────────────────────────────────────────

def execution_logs(ss) -> None:  # noqa: ANN001
    """Stream the orchestrator's stdout — append-only, no flicker.

    The pane is a single `<div class="log-pane">` whose contents we
    rewrite in place each tick. `.log-pane` (defined in styles.py)
    uses `overflow-anchor` so the scroll position pins to the last
    line automatically whenever new content appends — pure CSS,
    no JS scroll script, no scroll jank.

    Skip rule: if the line count hasn't changed since the last tick
    (`ss._render_unchanged` was set by the parent fragment), we
    re-render the SAME content into the SAME placeholder. Streamlit's
    DOM diff is a no-op in that case; the pane doesn't even repaint.
    """
    lines = list(ss.stdout_lines)
    if not lines:
        st.markdown(
            '<div class="card"><div class="muted">'
            'No console output yet. The orchestrator prints one line per '
            'node as it completes.</div></div>',
            unsafe_allow_html=True,
        )
        return

    # Tail to a sane bound (the deque already caps at 800; we render the
    # last 400 to keep the DOM string small). Escape only `<` to avoid
    # accidental HTML interpretation — the rest is fine inside a styled
    # text container.
    tail = lines[-400:]
    body = "\n".join(tail).replace("<", "&lt;")
    html = f'<div class="log-pane">{body}</div>'
    st.markdown(html, unsafe_allow_html=True)
    st.caption(
        f"Showing last {len(tail)} of {len(lines)} lines · "
        "scroll pinned to tail via CSS `overflow-anchor`."
    )


# ── tab 5: Metrics ──────────────────────────────────────────────────────────

def metrics(nodes: list[dict[str, Any]]) -> None:
    m = runtime.compute_metrics(nodes)
    speedup = m["speedup"]
    delta = (
        f"+{(speedup-1)*100:.0f}% over serial" if speedup > 1.005
        else "no fan-out yet"
    )
    metric_row([
        ("End-to-end wall clock", fmt_dur(m["wall_clock"]), None, False),
        ("Σ elapsed (serial)", fmt_dur(m["sum_elapsed"]), None, False),
        ("Parallel speedup", f"{speedup:.2f}×", delta, speedup > 1.005),
        ("Nodes  done / total",
         f"{m['n_done']} / {m['n_total']}",
         f"{m['in_flight']} in flight" if m['in_flight'] else None, False),
    ])
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    if m["n_total"]:
        st.progress(
            m["n_done"] / max(1, m["n_total"]),
            text=f"{m['n_done']} of {m['n_total']} nodes complete",
        )

    if not nodes:
        return

    # ── Gantt ──
    starts = [n["started_at"] for n in nodes if n.get("started_at")]
    if not starts:
        return
    t0 = min(starts)
    rows = []
    for n in nodes:
        s = n.get("started_at")
        if s is None:
            continue
        e = n.get("completed_at")
        if e is None:
            e = time.time() if n["status"] == "running" else s
        rows.append({
            "node": n["node_id"],
            "skill": n["skill"],
            "label": f'{n["node_id"]}  {n["skill"]}',
            "start_s": s - t0,
            "end_s": e - t0,
            "duration_s": e - s,
            "status": n["status"],
        })
    gdf = pd.DataFrame(rows)
    if gdf.empty:
        return
    order = list(gdf["label"])
    chart = (
        alt.Chart(gdf)
        .mark_bar(cornerRadius=4, height=22)
        .encode(
            x=alt.X("start_s:Q",
                    axis=alt.Axis(title="seconds from first node start",
                                  grid=True, gridOpacity=0.08)),
            x2="end_s:Q",
            y=alt.Y("label:N", sort=order, title=None,
                    axis=alt.Axis(labelLimit=260)),
            color=alt.Color("skill:N",
                            scale=alt.Scale(scheme="tableau20"),
                            legend=alt.Legend(orient="bottom",
                                              labelLimit=180)),
            tooltip=[
                alt.Tooltip("node:N"),
                alt.Tooltip("skill:N"),
                alt.Tooltip("status:N"),
                alt.Tooltip("start_s:Q", format=".2f"),
                alt.Tooltip("end_s:Q", format=".2f"),
                alt.Tooltip("duration_s:Q", format=".2f"),
            ],
        )
        .properties(
            height=max(160, 32 * len(rows)),
            title=("Node execution timeline — parallel siblings overlap "
                   "horizontally"),
        )
        .configure_view(strokeWidth=0)
        .configure_axis(labelColor="#94a3b8", titleColor="#94a3b8")
        .configure_title(color="#cbd5e1", fontSize=13, anchor="start")
        .configure_legend(labelColor="#94a3b8", titleColor="#94a3b8")
    )
    st.altair_chart(chart, width="stretch")

    # ── per-node detail table ──
    st.markdown(
        '<div class="metric-label" style="margin-top:18px">'
        'Per-node detail</div>', unsafe_allow_html=True,
    )
    tbl = []
    for n in nodes:
        result = n.get("result") or {}
        s = n.get("started_at")
        e = n.get("completed_at")
        tbl.append({
            "node": n["node_id"],
            "skill": n["skill"],
            "status": n["status"],
            "start": fmt_time(s),
            "elapsed": (
                f"{result['elapsed_s']:.2f} s"
                if isinstance(result.get("elapsed_s"), (int, float)) and result.get("elapsed_s")
                else "—"
            ),
            "finish": fmt_time(e),
            "provider": result.get("provider") or "—",
            "inputs": ", ".join(n.get("inputs", []) or []) or "—",
            "error": (result.get("error") or "—")[:80],
        })
    st.dataframe(pd.DataFrame(tbl), width="stretch", hide_index=True)


# ── helpers ─────────────────────────────────────────────────────────────────

def _escape(s: str) -> str:
    """Minimal HTML escape; not for hostile input, just for embedded display."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def _copy_button(text: str, *, key: str) -> None:
    """Render a copy-to-clipboard button as static HTML + inline JS.

    Why not `st.button`? Streamlit buttons trigger a full-page rerun on
    click — which would reset the streaming fragment, momentarily blank
    the output pane, and lose any scroll position. A plain `<button>` is
    inert from Streamlit's perspective; clicking it copies and shows a
    short "Copied!" flash entirely client-side, with zero rerun cost.

    `key` is required because the button's id must be unique across
    re-renders, otherwise multiple copies on the same page bind to the
    first payload only.
    """
    # JSON-encode through json.dumps so quotes/newlines inside `text` can't
    # break out of the JS string literal.
    payload = json.dumps(text)
    bid = f"copybtn_{key}"
    html = f"""
<button id="{bid}" type="button"
        style="width:100%; padding:8px 14px; border-radius:10px;
               border:1px solid var(--border-strong); background:var(--bg-elev);
               color:var(--text); font-weight:600; font-size:0.9rem;
               cursor:pointer; font-family:inherit;">
  ⧉ Copy
</button>
<script>
(function() {{
  const btn = document.getElementById("{bid}");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {{
    try {{
      await navigator.clipboard.writeText({payload});
      const orig = btn.innerText;
      btn.innerText = "✓ Copied";
      setTimeout(() => {{ btn.innerText = orig; }}, 1200);
    }} catch (e) {{
      btn.innerText = "Copy failed";
    }}
  }});
}})();
</script>
"""
    st.markdown(html, unsafe_allow_html=True)
