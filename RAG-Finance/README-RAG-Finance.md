# RAG-Finance — corpus scraper + Streamlit viewer

This folder is a **copy of the Session-7 agent**, plus two standalone add-ons:

1. `scrape_corpus.py` — a corpus scraper (no agent code involved).
2. `app.py` — a Streamlit **viewer** that runs the existing agent and shows its
   Perception → Decision → Action → Memory loop.

The agent itself (`agent7.py`, the four roles, the MCP tools, the gateway, FAISS
persistence) is **unchanged**. The viewer is a window onto the agent, not a new
RAG system: retrieval, embeddings, and chunking all stay inside the agent.

```
RAG-Finance/
├── agent7.py, perception.py, decision.py, action.py, memory.py …  (unchanged agent)
├── mcp_server.py                 (unchanged — index_document / search_knowledge live here)
├── llm_gatewayV7/                (unchanged gateway)
├── state/                        (empty — fresh FAISS index builds here)
├── sandbox/finance/              (scraper writes the corpus here)
├── sources.txt                   (URL list for the scraper)
├── scrape_corpus.py              (NEW — standalone scraper)
├── app.py                        (NEW — Streamlit viewer)
└── requirements-app.txt          (NEW — scraper + viewer deps)
```

---

## 1. Setup

The scraper + viewer deps (`streamlit`, `trafilatura`, `pypdf`) are declared in
`pyproject.toml` alongside the agent's own deps (incl. `faiss-cpu`). Install
everything into one consistent venv with:

```powershell
# from RAG-Finance/
uv sync
```

> **Why `uv sync` and not `uv pip install`:** `uv run`/`uv sync` reconcile the
> venv to `uv.lock`. If the add-on deps were only pip-installed (not in
> `pyproject.toml`), a later `uv run` could churn the venv and even drop
> `faiss-cpu`, producing *"faiss-cpu is required for S7"*. Declaring them in
> `pyproject.toml` keeps faiss + agent + add-ons locked together. (For a non-uv
> setup: `pip install -r requirements-app.txt`.)

Make sure `.env` has the keys the agent needs (`GEMINI_API_KEY`, `TAVILY_API_KEY`,
etc.) — it was copied from Session 7.

---

## 2. Scrape the corpus

`scrape_corpus.py` reads `sources.txt`, fetches each URL, extracts the main
article text (trafilatura for HTML, pypdf for PDFs), and writes one slugified
`.md` per page into `sandbox/finance/` with a 3-line front matter
(`source_url`, `fetched_at`, `title`).

```powershell
uv run scrape_corpus.py                       # all URLs in sources.txt
uv run scrape_corpus.py --limit 10            # first 10 only (quick test)
uv run scrape_corpus.py --delay 3 --overwrite # slower; re-fetch existing
```

It **respects robots.txt**, rate-limits **≥ 2 s** between requests, logs failures
to `sandbox/finance/skipped.txt`, and never aborts on a single bad URL. A summary
(`saved / skipped / total`) prints at the end.

> Some URLs in `sources.txt` are category/landing pages (marked in the file).
> Open those in a browser and replace them with concrete article URLs if you want
> single-article pages. ⚠️ tax/rate articles are date-sensitive — treat the
> indexed copy as a concept tutor, not a filing reference.

---

## 3. Index the corpus (via the agent — not the scraper)

The scraper only produces Markdown. Indexing is done by the agent's **existing**
`index_document` tool, exactly as on the command line:

```powershell
uv run agent7.py "Index every .md file under finance/ and tell me how many chunks were added."
```

This chunks each file and writes FAISS-searchable facts into `state/`.

---

## 4. Run the viewer

```powershell
uv run streamlit run app.py
```

Then in the browser:

- type a query (e.g. *"What do the sources say about emergency fund vs home-loan
  prepayment?"*) and press **Run**;
- watch each iteration appear as an expandable **status block** showing, in order:
  - 💾 **Memory (read)** — how many hits memory returned,
  - 👁 **Perception** — the goals it set (🟡 open / ✅ done),
  - 🧭 **Decision** — the tool it picked + args, or its answer,
  - ⚡ **Action** — the tool result preview,
  - 💾 **Memory (write)** — tool outcome recorded;
- the **Final answer** renders at the bottom, with a **Retrieved sources** panel
  listing each `search_knowledge` call's previewed chunks.

### How it works (and what it does NOT do)

The viewer launches `agent7.py` as a **subprocess** and streams its stdout, parsing
the agent's existing log prefixes (`─── iter N`, `[memory.read]`, `[perception]`,
`[decision]`, `[action]`, `FINAL:`). It forces `PYTHONIOENCODING=utf-8` on the child
so the agent's box-drawing/arrow glyphs stream cleanly on Windows. **No agent file is
modified.**

**Known limitation (by design):** the agent truncates its `[action]` line to ~200
chars. So the *Retrieved sources* panel shows those ≤200-char previews, not the full
chunk text. Reconstructing full chunks would mean re-querying the index or reading
`state/memory.json` — deliberately skipped to keep this a pure read-only viewer.

---

## Disclaimer

Educational only — **not personalized financial advice.** Mirror what the sources
themselves state: this is for learning, not for filing or investment decisions.
