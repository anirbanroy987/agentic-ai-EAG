# Session 7 — RAG Agent (FAISS-backed vector memory)

S6 cognitive architecture (Memory → Perception → Decision → Action) plus
FAISS vector retrieval and two new MCP tools: `index_document` and
`search_knowledge`. The agent loop is unchanged from S6; only the Memory
service, the Decision/Perception prompts, and the gateway changed.

## Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- A `.env` in this folder (see `.env.example`) with at least:
  - `GEMINI_API_KEY` — used for embeddings (the model is pinned at the gateway)
  - `TAVILY_API_KEY` — web search (DuckDuckGo fallback if absent)
  - `OPEN_ROUTER_API_KEY`, `GROQ_API_KEY` — chat-routing fallbacks when Gemini 429s
- Playwright Chromium for `fetch_url`:
  ```powershell
  uv run python -m playwright install chromium
  ```

The gateway (`llm_gatewayV7/`) auto-starts on port 8107 the first time you
run the agent. To watch its provider-routing logs, run it manually in a
second terminal instead:

```powershell
cd llm_gatewayV7
uv run main.py
```

## Running a single query

```powershell
uv run agent7.py "Fetch the Wikipedia page for Claude Shannon and tell me what he is most famous for."
```

## Running the full regression suite

`run_queries.ps1` runs all 8 base queries (10 invocations) one at a time,
pausing between each. Ordering matters — C2 reads memory written by C1, and
F2 reads the FAISS index built by F1.

```powershell
.\run_queries.ps1            # all queries, pause between each
.\run_queries.ps1 -Fresh     # wipe state/ first, then run all
.\run_queries.ps1 -NoPause   # run back-to-back, no pauses
.\run_queries.ps1 -Only A,E  # run only specific queries
```

## The 8 base queries

| ID | Iters | What it proves | Query |
|----|-------|----------------|-------|
| A  | 3  | Pure artifact-attach path; vector machinery uninvolved | Fetch the Wikipedia page for Claude Shannon and tell me what he is most famous for. |
| B  | 8  | Multi-goal; memory carries tool outcome between goals | Suggest 3 activities to do in Tokyo on Saturday based on the weather forecast. |
| C1 | 4  | Durable memory write across runs | My mom's birthday is on 15 May 2026. Save a reminder note and another reminder two weeks before. |
| C2 | 3 (0 tools) | Recall from durable memory, no tool calls | When is my mom's birthday? |
| D  | 6  | In-run artifact synthesis | Research Python asyncio best practices from the top 3 web results and summarise them. |
| E  | 5  | Simplest RAG: index once, retrieve once | Index the file papers/attention.md and then tell me what attention mechanism it describes. |
| F1 | 11 | Index whole corpus | Index every .md file under papers/ and then tell me which paper introduces LoRA. |
| F2 | 3  | FAISS persistence across process boundaries | Across the indexed papers, which one is about Direct Preference Optimization? |
| G  | 4  | Dense retrieval beats grep ("credit assignment" = 0 grep hits) | What do the indexed papers say about the credit assignment problem? |
| H  | 3  | Cross-document synthesis with attribution | Compare ReAct and Chain-of-Thought based on the indexed papers and cite which paper each idea came from. |

## State directory

After the corpus is indexed, `state/` holds exactly three files:

```
state/
  memory.json        items: chunk facts, tool_outcomes, classifier facts
  index.faiss        N vectors at dim 768
  index_ids.json     N id strings in FAISS row order
```

Off-by-one check: `memory.json` item count = FAISS vector count + 1 (the
user-query scratchpad item is intentionally not embedded).

To wipe memory, delete all three files (not just `memory.json`):

```powershell
Remove-Item state\memory.json, state\index.faiss, state\index_ids.json
```

## Sanity checks

1. Perception prompt contains zero MCP tool names (architectural gate).
2. Delete `state/` and re-run F2 to confirm cross-process persistence works
   on a fresh process against a populated state directory.
