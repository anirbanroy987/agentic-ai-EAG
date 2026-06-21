"""Session 9 — the 8-point Replay Report generator.

This is the assignment's *Replay Viewer*: it turns a persisted flow-run
into a single, shareable report covering the eight points the grader asks
for, in order:

    1. Original user goal
    2. Planner DAG
    3. Browser path chosen   (extract / deterministic / a11y / vision / blocked)
    4. Browser actions taken
    5. Screenshots or page-state logs
    6. Extracted data
    7. Final comparison table
    8. Turn count and cost summary

It is a pure *reader*. It never imports flow.py's orchestration, never
mutates a session, and adds no node to the graph — so it honours the
"do not modify the orchestrator" rule. Everything it shows already lives
on disk (state/sessions/<sid>/) or in the V9 gateway's ledger; this file
only assembles it.

Usage
-----
    uv run python report.py <session_id>              # md + html
    uv run python report.py <session_id> --format md
    uv run python report.py <session_id> --gateway http://localhost:8109
    uv run python report.py                           # list sessions

Outputs land next to the session:
    state/sessions/<sid>/report.md
    state/sessions/<sid>/report.html   (screenshots embedded — open in a browser)
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx

# Windows consoles default to cp1252, which cannot encode '₹' and friends.
# The report routinely prints them (prices, the query), so force UTF-8 on
# our own streams. Harmless on platforms that are already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from persistence import SESSIONS_ROOT, SessionStore, list_sessions
from schemas import AgentResult, NodeState

# ── tiny presentation helpers ────────────────────────────────────────────────

_STATUS_GLYPH = {
    "complete": "✓",
    "failed": "✗",
    "skipped": "⤼",
    "running": "…",
    "pending": "·",
}

# Layer the cascade ran, normalised to the five outcomes the assignment grades.
_PATH_LABEL = {
    "extract": "Layer 1 · Extract (HTTPX + Trafilatura, no browser, no LLM)",
    "deterministic": "Layer 2A · Deterministic (Playwright + CSS selectors, no LLM)",
    "a11y": "Layer 2B · A11y tree (accessibility legend + cheap LM)",
    "vision": "Layer 3 · Vision (VLM + Set-of-Mark boxes — last resort)",
}


def _mermaid_id(node_id: str) -> str:
    return node_id.replace(":", "").replace("-", "_")


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n].rstrip() + " …"


# ── load everything the report needs ─────────────────────────────────────────

class RunData:
    """Everything assembled from one on-disk session, lenient to partial runs."""

    def __init__(self, session_id: str, gateway: str):
        self.session_id = session_id
        self.gateway = gateway.rstrip("/")
        self.store = SessionStore(session_id)
        self.query = self.store.read_query() or "(no query.txt on disk)"
        self.nodes: list[NodeState] = self.store.read_all_nodes()
        self.graph = self._read_graph_raw()
        self.cost = self._fetch_cost()

    # graph.json is read raw (not via read_graph) so a node whose AgentResult
    # no longer round-trips does not abort the whole report — we only need
    # ids / skills / statuses / edges for the DAG picture.
    def _read_graph_raw(self) -> dict:
        p = self.store.graph_path
        if not p.exists():
            return {"nodes": [], "edges": []}
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {"nodes": [], "edges": []}

    def _fetch_cost(self) -> dict | None:
        """Point 8's authoritative source is the gateway ledger. If the
        gateway is down we fall back (in render) to per-node wall-clock."""
        try:
            r = httpx.get(
                f"{self.gateway}/v1/cost/by_agent",
                params={"session": self.session_id},
                timeout=5.0,
            )
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            return None

    # convenience views ------------------------------------------------------

    def node_by_id(self, nid: str) -> NodeState | None:
        return next((n for n in self.nodes if n.node_id == nid), None)

    def nodes_of_skill(self, skill: str) -> list[NodeState]:
        return [n for n in self.nodes if n.skill == skill]

    def browser_nodes(self) -> list[NodeState]:
        return self.nodes_of_skill("browser")

    def formatter_answer(self) -> str | None:
        for n in reversed(self.nodes):
            if n.skill == "formatter" and n.result and isinstance(n.result.output, dict):
                fa = n.result.output.get("final_answer")
                if isinstance(fa, str) and fa.strip():
                    return fa
        return None

    def browser_artifact_dirs(self) -> list[Path]:
        root = self.store.dir / "browser"
        if not root.exists():
            return []
        # Each per-layer dir holds turn_##_*.png / turn_##_legend.txt.
        return sorted(p for p in root.rglob("*") if p.is_dir()
                      and any(p.glob("turn_*")))


# ── section builders (return Markdown; HTML wraps the same text) ─────────────

def _sec_goal(d: RunData) -> str:
    return f"## 1 · Original user goal\n\n> {d.query.strip()}\n\n`session: {d.session_id}`\n"


def _sec_dag(d: RunData) -> str:
    nodes = d.graph.get("nodes", [])
    edges = d.graph.get("edges", [])
    if not nodes:
        return "## 2 · Planner DAG\n\n_(no graph.json found for this session)_\n"

    # Mermaid block — renders on GitHub and most Markdown viewers.
    lines = ["## 2 · Planner DAG\n", "```mermaid", "graph TD"]
    for n in nodes:
        nid = n.get("id", "?")
        skill = n.get("skill", "?")
        status = n.get("status", "?")
        glyph = _STATUS_GLYPH.get(status, "?")
        lines.append(f'    {_mermaid_id(nid)}["{nid} · {skill} {glyph}"]')
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s and t:
            lines.append(f"    {_mermaid_id(s)} --> {_mermaid_id(t)}")
    lines.append("```\n")

    # Plain-text edge list as a fallback for viewers without Mermaid.
    lines.append("Edges (parent → child):\n")
    if edges:
        for e in edges:
            lines.append(f"- `{e.get('source')}` → `{e.get('target')}`")
    else:
        lines.append("- _(single-node run, no edges)_")
    lines.append("")
    lines.append("Node legend: " + "  ".join(
        f"{g} {name}" for name, g in
        (("complete", "✓"), ("failed", "✗"), ("skipped", "⤼"), ("pending", "·"))
    ))
    return "\n".join(lines) + "\n"


def _browser_outcome(n: NodeState) -> tuple[str, str]:
    """Returns (path_label, note). Folds the blocked outcome into the path
    so point 3 always reports one of the five branches."""
    r = n.result
    if r is None:
        return ("(no result)", "node did not complete")
    if r.error_code == "gateway_blocked":
        return ("Gateway-Blocked (CAPTCHA / login / geo / rate-limit)",
                r.error or "blocked before any cascade layer ran")
    if not r.success:
        return (f"failed ({r.error_code or 'error'})", r.error or "")
    path = (r.output or {}).get("path", "?")
    return (_PATH_LABEL.get(path, path), "")


def _sec_path(d: RunData) -> str:
    bnodes = d.browser_nodes()
    out = ["## 3 · Browser path chosen (the cascade winner per step)\n"]
    if not bnodes:
        out.append("_(no Browser node ran in this session — the Planner did "
                    "not route through the Browser skill.)_\n")
        return "\n".join(out)
    out.append("| node | url | chosen path | note |")
    out.append("|---|---|---|---|")
    for n in bnodes:
        url = (n.result.output or {}).get("url", "") if n.result else ""
        label, note = _browser_outcome(n)
        out.append(f"| `{n.node_id}` | {_truncate(url, 60)} | **{label}** | {_truncate(note, 70)} |")
    out.append("\nThe cascade always tries cheapest-first and stops at the "
               "first layer that produces a useful answer; the **chosen path** "
               "column is that winner, surfaced (not hidden) per the brief.\n")
    return "\n".join(out)


def _sec_actions(d: RunData) -> str:
    bnodes = d.browser_nodes()
    out = ["## 4 · Browser actions taken\n"]
    if not bnodes:
        out.append("_(no Browser node ran.)_\n")
        return "\n".join(out)
    for n in bnodes:
        r = n.result
        out.append(f"### {n.node_id} — goal\n")
        goal = (r.output or {}).get("goal", "") if r else ""
        out.append(f"> {goal}\n")
        if r and r.error_code == "gateway_blocked":
            out.append("_Blocked at the precondition check — no actions were "
                       "issued (the agent does **not** attempt to bypass the "
                       "gateway). This is the clean 'blocked' outcome._\n")
            continue
        actions = (r.output or {}).get("actions", []) if r else []
        if not actions:
            out.append("_No interactive turns recorded (e.g. Layer 1 extract — "
                       "static fetch needs no actions)._\n")
            continue
        out.append("| turn | actions | outcome |")
        out.append("|---:|---|---|")
        n_visible = 0
        for step in actions:
            turn = step.get("turn", "?")
            acts = step.get("actions", [])
            outcome = step.get("outcome", "")
            pretty = ", ".join(
                f"{a.get('type')}(" +
                (f"mark={a.get('mark')}" if a.get("mark") is not None else "") +
                (f"'{_truncate(str(a.get('value')), 24)}'" if a.get("value") else "") +
                ")"
                for a in acts
            )
            n_visible += sum(1 for a in acts if a.get("type") in
                             ("click", "type", "key", "scroll", "drag"))
            out.append(f"| {turn} | {pretty} | {_truncate(outcome, 50)} |")
        out.append(f"\n**Visible browsing actions in `{n.node_id}`: {n_visible}** "
                   f"(brief requires ≥ 3).\n")
    return "\n".join(out)


def _sec_screens(d: RunData, *, embed: bool) -> str:
    out = ["## 5 · Screenshots / page-state logs\n"]
    dirs = d.browser_artifact_dirs()
    if not dirs:
        out.append("_(no per-turn artifacts on disk — an extract-only or "
                   "blocked run produces none.)_\n")
        return "\n".join(out)
    for ddir in dirs:
        layer = ddir.name
        rel = ddir.relative_to(d.store.dir)
        out.append(f"### Layer `{layer}` — `{rel}`\n")
        pngs = sorted(ddir.glob("turn_*_marked.png")) or sorted(ddir.glob("turn_*_raw.png"))
        legends = sorted(ddir.glob("turn_*_legend.txt"))
        for png in pngs:
            if embed:
                b64 = base64.b64encode(png.read_bytes()).decode()
                out.append(f"**{png.name}**\n")
                out.append(f'<img src="data:image/png;base64,{b64}" '
                           f'style="max-width:900px;border:1px solid #ccc" />\n')
            else:
                out.append(f"- ![{png.name}]({png.relative_to(d.store.dir)})")
        if legends:
            first = legends[0]
            snippet = _truncate(first.read_text(encoding="utf-8", errors="replace"), 600)
            out.append(f"\n_Page-state legend (`{first.name}`), the text the "
                       f"a11y layer reasoned over instead of raw HTML:_\n")
            out.append(f"```\n{snippet}\n```\n")
    return "\n".join(out)


def _sec_extracted(d: RunData) -> str:
    out = ["## 6 · Extracted data\n"]
    # Raw page content the Browser skill pulled.
    for n in d.browser_nodes():
        content = (n.result.output or {}).get("content") if n.result else None
        if content:
            out.append(f"### `{n.node_id}` raw page content (trafilatura over the "
                       f"final DOM, truncated)\n")
            out.append(f"```\n{_truncate(content, 1500)}\n```\n")
    # Structured fields the Distiller produced.
    for n in d.nodes_of_skill("distiller"):
        if n.result and isinstance(n.result.output, dict):
            out.append(f"### `{n.node_id}` distilled structured fields\n")
            out.append("```json\n" +
                       json.dumps(n.result.output, indent=2, ensure_ascii=False)[:2000] +
                       "\n```\n")
    # Critic verdicts, if any (shows the validate-extraction step ran).
    for n in d.nodes_of_skill("critic"):
        if n.result and isinstance(n.result.output, dict):
            verdict = n.result.output.get("verdict") or n.result.output.get("pass")
            rationale = n.result.output.get("rationale", "")
            out.append(f"- **critic `{n.node_id}`** → `{verdict}` — "
                       f"{_truncate(str(rationale), 120)}")
    if len(out) == 1:
        out.append("_(no extracted content recorded.)_")
    return "\n".join(out) + "\n"


def _sec_table(d: RunData) -> str:
    out = ["## 7 · Final comparison table\n"]
    fa = d.formatter_answer()
    if not fa:
        out.append("_(no formatter final_answer found — the run did not reach a "
                   "terminal formatter, or it produced empty output.)_\n")
        return "\n".join(out)
    # The formatter is told to adapt format to the question; for a comparison
    # query it emits a Markdown table, which we pass through verbatim.
    out.append(fa.strip() + "\n")
    return "\n".join(out)


def _sec_cost(d: RunData) -> str:
    out = ["## 8 · Turn count & cost summary\n"]

    # Per-node wall-clock + browser turns from the persisted records.
    total_elapsed = 0.0
    browser_turns = 0
    out.append("| node | skill | status | elapsed | provider | turns |")
    out.append("|---|---|---|---:|---|---:|")
    for n in d.nodes:
        r = n.result
        el = r.elapsed_s if r else 0.0
        total_elapsed += el or 0.0
        turns = ""
        if n.skill == "browser" and r:
            t = (r.output or {}).get("turns", 0)
            browser_turns += t or 0
            turns = str(t)
        prov = (r.provider if r and r.provider else "—")
        out.append(f"| `{n.node_id}` | {n.skill} | "
                   f"{_STATUS_GLYPH.get(n.status, '?')} {n.status} | "
                   f"{el:.1f}s | {prov} | {turns} |")
    out.append(f"\n- **Nodes executed:** {len(d.nodes)}")
    out.append(f"- **Browser interaction turns:** {browser_turns}")
    out.append(f"- **Total node wall-clock:** {total_elapsed:.1f}s "
               f"(nodes on the same level run concurrently, so real time is less)")

    # Authoritative per-agent ledger from the gateway.
    out.append("\n### Gateway ledger (per-agent, scoped to this session)\n")
    if not d.cost:
        out.append("_Gateway not reachable at report time — start the V9 "
                   "gateway and re-run `report.py` to populate the live "
                   "token/$ ledger. (The wall-clock table above is "
                   "self-contained and needs no gateway.)_\n")
        return "\n".join(out)
    if not any(d.cost.values()):
        out.append("_Ledger returned no rows for this session id "
                   "(the run may have used a different session tag, or the "
                   "gateway DB was reset)._\n")
        return "\n".join(out)
    out.append("| agent | provider | calls | in tok | out tok | $ | ok/err |")
    out.append("|---|---|---:|---:|---:|---:|---:|")
    tot_calls = tot_in = tot_out = 0
    for agent, rows in d.cost.items():
        for row in rows:
            calls = row.get("calls", 0) or 0
            in_tok = row.get("in_tok", row.get("input_tokens", 0)) or 0
            out_tok = row.get("out_tok", row.get("output_tokens", 0)) or 0
            dollars = row.get("dollars", "")
            ok = row.get("ok", "")
            err = row.get("errors", "")
            tot_calls += calls
            tot_in += in_tok
            tot_out += out_tok
            out.append(f"| {agent} | {row.get('provider','')} | {calls} | "
                       f"{in_tok} | {out_tok} | {dollars} | {ok}/{err} |")
    out.append(f"\n- **Total gateway calls:** {tot_calls}")
    out.append(f"- **Total tokens:** {tot_in} in / {tot_out} out")
    out.append("\n_Note: an `extract`-path Browser step makes **zero** gateway "
               "calls (trafilatura runs locally) — if Browser is absent from "
               "the ledger above, the cascade's cheapest layer won, for free._")
    return "\n".join(out)


# ── assembly ─────────────────────────────────────────────────────────────────

def build_markdown(d: RunData, *, embed_images: bool) -> str:
    head = (
        f"# Replay Report — Session 9 Browser Agent\n\n"
        f"_Browser-capable comparison run, walked through the official "
        f"pipeline (Planner → Researcher → Browser → 4-layer cascade → "
        f"Distiller → Critic → Formatter) and reported across the 8 "
        f"required points._\n"
    )
    sections = [
        _sec_goal(d),
        _sec_dag(d),
        _sec_path(d),
        _sec_actions(d),
        _sec_screens(d, embed=embed_images),
        _sec_extracted(d),
        _sec_table(d),
        _sec_cost(d),
    ]
    return head + "\n" + "\n\n".join(s.strip() for s in sections) + "\n"


_HTML_SHELL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Replay Report — {sid}</title>
<style>
 body{{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;max-width:980px;
      margin:2rem auto;padding:0 1rem;color:#1b1b1b}}
 h1{{border-bottom:3px solid #333;padding-bottom:.3rem}}
 h2{{margin-top:2.4rem;border-bottom:1px solid #ddd;padding-bottom:.2rem}}
 table{{border-collapse:collapse;width:100%;margin:.6rem 0}}
 th,td{{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;font-size:14px}}
 th{{background:#f3f3f3}}
 code{{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px}}
 pre{{background:#1e1e1e;color:#d4d4d4;padding:.8rem;border-radius:6px;overflow:auto}}
 pre code{{background:none;color:inherit;padding:0}}
 blockquote{{border-left:4px solid #888;margin:.5rem 0;padding:.2rem .9rem;color:#444}}
 img{{display:block;margin:.5rem 0}}
</style>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
</head><body>
<div id="md" style="display:none">{md}</div>
<div id="out"></div>
<script>
 const raw = document.getElementById('md').textContent;
 marked.setOptions({{breaks:false, gfm:true}});
 document.getElementById('out').innerHTML = marked.parse(raw);
 // Re-tag fenced mermaid blocks so mermaid can find them.
 document.querySelectorAll('pre code.language-mermaid').forEach(el => {{
   const p = el.parentElement;
   const d = document.createElement('div');
   d.className = 'mermaid'; d.textContent = el.textContent;
   p.replaceWith(d);
 }});
 mermaid.initialize({{startOnLoad:true, securityLevel:'loose'}});
</script>
</body></html>
"""


def build_html(markdown_with_images: str, sid: str) -> str:
    # Embed the Markdown as text and render client-side with marked + mermaid.
    # Escaping </script> and backticks keeps the injected blob inert.
    safe = (markdown_with_images
            .replace("</script>", "<\\/script>")
            .replace("</div>", "<\\/div>"))
    return _HTML_SHELL.format(sid=sid, md=safe)


# ── CLI ──────────────────────────────────────────────────────────────────────

def generate(session_id: str, *, gateway: str, fmt: str) -> int:
    if not (SESSIONS_ROOT / session_id).exists():
        print(f"report: no session at {SESSIONS_ROOT / session_id}", file=sys.stderr)
        return 2
    d = RunData(session_id, gateway)
    written: list[Path] = []

    if fmt in ("md", "both"):
        md = build_markdown(d, embed_images=False)
        p = d.store.dir / "report.md"
        p.write_text(md, encoding="utf-8")
        written.append(p)
    if fmt in ("html", "both"):
        md_embed = build_markdown(d, embed_images=True)
        html = build_html(md_embed, session_id)
        p = d.store.dir / "report.html"
        p.write_text(html, encoding="utf-8")
        written.append(p)

    print(f"session  {session_id}")
    print(f"query    {_truncate(d.query, 90)}")
    print(f"nodes    {len(d.nodes)}  |  browser nodes {len(d.browser_nodes())}  |  "
          f"ledger {'live' if d.cost else 'unavailable'}")
    for p in written:
        print(f"wrote    {p}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    gateway = "http://localhost:8109"
    fmt = "both"
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--gateway":
            gateway = args[i + 1]; i += 2; continue
        if a == "--format":
            fmt = args[i + 1]; i += 2; continue
        positional.append(a); i += 1

    if not positional:
        sessions = list_sessions()
        if not sessions:
            print("report: no sessions under state/sessions/", file=sys.stderr)
            return 2
        print("available sessions:")
        for s in sessions:
            print(f"  {s}")
        print("\nusage: uv run python report.py <session_id> "
              "[--format md|html|both] [--gateway URL]")
        return 0
    if fmt not in ("md", "html", "both"):
        print(f"report: --format must be md|html|both, got {fmt!r}", file=sys.stderr)
        return 2
    return generate(positional[0], gateway=gateway, fmt=fmt)


if __name__ == "__main__":
    sys.exit(main())
