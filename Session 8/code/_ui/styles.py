"""CSS palettes and global styling for the dashboard.

Two themes are defined as CSS variables. The active theme is chosen
at render time by injecting either `--theme-light` or `--theme-dark`
overrides into the document. Everything downstream of `:root` reads
those variables, so a theme switch is a single re-render away.

Typography uses Inter (the de-facto SaaS sans) loaded from Google Fonts.
Cards use a 1px border + subtle shadow rather than heavy drop-shadow —
that's the difference between "premium" and "skeuomorphic."
"""

from __future__ import annotations

LIGHT = """
:root {
    --bg: #fafafa;
    --bg-elev: #ffffff;
    --bg-subtle: #f4f4f5;
    --border: rgba(0,0,0,0.06);
    --border-strong: rgba(0,0,0,0.10);
    --text: #18181b;
    --text-muted: #71717a;
    --text-faint: #a1a1aa;
    --accent: #6366f1;
    --accent-soft: rgba(99,102,241,0.10);
    --accent-text: #4338ca;
    --success: #10b981;
    --success-soft: rgba(16,185,129,0.12);
    --warning: #f59e0b;
    --warning-soft: rgba(245,158,11,0.12);
    --error: #ef4444;
    --error-soft: rgba(239,68,68,0.12);
    --shadow-card: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.04);
    --shadow-card-hover: 0 1px 2px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.06);
}
"""

DARK = """
:root {
    --bg: #09090b;
    --bg-elev: #18181b;
    --bg-subtle: #27272a;
    --border: rgba(255,255,255,0.06);
    --border-strong: rgba(255,255,255,0.10);
    --text: #fafafa;
    --text-muted: #a1a1aa;
    --text-faint: #71717a;
    --accent: #818cf8;
    --accent-soft: rgba(129,140,248,0.14);
    --accent-text: #c7d2fe;
    --success: #34d399;
    --success-soft: rgba(52,211,153,0.14);
    --warning: #fbbf24;
    --warning-soft: rgba(251,191,36,0.14);
    --error: #f87171;
    --error-soft: rgba(248,113,113,0.14);
    --shadow-card: 0 1px 2px rgba(0,0,0,0.3), 0 4px 12px rgba(0,0,0,0.2);
    --shadow-card-hover: 0 1px 2px rgba(0,0,0,0.4), 0 8px 24px rgba(0,0,0,0.3);
}
"""

GLOBAL = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [data-testid="stAppViewContainer"], .stApp {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg) !important;
    color: var(--text) !important;
}
* { box-sizing: border-box; }

/* Hide Streamlit chrome (hamburger menu + footer).
   IMPORTANT: do NOT use `visibility: hidden` on the header — the header
   element is what hosts the sidebar collapse/expand toggle. Hiding it
   strands the user with a collapsed sidebar and no way to re-open it
   (which is exactly what happened before this rule was rewritten).
   Instead we make the header transparent and pin its height to zero
   for the visual chrome, while keeping the collapse button visible and
   clickable. */
#MainMenu, footer { visibility: hidden; }
header[data-testid="stHeader"] {
    background: transparent !important;
    height: 0;
}
/* The collapse/expand chevron MUST always be clickable. Streamlit
   renders it under one of two testids depending on whether the sidebar
   is currently open or collapsed — pin both. */
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    visibility: visible !important;
    opacity: 1 !important;
    z-index: 9999 !important;
}

/* main container width + padding */
[data-testid="stAppViewContainer"] > .main {
    padding-top: 2rem;
}
.block-container {
    padding-top: 1rem !important;
    padding-bottom: 3rem !important;
    max-width: 1400px;
}

/* sidebar */
[data-testid="stSidebar"] {
    background: var(--bg-elev) !important;
    border-right: 1px solid var(--border);
}
[data-testid="stSidebar"] > div { padding-top: 1.5rem; }

/* === typography === */
h1, h2, h3, h4, h5, h6 { color: var(--text) !important; font-weight: 700; letter-spacing: -0.01em; }
h1 { font-size: 1.8rem; }
h2 { font-size: 1.3rem; }
h3 { font-size: 1.05rem; }
p, label, span, div { color: var(--text); }
.muted { color: var(--text-muted) !important; }

/* === card === */
.card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 24px;
    box-shadow: var(--shadow-card);
    transition: box-shadow 180ms ease, transform 180ms ease;
}
.card:hover { box-shadow: var(--shadow-card-hover); }
.card-tight { padding: 14px 18px; border-radius: 12px; }

/* === hero header === */
.hero-title {
    font-size: 1.7rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    margin: 0 0 4px 0;
    color: var(--text);
}
.hero-subtitle {
    font-size: 0.95rem;
    color: var(--text-muted);
    margin: 0 0 24px 0;
}

/* === badge === */
.badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.72rem; font-weight: 600;
    letter-spacing: 0.04em; text-transform: uppercase;
    border: 1px solid transparent;
}
.badge-running  { background: var(--warning-soft); color: var(--warning); border-color: var(--warning); }
.badge-complete { background: var(--success-soft); color: var(--success); border-color: var(--success); }
.badge-failed   { background: var(--error-soft);   color: var(--error);   border-color: var(--error); }
.badge-pending  { background: var(--bg-subtle);    color: var(--text-muted); border-color: var(--border-strong); }
.badge-skipped  { background: var(--bg-subtle);    color: var(--text-muted); border-color: var(--border-strong); }
.badge-idle     { background: var(--bg-subtle);    color: var(--text-muted); border-color: var(--border-strong); }

.badge-dot {
    width: 6px; height: 6px; border-radius: 50%;
    display: inline-block;
}
.badge-running .badge-dot  { background: var(--warning); animation: pulse 1.4s ease-in-out infinite; }
.badge-complete .badge-dot { background: var(--success); }
.badge-failed .badge-dot   { background: var(--error); }
.badge-pending .badge-dot, .badge-idle .badge-dot, .badge-skipped .badge-dot { background: var(--text-faint); }

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.5; transform: scale(1.4); }
}

/* === metric card === */
.metric-card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    box-shadow: var(--shadow-card);
}
.metric-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    font-weight: 600;
    margin-bottom: 6px;
}
.metric-value {
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
}
.metric-delta {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 4px;
    font-weight: 500;
}
.metric-delta.positive { color: var(--success); }

/* === buttons === */
.stButton > button {
    border-radius: 10px !important;
    border: 1px solid var(--border-strong) !important;
    background: var(--bg-elev) !important;
    color: var(--text) !important;
    font-weight: 600 !important;
    font-size: 0.9rem !important;
    padding: 8px 18px !important;
    transition: all 140ms ease !important;
}
.stButton > button:hover {
    background: var(--bg-subtle) !important;
    transform: translateY(-1px);
}
.stButton > button[kind="primary"] {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    color: white !important;
    box-shadow: 0 1px 2px rgba(99,102,241,0.2), 0 4px 12px rgba(99,102,241,0.18) !important;
}
.stButton > button[kind="primary"]:hover {
    filter: brightness(1.08);
}
.stButton > button:disabled { opacity: 0.45; cursor: not-allowed; }

/* === text inputs === */
.stTextArea textarea, .stTextInput input {
    background: var(--bg-elev) !important;
    border: 1px solid var(--border-strong) !important;
    border-radius: 10px !important;
    color: var(--text) !important;
    font-size: 0.95rem !important;
    font-family: 'Inter', sans-serif !important;
}
.stTextArea textarea:focus, .stTextInput input:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-soft) !important;
}

/* === selectbox / slider / radio === */
.stSelectbox > div > div, .stMultiSelect > div > div {
    background: var(--bg-elev) !important;
    border: 1px solid var(--border-strong) !important;
    border-radius: 10px !important;
}
.stSlider [data-baseweb="slider"] > div > div { background: var(--accent) !important; }

/* === tabs === */
.stTabs [data-baseweb="tab-list"] {
    background: transparent !important;
    border-bottom: 1px solid var(--border) !important;
    gap: 4px;
}
.stTabs [data-baseweb="tab"] {
    background: transparent !important;
    border: none !important;
    color: var(--text-muted) !important;
    font-weight: 600 !important;
    font-size: 0.9rem !important;
    padding: 10px 16px !important;
    border-radius: 8px 8px 0 0 !important;
    transition: color 140ms ease, background 140ms ease;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--text) !important; background: var(--bg-subtle) !important; }
.stTabs [aria-selected="true"] {
    color: var(--accent-text) !important;
    background: var(--accent-soft) !important;
    border-bottom: 2px solid var(--accent) !important;
}

/* === expander === */
.streamlit-expanderHeader, [data-testid="stExpander"] summary {
    background: var(--bg-elev) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    color: var(--text) !important;
}
[data-testid="stExpander"] { border: none !important; }

/* === dataframe === */
[data-testid="stDataFrame"] {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: var(--shadow-card);
}

/* === code blocks === */
[data-testid="stCodeBlock"] pre {
    background: var(--bg-subtle) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    font-family: 'JetBrains Mono', ui-monospace, monospace !important;
    font-size: 0.85rem !important;
}

/* === progress === */
.stProgress > div > div > div > div { background: var(--accent) !important; }
.stProgress > div > div > div { background: var(--bg-subtle) !important; }

/* === alerts === */
[data-testid="stAlert"] {
    border-radius: 12px !important;
    border: 1px solid var(--border-strong) !important;
}

/* === scrollbar polish === */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }

/* === step item (Reasoning tab) === */
.step-row {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 14px 18px;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 8px;
}
.step-num {
    min-width: 28px; height: 28px;
    background: var(--accent-soft); color: var(--accent-text);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.85rem;
}
.step-body { flex: 1; }
.step-skill {
    font-weight: 700; font-size: 0.95rem; color: var(--text);
    font-family: 'JetBrains Mono', monospace; font-size: 0.88rem;
}
.step-meta {
    font-size: 0.78rem; color: var(--text-muted); margin-top: 2px;
}
.step-rationale {
    margin-top: 8px; font-size: 0.88rem; color: var(--text);
    background: var(--bg-subtle); padding: 8px 12px; border-radius: 8px;
}

/* === chip === */
.chip {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent-text);
    font-size: 0.72rem; letter-spacing: 0.04em; margin-right: 6px;
    font-weight: 600;
}

/* === streaming-stability layer ===
   These rules exist purely to prevent layout shift, flicker, and scroll
   jumps when fragments re-render on the 1s tick. Every selector here is
   defensive; removing one will reintroduce a specific bug. */

/* Reserve scrollbar width — without this, the page-width snaps in/out
   when content grows past the viewport, which reads as a horizontal flicker
   across the entire dashboard on every tick. */
html { scrollbar-gutter: stable; }

/* Disable scroll-anchoring globally; we re-enable it ONLY on the panes
   that should sticky-scroll-to-bottom (output, log). Default scroll
   anchoring tries to "preserve view" and ends up snapping the page
   upward whenever the log tab gains a line. */
html, body { overflow-anchor: none; }

/* Kill the hover translate on buttons — it caused the run/stop pair to
   jitter vertically each time the fragment re-rendered while the user's
   pointer happened to be over them. Visual hover state stays via the
   background change. */
.stButton > button:hover { transform: none !important; }

/* Tab-panel: no transition. Streamlit's default fades tabpanels in/out;
   when the fragment re-renders, this fade restarts and you see a flash. */
.stTabs [role="tabpanel"] {
    animation: none !important;
    transition: none !important;
}

/* Cards: drop the hover transition. Same flash story as tabs above —
   each re-render briefly fires the transition. */
.card { transition: none !important; }
.card:hover { box-shadow: var(--shadow-card); }  /* freeze hover state */

/* Reasoning-step rows: contain layout so growing the list doesn't reflow
   sibling tabs (each row becomes its own paint context). */
.step-row { contain: layout style; }

/* === output-pane: where the final answer renders ===
   * `contain` isolates its reflow from the rest of the page.
   * `overflow-anchor: auto` on the wrapper PLUS `none` on children except
     the last keeps the scrollbar pinned to the most recent line when new
     text appends — the canonical "chat-like auto-scroll" behaviour with
     zero JS. */
.output-pane {
    contain: layout style;
    min-height: 220px;
    max-height: 70vh;
    overflow-y: auto;
    overflow-anchor: auto;
    padding-right: 6px;  /* breathing room from the scrollbar */
    scroll-behavior: auto;  /* not smooth — smooth scroll fights auto-anchor */
}
.output-pane * { overflow-anchor: none; }
.output-pane > *:last-child { overflow-anchor: auto; }

/* === log-pane: same idea but tighter and monospaced === */
.log-pane {
    contain: layout style;
    max-height: 460px;
    overflow-y: auto;
    overflow-anchor: auto;
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.82rem;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
}
.log-pane * { overflow-anchor: none; }
.log-pane > *:last-child { overflow-anchor: auto; }

/* Streamlit code blocks (used as a fallback in some tabs): clamp height so
   the page doesn't grow with every line and reflow the world. */
[data-testid="stCodeBlock"] pre {
    max-height: 520px;
    overflow-y: auto;
    overflow-anchor: none;
    contain: layout style;
}

/* === safety net for the few widget surfaces our CSS doesn't reach ===
   Framework theme is now set in .streamlit/config.toml which gives
   native widgets correct colors at the BaseWeb level. The rules below
   are only the small handful we still need because they touch elements
   whose color is decided by inline syntax-highlighter spans or
   portaled-out popovers that escape config.toml's reach. */

/* st.code tokens are colored by inline spans (Streamlit's syntax
   highlighter sets per-token color directly). Force readable
   foreground so prompt-sent / raw-output blocks read in both themes. */
[data-testid="stCodeBlock"] pre,
[data-testid="stCodeBlock"] pre *,
[data-testid="stCode"] pre,
[data-testid="stCode"] pre * {
    color: var(--text) !important;
    -webkit-text-fill-color: var(--text) !important;
}

/* Dropdown popover is portaled outside the .stSelectbox subtree, so
   theme inheritance can drop. Pin it explicitly. */
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="popover"] [role="option"] {
    color: var(--text) !important;
    background-color: var(--bg-elev) !important;
}
[data-baseweb="popover"] [role="option"]:hover {
    background-color: var(--bg-subtle) !important;
}
"""


def inject(theme: str) -> str:
    """Return one block of CSS with the chosen theme's variables applied first.

    Streamlit's `st.markdown(..., unsafe_allow_html=True)` is the carrier;
    this function just composes the string. Theme is `"light"` or `"dark"`.
    """
    palette = DARK if theme == "dark" else LIGHT
    return f"<style>{palette}{GLOBAL}</style>"
