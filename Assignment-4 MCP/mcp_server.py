"""
mcp_server.py — Research & Study MCP Server

Tools:
  - search_internet      Tavily web search (generic web research)
  - fetch_arxiv          arXiv API (specialized for academic papers)
  - manage_local_file    Local filesystem CRUD
  - generate_custom_ui   Dynamic Prefab UI generation

Slash prompts:
  - /project_insight      EQUINOX equity research — top-10 stock screen for a sector
  - /weekly_study_plan    Personalized weekly study plan for a data scientist
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastmcp import FastMCP
from prefab_ui.app import PrefabApp
from prefab_ui.components import Column, Heading, Markdown, Card, Container

# Load environment variables explicitly from the script's directory
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(env_path)

# Initialize the MCP Server
mcp = FastMCP("Research & Study Server")


# ─────────────────────────────────────────────────────────────────────────────
# arXiv rate-limit guard
#
# arXiv's API allows ~1 request / 3s and returns HTTP 429 (with a multi-minute
# cooldown) on bursts. fetch_arxiv is called repeatedly — directly and from
# inside /project_insight — so without spacing + caching it gets throttled fast.
# This in-process cache + cross-call throttle keeps normal usage under the
# limit without violating arXiv's politeness policy.
# ─────────────────────────────────────────────────────────────────────────────

_ARXIV_CACHE: dict[str, tuple[float, str]] = {}
_ARXIV_CACHE_TTL = 30 * 60        # serve a cached result for 30 minutes
_ARXIV_MIN_SPACING = 3.0          # min seconds between real arXiv requests
_ARXIV_LOCK = threading.Lock()    # serialize calls + protect shared state
_arxiv_last_request = 0.0         # epoch of the last network request


def _arxiv_cache_key(search_query: str, days: int, max_results: int,
                     sort_by: str) -> str:
    return f"{search_query}||{days}||{max_results}||{sort_by}"


def _arxiv_cache_get(key: str, allow_stale: bool = False):
    """Return (value, age_seconds) if present (and fresh, unless allow_stale)."""
    entry = _ARXIV_CACHE.get(key)
    if not entry:
        return None
    stored_at, value = entry
    age = time.time() - stored_at
    if allow_stale or age <= _ARXIV_CACHE_TTL:
        return value, age
    return None


def _arxiv_cache_put(key: str, value: str) -> None:
    _ARXIV_CACHE[key] = (time.time(), value)


def _arxiv_throttle() -> None:
    """Block until >= _ARXIV_MIN_SPACING seconds since the last request."""
    global _arxiv_last_request
    wait = _ARXIV_MIN_SPACING - (time.time() - _arxiv_last_request)
    if wait > 0:
        time.sleep(wait)
    _arxiv_last_request = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — Generic web search (Tavily)
# Use for: blogs, essays, product launches, news, company announcements
# Do NOT use for: arXiv papers (use fetch_arxiv instead)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_internet(query: str) -> str:
    """
    Search the web using Tavily. Use for blogs, essays, product launches,
    news, and general web content. For arXiv papers, use fetch_arxiv instead.

    Args:
        query: The search query.

    Returns:
        Formatted string of search results with title, URL, and content excerpt.
    """
    from tavily import TavilyClient

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: TAVILY_API_KEY environment variable is not set."

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(query=query, search_depth="basic")
        results = response.get("results", [])

        if not results:
            return f"No results found for: {query}"

        output = [f"Search Results for '{query}':\n"]
        for i, res in enumerate(results, 1):
            output.append(f"{i}. {res.get('title')}")
            output.append(f"   URL: {res.get('url')}")
            output.append(f"   Content: {res.get('content')}\n")
        return "\n".join(output)

    except Exception as e:
        return f"Error performing search: {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — arXiv API (specialized academic paper search)
# Use for: any arXiv paper lookup, recent ML/CS research, paper metadata
# Better than Tavily for arXiv: real API, exact date filter, full abstracts,
# author lists, category filters, no Tavily quota burn.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def fetch_arxiv(
    query: str,
    days: int = 7,
    max_results: int = 10,
    categories: Optional[str] = None,
    sort_by: str = "submittedDate",
) -> str:
    """
    Fetch recent papers from arXiv matching a query.

    Use this for ALL arXiv lookups. It hits the official arXiv API directly
    and returns clean structured metadata — much better than scraping or
    web-searching for arXiv content.

    Args:
        query: Free-text search query (e.g., "retrieval augmented generation",
            "mixture of experts routing").
        days: Only return papers submitted in the last N days. Default 7.
            Min 1, max 90.
        max_results: Maximum papers to return. Default 10, max 50.
        categories: Optional comma-separated arXiv category codes
            (e.g., "cs.LG,cs.CL,cs.AI,stat.ML"). If None, no category filter.
            Common codes:
              cs.LG   — Machine Learning
              cs.CL   — Computation and Language (NLP)
              cs.AI   — Artificial Intelligence
              cs.CV   — Computer Vision
              cs.IR   — Information Retrieval
              stat.ML — Statistical ML
        sort_by: Either "submittedDate" (newest first) or "relevance".
            Default "submittedDate".

    Returns:
        Formatted string with title, authors, abstract, categories, arXiv ID,
        submission date, and direct PDF/abstract URLs for each paper.
    """
    try:
        import arxiv
    except ImportError:
        return (
            "Error: arxiv package not installed. "
            "Add 'arxiv>=2.1' to pyproject.toml and run `uv sync` "
            "(or `pip install arxiv`)."
        )

    # Cap inputs to safe ranges
    max_results = max(1, min(max_results, 50))
    days = max(1, min(days, 90))

    # Build the search query — add category filter if provided
    search_query = query
    if categories:
        cat_list = [c.strip() for c in categories.split(",") if c.strip()]
        if cat_list:
            cat_filter = " OR ".join(f"cat:{c}" for c in cat_list)
            search_query = f"({query}) AND ({cat_filter})"

    sort_criterion = (
        arxiv.SortCriterion.SubmittedDate
        if sort_by == "submittedDate"
        else arxiv.SortCriterion.Relevance
    )

    cache_key = _arxiv_cache_key(search_query, days, max_results, sort_by)

    # Fresh cache hit — return immediately, zero arXiv requests. This is what
    # stops repeated/identical queries (and /project_insight retries) from
    # re-triggering the rate limit.
    fresh = _arxiv_cache_get(cache_key)
    if fresh is not None:
        value, age = fresh
        return f"{value}\n(cached {int(age)}s ago — arXiv not re-queried)"

    try:
        # Serialize network calls so concurrent tool invocations can't burst
        # arXiv; re-check the cache inside the lock in case another call just
        # populated it while we were waiting.
        with _ARXIV_LOCK:
            fresh = _arxiv_cache_get(cache_key)
            if fresh is not None:
                value, age = fresh
                return (
                    f"{value}\n(cached {int(age)}s ago — arXiv not re-queried)"
                )

            # Keep >= _ARXIV_MIN_SPACING between real requests, across calls.
            _arxiv_throttle()

            # Over-fetch a bit because we filter by date after the API call.
            # Higher delay_seconds/num_retries let a transient 429 self-recover
            # inside the library instead of bubbling straight up.
            client = arxiv.Client(
                page_size=max_results * 2,
                delay_seconds=5,
                num_retries=5,
            )
            search = arxiv.Search(
                query=search_query,
                max_results=max_results * 2,
                sort_by=sort_criterion,
                sort_order=arxiv.SortOrder.Descending,
            )

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            results = []

            for paper in client.results(search):
                if paper.published < cutoff:
                    # Sorted by submitted date desc: once older than cutoff,
                    # the rest are older too.
                    if sort_by == "submittedDate":
                        break
                    else:
                        continue
                results.append(paper)
                if len(results) >= max_results:
                    break

        if not results:
            return (
                f"No arXiv papers found for '{query}' in the last {days} "
                f"days (categories: {categories or 'any'}). "
                f"Try widening days, removing category filter, or rephrasing "
                f"the query."
            )

        # Format the response — keep token budget in mind, abstracts are long
        out = [
            f"arXiv results for '{query}' "
            f"(last {days} days, {len(results)} paper"
            f"{'s' if len(results) != 1 else ''}):\n"
        ]
        for i, p in enumerate(results, 1):
            authors = ", ".join(a.name for a in p.authors[:3])
            if len(p.authors) > 3:
                authors += f", +{len(p.authors) - 3} more"

            # Truncate abstract to ~600 chars to keep responses lean
            abstract = " ".join(p.summary.split())  # collapse whitespace
            if len(abstract) > 600:
                abstract = abstract[:600].rsplit(" ", 1)[0] + "..."

            cats = ", ".join(p.categories[:4])
            arxiv_id = p.get_short_id()
            submitted = p.published.strftime("%Y-%m-%d")

            out.append(f"{i}. {p.title.strip()}")
            out.append(f"   arXiv ID: {arxiv_id}   Submitted: {submitted}")
            out.append(f"   Authors: {authors}")
            out.append(f"   Categories: {cats}")
            out.append(f"   Abstract: {abstract}")
            out.append(f"   Abs: https://arxiv.org/abs/{arxiv_id}")
            out.append(f"   PDF: {p.pdf_url}")
            out.append("")

        result_str = "\n".join(out)
        _arxiv_cache_put(cache_key, result_str)
        return result_str

    except Exception as e:
        msg = str(e)
        rate_limited = "429" in msg or "too many requests" in msg.lower()
        if rate_limited:
            # Serve a stale cache entry if we have one — better than nothing
            # while arXiv is in cooldown.
            stale = _arxiv_cache_get(cache_key, allow_stale=True)
            if stale is not None:
                value, age = stale
                mins, secs = int(age // 60), int(age % 60)
                return (
                    f"{value}\n(arXiv is rate-limiting right now — showing a "
                    f"cached result from {mins}m {secs}s ago)"
                )
            return (
                "arXiv is rate-limiting this IP (HTTP 429). This is temporary "
                "and not a config or code problem. Wait ~10-15 minutes, then "
                "make a SINGLE fetch_arxiv call — repeated retries extend the "
                "cooldown. search_internet and manage_local_file are "
                "unaffected in the meantime."
            )
        return f"Error fetching arXiv: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — Local filesystem CRUD
# Use for: persisting reports, audit logs, study plans, anything on disk
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def manage_local_file(action: str, filepath: str, content: str = "") -> str:
    """
    Perform CRUD operations on a local file.

    Args:
        action: One of 'read', 'create', 'update' (appends), or 'delete'.
        filepath: Path to the file.
        content: Content to write/append (only used for 'create' and 'update').

    Returns:
        A string indicating the result of the operation.
    """
    try:
        if action == "read":
            if not os.path.exists(filepath):
                return f"Error: File '{filepath}' does not exist."
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()

        elif action == "create":
            if os.path.exists(filepath):
                return (
                    f"Error: File '{filepath}' already exists. "
                    f"Use 'update' to append."
                )
            # Ensure parent directory exists
            parent = os.path.dirname(filepath)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully created file '{filepath}'."

        elif action == "update":
            if not os.path.exists(filepath):
                return (
                    f"Error: File '{filepath}' does not exist. "
                    f"Use 'create' first."
                )
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n{content}")
            return f"Successfully updated file '{filepath}'."

        elif action == "delete":
            if not os.path.exists(filepath):
                return f"Error: File '{filepath}' does not exist."
            os.remove(filepath)
            return f"Successfully deleted file '{filepath}'."

        else:
            return (
                f"Error: Invalid action '{action}'. "
                f"Use 'read', 'create', 'update', or 'delete'."
            )

    except Exception as e:
        return f"Error performing {action} on {filepath}: {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4 — Dynamic Prefab UI generation
# Use for: rendering dashboards, reports, study plans as interactive UI
# The agent emits Python code; this tool executes it to produce a PrefabApp.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool(app=True)
def generate_custom_ui(python_code: str) -> PrefabApp:
    """
    Execute the provided python_code to generate a dynamic Prefab UI on the fly.

    The code MUST define and assign a valid PrefabApp object to a variable
    named `app`.

    prefab-ui 0.19.1 API rule (violating it raises "takes 1 positional
    argument but 2 were given"):
      - TEXT components take the string positionally: Heading, Text, Markdown,
        Code, Badge, Button, CardTitle, AlertTitle, AlertDescription,
        TableHead, TableCell — plus Tab/AccordionItem (title positional).
      - LAYOUT/container components take NO positional arg — open them as a
        `with` block and nest children: Container, Column, Row, Grid,
        GridItem, Separator, Card, CardHeader, CardContent, Alert, Tabs,
        Accordion, Table, TableHeader, TableBody, TableRow, Carousel, Metric.
        `Card("Title")` / `Column(child)` / `Metric("x", 1)` are INVALID.

    Example python_code:
    ```python
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import Column, Card, CardHeader, CardTitle, \
        CardContent, Heading, Text

    with Column() as view:
        Heading("Dynamic Title")
        with Card():
            with CardHeader():
                CardTitle("Section")
            with CardContent():
                Text("Dynamic content generated on the fly!")
    app = PrefabApp(view=view)
    ```

    Args:
        python_code: Python code that constructs the PrefabApp.

    Returns:
        The resulting PrefabApp, or an error-display PrefabApp if execution failed.
    """
    local_env = {}
    try:
        exec(python_code, globals(), local_env)

        if "app" in local_env and isinstance(local_env["app"], PrefabApp):
            return local_env["app"]

        # Fallback if `app` variable is missing or wrong type
        from prefab_ui.components import Container, Heading, Text

        with Container() as view:
            Heading(
                "UI Generation Error",
                size="xl",
                css_class="text-red-600 mb-4",
            )
            Text(
                "The code executed, but failed to assign a valid PrefabApp "
                "to the variable 'app'."
            )
        return PrefabApp(view=view)

    except Exception as e:
        from prefab_ui.components import Container, Heading, Code, Markdown

        hint = ""
        if "positional argument" in str(e):
            hint = (
                "Likely cause: a layout/container component (Card, Column, "
                "Row, Container, Grid, Tabs, Table, Metric, …) was called "
                "with a positional argument. Those take NO positional args — "
                "open them as a `with` block and nest children. Only text "
                "components (Heading, Text, Markdown, Code, Badge, Button, "
                "CardTitle, …) take a positional string. Regenerate the code "
                "accordingly."
            )

        with Container() as view:
            Heading(
                "Error Executing UI Code",
                size="xl",
                css_class="text-red-600 mb-4",
            )
            Code(str(e))
            if hint:
                Markdown(hint)
        return PrefabApp(view=view)


# ─────────────────────────────────────────────────────────────────────────────
# SLASH PROMPT 1 — /project_insight  (display name: Project Insight)
# EQUINOX v3.1 — autonomous equity-research agent. Ranks the top 10 stocks
# in a user-specified sector against a 22-metric fundamental + technical
# framework, persists an audit trail, and renders a Prefab dashboard.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.prompt()
def project_insight(topic: str) -> str:
    """
    Project Insight — EQUINOX autonomous equity-research agent. Ranks the
    top 10 stocks in a user-specified sector against a 22-metric
    fundamental + quarterly-momentum + SMA-pullback framework.
    """
    system_prompt = """SYSTEM PROMPT: Autonomous Equity Research Agent (v3.1)

You are EQUINOX, an elite autonomous AI Equity Research Agent operating through an MCP Server. You combine the discipline of a buy-side analyst with the rigor of a forensic accountant and the patience of a trend-follower. You think in evidence, cite, cross-verify, and never confuse a story with a fact.

═══════════════════════════════════════════════════════════════════════
🆕 WHAT CHANGED IN v3.1
═══════════════════════════════════════════════════════════════════════
Technical Entry Signal (section H below) has been refined:
• Uptrend stack wording made explicit and unambiguous: SMA 20 > SMA 50 > SMA 200 (SMA 20 has the highest value, SMA 200 the lowest)
• Condition 22 changed from "Price < SMA 20" to "Current Price within ±6% of SMA 20" i.e. 0.94 × SMA20 ≤ Price ≤ 1.06 × SMA20
Rationale: a stock 4% above its SMA20 is just as valid a trend-following entry as one 4% below it. The strict "below" rule excluded healthy slight extensions; the ±6% band captures both pullbacks AND controlled extensions, while still rejecting runaway momentum (price >6% above SMA20 = chasing).
All other sections (Phases 1–4, the other 21 metrics, the audit trail, the dashboard requirements) are unchanged from v3.0.

═══════════════════════════════════════════════════════════════════════
🛠️ AVAILABLE TOOLS
═══════════════════════════════════════════════════════════════════════
| Tool | Purpose |
| search_internet    | Multi-hop search across financial sources — screener.in, moneycontrol, NSE/BSE filings, tickertape, trendlyne, AR PDFs, broker notes. |
| manage_local_file  | CRUD ops on local filesystem for audit trail. |
| generate_custom_ui | Generate interactive dashboard via Python + prefab-ui. |

Optional query prefixes:
[FILING] <co> <yr> → BSE/NSE filing portal
[PEER] <co> → peer comparison set
[TECH] <ticker> → SMA 200, SMA 50, SMA 20, RSI, support/resistance
[QUARTERLY] <co> → last 4 quarters of P&L

═══════════════════════════════════════════════════════════════════════
🎯 MISSION
═══════════════════════════════════════════════════════════════════════
Conduct a rigorous, multi-layered fundamental + technical assessment to shortlist the TOP 10 STOCKS from a user-specified sector or universe. Evaluate valuation, profitability, financial health, growth, cash-flow integrity, recent quarterly momentum, shareholding hygiene, and a precise technical entry signal. Deliver a defensible, dashboard-ready verdict.

═══════════════════════════════════════════════════════════════════════
⏱️ EFFORT BUDGET — 25 tool calls total
═══════════════════════════════════════════════════════════════════════
Phase 1 (industry + universe): ≤ 6 calls
Phase 2 (22-metric filter): ≤ 10 calls (batch where possible)
Phase 3 (file persistence): ≤ 5 calls
Phase 4 (dashboard render): 1–2 calls
If 20 calls are spent without a complete dashboard, summarize what you have and exit. Partial truth > fabricated completeness.

═══════════════════════════════════════════════════════════════════════
🔒 VERIFY BEFORE YOU WRITE
═══════════════════════════════════════════════════════════════════════
Every material number cross-verified across ≥ 2 independent sources. Disagreements > 5% → flag in the report, never silently pick one.
After every manage_local_file create or update, immediately read the file back and confirm content matches intent.
Never fabricate. Hallucinated tickers, P/E ratios, SMA values, or promoter holdings = critical failure. When in doubt, mark N/A.
If a source URL cannot be cited, do not include the data point. Write "Not verified — source unavailable" instead.

═══════════════════════════════════════════════════════════════════════
📋 WORKFLOW — EXECUTE EXACTLY IN ORDER
═══════════════════════════════════════════════════════════════════════

────────────────────────────────────────────────
PHASE 1 — INDUSTRY & UNIVERSE INTELLIGENCE
────────────────────────────────────────────────
1.1 SECTOR MAP — Tailwinds, headwinds, regulatory shifts, raw-material cycle, competitive structure, entry barriers, 3-yr demand outlook.
1.2 UNIVERSE SHORTLIST — Top 15–20 listed companies by mcap/revenue. Narrow to 10 based on market position, brand, distribution.
1.3 ANNUAL REPORT SCAN (per finalist) — MD&A themes, capex plans, segment mix, contingent liabilities, related-party transactions, auditor remarks. Flag any narrative inconsistencies.

────────────────────────────────────────────────
PHASE 2 — 22-METRIC FILTER (PASS / WARN / FAIL per metric)
────────────────────────────────────────────────
A. VALUATION & PROFITABILITY (5)
1. P/E vs peer median → at or below peer median
2. ROE → > 15%
3. ROCE → > 15%
4. Operating Profit Margin (OPM) → stable or expanding
5. CFO / Net Profit ratio → close to 1.0

B. FINANCIAL HEALTH (3)
6. Debt / Equity → < 0.6 (ideally < 0.3)
7. Reserves trend → rising YoY
8. Borrowings trend → flat or falling

C. LONG-TERM GROWTH (2)
9. Sales CAGR (3 / 5 / 10 yr) → consistent double-digit
10. Profit CAGR (3 / 5 / 10 yr) → consistent double-digit

D. LATEST QUARTERLY RESULTS BATTERY (4)
── All four sequential, from the most recent reported quarter, compared to the immediately preceding quarter ──
11. Sales QoQ → positive
12. Operating Profit QoQ → positive
13. Net Profit QoQ → positive
14. EPS QoQ → positive
Battery scoring:
• 4/4 positive → PASS (strong sequential momentum)
• 3/4 positive → WARN (one-metric slip, investigate)
• ≤ 2 positive → FAIL (broken sequential trend)

E. CASH FLOW INTEGRITY (1) — NON-NEGOTIABLE
15. Operating Cash Flow → POSITIVE EVERY YEAR

F. WORKING CAPITAL & COST (2)
16. Receivable days → < 90 (> 180 = critical red flag)
17. Employee + RM cost % of sales → controlled, peer-aligned

G. SHAREHOLDING HYGIENE (2)
18. Promoter holding → ≥ 50%, stable, no selling
19. Promoter pledge % → near zero — ANY pledge = red flag

H. TECHNICAL ENTRY SIGNAL (3 conditions, ALL must hold)
┌──────────────────────────────────────────────────────────────┐
│ The "uptrend stack" — fixed ordering by value                  │
│                                                                │
│            SMA 20 > SMA 50 > SMA 200                            │
│                                                                │
│ • SMA 20 has the HIGHEST value                                 │
│ • SMA 200 has the LOWEST value                                 │
│ • SMA 50 sits between them                                     │
│                                                                │
│ Short-term average above mid-term average, above long-term     │
│ average. The trend is up across all three timeframes.          │
└──────────────────────────────────────────────────────────────┘
20. SMA 20 > SMA 50 → short-term above mid-term
21. SMA 50 > SMA 200 → mid-term above long-term
22. |Price − SMA 20| / SMA 20 ≤ 6% → price within ±6% of SMA 20 i.e. 0.94 × SMA20 ≤ Price ≤ 1.06 × SMA20
Rationale for condition 22:
- Below SMA20 by 0–6%: healthy pullback inside an uptrend.
- Above SMA20 by 0–6%: controlled extension, trend confirmed.
- More than 6% above SMA20: chasing; wait for cool-off.
- More than 6% below SMA20: trend may be breaking; investigate.
Technical verdict (binary, not graded):
• All 3 conditions met → BUY-ZONE (technical PASS)
• Any condition broken → NOT-YET (technical FAIL)
Interpretation cheat-sheet:
- SMA 20 < SMA 50 → short-term momentum weakening
- SMA 50 < SMA 200 → mid-term in downtrend
- Price > SMA 20 by >6% → extended; wait for pullback
- Price < SMA 20 by >6% → potential breakdown
- Uptrend stack holds AND price inside ±6% band → BUY-ZONE (the entry this framework prioritises)

Ranking: total PASS count across all 22 metrics. Tiebreaker order: (1) Technical BUY-ZONE, (2) ROCE, (3) CFO/NP ratio.

────────────────────────────────────────────────
PHASE 3 — SYSTEM OF RECORD (file CRUD)
────────────────────────────────────────────────
3.1 manage_local_file CREATE equity_audit.log "[<ISO ts>] Audit initiated for <SECTOR> universe. 22-metric framework v3.1 engaged."
3.2 manage_local_file CREATE stock_scorecard.md — sections:
• Executive summary (top 3 picks + 2-line rationale each)
• Industry thesis
• Company-by-company 22-metric scorecard
• Quarterly battery breakdown (Sales / OP / NP / EPS QoQ)
• Technical formation table (Price, SMA20, SMA50, SMA200, Δ% from SMA20, stack-verdict, ±6%-band-verdict)
• Red-flag annexure (FAIL on CFO, pledge, receivables)
• Source URL footnotes for every material data point
3.3 manage_local_file READ stock_scorecard.md → integrity check.
3.4 manage_local_file APPEND equity_audit.log "[<ISO ts>] Scorecard verified. Quarterly + SMA checks done. 10 candidates ranked. Dashboard render pending."

────────────────────────────────────────────────
PHASE 4 — DYNAMIC DASHBOARD
────────────────────────────────────────────────
generate_custom_ui → Python script using prefab_ui.components. Assign the final PrefabApp to a variable named app.
Valid components ONLY (Divider does NOT exist — use Separator): Container, Column, Row, Grid, GridItem, Separator, Card, CardHeader, CardTitle, CardContent, Heading, Text, Markdown, Badge, Alert, AlertTitle, AlertDescription, Table, TableHeader, TableBody, TableRow, TableHead, TableCell, Tabs, Tab, Accordion, AccordionItem, Carousel, Code.

**Prefab API contract (prefab-ui 0.19.1) — break this and the dashboard fails with "takes 1 positional argument but 2 were given":**
- TEXT components take the string positionally: `Heading("..")`, `Text("..")`, `Markdown("..")`, `Code("..")`, `Badge("..")`, `CardTitle("..")`, `AlertTitle("..")`, `AlertDescription("..")`, `TableHead("..")`, `TableCell("..")`; `Tab("name")` and `AccordionItem("title")` take their title positionally.
- LAYOUT/container components take NO positional arg — open as a `with` block and nest children: `Container, Column, Row, Grid, GridItem, Separator, Card, CardHeader, CardContent, Alert, Tabs, Accordion, Table, TableHeader, TableBody, TableRow, Carousel`. NEVER `Card("Title")` or `Column(child)`.
- Correct card: `with Card():` → `with CardHeader(): CardTitle("Title")` → `with CardContent(): Badge("PASS"); Markdown("detail")`.

MANDATORY UI elements:
✓ Red Alert for any non-negotiable failure (-ve CFO, pledge > 0, receivable days > 180, Quarterly battery 0/4 or 1/4)
✓ Color-coded Badges per metric: green = PASS, amber = WARN, red = FAIL
✓ Master ranked Table of the final 10
✓ Quarterly battery panel: 4 columns (Sales, OP, NP, EPS), each cell shows QoQ % with up/down arrow
✓ Technical formation panel: visualize SMA-stack — a horizontal track per stock showing SMA 200, SMA 50, SMA 20, and Price as distinct LABELED markers. Shade the ±6% band around SMA 20. Colored green if BUY-ZONE formation holds (uptrend stack AND price within ±6% band).
✓ Code block embedding last line of equity_audit.log
Suggested layout pieces (pick what fits):
Tabs → one tab per company for deep-dive
Table → master 10-stock comparison
Accordion → expandable 22-metric breakdown per company
Grid+Cards → visual tiles for top 3 picks
Carousel → "BUY-ZONE watchlist" — only the stocks where uptrend stack holds AND price is inside ±6% band

═══════════════════════════════════════════════════════════════════════
⚙️ EXECUTION RULES
═══════════════════════════════════════════════════════════════════════
Narrate reasoning briefly (1–2 sentences) between tool calls.
Handle search failures gracefully: re-query with synonyms, try BSE/NSE/SEBI portals, IR pages before giving up.
Cross-verify every material number across ≥ 2 sources.
Cite source URLs in stock_scorecard.md for every data point.
Always disclose: "Not financial advice. Data is point-in-time and may be revised. Verify independently before investing."
Surprise the user with a dashboard that is genuinely useful — not template-driven.

═══════════════════════════════════════════════════════════════════════
📥 REQUIRED USER INPUTS
═══════════════════════════════════════════════════════════════════════
Before starting, confirm:
1. Sector / industry / stock universe
2. Investment horizon (short < 1 yr / medium 1–3 yr / long 3+ yr)
3. (Optional) Market-cap preference (large / mid / small / multi)
If 1 or 2 is missing, ask before searching.

═══════════════════════════════════════════════════════════════════════
🎯 GOAL (one sentence)
═══════════════════════════════════════════════════════════════════════
Deliver a verifiable, dashboard-ready ranking of the top 10 stocks in a user-specified sector, scored against a 22-metric fundamental + quarterly-momentum + SMA-pullback framework (uptrend stack SMA 20 > SMA 50 > SMA 200, price within ±6% of SMA 20), with every material claim sourced and a full audit trail persisted to disk.
"""
    return (
        system_prompt
        + "\n═══════════════════════════════════════════════════════════════════════\n"
        + "📌 SESSION INPUT (provided via the /project_insight slash command)\n"
        + "═══════════════════════════════════════════════════════════════════════\n"
        + "Sector / industry / stock universe: "
        + topic
        + "\n\nTreat the line above as REQUIRED USER INPUT #1 — do not ask for it "
        + "again. Still confirm REQUIRED USER INPUT #2 (investment horizon) and "
        + "the optional market-cap preference with the user before you begin "
        + "Phase 1 searching, unless they are already specified above.\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SLASH PROMPT 2 — /weekly_study_plan
# Personalized weekly study plan for a data scientist tracking concepts,
# new research (arXiv), and product launches.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.prompt()
def weekly_study_plan() -> str:
    """
    Generate a personalized weekly study plan for a data scientist focused on
    AI concepts, new research, and product launches. Uses fetch_arxiv for
    papers and search_internet for everything else.
    """
    return """You are CURATOR, an autonomous AI research and learning assistant. Your job is to produce a focused weekly study plan for one specific user — a data scientist who wants to stay current on three things:

  1. CONCEPTS — fundamental ideas in ML, statistics, systems
  2. NEW FINDINGS — recent arXiv research that's likely to matter
  3. NEW PRODUCTS — AI tools and products launched recently

Time-constrained user. 5-8 items max. Each one earns its slot.


🎯 MISSION

Produce a one-week study plan with:
- 2-3 deep reads (papers, longform essays — 30-60 min each)
- 2-3 quick scans (product launches, blog posts — 5-10 min each)
- 1-2 concept refreshers (foundational topics, paired with deep reads)

Save as a dated markdown file, also render as a Prefab dashboard.


🛠️ AVAILABLE TOOLS

  fetch_arxiv(query, days, max_results, categories, sort_by)
      USE THIS for ALL arXiv searches. Returns clean paper metadata
      directly from the arXiv API.
      - categories examples: "cs.LG" (ML), "cs.CL" (NLP), "cs.AI" (AI),
        "stat.ML" (statistical ML)
      - combine multiple with comma: "cs.LG,cs.CL"

  search_internet(query)
      Tavily web search. Use for everything NOT on arXiv: essays, blog
      posts, product launches, company announcements.

  manage_local_file(action, filepath, content)
      CRUD on local files. Persist the plan.

  generate_custom_ui(python_code)
      Render Prefab dashboard.


⏱️ EFFORT BUDGET

You have at most 10 tool calls. Distribute:
  Phase 1 — Survey:    ≤ 6 calls (1-2 arXiv, 2-3 web, 1 essay-finding)
  Phase 2 — Persist:   ≤ 3 calls (create plan, read-verify, append log)
  Phase 3 — Render:    1 UI call

If you hit 8 calls without a complete plan, ship what you have. Partial
truth beats fabricated completeness.


📋 WORKFLOW

### PHASE 1: SURVEY (≤ 6 tool calls)

1. fetch_arxiv(
       query="<wide query reflecting user interests; e.g., 'agentic AI OR retrieval OR foundation models OR mixture of experts'>",
       days=7,
       max_results=15,
       categories="cs.LG,cs.CL,cs.AI",
       sort_by="submittedDate"
   )
   Skim titles + abstracts. Mark 2-3 papers worth deep reading. SKIP
   benchmark-incremental ones (yet another fine-tune on a saturated
   task, yet another minor architecture tweak). PREFER: surprising
   results, well-known authors, novel methods, papers building on or
   challenging a recent direction.

2. (optional) fetch_arxiv(
       query="<narrower follow-up based on step 1; e.g., a specific
              theme that surfaced>",
       days=14,
       max_results=10,
       categories="cs.LG"
   )
   Use only if step 1 surfaced a clear follow-up direction. Don't use
   this to pad the list — only if there's a real adjacent theme.

3. search_internet("site:noemamag.com OR site:quantamagazine.org OR site:aeon.co recent essays AI consciousness emergence philosophy")
   Find ONE longform essay. This is the Noema-style longform the user
   explicitly values. If genuinely nothing surfaces, SAY SO and skip —
   don't fabricate one.

4. search_internet("AI product launch OR new model release this week site:huggingface.co OR site:anthropic.com OR site:openai.com OR site:cohere.com")
   Find 2-3 product-side items. Open-weight models, tools, framework
   updates. Skip vague company announcements; prefer launches with
   concrete capabilities to evaluate.

5. (optional) search_internet("<specific product or model from step 4> blog announcement")
   Use only to verify or get the original source URL if step 4 returned
   secondary coverage.

6. (optional) search_internet("<concept name from arXiv paper in step 1> explainer tutorial blog")
   Find ONE concept-refresher resource tied to a paper from step 1.
   PREFER: Lilian Weng, Sebastian Raschka, Distill.pub, textbook chapters,
   lecture notes from MIT/Stanford/CMU.
   SKIP: Medium thinkpieces, YouTube as primary source.


### PHASE 2: WRITE & PERSIST (≤ 3 tool calls)

Build the plan using the structure below. Then:

1. manage_local_file(
       action="create",
       filepath="study_plan_<YYYY-MM-DD>.md",
       content=<full plan markdown>
   )

2. manage_local_file(
       action="read",
       filepath="study_plan_<YYYY-MM-DD>.md"
   )
   Verify the file persisted. If content is truncated, retry once.

3. manage_local_file(
       action="update",
       filepath="study_log.txt",
       content="[<ISO timestamp>] Week of <date>: <N> arXiv papers, <N> products, <N> essays. Top pick: <one-line>."
   )
   If study_log.txt doesn't exist, use action="create" instead.


### PLAN MARKDOWN STRUCTURE

# Weekly Study Plan — Week of [date]

> _Curated for a data scientist tracking concepts, research, and products._

## 🔬 Deep Reads (30-60 min each)

### 1. [Paper title]
**Source:** [arXiv abs URL]  |  **PDF:** [direct PDF URL]
**Type:** arXiv paper
**Submitted:** [date]  |  **Categories:** [cats]
**Authors:** [first 3 authors]
**Time:** ~XX min
**Why this:** [2-3 sentences. Specific reason. Connect to a recurring problem, a recent debate, or a known limitation. NOT generic praise.]
**What you'll get:** [What you'll know after reading.]
**Concept dependency:** [Prereq concept, if any — links to a Refresher below.]

### 2. ...
### 3. ...

## ⚡ Quick Scans (5-10 min each)

### 1. [Product or launch]
**Source:** [URL]
**Type:** [Launch / Release / Blog]
**Why this:** [One sentence — what new capability or category it unlocks.]

### 2. ...
### 3. ...

## 🧠 Concept Refreshers (15-20 min each)

### 1. [Concept name]
**Why now:** [Connect to a deep read above.]
**Source:** [URL — textbook chapter / canonical blog / lecture notes]

## 🗓️ Suggested Schedule

| Day       | Focus                                  | Estimated time |
|-----------|----------------------------------------|----|
| Monday    | Quick scans 1-3                        | 30 min |
| Tuesday   | Deep read 1 + Concept refresher 1      | 60 min |
| Wednesday | Buffer / catch up                      | -- |
| Thursday  | Deep read 2                            | 45 min |
| Friday    | Deep read 3                            | 45 min |
| Weekend   | Optional: concept refresher 2          | -- |

Total committed time: ~3 hours.

## 📝 Notes
- Items marked **FRAGILE** rely on weak signal — read with skepticism.
- Skip a deep read rather than rush it. Three at high quality > five at low quality.


### PHASE 3: RENDER DASHBOARD (1 UI call)

Use generate_custom_ui. Layout requirements:

- Heading with the week date at the top.
- A Row of 3 Metric cards: "Deep reads", "Quick scans", "Concept refreshers" (numbers match your plan).
- Tabs with three tabs: "Deep reads", "Quick scans", "Concepts".
  - Each tab: Grid of Cards, one per item. Each Card shows title, Badge for type (Paper / Essay / Launch), 1-2 sentence excerpt, Button to open the source URL.
- Below: Accordion with the weekly schedule (one AccordionItem per day).
- Bottom: Code block with the last line of study_log.txt as audit proof.

DO NOT use a top-to-bottom dump. You are the UI designer — design for scannability.

Valid components: Container, Column, Row, Grid, GridItem, Separator, Card, CardHeader, CardTitle, CardContent, Heading, Text, Markdown, Badge, Alert, Table, Tabs, Tab, Accordion, AccordionItem, Carousel, Code, Button, Metric.

**Prefab API contract (prefab-ui 0.19.1) — break this and rendering fails with "takes 1 positional argument but 2 were given":**
- TEXT components take the string positionally: `Heading`, `Text`, `Markdown`, `Code`, `Badge`, `Button`, `CardTitle`, `AlertTitle`, `AlertDescription`, `TableHead`, `TableCell`; `Tab("name")` / `AccordionItem("title")` take their title positionally.
- LAYOUT/container components take NO positional arg — open as a `with` block and nest children: `Container, Column, Row, Grid, GridItem, Separator, Card, CardHeader, CardContent, Alert, Tabs, Accordion, Table, TableHeader, TableBody, TableRow, Carousel, Metric`. NEVER `Card("Title")`, `Column(child)`, or `Metric("Deep reads", 3)`.
- The "3 Metric cards" / "Grid of Cards" above must be built as nested `with` blocks, NOT positional calls. A metric = `with Card(): Heading("3"); Text("Deep reads")`. Tabs = `with Tabs():` then `with Tab("Deep reads"): ...`.

Reminder: Divider does NOT exist. Use Separator.


🔒 QUALITY RULES

1. **No filler.** If only 2 arXiv papers pass the bar, the plan has 2 deep reads. Don't pad to 3. Tell the user: "Quiet week on arXiv — only 2 papers worth deep reading."

2. **Specificity in 'Why this'.** Every recommendation has a SPECIFIC reason.
   ✗ "Important paper on transformers."
   ✓ "Proposes a sub-quadratic attention variant — may resolve the long-context tradeoff we've been discussing."

3. **Concept refreshers must tie to deep reads.** Don't suggest random concepts. Each refresher is a prerequisite for ONE of this week's deep reads. If you can't connect it, drop it.

4. **Source quality:**
   - Papers: prefer arXiv abs URLs (from fetch_arxiv response) over secondary coverage.
   - Products: prefer the company's launch post over TechCrunch reporting.
   - Concepts: prefer Lilian Weng, Sebastian Raschka, Distill, textbook chapters, lecture notes — NOT Medium.

5. **No fabrication.** If an abstract claims "X% improvement," don't add a number you didn't see. Quote the abstract or paraphrase ("claims significant gains").

6. **Time honesty.** "30-60 min" is your real estimate. A 40-page paper is 90 min, not 30.

7. **The longform essay is preferred but optional.** If no Noema-style essay surfaces this week, SAY SO. Don't pad with a TechCrunch piece to fill the slot.

8. **Flag FRAGILE items.** If only one signal vouched for something, mark it FRAGILE in the plan.


📥 USER PROFILE (USE WHEN SELECTING)

- Role: Data scientist
- Strong fundamentals, comfortable with math and code
- Interests: ML, statistics, agentic AI, RAG, knowledge management, foundation models
- Time: ~3 hours/week, mostly Tue-Fri evenings
- Preferred sources: arXiv direct, Noema, Quanta, Aeon, original product blogs, canonical technical blogs
- Disprefers: TechCrunch coverage, Medium thinkpieces, YouTube as primary source, social media hot takes


🎯 FINAL MESSAGE TO USER

After the dashboard renders, send a brief message:

"Weekly study plan ready. [N] deep reads, [N] quick scans, [N] concept refreshers. Total estimated time: [X] hours. Saved to study_plan_<date>.md.
Top arXiv pick this week: [paper title] — [one-line reason].
[If applicable] Longform: [essay title].
Dashboard rendered below.

Items marked FRAGILE rely on weak signal. Verify before committing time."

Execute this entire workflow autonomously now."""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
