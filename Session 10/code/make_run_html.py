"""Generate a self-contained recording.html for each recorded run.

Reads a cua-driver `start_recording` trajectory dir (session.json + per-turn
action.json / screenshot.png / click.png) and emits a single portable HTML with
every screenshot base64-embedded, so the run can be reviewed without the gateway,
the daemon, or even the original files.

    uv run python make_run_html.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "state" / "runs"

# Task metadata per run (from the recorded results; see README.md).
META = {
    "run_1782059771": dict(
        title="Task 1 — Calculator 42 × 18 = 756",
        layer="L2a · deterministic", path="deterministic",
        constraint="zero vision (and zero LLM)",
        entry="tasks calc",
        provider="— (no LLM, no vision call)",
        result="Display is 756  ✓ post-condition met",
    ),
    "run_1782059608": dict(
        title="Task 2 — VS Code → hello_world.txt",
        layer="Electron · page", path="page",
        constraint="Electron page path",
        entry="tasks writefile",
        provider="— (deterministic write)",
        result=r"C:\Users\HP\hello_world.txt = 'Hello World' (11 bytes) — verified on disk",
    ),
    "run_1782057892": dict(
        title="Task 3 — Paint: one canvas stroke",
        layer="L3 · vision", path="vision",
        constraint="uses vision",
        entry="run.py --app Paint --force vision",
        provider="groqvision · meta-llama/llama-4-scout-17b-16e-instruct",
        result="success — stroke drawn (drag 350,275 → 450,275)",
    ),
}

CSS = """
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
--ok:#3fb950;--accent:#58a6ff;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:24px}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 18px}
.meta{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;
background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:24px}
.meta dt{color:var(--mut)}
.meta dd{margin:0;font-weight:600}
.pill{display:inline-block;background:#1f6feb22;color:var(--accent);
border:1px solid #1f6feb55;border-radius:999px;padding:1px 10px;font-size:12px;font-weight:600}
.turn{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:16px;margin-bottom:16px}
.turn h3{margin:0 0 10px;font-size:15px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tool{background:#238636;color:#fff;border-radius:6px;padding:1px 8px;font-size:12px}
.dur{color:var(--mut);font-weight:400;font-size:12px}
pre{background:#0b0f14;border:1px solid var(--bd);border-radius:8px;
padding:10px 12px;overflow:auto;font-size:12px;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.res{color:var(--ok)}
.shots{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}
figure{margin:0}
figure img{max-width:440px;border:1px solid var(--bd);border-radius:8px;display:block}
figcaption{color:var(--mut);font-size:11px;margin-top:4px}
footer{color:var(--mut);font-size:12px;margin-top:24px;text-align:center}
"""


def b64img(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build(run: str, meta: dict) -> tuple[Path, int]:
    rd = RUNS_DIR / run
    sess = json.loads((rd / "session.json").read_text())
    cursor_n = sess.get("cursor", {}).get("sample_count", 0)
    turns = sorted(d for d in rd.iterdir() if d.is_dir() and d.name.startswith("turn-"))

    blocks = []
    for t in turns:
        a = json.loads((t / "action.json").read_text())
        dur = a.get("t_ms_from_session_start", 0) - a.get("t_start_ms_from_session_start", 0)
        cp = a.get("click_point")
        cps = f" · click ({cp['x']:.0f},{cp['y']:.0f})" if cp else ""
        shots = ""
        for name in ("screenshot.png", "click.png"):
            f = t / name
            if f.exists():
                shots += f'<figure><img src="{b64img(f)}"><figcaption>{name}</figcaption></figure>'
        blocks.append(f"""
    <section class="turn">
      <h3>{esc(t.name)} <span class="tool">{esc(a.get('tool'))}</span>
          <span class="dur">{dur} ms{esc(cps)}</span></h3>
      <pre class="args">{esc(json.dumps(a.get('arguments', {}), indent=2))}</pre>
      <pre class="res">{esc(a.get('result_summary', ''))}</pre>
      <div class="shots">{shots}</div>
    </section>""")

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(meta['title'])} — {run}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
  <h1>{esc(meta['title'])}</h1>
  <p class="sub">cua-driver recording · <code>{run}</code></p>
  <dl class="meta">
    <dt>Layer</dt><dd>{esc(meta['layer'])} <span class="pill">path: {esc(meta['path'])}</span></dd>
    <dt>Constraint</dt><dd>{esc(meta['constraint'])}</dd>
    <dt>Entry</dt><dd><code>{esc(meta['entry'])}</code></dd>
    <dt>Provider</dt><dd>{esc(meta['provider'])}</dd>
    <dt>Result</dt><dd>{esc(meta['result'])}</dd>
    <dt>Turns</dt><dd>{len(turns)}</dd>
    <dt>Cursor samples</dt><dd>{cursor_n}</dd>
  </dl>
  <h2>Timeline ({len(turns)} recorded driver calls)</h2>
  {''.join(blocks)}
  <footer>Generated from <code>state/runs/{run}</code> by make_run_html.py · screenshots embedded base64 (self-contained)</footer>
</div></body></html>"""

    out = rd / "recording.html"
    out.write_text(html, encoding="utf-8")
    return out, len(turns)


if __name__ == "__main__":
    for run, meta in META.items():
        if (RUNS_DIR / run).is_dir():
            out, n = build(run, meta)
            print(f"wrote {out}  ({n} turns, {out.stat().st_size // 1024} KB)")
        else:
            print(f"SKIP {run} (dir missing)")
