"""app.py — Streamlit viewer for the EXISTING Session-7 agent.

This is a VIEWER, not a RAG system. It runs the unmodified `agent7.py` as a
subprocess, streams its stdout, and renders each loop iteration as a
Perception → Decision → Action → Memory timeline. No agent code, prompts,
or MCP tools are touched. Retrieval, embeddings, and chunking all happen
inside the agent exactly as they do on the command line.

How it reads per-iteration state: agent7.py prints stable, prefixed lines to
stdout (`─── iter N`, `[memory.read]`, `[perception]`, `[decision]`,
`[action]`, `FINAL:`). We parse those lines as they arrive. The agent
truncates its `[action]` preview to ~200 chars, so the Retrieved-sources
panel shows those 200-char previews (by design — we do not re-query or read
the index, keeping this a pure viewer).

Run (from the RAG-Finance folder, in the same venv as the agent):
    uv run streamlit run app.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st

AGENT_DIR = Path(__file__).resolve().parent
AGENT_SCRIPT = AGENT_DIR / "agent7.py"

ITER_RE = re.compile(r"iter\s+(\d+)")
# Args are truncated to 120 chars by the agent, so the closing ')' may be
# missing; don't require it. Strip a trailing ')' if present.
TOOLCALL_RE = re.compile(r"TOOL_CALL:\s*([\w\-]+)\((.*?)\)?\s*$")


# ── line classification ───────────────────────────────────────────────────────

def classify(line: str) -> tuple[str, dict]:
    """Map one stdout line to (kind, payload). Unknown lines → ('other', ...)."""
    s = line.rstrip()
    stripped = s.strip()

    if "─── iter" in s:
        m = ITER_RE.search(s)
        return "iter", {"n": int(m.group(1)) if m else None}

    if stripped.startswith("[memory.read]"):
        return "memory_read", {"text": stripped.replace("[memory.read]", "").strip()}

    if stripped.startswith("[perception]"):
        body = stripped.replace("[perception]", "").strip()
        done = body.startswith("✓")
        return "perception", {"text": body, "done": done}

    if stripped.startswith("[attach]"):
        return "attach", {"text": stripped.replace("[attach]", "").strip()}

    if stripped.startswith("[decision]"):
        body = stripped.replace("[decision]", "").strip()
        tc = TOOLCALL_RE.search(body)
        if tc:
            return "decision_tool", {"tool": tc.group(1), "args": tc.group(2)}
        if body.startswith("ANSWER:"):
            return "decision_answer", {"text": body[len("ANSWER:"):].strip()}
        return "decision_other", {"text": body}

    if stripped.startswith("[action]"):
        return "action", {"text": stripped.replace("[action]", "").strip().lstrip("→").strip()}

    if stripped.startswith("[done]"):
        return "done", {"text": stripped.replace("[done]", "").strip()}

    if stripped.startswith("[mcp]"):
        return "mcp", {"text": stripped.replace("[mcp]", "").strip()}

    if stripped.startswith("[gateway]") or stripped.startswith("[memory"):
        return "infra", {"text": stripped}

    if stripped.startswith("FINAL:"):
        return "final_start", {"text": stripped[len("FINAL:"):].strip()}

    if set(stripped) == {"═"} and stripped:
        return "banner", {}

    if stripped.startswith("run ") and "query:" in stripped:
        return "runhdr", {"text": stripped}

    return "other", {"text": s}


# ── subprocess runner ─────────────────────────────────────────────────────────

def agent_python() -> str:
    """Pick the Python that has the agent's deps (faiss-cpu, mcp, ...).

    Streamlit may be launched from a *different* venv (e.g. the parent repo's
    .venv) whose interpreter lacks faiss-cpu — then `sys.executable` would run
    the agent with the wrong env and it dies with 'faiss-cpu is required'.
    Prefer this project's own venv interpreter when it exists; fall back to
    whatever is running Streamlit.
    """
    candidates = [
        AGENT_DIR / ".venv" / "Scripts" / "python.exe",  # Windows
        AGENT_DIR / ".venv" / "bin" / "python",          # POSIX
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def launch_agent(query: str) -> subprocess.Popen:
    env = os.environ.copy()
    # The agent prints box-drawing / arrow glyphs; force UTF-8 so a piped
    # Windows console (cp1252) doesn't crash the child, and unbuffer so lines
    # stream as they are produced.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # Pin the agent to THIS project's venv so it always finds faiss-cpu etc.,
    # regardless of which interpreter launched Streamlit.
    env.pop("VIRTUAL_ENV", None)
    return subprocess.Popen(
        [agent_python(), str(AGENT_SCRIPT), query],
        cwd=str(AGENT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )


# ── UI ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Finance RAG — Agent Viewer", page_icon="💰", layout="wide")

st.title("💰 Personal-Finance RAG — Agent Viewer")
st.warning("**Educational only — not personalized financial advice.**", icon="⚠️")
st.caption(
    "This viewer runs the existing Session-7 agent (`agent7.py`) as a subprocess and "
    "shows its Perception → Decision → Action → Memory loop. It does not re-implement "
    "retrieval, embeddings, or chunking."
)

with st.sidebar:
    st.subheader("Run config")
    st.text(f"Agent: {AGENT_SCRIPT.name}")
    st.text(f"Dir:   {AGENT_DIR.name}")
    if not AGENT_SCRIPT.exists():
        st.error("agent7.py not found next to app.py.")
    st.markdown(
        "**Tip:** index your corpus first via the agent, e.g.\n\n"
        '`uv run agent7.py "Index every .md file under finance/ '
        'and tell me how many chunks were added."`'
    )

query = st.text_input(
    "Ask the knowledge base",
    placeholder="e.g. What do the sources say about emergency fund vs home-loan prepayment?",
)
run = st.button("Run", type="primary", disabled=not AGENT_SCRIPT.exists())

if run and not query.strip():
    st.info("Enter a query first.")

if run and query.strip():
    timeline = st.container()
    retrieved: list[dict] = []  # search_knowledge action previews
    final_chunks: list[str] = []
    capturing_final = False
    raw_lines: list[str] = []

    current_status = None
    startup_status = None
    last_tool: str | None = None
    last_tool_args: str | None = None
    error_exit = False

    try:
        proc = launch_agent(query.strip())
    except Exception as e:  # pragma: no cover
        st.error(f"Failed to launch agent: {e}")
        st.stop()

    with timeline:
        for raw in proc.stdout:  # streams line by line
            raw_lines.append(raw.rstrip("\n"))
            kind, p = classify(raw)

            # collect the full final answer block (FINAL: ... up to next banner)
            if capturing_final:
                if kind == "banner":
                    capturing_final = False
                else:
                    final_chunks.append(raw.rstrip("\n"))
                continue

            if kind == "iter":
                if current_status is not None:
                    current_status.update(state="complete")
                label = f"Iteration {p['n']}" if p["n"] else "Iteration"
                current_status = st.status(label, expanded=True)
                last_tool = None
                continue

            if kind == "mcp":
                if startup_status is None:
                    startup_status = st.status("Startup", expanded=False)
                startup_status.write(f"🔌 MCP tools loaded — {p['text']}")
                continue

            if kind == "infra":
                tgt = current_status or startup_status
                if tgt is None:
                    startup_status = st.status("Startup", expanded=False)
                    tgt = startup_status
                tgt.write(f"⚙️ {p['text']}")
                continue

            if kind == "runhdr":
                continue

            target = current_status
            if target is None:
                # pre-iteration noise
                if startup_status is None:
                    startup_status = st.status("Startup", expanded=False)
                target = startup_status

            if kind == "memory_read":
                target.write(f"💾 **Memory (read)** — {p['text']}")
            elif kind == "perception":
                icon = "✅" if p["done"] else "🟡"
                target.write(f"👁 **Perception** {icon} {p['text']}")
            elif kind == "attach":
                target.write(f"📎 **Attach** — {p['text']}")
            elif kind == "decision_tool":
                last_tool, last_tool_args = p["tool"], p["args"]
                target.write(f"🧭 **Decision → tool** `{p['tool']}`  \nargs: `{p['args']}`")
            elif kind == "decision_answer":
                target.write(f"🧭 **Decision → answer** {p['text']}")
            elif kind == "decision_other":
                target.write(f"🧭 **Decision** {p['text']}")
            elif kind == "action":
                target.write(f"⚡ **Action** — {p['text']}")
                # tool outcome is recorded to memory by the agent right after
                target.write("💾 **Memory (write)** — tool outcome recorded")
                if last_tool == "search_knowledge":
                    retrieved.append({
                        "args": last_tool_args or "",
                        "preview": p["text"],
                    })
            elif kind == "done":
                target.write(f"🏁 {p['text']}")
            elif kind == "final_start":
                capturing_final = True
                if p["text"]:
                    final_chunks.append(p["text"])
            # 'banner' / 'other' before final: ignore in timeline

        proc.wait()
        if current_status is not None:
            current_status.update(state="complete")
        if proc.returncode not in (0, None):
            error_exit = True

    # ── final answer ──────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Final answer")
    final_text = "\n".join(final_chunks).strip()
    if final_text:
        st.markdown(final_text)
    elif error_exit:
        st.error(f"Agent exited with code {proc.returncode} and produced no final answer.")
    else:
        st.info("No FINAL answer was emitted (the run may have stopped early).")

    # ── retrieved sources ───────────────────────────────────────────────────────
    st.subheader("Retrieved sources")
    if retrieved:
        st.caption(
            "Chunks fed to the answer, as previewed in the agent's `search_knowledge` "
            "action output (≤200 chars per the agent's own truncation)."
        )
        for i, r in enumerate(retrieved, 1):
            with st.expander(f"search_knowledge call #{i} — args: {r['args']}", expanded=False):
                st.text(r["preview"])
    else:
        st.caption("No `search_knowledge` calls in this run (e.g. an indexing-only or compute query).")

    with st.expander("Raw agent stdout", expanded=False):
        st.code("\n".join(raw_lines) or "(no output)", language="text")

    if error_exit:
        st.warning(f"Agent process exit code: {proc.returncode}")
