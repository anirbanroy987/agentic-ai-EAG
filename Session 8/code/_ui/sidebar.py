"""Left rail: theme toggle, agent configuration, session history, controls.

Configuration values are persisted to st.session_state. They are also
exported as env vars onto the orchestrator subprocess when a run starts
(see runtime.start_run) — that's the only way to wire them without
editing flow.py, which is out of scope. The orchestrator currently
ignores them; the wiring is in place for a future hook.
"""

from __future__ import annotations

import streamlit as st
import yaml
from pathlib import Path

from . import runtime
from .components import badge, fmt_dur, toast


# Cap for the per-node rationale snippet in the sidebar explainer.
# Long rationales push the sidebar height; 90 chars keeps each row to
# one or two visual lines without truncating the gist.
_RATIONALE_MAX = 90


def _escape(s: str) -> str:
    """Minimal HTML escape for embedding query text / answer previews into
    the history card. The preview can contain user input plus model
    output, both untrusted from an XSS perspective."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _node_one_liner(node: dict) -> str:
    """Pick the most informative one-line description of a node.

    Skills emit different shapes — planner has `rationale`, researcher
    has `topic`/`question`, formatter has `final_answer` — so we walk
    a preference list and return the first non-empty hit. Falls back
    to "" if the node hasn't produced anything yet (still running).
    """
    out = (node.get("result") or {}).get("output") or {}
    for key in ("rationale", "topic", "question", "summary",
                "final_answer", "answer"):
        v = out.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Pending / running nodes have no output yet — show the inputs to
    # convey "this node is waiting on …".
    inputs = node.get("inputs") or []
    if inputs:
        return f"waiting on: {', '.join(inputs)}"
    return ""


def _render_node_explainer(sid: str) -> None:
    """Compact per-node breakdown for the selected session.

    Rendered inline below the session card so the user gets a quick
    overview without leaving the sidebar. Each row is a single
    `st.markdown` block (no widgets) which keeps the cost flat — no
    button-id collisions, no rerun side effects.

    Reads are uncached intentionally: when the user has just clicked
    View output, they want the *current* on-disk state, and the
    sidebar only re-renders on full-page reruns (not on the 1s tick),
    so cost is negligible.
    """
    try:
        nodes = runtime.load_nodes(sid)
    except Exception as e:  # noqa: BLE001 — explainer must not kill the sidebar
        st.caption(f"(could not read nodes: {type(e).__name__})")
        return

    if not nodes:
        st.caption("(no node files yet on disk)")
        return

    # Order by start time so parallel siblings interleave naturally —
    # matches the Reasoning-steps tab in the main pane.
    sorted_nodes = sorted(nodes, key=lambda n: n.get("started_at") or 0)

    rows: list[str] = []
    for i, n in enumerate(sorted_nodes, 1):
        result = n.get("result") or {}
        nid = _escape(str(n.get("node_id", "?")))
        skill = _escape(str(n.get("skill", "—")))
        status = n.get("status", "pending")
        elapsed = result.get("elapsed_s")
        elapsed_str = fmt_dur(elapsed) if elapsed else "—"
        line = _node_one_liner(n)
        if len(line) > _RATIONALE_MAX:
            line = line[:_RATIONALE_MAX] + "…"
        line_html = (
            f"<div style='font-size:0.74rem; color:var(--text-muted); "
            f"margin-top:3px; line-height:1.4'>{_escape(line)}</div>"
            if line else ""
        )
        rows.append(
            f"<div style='padding:8px 10px; border-radius:8px; "
            f"background:var(--bg-subtle); margin-bottom:6px'>"
            f"<div style='display:flex; justify-content:space-between; "
            f"align-items:center; gap:6px'>"
            f"<div style='display:flex; align-items:center; gap:8px; "
            f"min-width:0'>"
            f"<span style='display:inline-flex; min-width:20px; height:20px; "
            f"background:var(--accent-soft); color:var(--accent-text); "
            f"border-radius:50%; align-items:center; justify-content:center; "
            f"font-size:0.7rem; font-weight:700'>{i}</span>"
            f"<code style='font-size:0.7rem; color:var(--text); "
            f"overflow:hidden; text-overflow:ellipsis; white-space:nowrap'>"
            f"{nid}</code>"
            f"</div>"
            f"{badge(status)}"
            f"</div>"
            f"<div style='display:flex; justify-content:space-between; "
            f"align-items:center; margin-top:4px; gap:6px'>"
            f"<span style='font-size:0.76rem; font-weight:600; "
            f"color:var(--text)'>{skill}</span>"
            f"<span style='font-size:0.7rem; color:var(--text-muted); "
            f"font-variant-numeric:tabular-nums'>{elapsed_str}</span>"
            f"</div>"
            f"{line_html}"
            f"</div>"
        )

    st.markdown(
        f"<div style='margin:6px 0 12px 0; padding:10px; "
        f"border:1px solid var(--accent); border-radius:10px; "
        f"background:var(--bg-elev)'>"
        f"<div class='metric-label' style='margin-bottom:8px'>"
        f"Nodes ({len(sorted_nodes)})</div>"
        f"{''.join(rows)}"
        f"</div>",
        unsafe_allow_html=True,
    )

ROOT = Path(__file__).resolve().parent.parent
AGENT_CONFIG = ROOT / "agent_config.yaml"


def _load_agent_config() -> dict[str, dict]:
    if not AGENT_CONFIG.exists():
        return {}
    try:
        return yaml.safe_load(AGENT_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def render(ss) -> None:  # noqa: ANN001
    with st.sidebar:
        st.markdown(
            '<div class="hero-title" style="font-size:1.15rem">⌬ Session 8</div>'
            '<div class="muted" style="font-size:0.8rem; margin-bottom:18px">'
            'Growing-Graph Orchestrator</div>',
            unsafe_allow_html=True,
        )

        # ── theme ──────────────────────────────────────────────────────────
        with st.container():
            st.markdown('<div class="metric-label">Appearance</div>',
                        unsafe_allow_html=True)
            ss.theme = st.radio(
                "Theme", ["dark", "light"],
                index=0 if ss.theme == "dark" else 1,
                horizontal=True, label_visibility="collapsed",
            )

        st.divider()

        # ── agent configuration (per-skill view from yaml) ─────────────────
        # Expanded by default — this is the panel a user opens the sidebar
        # to look at, and a collapsed expander reads as "the panel is missing".
        with st.expander("Agent configuration", expanded=True):
            cfg = _load_agent_config()
            if not cfg:
                st.caption("agent_config.yaml not readable")
            else:
                skills = [k for k in cfg.keys() if k != "browser"]
                ss.skill_focus = st.selectbox(
                    "Skill", skills,
                    index=skills.index(ss.skill_focus) if ss.skill_focus in skills else 0,
                )
                sk = cfg.get(ss.skill_focus, {})
                st.markdown(
                    f"<div class='muted' style='font-size:0.82rem; "
                    f"margin-bottom:10px'>{sk.get('description', '—')}</div>",
                    unsafe_allow_html=True,
                )

                # Model preference (informational override; env var is set on
                # next run but flow.py currently doesn't read it — placeholder
                # for the future LLM-routing wiring the user asked for)
                ss.model_override = st.text_input(
                    "Model override",
                    value=ss.model_override,
                    placeholder="e.g. gemini-2.5  (blank = use gateway routing)",
                    help="Exported as MODEL_OVERRIDE to the subprocess. "
                         "Wire up in agent_routing.yaml or via a future flow.py hook.",
                )

                # Temperature
                yaml_temp = float(sk.get("temperature", 0.3))
                ss.temperature_override = st.slider(
                    "Temperature",
                    min_value=0.0, max_value=1.5, step=0.05,
                    value=ss.temperature_override if ss.temperature_override >= 0
                          else yaml_temp,
                    help=f"yaml default: {yaml_temp}. "
                         "Exported as TEMPERATURE_OVERRIDE on next run.",
                )

                st.caption(
                    f"tools: `{', '.join(sk.get('tools_allowed', [])) or '—'}`  ·  "
                    f"max_tokens: `{sk.get('max_tokens', '—')}`"
                )

        # ── run options ────────────────────────────────────────────────────
        with st.expander("Run options", expanded=True):
            ss.refresh_interval = st.slider(
                "Auto-refresh interval (s)",
                min_value=0.5, max_value=5.0, step=0.5,
                value=ss.refresh_interval,
                help="How often to poll for new node files while a run is alive.",
            )
            ss.autoscroll_log = st.checkbox(
                "Auto-scroll execution log",
                value=ss.autoscroll_log,
            )

        st.divider()

        # ── resume by session id ───────────────────────────────────────────
        # Lets the user resume any sid on disk, including ones not in the
        # 50-item history list (e.g. an older session typed manually) and
        # any session whose subprocess was killed. The handler lives in
        # streamlit_app.py — we just stash the sid in session_state and
        # rerun; the page picks it up on the next pass and spawns the
        # subprocess through the same code path as a fresh Run.
        st.markdown('<div class="metric-label">Resume session</div>',
                    unsafe_allow_html=True)
        resume_sid_input = st.text_input(
            "Session id",
            value="",
            placeholder="s8-xxxxxxxx",
            label_visibility="collapsed",
            key="resume_sid_input",
        )
        if st.button("▶  Resume by id", width="stretch",
                     disabled=runtime.is_running(ss.proc)):
            target_sid = (resume_sid_input or "").strip()
            if not target_sid:
                toast("Enter a session id first", icon="⚠")
            elif not runtime.can_resume(target_sid):
                toast(
                    f"Cannot resume {target_sid}: "
                    "no graph.pkl on disk for that session",
                    icon="✗",
                )
            else:
                ss._pending_resume = target_sid
                st.rerun()

        st.divider()

        # ── session history ────────────────────────────────────────────────
        st.markdown('<div class="metric-label">Session history</div>',
                    unsafe_allow_html=True)
        # Bumped from 20 → 50 — disk listing is microseconds and the per-card
        # render is also cheap (each card is ~5 disk reads via summarise).
        sessions = runtime.list_sessions(limit=50)
        if not sessions:
            st.caption("No sessions yet — run a query to see it here.")
        else:
            st.caption(
                f"{len(sessions)} session(s) on disk · click View to load "
                "into the tabs (Final answer / Reasoning / Logs / Metrics)."
            )
            for sid in sessions:
                summary = runtime.summarise_session(sid)
                # One-line preview of the formatter's output — gives the
                # history list its meaning. If formatter didn't run yet
                # (incomplete session) we fall back to the query text.
                preview = runtime.load_final_answer(sid)
                preview_short = (
                    (preview[:120] + "…") if len(preview) > 120 else preview
                )
                selected = ss.sid == sid
                container_style = (
                    "border-color: var(--accent);"
                    if selected else "border-color: var(--border);"
                )
                preview_html = (
                    f"<div style='font-size:0.78rem; margin-top:6px; "
                    f"padding:6px 8px; background:var(--bg-subtle); "
                    f"border-radius:6px; line-height:1.45'>"
                    f"<span class='muted' style='font-size:0.68rem; "
                    f"text-transform:uppercase; letter-spacing:0.06em'>"
                    f"Answer</span><br/>{_escape(preview_short)}"
                    f"</div>"
                    if preview_short else ""
                )
                st.markdown(
                    f"<div class='card card-tight' "
                    f"style='margin-bottom:6px; {container_style}'>"
                    f"<div style='display:flex; justify-content:space-between; "
                    f"align-items:center; gap:8px'>"
                    f"<code style='font-size:0.72rem; color: var(--text)'>{sid}</code>"
                    f"{badge(summary['status'])}</div>"
                    f"<div class='muted' style='font-size:0.78rem; "
                    f"margin-top:4px'>{_escape(summary['query']) or '(no query)'}</div>"
                    f"<div class='muted' style='font-size:0.72rem; margin-top:2px'>"
                    f"{summary['n_done']}/{summary['n_total']} nodes  ·  "
                    f"{fmt_dur(summary['wall'])}</div>"
                    f"{preview_html}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                # Inline node explainer for the currently-selected session.
                # Renders right under its card so the sidebar acts as a
                # mini-overview of "what happened in this run" without
                # requiring a click into the main tabs. Only renders for
                # the one selected session — listing every session's nodes
                # would blow up the sidebar height.
                if selected:
                    _render_node_explainer(sid)

                # Two-button row: "View output" loads the session's saved
                # state into the tabs (read-only); "Resume" continues the
                # session from disk via `flow.py --resume <sid>`. Resume
                # is only meaningful if the planner already persisted a
                # graph, and is disabled while another run is alive.
                resumable = runtime.can_resume(sid)
                btn_l, btn_r = st.columns(2)
                with btn_l:
                    view_clicked = st.button(
                        "View output", key=f"load-{sid}", width="stretch",
                    )
                with btn_r:
                    resume_clicked = st.button(
                        "▶ Resume", key=f"resume-{sid}", width="stretch",
                        disabled=(not resumable) or runtime.is_running(ss.proc),
                        help=("Continue this session from its persisted "
                              "graph.pkl. Resets running→pending and re-runs."
                              if resumable else
                              "No graph.pkl on disk — this session can't be "
                              "resumed (it failed before the planner persisted)."),
                    )

                if view_clicked:
                    # Don't load over a live run — the deque is shared with
                    # the reader thread. Stop the run first or refuse cleanly.
                    if runtime.is_running(ss.proc):
                        toast(
                            "Stop the current run before loading history",
                            icon="⚠",
                        )
                    else:
                        ss.sid = sid
                        ss.viewing_history = True
                        # Repopulate the in-memory log buffer from disk so
                        # the Execution-logs tab shows this session's
                        # stdout, not a stale or empty deque.
                        hist_lines = runtime.load_log(sid, tail=800)
                        from collections import deque as _deque
                        ss.stdout_lines = _deque(hist_lines, maxlen=800)
                        # Wall-clock fields drive the header "process wall"
                        # readout; populate from on-disk summary so it shows
                        # the historical duration instead of "—".
                        if summary["wall"]:
                            ss.run_start_at = (
                                summary["mtime"] - summary["wall"]
                            )
                            ss.run_end_at = summary["mtime"]
                        toast(
                            f"Loaded session {sid} "
                            f"({len(hist_lines)} log lines)",
                            icon="📂",
                        )
                        st.rerun()

                if resume_clicked:
                    # Stash the target and let streamlit_app.py spawn it
                    # — keeping subprocess lifecycle in one place avoids
                    # duplicating the env-override + reader-thread setup
                    # that the main page already does for fresh runs.
                    ss._pending_resume = sid
                    st.rerun()

        st.divider()

        # ── danger zone ────────────────────────────────────────────────────
        if st.button("🗑  Clear current session view", width="stretch"):
            ss.sid = None
            ss.viewing_history = False
            ss.stdout_lines.clear()
            ss.run_start_at = None
            ss.run_end_at = None
            toast("Session view cleared", icon="🗑")
            st.rerun()

        # Clear ALL Streamlit caches in-process. Wipes:
        #   * `@st.cache_data`   — the `_cached_nodes` JSON-parse cache that
        #                          keyed on (sid, nodes-dir mtime). Useful
        #                          when you've edited a node file on disk
        #                          and want the dashboard to re-read it.
        #   * `@st.cache_resource` — any singleton resources (none today,
        #                          but cheap to clear pre-emptively).
        # Followed by a full-page rerun so the next render rebuilds from
        # cold state. No process restart needed.
        if st.button("♻  Clear all Streamlit caches", width="stretch",
                     help="Wipes @st.cache_data and @st.cache_resource. "
                          "Equivalent to `streamlit cache clear` plus an "
                          "in-process flush. Use after editing on-disk "
                          "node files or to force-refresh."):
            st.cache_data.clear()
            st.cache_resource.clear()
            toast("All Streamlit caches cleared", icon="♻")
            st.rerun()

        # footer
        st.markdown(
            "<div class='muted' style='font-size:0.72rem; margin-top:20px; "
            "text-align:center'>"
            "gateway · <code>:8108</code><br/>"
            "v8 — growing graph"
            "</div>",
            unsafe_allow_html=True,
        )
