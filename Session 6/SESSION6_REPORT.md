# Session 6 Agentic AI Loop — Technical Engineering Report

---

## 1. High-Level Overview

### Purpose

The project implements an **agentic AI executor** for the EAGV3 Session 6 assignment. It takes a free-form natural-language query (e.g. *"Find 3 family-friendly Tokyo activities, check Saturday weather, recommend the best one"*) and answers it autonomously by:

1. Decomposing the query into atomic goals
2. Pulling tools from an MCP server (web search, URL fetch, file ops, etc.)
3. Routing each LLM call through a multi-provider gateway (Gemini / Groq / OpenRouter / …)
4. Stashing large outputs in an in-memory artifact store so the conversation context never blows up
5. Persisting facts in a keyword-indexed JSON memory store across runs

### Business / Engineering Goal

Build a **cognitively decomposed agent** that survives the realities of free-tier LLM APIs (daily quotas, JSON-mode quirks, schema rejections), unreliable web tools (crawl4ai's Chromium download, 404 URLs, Unicode-hostile Windows consoles), and the architectural constraints of the Session 6 lecture (sequential processing, immutable goals, single-tool-call decisions, no RAG). Everything that talks across module boundaries is a typed Pydantic object so type drift never quietly corrupts the loop.

### Tech Stack

| Layer | Tech |
|---|---|
| Orchestration | Python 3.11, asyncio, `mcp.client.stdio` |
| Contracts | Pydantic v2 |
| LLM Routing | LLM Gateway V3 (FastAPI, multi-provider failover) |
| Tools | MCP stdio server with 9 tools (Tavily/DDG search, crawl4ai fetch, sandboxed file ops) |
| HTTP | httpx |
| Time zones | zoneinfo + tzdata |

### Execution Flow (high-level)

```
user query
   │
   ▼
agent6.run()  ───────────────────────────────────────────────────┐
   │                                                             │
   ├─► memory.remember(query)          [classify user input]     │
   │                                                             │
   ▼                                                             │
┌──── for iter in 1..MAX_ITERATIONS ─────────────────────────┐   │
│                                                            │   │
│  memory.read(query, history)  ─────► hits                  │   │
│  perception.observe(...)      ─────► GoalList              │   │
│  if all_done: break                                        │   │
│  attach artifacts (loop crosses the artifact wall)         │   │
│  decision.next_step(goal)     ─────► DecisionOutput        │   │
│         │                                                  │   │
│         ├─ is_answer: record, mark goal done               │   │
│         └─ tool_call: action.execute() → maybe artifact    │   │
│                       memory.record_outcome()              │   │
└────────────────────────────────────────────────────────────┘   │
   │                                                             │
   ▼                                                             │
final_answer print  ◄────────────────────────────────────────────┘
```

---

## 2. Initial State / Starting Point

### What was on disk before the work began

The `Session 6/` directory contained:

| File | State | Note |
|---|---|---|
| `agent5.py` | 344 lines, working | Session-5 monolithic reference for arithmetic task |
| `mcp_server.py` | 295 lines | 9-tool MCP server |
| `assignment.py` | 50 lines | Lecture's mini-demo of keyword memory |
| `llm_gatewayV3/` | full package | Vendored FastAPI gateway |
| `agent6.py` | **0 bytes** | empty stub |
| `perception.py` (misspelled `percention.py`) | **0 bytes** | empty stub |
| `decision.py` | **0 bytes** | empty stub |
| `action.py` | **0 bytes** | empty stub |
| `memory.py` | **0 bytes** | empty stub |
| `artifacts.py` | **0 bytes** | empty stub |
| `schemas.py` | **0 bytes** | empty stub |

### How the agent worked originally

Only `agent5.py` was operational. It used a single-file, single-prompt, native-tool-use loop against the **V2** gateway:

```python
# agent5.py — conceptual sketch
llm = LLM()
messages = [{"role": "user", "content": user_task}]
for turn in range(max_turns):
    reply = llm.chat(messages=messages, tools=mcp_tools, ...)
    if not reply["tool_calls"]:
        return reply["text"]
    results = await dispatch_tool_calls(session, reply["tool_calls"])
    messages.extend(results)
```

**Limitations:**

- **No cognitive separation** — perception/decision/memory all collapsed into one prompt.
- **No artifact wall** — full tool outputs went straight into the conversation; a Wikipedia fetch would overflow context.
- **No memory across runs** — JSON memory wasn't read or written.
- **No failover** — `provider="g"` (Gemini) was pinned; quota burns or 5xx errors killed the run.
- **Hardcoded `llm_gatewayV2`** path — pointed at a folder that doesn't exist in Session 6.
- **No date anchoring** — queries about "today" / "this weekend" relied on the model's internal date guess.

---

## 3. Step-by-Step Change Log

### Step 1 — First-pass PDA-M scaffold (modular agent built from empty stubs)

**What changed:** Wrote initial versions of `schemas.py`, `perception.py`, `memory.py`, `decision.py`, `action.py`, `artifacts.py`, `agent6.py`, plus a `_gateway.py` shim re-exporting `llm_gatewayV3.client.LLM`.

**Why:** The user wanted the lecture's PDA-M layout — each cognitive role in its own module — with the V3 gateway's `auto_route` labels (`"perception"`, `"memory"`, `"decision"`) so the gateway's router-LLM picks workers per role.

**Before:** Empty stubs; no working pipeline.
**After:** Working modular loop end-to-end on the Shannon Wikipedia task.

**Impact:** Established clean module boundaries; every Pydantic boundary lived in `schemas.py`; the gateway shim isolated the import path so each module just did `from _gateway import LLM`.

---

### Step 2 — User-driven `agent6.py` rewrite, all modules re-aligned

**What changed:** The user replaced `agent6.py` with a richer orchestrator contract (singleton `memory` / `artifacts`, `Goal` / `GoalList` / `DecisionOutput`, `perception.observe(...)`, `decision.next_step(...)`, `action.execute(session, tool_call) → (text, art_id)`, an explicit artifact-wall comment). All six PDA-M modules were rewritten to match this surface.

**Why:** The user wanted the orchestrator to be the canonical contract — modules conform to it, not vice versa. Made it explicit that only the loop crosses the artifact wall.

**Technical meaning:** Singleton pattern for `memory` and `artifacts`; one source of truth per cognitive store. `Goal` carries an `attach_artifact_id` so perception can request bytes for the next decision turn without seeing them itself.

**Impact:** Every module now had a clear, narrow API; the orchestrator's flow became obvious to read.

---

### Step 3 — FastAPI dependency, gateway startup

**What changed:** Documented the install commands; no code change.

**Why:** `python llm_gatewayV3/main.py` threw `ModuleNotFoundError: fastapi`. The gateway is a separate process and ships its own `requirements.txt`.

**Impact:** Established the two-shell run protocol (gateway in shell 1, agent in shell 2) and the `.env` requirement for at least one provider key.

---

### Step 4 — Fixed the `sys.path` shadowing in `_gateway.py`

**What changed:** Replaced `sys.path.insert(0, "llm_gatewayV3")` with `importlib.util.spec_from_file_location()`.

**Why:** Inserting `llm_gatewayV3/` on `sys.path` made its own `schemas.py` shadow Session-6's `schemas.py`. The first `from schemas import Goal` then resolved to the gateway's `ChatRequest` module, raising `ImportError`.

**Before:**
```python
sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM
```

**After:**
```python
spec = importlib.util.spec_from_file_location("_v3_client",
        Path(__file__).parent / "llm_gatewayV3" / "client.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
_RawLLM = mod.LLM
```

**Technical meaning:** File-path imports never touch the search path, so name collisions across separate trees disappear.

---

### Step 5 — Gateway error body surfacing

**What changed:** Subclassed `LLM` to catch `httpx.HTTPStatusError`, extract the response body (which carries provider name + actual failure reason), wrap it in a `GatewayError(status, url, body)`.

**Why:** Default `raise_for_status()` discards the body. A 502 from the gateway became opaque — no way to see *which* provider failed and *why*.

**Impact:** All subsequent debugging — quota errors, JSON validation failures, provider-specific 400s — became immediately diagnosable.

---

### Step 6 — MCP server's crawl4ai banner corrupting RPC

**What changed:** Hoisted `from crawl4ai import AsyncWebCrawler` to module load, wrapped it in `os.dup2(2, 1)` to redirect stdout → stderr **during the import itself**.

**Why:** crawl4ai prints `[INIT].... → Crawl4AI 0.8.6` via Rich at import time. Under MCP stdio that landed on fd 1 — the JSON-RPC pipe to agent6. The very first tool call's response sat behind invalid JSON; the MCP client waited forever for a valid frame. Every subsequent tool call timed out.

**Before:**
```python
async def _crawl4ai_fetch(url):
    from crawl4ai import AsyncWebCrawler        # banner fires here
    saved_fd = os.dup(1); os.dup2(2, 1)         # redirect kicks in too late
    ...
```

**After:**
```python
# module load
_saved = os.dup(1); os.dup2(2, 1)
try:
    from crawl4ai import AsyncWebCrawler        # banner absorbed
finally:
    os.dup2(_saved, 1); os.close(_saved)
```

**Impact:** RPC channel stays clean for the entire MCP session; all subsequent tool calls work.

---

### Step 7 — Windows console UTF-8 reconfiguration

**What changed:** Added `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` (and same for stderr) at the top of `mcp_server.py`.

**Why:** Even after redirecting fd 1 → fd 2, Rich's `→` character hit Python's `TextIOWrapper` (default `cp1252` on Windows) and raised `UnicodeEncodeError: 'charmap' codec can't encode character '→'`. The encoding happens *before* the bytes reach the redirected fd.

**Impact:** The MCP server runs cleanly on any Windows console; non-ASCII characters in tool output never crash the JSON-RPC stream.

---

### Step 8 — Per-tool timeout in `action.execute`

**What changed:** Wrapped `session.call_tool(...)` in `asyncio.wait_for(..., timeout=180)`; on timeout, returned a synthetic `[tool_timeout]` result instead of hanging.

**Why:** crawl4ai's first-ever run downloads a headless Chromium (~120 MB) silently — no progress, no timeout in the MCP path. Without this guard the loop hangs indefinitely.

**Impact:** Bounded latency at the action layer; decision sees the timeout text and can pick a different tool on the next iter.

---

### Step 9 — Loop didn't terminate after answer (goal-flip fix)

**What changed:** After `out.is_answer` in the orchestrator, mutate the matching goal in `prior_goals` to `done=True` before continuing.

**Why:** Decision returned the correct Shannon answer on iter 2, but perception never flipped the goal's done flag based on history — it kept saying "open" — so the loop ran to MAX_ITERATIONS producing identical answers eight times.

**Before:**
```python
if out.is_answer:
    history.append({"kind": "answer", "text": out.answer, ...})
    continue                  # goal still 'open' next iter
```

**After:**
```python
if out.is_answer:
    history.append({...})
    for g in prior_goals:
        if g.id == goal.id: g.done = True; break
    continue
```

**Impact:** Loop terminates immediately on the iter after an answer (the next perception call sees `all_done=True`).

---

### Step 10 — Four architectural fixes from the lecture audit

After the user shared the lecture notes, four divergences got fixed in one pass:

| Fix | Why | File |
|---|---|---|
| Artifact IDs: SHA-256 hex → **integer counter** | "Weaker LLMs hallucinate long hex strings"; iter 9 of the Shannon run literally showed perception attaching the wrong hex ID | `artifacts.py` |
| Artifacts: disk-persisted → **RAM-only dict** | Lecture: "Artifacts are NOT saved to disk by default" | `artifacts.py` |
| Perception: `auto_route="perception"` → **pinned `provider="g"`** | Lecture: "Perception always goes to Gemini" | `perception.py` |
| Goals: mutable across iters → **immutable after iter 1** | Lecture: "Goals are immutable in Session 6"; iter 9 showed perception inventing a duplicate goal | `agent6.py` |

Also dropped `cache_system=True` (fake on free tiers) and switched `temperature=0` → `temperature=1` everywhere.

---

### Step 11 — Gemini-pinned perception, fallback chain

**What changed:** When pinned to Gemini, a 429 quota error terminated the run because explicit-provider failures don't fall back inside the gateway. Added a per-call chain `["g", "gr", "or"]` in `perception.observe()` with a `_should_fallback(err)` predicate.

**Why:** Gemini's free tier (20 generate_content/day) burns fast during development. The lecture's "always Gemini" rule is impossible to honor under that constraint.

**Impact:** The run survives Gemini outages by silently degrading to Groq for that single call. Gemini is still tried first every call; once its quota resets the run quietly returns to it.

---

### Step 12 — Multi-attach support (`attach_artifact_ids`)

**What changed:** `Goal.attach_artifact_id: Optional[str]` → `Goal.attach_artifact_ids: list[str]`. Updated `GOALLIST_SCHEMA`, perception parsing, orchestrator iteration, and decision render.

**Why:** Iter 5 of the Tokyo run showed a synthesis goal *"recommend best activity given activities + weather"* that needed **both** artifact 1 (activities) and artifact 2 (weather). With a single-string field, only one could attach; decision tried to re-fetch the missing one, hit a 404, looped, and eventually crashed.

**Impact:** Synthesis goals work; iter count drops; no more 404-retry loops.

---

### Step 13 — Iterative widening of the fallback predicate

The `_should_fallback` predicate grew over four iterations, each driven by a new failure signature:

| Iteration | New signature added | Cause |
|---|---|---|
| 1 | `quota`, `rate limit`, `rpm/rpd`, `exceeded`, `unavailable` (status 429/503) | Gemini quota burned (auto_route path) |
| 2 | `json_validate_failed`, `failed_generation`, `max completion tokens reached` (status 400/502) | Groq's structured-output engine returned `failed_generation: ""` |
| 3 | All recoverable signatures on **any** status in `(400, 429, 502, 503)` | Status mismatch — pinned-provider failures wrapped as 502 not 429 |
| 4 | `structured output failed validation`, `output is not json`, `did not match the required json schema`, plus statuses `(400, 408, 429, 500, 502, 503, 504)` | Gateway's own validation phrasing didn't match worker phrasing |

Real bugs (auth 403, missing-tool 404, generic 500 with no signature, schema-shape 400) still surface as `GatewayError` rather than silently retrying.

---

### Step 14 — `httpx.ConnectError` handling

**What changed:** Caught `ConnectError` / `ConnectTimeout` in `_gateway.LLM.chat` and raised a friendly `GatewayError(0, url, "gateway unreachable — is …running?")`.

**Why:** When the gateway process wasn't running, the user got a 60-line httpx/httpcore stacktrace instead of "the server is down."

---

### Step 15 — Date-anchor injection (`NOW: …`)

**What changed:** Added `_now_block()` to both `perception.py` and `decision.py`, prepended to the user_block on every call. Defensive fallback to local naive time when `tzdata` isn't installed.

**Why:** The Tokyo query mentioned "this weekend" / "Saturday." The model knew Saturdays in May 2026 (correctly listed 2, 9, 16, 23, 30) but had no way to know **which** Saturday was "this" weekend without an anchor. Injecting `NOW: Friday, 22 May 2026` resolves the ambiguity without burning an iter on `get_time`.

**Impact:** Temporal queries succeed in iter count proportional to the task, not bloated by date-discovery turns.

---

### Step 16 — Move `memory.remember` back to top of `run()`, tighten perception's done-rule

**What changed:**
- Moved `memory.remember(query)` from end-of-run to top-of-run.
- Hardened perception's `PERCEPTION_SYSTEM` to say: `done: true` requires **HISTORY** evidence, never just memory hints; on iter 1 HISTORY is empty so every goal must start `done: false`.

**Why:** "When is my mom's birthday?" returned `(no answer was produced)` because perception saw 1 memory hit and pre-marked the only goal `done`, short-circuiting the loop before decision was ever called. End-of-run `remember` also missed fact-capture for queries that died mid-loop. Both fixes align the implementation with the lecture's Run 1/Run 2 reference traces.

---

## 4. Deep Dive Into Core Components

### `schemas.py` — The contract layer

**Responsibility:** Single source of truth for every Pydantic boundary in the loop.

**Key types:**
- `ToolDef` — what the gateway expects on `tools=[...]` requests
- `Goal` / `GoalList` — perception's output (with `all_done` property and `next_unfinished()` helper)
- `ToolCall` / `DecisionOutput` — decision's output (XOR: `is_answer` or `tool_call`)
- `MemoryItem` — one row in the persistent store; kinds: `fact | preference | scratchpad | tool_outcome | answer`

**Design decision:** Every cross-module value is a Pydantic model, not a dict. Type drift never silently corrupts the loop; a wrong shape raises `ValidationError` at the boundary.

---

### `_gateway.py` — Gateway client shim

**Responsibility:** Load the V3 client module without polluting `sys.path`; surface failures with actionable bodies.

**Workflow:**
```
chat() called
   │
   ▼
super().chat()              ┌─► HTTPStatusError → GatewayError(status, url, body)
   ├──────────────────────► ┼─► ConnectError    → GatewayError(0, url, "gateway unreachable…")
   ▼                        └─► success         → response JSON
return JSON
```

**Edge case:** Status `0` is a sentinel — no real HTTP code is `0`, but it surfaces in the trace as unambiguous evidence the gateway was unreachable, distinct from any `4xx/5xx`.

---

### `memory.py` — Persistent JSON memory with semantic write

**Responsibility:** Implements the lecture's dual-cost contract:
- **READ:** keyword overlap on tokenized query + recent history → free, no LLM
- **WRITE 1:** `remember(text)` runs an LLM classifier (`auto_route="memory"`) → semantic kind + keywords + descriptor
- **WRITE 2:** `record_outcome(tool_call, result_text, artifact_id, …)` builds keywords from tool name + args + result text → no LLM, cheap

**Why two write paths:** Tool outcomes don't need semantic classification — they're always `kind="tool_outcome"`. Only the user's natural-language input needs LLM judgement to decide *fact* vs. *preference* vs. *scratchpad*.

**Persistence:** JSON list at `./memory_store.json`. Loaded at agent start, saved on each write. A corrupt store is silently re-initialized rather than crashing the run.

---

### `perception.py` — Decompose, verify, attach

**Responsibility:** Given (query, memory hits, history, prior goals), return a `GoalList`. Pinned to Gemini with Groq/OpenRouter fallback.

**Internal flow:**
```
build user_block:
  NOW: ...                  (date anchor)
  USER_QUERY: ...
  RECALLED_MEMORY: ...
  PRIOR_GOALS: ...
  HISTORY: ...              (last 8 events)
  ARTIFACT_CATALOGUE: ...   (id + descriptor + size only — never bytes)
   │
   ▼
for provider in [g, gr, or]:
    try: llm.chat(structured-output GoalList)
    except GatewayError as e:
        if _should_fallback(e): continue
        else: raise
   │
   ▼
parse JSON → GoalList
  drop invented artifact IDs (not in catalog)
  drop empty-text goals
  coerce singular `attach_artifact_id` → list (backward compat)
```

**Design decision:** Perception **never sees artifact bytes**. The catalog has only `(id, descriptor, size)`. This is the lecture's "artifact wall" — perception decides *which* artifact decision needs; the orchestrator is the only stage that materializes bytes.

---

### `decision.py` — One goal → one answer or one tool call

**Responsibility:** Given a single goal + its attached artifact bytes + history + tool catalog, emit a `DecisionOutput`. Uses `auto_route="decision"` so the gateway's router picks a worker (TINY/LARGE tier).

**System prompt rules (sharpened over the conversation):**
1. If ATTACHED_ARTIFACTS contains the info, **ANSWER** — never re-fetch.
2. Never retry a tool call with arguments that just failed in history.
3. Prefer one tool call over guessing when context genuinely lacks info.
4. When you answer, be terse and structured.

**Why the "don't re-fetch" rule was added:** Iter 5–6 of an early Tokyo run repeatedly called `fetch_url` on a URL that returned 404, looping uselessly.

---

### `action.py` — Pure dispatch + artifact wall (write side)

**Responsibility:** Execute exactly one `ToolCall` via MCP. **No LLM.**

**The artifact wall (write side):**
```python
text = _extract_text(result)
if len(text) <= ARTIFACT_THRESHOLD (4000):
    return text, None                          # inline, no artifact
art_id = artifacts.put_bytes(text.encode(), descriptor=...)
inline = text[:1200] + f"\n[stashed as artifact {art_id}...]"
return inline, art_id
```

Decision sees a 1.2 kB head + the integer artifact id. Perception sees only the catalog entry. Bytes only re-enter decision's context when perception explicitly puts the id in `attach_artifact_ids`.

**Error handling:** `asyncio.wait_for(..., timeout=180)` — on timeout returns a `[tool_timeout]` result text + `None` artifact id. Decision can then pick a different tool.

---

### `artifacts.py` — RAM-only integer-ID blob store

**Responsibility:** Hand out monotonically increasing string IDs (`"1"`, `"2"`, `"3"`…) for blobs. Live in two dicts (`_blobs`, `_meta`). **No disk.** Persistence is explicitly the caller's choice if they want it.

**Why integers:** SHA-256 prefixes (the original design) caused weaker LLMs to hallucinate IDs ("attach=880c6b6177fc" looked very similar to "attach=a0d13b1d258d" — the model couldn't track which was current). Integer counters are unambiguous tokens.

**API:**
```python
put_bytes(data, *, descriptor, source) → str  # returns id as string
exists(art_id) → bool
get_bytes(art_id) → bytes
catalog() → list[{id, descriptor, size}]
reset() → None                                # for tests
```

---

### `agent6.py` — The orchestrator

**Responsibility:** The only file that crosses the artifact wall (from store back into decision's context). Manages the run lifecycle.

**Run lifecycle:**

```
1. memory.remember(query)               [classify user input → fact/scratchpad]
2. open MCP session, list tools, build ToolDef list
3. locked_goals = None                  [iter-1 freeze]
4. for it in 1..MAX_ITERATIONS:
   a. hits = memory.read(query, history)
   b. goal_list = perception.observe(...)
      - if first iter: snapshot to locked_goals
      - else: reconcile (only accept done/attach flips for known IDs)
   c. if all_done: break
   d. goal = next_unfinished()
   e. attached = []; for art_id in goal.attach_artifact_ids:
        attached.append((art_id, artifacts.get_bytes(art_id)))
   f. out = decision.next_step(goal, hits, attached, history, tools)
   g. if out.is_answer:
        history.append(answer); mark goal.done=True; continue
   h. result_text, art_id = action.execute(session, out.tool_call)
   i. memory.record_outcome(...); history.append(action)
5. answer = final_answer_from(history)
6. print FINAL ANSWER
```

**Key design:** Goal freezing after iter 1. The reconciler accepts only `done` and `attach_artifact_ids` updates for goal IDs it already knows. New / removed / reordered goals from later iters are silently dropped — implementing the lecture's "goals immutable in Session 6" rule deterministically rather than trusting the LLM to comply.

---

### `mcp_server.py` — 9-tool MCP stdio server

**Responsibility:** Expose 9 tools over stdio JSON-RPC.

**Tools:** `web_search` (Tavily primary, DDG fallback), `fetch_url` (crawl4ai), `get_time`, `currency_convert`, plus 5 sandboxed file ops.

**Critical fixes during the project:**
1. crawl4ai imported at module load under fd-level stdout redirect.
2. `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` to survive Windows `cp1252`.

These two fixes were what made the agent functional on Windows at all — without them, every fetch_url silently corrupted the RPC stream.

---

## 5. Architectural Evolution

| Axis | Start | End |
|---|---|---|
| Cognition | Single-prompt monolith (agent5 style) | 4-role PDA-M split, each in its own module with own LLM contract |
| Artifact IDs | (didn't exist) → SHA-256 hex prefix | Monotonic integers ("1", "2", "3"…) |
| Artifact storage | (didn't exist) → on-disk blob files | In-process dict (RAM-only) |
| Memory | (didn't exist) | JSON-backed, keyword-overlap read, LLM-classified write |
| Goal lifecycle | LLM emits goal list every iter (mutable) | Frozen after iter 1; reconciler accepts only done/attach flips |
| Attachment | `attach_artifact_id: Optional[str]` (single) | `attach_artifact_ids: list[str]` (multi) |
| Provider strategy | `auto_route` only | Per-stage: perception pinned to Gemini with fallback chain; decision/memory `auto_route` |
| Error visibility | `raise_for_status()` swallowed bodies | `GatewayError(status, url, body)` |
| Connection failures | Raw httpx tracebacks | Friendly `GatewayError(0, url, "gateway unreachable")` |
| Date context | None | Auto-injected `NOW: …` block in perception + decision |
| MCP stdio safety | Banner corruption, Unicode crashes | Fd-redirected import, UTF-8 stdio |
| Loop termination | Ran to MAX_ITERATIONS even with valid answer | Orchestrator flips goal `done` after answer; perception prompt forbids iter-1 done |

---

## 6. Important Engineering Concepts Used

### Pydantic boundary contracts

Every cross-module value is a Pydantic model — `Goal`, `ToolCall`, `Observation`, `MemoryItem`, `DecisionOutput`. Why it matters: type drift across modules is a class of bug that disappears entirely. The cost is one `model_validate` per boundary, well worth it for an agent that has to survive flaky LLM JSON.

### Cognitive separation (PDA-M)

Lecture's core idea. The four roles aren't just files — they're **separable failure domains**. A perception bug doesn't break action; a memory persistence failure doesn't lose the run's answer. Each module can use a different LLM tier (perception: Gemini; decision/memory: router).

### Artifact wall

A custom **content-addressing pattern**. Tool outputs > 4 KB become artifacts; only the orchestrator can re-materialize bytes; perception only sees IDs; decision only sees what the orchestrator hands it. Solves the "Wikipedia in the context" problem without needing summarization.

### Multi-provider failover with semantic predicate

Falling back isn't unconditional — `_should_fallback` distinguishes **recoverable** failures (worker-specific quota/JSON issues) from **real bugs** (auth errors, missing tools, generic 500s). Silent fallback for the former; loud failure for the latter.

### Goal immutability via orchestrator-side reconciliation

The lecture says "goals are immutable in Session 6." Rather than hope the LLM complies, the orchestrator freezes the iter-1 GoalList and **structurally drops** anything outside the original ID set on later iters. Trust the data structure, not the model.

### File-path module loading via `importlib.util.spec_from_file_location`

A small but important pattern — loads a Python file as a module without inserting its directory on `sys.path`. Avoids cross-tree name collisions (Session 6's `schemas.py` vs gateway's `schemas.py`).

### Stdio-aware subprocess hardening

The crawl4ai-banner-corruption fix is a textbook example of "you don't own the process's stdout when you're an MCP stdio server." Three-layer defense: import-time fd redirect + runtime fd redirect + Python TextIOWrapper UTF-8 reconfiguration.

### Date anchoring as context engineering

Rather than burn an iteration on `get_time`, the orchestrator computes `datetime.now()` (free, deterministic) and stamps it on every perception/decision call. Embodies the "context engineering" idea — context belongs to the orchestrator, not the LLM.

---

## 7. End-to-End Example Walkthrough

**Input:** `python agent6.py "Find 3 family-friendly Tokyo activities + check Saturday weather + recommend best one"`

**Step-by-step:**

1. **`agent6.main`** parses argv, calls `asyncio.run(run(query))`.

2. **`memory.remember(query)`** — LLM classifier sees the query, returns `kind="scratchpad"` (it's a request, not a fact). Not persisted; returns `None`.

3. **`mcp_session()`** spawns `mcp_server.py` as subprocess, opens stdio session.

4. **Iter 1:**
   - `memory.read` finds 0 hits (fresh run).
   - `perception.observe` (Gemini, with `NOW: Friday, 22 May 2026` in prompt) emits 3 goals:
     - "Retrieve 3 family-friendly Tokyo activities" — open
     - "Get Saturday 23 May 2026 weather for Tokyo" — open
     - "Recommend best activity based on activities + weather" — open
   - Orchestrator snapshots into `locked_goals`.
   - First open goal → decision.
   - Decision calls `web_search({'query': 'family friendly Tokyo activities'})`.
   - Action stashes 256 KB result as **artifact 1**, returns short head + id.

5. **Iter 2:**
   - perception flips goal 1 → done; attaches `[1]` to goal 3 (it already knows).
   - Decision on goal 2 calls `web_search({'query': 'Tokyo weather Saturday May 23'})`.
   - Action stashes ~9 KB result as **artifact 2**.

6. **Iter 3:**
   - perception flips goal 2 → done; updates goal 3's attachments to `[1, 2]`.
   - Orchestrator materializes both artifacts (262 KB + 9 KB of bytes) into decision's context.
   - Decision reads both, picks "23 May = Patchy rain possible 21°/15°" from weather table, cross-references with activities, answers: *"Given the rain forecast for Saturday, I recommend Tokyo Skytree (indoor) over Ueno Zoo (outdoor) or Sumo experience (mixed)…"*
   - Orchestrator flips goal 3 → done.

7. **Iter 4:** `all_done = True` → break.

8. `final_answer_from(history)` returns the most recent answer event's text.

9. **FINAL ANSWER** printed.

---

## 8. Final State / What Was Ultimately Built

### Capabilities

- **Architecturally clean PDA-M loop** wired to a multi-provider gateway with semantic failover.
- **Multi-step queries** (search → search → synthesize) complete in 4–5 iterations.
- **Cross-run memory persistence** with keyword retrieval (Run 1 stores a fact, Run 2 retrieves it).
- **Robust to free-tier reality** — Gemini quota burn, Groq JSON-mode failures, OpenRouter 503s all fall back transparently.
- **Robust to Windows reality** — crawl4ai banner corruption, `cp1252` Unicode crashes, missing `tzdata` package all handled.
- **Bounded context** — artifact wall keeps any single decision call under 8 K tokens regardless of how big tool outputs get.
- **Bounded execution** — 12-iter cap; 180-second per-tool timeout; goal immutability prevents infinite decomposition loops.
- **Diagnosable failures** — every gateway error carries the actual provider response body; connection failures get a one-liner not a stack dump.

### Lines of code (rough)

| Module | LoC | Role |
|---|---|---|
| `agent6.py` | ~220 | Orchestrator |
| `perception.py` | ~250 | Goal decomposition + verification |
| `decision.py` | ~140 | Tool selection / answer |
| `memory.py` | ~190 | Persistent memory |
| `action.py` | ~95 | Tool dispatch |
| `artifacts.py` | ~80 | RAM blob store |
| `schemas.py` | ~100 | Pydantic contracts |
| `_gateway.py` | ~60 | Gateway shim |
| `mcp_server.py` | ~310 | MCP tool server |
| **Total** | **~1,445** | |

---

## 9. Technical Lessons & Key Takeaways

1. **Process-boundary problems hide in unexpected places.** crawl4ai's banner at *import time* destroyed the JSON-RPC channel before any tool call ran. Lesson: subprocess servers must absorb every byte of unexpected stdout, including from third-party-library banners triggered by lazy imports.

2. **`raise_for_status()` is too thin for debugging gateways.** Always preserve the response body — that's where the real reason lives. Wrap it in a typed exception that carries `(status, url, body)`.

3. **Pinned providers + free-tier quotas don't mix.** "Always use the best provider" is great until the quota burns. Build a fallback chain that's loud about degradation (`(fell back to provider=gr)` line in the trace) but doesn't kill the run.

4. **Trust the data structure, not the model.** Goal immutability is implemented orchestrator-side, not just in the prompt. A bad LLM reply hits the reconciler and gets dropped silently.

5. **The right abstraction for big tool outputs is content addressing, not summarization.** The artifact wall lets the loop carry around references to 250 KB of Wikipedia markdown without ever putting it in a context that doesn't need it. Cheaper, exact, simpler.

6. **Integer IDs > hash IDs for LLM-handled tokens.** Weaker workers hallucinate long hex; short integers (1, 2, 3) are unambiguous and the model tracks them reliably.

7. **Recoverable vs. non-recoverable failures need an explicit predicate.** Without one, you either retry too aggressively (masking real bugs) or not at all (dying on transient quota errors). The predicate is one of the most-modified files in the project — a sign that classifying errors is genuinely hard.

8. **Context engineering > tool calls for things the orchestrator already knows.** Today's date doesn't need `get_time` — `datetime.now()` is free. Inject it.

9. **Defensive coding for missing optional dependencies.** `tzdata`-missing on Windows broke the agent immediately. A `try/except ZoneInfoNotFoundError` keeps the loop alive with a slight degradation.

10. **The fallback predicate is a living document.** Each new provider, each new failure mode adds a row. Treat it as the project's "things we've learned about provider failure shapes" registry.

---

## 10. Executive Summary (Non-Technical)

We built an **AI assistant** that can answer multi-step questions by autonomously deciding which web searches, page fetches, or file operations to run, then synthesizing a final answer.

**What's different from a regular chatbot:**

- **It plans before acting.** Given *"find 3 Tokyo activities, check Saturday weather, recommend the best one,"* it breaks the task into pieces, does them in order, then puts the answer together.
- **It has memory across sessions.** If you tell it your mom's birthday today, asking next week "when is mom's birthday?" works without re-telling it.
- **It handles failure gracefully.** Free AI APIs have daily limits, occasional outages, formatting quirks. Instead of giving up, the assistant retries on a different provider, switches tools, or degrades gracefully — and tells you when it's doing so.
- **It doesn't blow up its own brain.** When it scrapes a 250-page Wikipedia article, it doesn't try to remember the whole thing — it files it away with a number and only opens that file when it needs to read it.

**What was hard to get right:**

- Making four AI providers (Google's Gemini, Groq, OpenRouter, and others) fail over to each other when one is down, **without** masking real bugs.
- Stopping the AI from looping forever after already having the answer.
- Stopping the AI from re-trying the same broken URL three times in a row.
- Making it work cleanly on Windows where character encoding crashes, missing time-zone data, and a headless browser's startup banner all conspired to break it.

**Final result:** An agent that runs a multi-step real-world query (fetch → search → synthesize) in roughly 4 iterations, completes in under a minute, costs almost nothing per run, and survives the day-to-day reality of free-tier AI usage. The same code structure scales up to harder queries as the lecture's later sessions add parallel goal execution, RAG retrieval, and goal-rewrite mid-run.
