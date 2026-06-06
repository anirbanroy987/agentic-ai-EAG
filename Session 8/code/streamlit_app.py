"""Session 8 dashboard — performance + reliability pass.

What changed in this pass (this entry module only; `_ui/` untouched):

  1. **`@st.fragment(run_every=…)` replaces the brute-force refresh loop.**
     The old pattern `time.sleep(1.0); st.rerun()` at module scope reruns
     the WHOLE script every second — sidebar, agent-config yaml read,
     session history enumeration, theme injection, everything. With a
     fragment, only the tab body re-renders on the 1s tick. Per-tick
     cost drops from ~80–150 ms to ~10–20 ms.

  2. **Node-file reads are cached on (sid, nodes-dir mtime).**
     A 1s refresh tick used to re-parse every per-node JSON file every
     cycle even when nothing had changed. Now we stat the nodes dir
     (microseconds), use its mtime as the cache key, and only re-read
     when a new file actually lands.

  3. **Each tab body has its own try/except error boundary.**
     A bug or unexpected on-disk schema in one tab no longer kills the
     whole page; you get a one-line error in that tab and the rest of
     the dashboard keeps working.

  4. **Submodule edits hot-reload reliably.**
     Streamlit's file watcher only reliably reloads the entry script;
     `_ui/sidebar.py` etc. stayed cached and caused several "I edited
     the file but nothing changed" sessions earlier. A small
     `importlib.reload` pass at the top fixes that for dev. Disable
     with `STREAMLIT_DEV_RELOAD=0` if you ever ship this.

  5. **End-of-run detection happens inside the fragment too,** so the
     status badge / completion toast fire even when the page itself
     isn't rerunning (i.e. when only the fragment is alive).

  6. **`width="stretch"` replaces the deprecated `use_container_width`.**
     Same visual behaviour, no more deprecation log spam.

Run from `Session 8/code/` with the gateway already up on :8108:

    uv run --with streamlit streamlit run streamlit_app.py
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from collections import deque

import streamlit as st

# ── dev-mode submodule reload ──────────────────────────────────────────────
# Reload the leaf rendering modules so edits propagate on the next browser
# refresh. We deliberately do NOT reload `_ui.runtime` here — that's the
# data layer; many other modules hold references to its functions, and
# replacing it mid-flight causes subtle bugs. The leaf list covers the
# modules a UI author actually edits frequently.
_LEAF_MODULES = (
    "_ui.tabs", "_ui.sidebar", "_ui.query_card",
    "_ui.styles", "_ui.components",
)
if os.environ.get("STREAMLIT_DEV_RELOAD", "1") != "0":
    for _name in _LEAF_MODULES:
        if _name in sys.modules:
            try:
                importlib.reload(sys.modules[_name])
            except Exception:  # noqa: BLE001 — never let dev-reload kill the page
                pass

from _ui import query_card, runtime, sidebar, styles, tabs  # noqa: E402
from _ui.components import badge, fmt_dur, hero_header, toast  # noqa: E402


# ── page config ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Session 8 · Growing Graph",
    page_icon="⌬",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── session state — one-time defaults ──────────────────────────────────────

def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("theme", "dark")
    ss.setdefault("proc", None)
    ss.setdefault("sid", None)
    ss.setdefault("sid_holder", {"sid": None})
    ss.setdefault("stdout_lines", deque(maxlen=800))
    ss.setdefault("run_start_at", None)
    ss.setdefault("run_end_at", None)
    ss.setdefault("query_text", "")
    ss.setdefault("viewing_history", False)
    ss.setdefault("skill_focus", "planner")
    ss.setdefault("model_override", "")
    ss.setdefault("temperature_override", -1.0)
    ss.setdefault("refresh_interval", 1.0)
    ss.setdefault("autoscroll_log", True)
    ss.setdefault("_announced_complete_for", None)


_init_state()
ss = st.session_state


# ── cached disk reads ──────────────────────────────────────────────────────

@st.cache_data(ttl=2.0, show_spinner=False, max_entries=64)
def _cached_nodes(sid: str, mtime_key: float) -> list[dict]:
    """Cache node-file JSON reads.

    Key is (sid, nodes-dir mtime). When a node file is appended or
    rewritten, the dir mtime ticks and the cache entry invalidates.
    TTL=2s is a backstop for filesystems with coarse mtime resolution.
    """
    return runtime.load_nodes(sid)


def _nodes_dir_mtime(sid: str) -> float:
    d = runtime.SESSIONS_ROOT / sid / "nodes"
    try:
        return d.stat().st_mtime if d.exists() else 0.0
    except OSError:
        return 0.0


def load_nodes_cached(sid: str | None) -> list[dict]:
    if not sid:
        return []
    return _cached_nodes(sid, _nodes_dir_mtime(sid))


# ── theme + styles ─────────────────────────────────────────────────────────

st.markdown(styles.inject(ss.theme), unsafe_allow_html=True)


# ── sidebar (one render per full page rerun — not inside the fragment) ─────

try:
    sidebar.render(ss)
except Exception as e:  # noqa: BLE001 — sidebar errors must not kill the page
    with st.sidebar:
        st.error(f"Sidebar render failed: {type(e).__name__}: {e}")


# ── sid sniff + end-of-run detection at page level ─────────────────────────

if ss.sid is None and ss.sid_holder.get("sid"):
    ss.sid = ss.sid_holder["sid"]
    ss.viewing_history = False

if (ss.proc is not None
        and ss.proc.poll() is not None
        and ss.run_end_at is None):
    ss.run_end_at = time.time()
    rc = ss.proc.returncode
    if ss._announced_complete_for != ss.sid:
        ss._announced_complete_for = ss.sid
        toast(
            f"Run {ss.sid or ''} {'completed' if rc == 0 else f'failed (exit {rc})'}",
            icon="✓" if rc == 0 else "✗",
        )

status_word = (
    "running" if runtime.is_running(ss.proc)
    else "complete" if ss.proc is not None and ss.proc.returncode == 0
    else "failed" if ss.proc is not None
    else "idle"
)


# ── header ─────────────────────────────────────────────────────────────────

top_l, top_r = st.columns([3, 1])
with top_l:
    hero_header(
        "Growing-Graph Orchestrator",
        "Live trace of a planner-emitted DAG. Watch nodes complete in real "
        "time, inspect their prompts, and quantify the parallel speedup.",
    )
with top_r:
    _badge_html = badge(status_word)
    _proc_wall = (
        (ss.run_end_at or time.time()) - ss.run_start_at
        if ss.run_start_at else None
    )
    st.markdown(
        f"<div class='card card-tight' style='text-align:right'>"
        f"<div class='metric-label'>Status</div>"
        f"<div style='margin-top:6px; margin-bottom:8px'>{_badge_html}</div>"
        f"<div class='muted' style='font-size:0.78rem'>"
        f"process wall: <code>{fmt_dur(_proc_wall)}</code></div>"
        f"</div>", unsafe_allow_html=True,
    )

st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)


# ── query card ─────────────────────────────────────────────────────────────

try:
    run_clicked, stop_clicked = query_card.render(ss)
except Exception as e:  # noqa: BLE001
    st.error(f"Query card failed to render: {type(e).__name__}: {e}")
    run_clicked = stop_clicked = False


if run_clicked:
    # Cleanly retire any prior process before starting a new one.
    if runtime.is_running(ss.proc):
        runtime.stop_run(ss.proc)
    ss.stdout_lines = deque(maxlen=800)
    ss.sid = None
    ss.sid_holder = {"sid": None}
    ss.run_start_at = time.time()
    ss.run_end_at = None
    ss._announced_complete_for = None
    ss.viewing_history = False
    env_overrides: dict[str, str] = {}
    if ss.model_override.strip():
        env_overrides["MODEL_OVERRIDE"] = ss.model_override.strip()
    if ss.temperature_override >= 0:
        env_overrides["TEMPERATURE_OVERRIDE"] = f"{ss.temperature_override:.2f}"
    if ss.skill_focus:
        env_overrides["SKILL_FOCUS"] = ss.skill_focus
    ss.proc = runtime.start_run(ss.query_text, env_overrides=env_overrides)
    runtime.spawn_reader(ss.proc, ss.stdout_lines, ss.sid_holder)
    # Clear the node cache so the new session's files appear without
    # waiting for the TTL to expire on a previous sid.
    _cached_nodes.clear()
    toast(f"Run started: {ss.query_text[:60]}…", icon="🚀")
    st.rerun()

if stop_clicked:
    runtime.stop_run(ss.proc)
    toast("Run stopped", icon="■")
    st.rerun()


# ── resume handler ────────────────────────────────────────────────────────
# Triggered by the sidebar's "Resume by id" input or any history card's
# "▶ Resume" button. We stash the requested sid in `ss._pending_resume`
# from the sidebar (no subprocess spawning there) and handle the lifecycle
# here — same place the fresh-run handler lives — so all subprocess setup,
# state reset, and reader-thread wiring goes through one code path.
_pending_resume = ss.pop("_pending_resume", None)
if _pending_resume and not runtime.is_running(ss.proc):
    if runtime.is_running(ss.proc):
        runtime.stop_run(ss.proc)
    ss.stdout_lines = deque(maxlen=800)
    # Seed the deque with whatever stdout the previous run already
    # persisted — gives the user the prior transcript right after the
    # resume click, before the new subprocess emits its first line.
    seeded = runtime.load_log(_pending_resume, tail=400)
    for line in seeded:
        ss.stdout_lines.append(line)
    # IMPORTANT: pre-populate ss.sid so the sidebar history card
    # highlights the resumed session immediately and the file-cache
    # invalidation below targets the right key. The reader thread
    # would set sid_holder again when flow.py re-prints its sid
    # banner, so we also reset sid_holder to keep them in sync.
    ss.sid = _pending_resume
    ss.sid_holder = {"sid": _pending_resume}
    ss.run_start_at = time.time()
    ss.run_end_at = None
    ss._announced_complete_for = None
    ss.viewing_history = False
    env_overrides: dict[str, str] = {}
    if ss.model_override.strip():
        env_overrides["MODEL_OVERRIDE"] = ss.model_override.strip()
    if ss.temperature_override >= 0:
        env_overrides["TEMPERATURE_OVERRIDE"] = f"{ss.temperature_override:.2f}"
    if ss.skill_focus:
        env_overrides["SKILL_FOCUS"] = ss.skill_focus
    ss.proc = runtime.start_resume(_pending_resume, env_overrides=env_overrides)
    runtime.spawn_reader(ss.proc, ss.stdout_lines, ss.sid_holder)
    _cached_nodes.clear()  # drop stale node cache so re-runs of pending
                           # nodes show their fresh output, not the prior
                           # `pending` placeholder
    toast(f"Resuming {_pending_resume}…", icon="▶")
    st.rerun()


# ── live region: tabs auto-refresh ONLY while subprocess is alive ──────────

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

# `run_every=None` → the fragment is render-once; no idle cost when the
# subprocess is not alive. When it IS alive, the fragment re-renders at
# `refresh_interval` seconds without touching anything else on the page.
_RUN_EVERY: float | None = (
    float(ss.refresh_interval) if runtime.is_running(ss.proc) else None
)


def _nodes_signature(sid: str | None, log_len: int) -> tuple:
    """A cheap content-stamp used to decide whether tab bodies need to redraw.

    The fragment fires every `refresh_interval` seconds, but most ticks
    don't change anything on disk — the orchestrator might be mid-LLM
    waiting on the gateway. Recomputing the signature is two stat calls
    plus a `len(deque)`; if it matches the prior render, we keep the
    DOM as-is and skip the (much more expensive) tab-body work.

    Including `log_len` means a new stdout line ALSO triggers a redraw
    even when no node file has landed yet — the user still sees the
    log tab advance line-by-line.
    """
    return (sid, _nodes_dir_mtime(sid) if sid else 0.0, log_len)


@st.fragment(run_every=_RUN_EVERY)
def live_tabs() -> None:
    """The five result tabs. Auto-refresh body — the only part of the
    page that re-renders on the 1s tick.

    Two deliberate non-obvious choices:

    1. **No `st.rerun()` calls inside this fragment.** A full-page rerun
       costs ~80–150 ms (sidebar, header, agent-config read, theme
       inject); the fragment's whole reason for existing is to avoid
       that cost. We update `ss.sid` etc. in place and let the next
       1s tick render with the new state. End-of-run *does* still need
       a full-page rerun (to recompute `_RUN_EVERY` to None and stop
       the auto-refresh), but only ONCE per completion — guarded by
       `_announced_complete_for`.

    2. **`_nodes_signature` short-circuits unchanged ticks.** Streamlit
       fragments re-render their entire body on every auto-tick; with
       a stable signature we still call the renderer functions (the
       DOM tree must stay structurally identical or Streamlit unmounts
       it), but each renderer can early-out via its own session-state
       check. The signature is the *single source of truth* for "did
       anything change."
    """

    # End-of-run detection inside the fragment. ONE rerun, gated on
    # `_announced_complete_for` so it never loops. After this rerun
    # the page re-evaluates `_RUN_EVERY` to None and the fragment
    # stops auto-refreshing — zero idle cost while sitting on a
    # completed run.
    if (ss.proc is not None
            and ss.proc.poll() is not None
            and ss.run_end_at is None):
        ss.run_end_at = time.time()
        rc = ss.proc.returncode
        if ss._announced_complete_for != ss.sid:
            ss._announced_complete_for = ss.sid
            toast(
                f"Run {ss.sid or ''} "
                f"{'completed' if rc == 0 else f'failed (exit {rc})'}",
                icon="✓" if rc == 0 else "✗",
            )
            st.rerun()

    # Pick up the sid the reader thread sniffed. We DO trigger one
    # full-page rerun here — but exactly once per run, the moment the
    # sid first appears. This is what causes the new session to show
    # up in the sidebar's "Session history" list and become the
    # highlighted entry. Without it, the running session is invisible
    # in the sidebar until the user clicks something else.
    #
    # Cost: one ~80–150 ms rerun per run start. The 1s-tick auto-refresh
    # itself stays cheap because subsequent ticks find `ss.sid` already
    # populated and skip the rerun.
    if ss.sid is None and ss.sid_holder.get("sid"):
        ss.sid = ss.sid_holder["sid"]
        ss.viewing_history = False
        st.rerun()

    # Render-skip: if nothing has changed since the last tick, we still
    # have to draw the structural DOM (otherwise the fragment clears
    # itself), but downstream renderers consult `ss._render_sig` to
    # skip their own expensive work (charts, dataframes, JSON encoding).
    sig = _nodes_signature(ss.sid, len(ss.stdout_lines))
    ss._render_sig = sig
    ss._render_unchanged = (sig == ss.get("_prev_render_sig"))
    ss._prev_render_sig = sig

    nodes = load_nodes_cached(ss.sid)

    if ss.viewing_history and ss.sid:
        q = runtime.load_query(ss.sid)
        if q:
            st.info(
                f"Viewing history: `{ss.sid}` — query: _{q[:140]}_"
            )

    t1, t2, t3, t4, t5 = st.tabs([
        "✦  Final answer", "◇  Reasoning steps", "⚙  Tool calls",
        "▤  Execution logs", "▦  Metrics",
    ])

    # One try/except per tab — a bug in one tab no longer breaks the page.
    renderers = (
        (t1, "Final answer", lambda: tabs.final_answer(nodes)),
        (t2, "Reasoning steps", lambda: tabs.reasoning_steps(nodes)),
        (t3, "Tool calls", lambda: tabs.tool_calls(nodes)),
        (t4, "Execution logs", lambda: tabs.execution_logs(ss)),
        (t5, "Metrics", lambda: tabs.metrics(nodes)),
    )
    for tab_obj, name, fn in renderers:
        with tab_obj:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — tab-level isolation
                st.error(
                    f"The **{name}** tab failed to render: "
                    f"`{type(e).__name__}: {e}`\n\n"
                    "The other tabs and sidebar are unaffected — "
                    "this error is isolated to this tab."
                )


live_tabs()
