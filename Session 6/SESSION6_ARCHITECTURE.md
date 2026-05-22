# Session 6 Agentic AI Loop — Architectural Deep Dive

# 1. System Overview

## What this is

A **modular agentic AI executor** — call it `agent6` — that answers free-form natural-language queries by orchestrating four cognitive roles (Perception, Decision, Action, Memory) around a tool-bearing MCP server and a multi-provider LLM gateway. It is the Session 6 implementation in a course-led architecture progression where each session strips a coupling or adds a constraint.

Architecturally it is best described as:

- **Plan-Execute-Verify** in tight cycles (perception plans + verifies, decision executes one step, action dispatches the tool, memory remembers).
- **Tool-Calling Agent** via MCP stdio.
- **Multi-tier LLM Router** at the gateway (router-LLM picks worker tier per role).
- **Sequential single-goal-per-iteration** state machine (parallel execution arrives in later sessions).
- **In-process orchestrator** — no message queue, no DB, no Celery; everything is one Python event loop.

## Primary goal

Make a single-process agent that:

1. Decomposes a real-world query into atomic goals.
2. Calls one tool per iteration to make progress.
3. Survives free-tier LLM realities (quota burns, JSON-mode quirks, schema rejections).
4. Survives real-world web tools (silent Chromium downloads, 404 URLs, Unicode-hostile consoles).
5. Persists facts across runs via a keyword-indexed JSON memory.
6. **Never lets a tool output (250 KB Wikipedia markdown) corrupt the LLM's context window** — the *artifact wall* is the project's defining abstraction.

## Execution lifecycle

```
                                     ┌─── persistent JSON ───┐
                                     │  memory_store.json    │
                                     └───────────┬───────────┘
                                                 │
   user query                                    ▼
       │                                ┌── Memory ──┐
       ▼                                │ keyword    │
   memory.remember(query)  ───────►     │ overlap    │
   (classify: fact/scratchpad)          │ retrieval  │
       │                                └─────┬──────┘
       ▼                                      │
   open MCP stdio session                     │
       │                                      ▼
       ▼                              hits = memory.read(query, history)
   ┌─── per iteration (max 12) ──────────────────────────────────────┐
   │                                                                 │
   │   memory.read ─► hits                                           │
   │      │                                                          │
   │      ▼                                                          │
   │   perception.observe(query, hits, history, prior_goals)         │
   │      │     (Gemini-pinned w/ Groq/OpenRouter fallback)          │
   │      ▼                                                          │
   │   GoalList                                                      │
   │      │                                                          │
   │      ▼  (iter 1: freeze; later: reconcile against locked_goals) │
   │   if all_done → break                                           │
   │      │                                                          │
   │      ▼                                                          │
   │   attach artifacts (the loop crosses the artifact wall)         │
   │      │                                                          │
   │      ▼                                                          │
   │   decision.next_step(goal, hits, attached, history, tools)      │
   │      │     (auto_route="decision" — gateway picks worker)       │
   │      ▼                                                          │
   │   DecisionOutput                                                │
   │     ├── is_answer ──► history.append, goal.done = True          │
   │     └── tool_call  ──► action.execute(session, tool_call)       │
   │                            │                                    │
   │                            ▼                                    │
   │                       (result_text, artifact_id?)               │
   │                            │                                    │
   │                            ▼                                    │
   │                       memory.record_outcome(...)                │
   │                       history.append(action_event)              │
   └─────────────────────────────────────────────────────────────────┘
       │
       ▼
   final_answer_from(history) ──► FINAL ANSWER (printed, returned)
```

The crucial detail: **only the orchestrator crosses the artifact wall.** Perception sees IDs (`catalog`); decision sees the bytes the orchestrator materializes; action writes bytes to the store. No other code path can.

---

# 2. Contract-Level Architecture Explanation

## `schemas.py` — The Contract Spine

### Responsibility
Define every Pydantic type that crosses a module boundary. Single source of truth for *Goal*, *GoalList*, *ToolCall*, *DecisionOutput*, *MemoryItem*, *ToolDef*.

### Contract / Interface
Every module imports from `schemas` and from nothing else for cross-boundary types. The orchestrator passes Pydantic models, never dicts. Each model has documented invariants:

- `Goal` — `id` (8-char hex), `text` (one short imperative), `done` (boolean flag set only by orchestrator), `attach_artifact_ids` (list of validated string IDs that exist in `artifacts.catalog()`), `note` (free-form, may be empty).
- `DecisionOutput` — XOR contract: either `is_answer=True` with `answer: str`, or `is_answer=False` with `tool_call: ToolCall`. Decision MUST emit one or the other; both modes are mutually exclusive.
- `MemoryItem` — `kind ∈ {fact, preference, scratchpad, tool_outcome, answer}`, with `kind=scratchpad` semantically meaning "never persist." Retrieval is keyword-based; the `keywords` field is authoritative for matching.

### Internal Logic
Pure declarative — no behavior beyond Pydantic v2's validation, `default_factory` for collections, `_gen_id()` helper for 8-char hex.

### Why This Separation Exists
Without a central contract module, every module would redefine its own `Goal` shape. The first time perception returned `attach_artifact_id` and the orchestrator read `attach_artifact_ids`, the loop would silently drop attachments and we'd debug for hours. Centralized schemas make any drift a `ValidationError` at the boundary, not a silent data loss.

### What Would Break If Removed
- Perception's parsing fallback (which tries to coerce the gateway's raw JSON into Goals) would have no canonical target.
- The orchestrator's `goal_list.next_unfinished()` and `goal_list.all_done` invariants would be reimplemented inconsistently in three places.
- The Pydantic `Field(default_factory=list)` defaults that prevent accidental shared-list bugs (a classic Python footgun) would be lost.

---

## `_gateway.py` — Gateway Adapter & Error Surface

### Responsibility
Adapt the vendored `llm_gatewayV3/client.py` into the agent's import space without polluting `sys.path`. Convert every HTTP failure into a typed `GatewayError(status, url, body)` carrying enough information to debug or fall back.

### Contract / Interface
- **In:** Any caller can `from _gateway import LLM, GatewayError` and call `LLM().chat(...)`.
- **Out:** On success, dict reply from V3. On failure, a `GatewayError` with `(status, url, body)` populated.
- **Invariant:** Status `0` is the sentinel for connection refused (never a real HTTP code) — distinguishes "gateway is down" from "gateway is up and returned 5xx."

### Internal Logic
```python
class LLM(_RawLLM):
    def chat(self, *args, **kwargs):
        try:
            return super().chat(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            raise GatewayError(e.response.status_code,
                              str(e.request.url),
                              e.response.text[:2000]) from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise GatewayError(0, str(...), f"gateway unreachable — ...") from e
```

The module-load trick:
```python
spec = importlib.util.spec_from_file_location("_v3_client", _CLIENT_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
_RawLLM = mod.LLM
```

### Why This Separation Exists
Two real problems solved:
1. **Name collision** — `llm_gatewayV3/schemas.py` would shadow Session-6's `schemas.py` if we inserted the gateway folder on `sys.path`. Loading by file path keeps the trees isolated.
2. **Debuggability** — `raise_for_status()` discards the body. The body is exactly where "groq HTTP 400: json_validate_failed" lives. Without it, every 502/503 looks the same.

### What Would Break If Removed
The perception fallback chain reads `err.status` and `err.body` to decide whether to try the next worker. Without `GatewayError`, that decision can't be made — every failure would be either an opaque `httpx.HTTPStatusError` or an even uglier `httpcore.ConnectError`. Diagnostics collapse.

---

## `memory.py` — Persistent JSON Memory with Dual Write Paths

### Responsibility
Implement the lecture's dual-cost contract:
- **Read:** Free, no LLM, keyword overlap on `(query ∪ recent_history_tokens)`.
- **Write 1 (`remember`):** LLM-classified, called once per run on the user query.
- **Write 2 (`record_outcome`):** Cheap, no LLM, called per tool invocation.

### Contract / Interface
- `read(query, history, top_k=8) → list[MemoryItem]` — pure function over current store state.
- `remember(text, *, source, run_id) → MemoryItem | None` — `None` returned when classifier picks `scratchpad`. Persistence is idempotent w.r.t. the in-memory list but appends to the JSON on disk.
- `record_outcome(*, tool_call, result_text, artifact_id, run_id, goal_id) → MemoryItem` — always returns a persisted item with `kind="tool_outcome"`. No LLM call.

### Internal Logic
```
remember(text):
    trim(text → 800 chars)
    LLM.chat(classifier prompt, structured-output schema, auto_route="memory")
    parse → if kind=="scratchpad": return None
    else: stamp created_at, append to items, save JSON, return item

record_outcome(...):
    kw = tokenize(tool_call.name) | tokenize(args) | tokenize(result_text[:200])
    descriptor = f"{tool_call.name}({args}) -> {result_text[:160]}"
    item = MemoryItem(kind="tool_outcome", keywords=sorted(kw)[:12], ...)
    append, save, return
```

Why two write paths: the classifier on every tool outcome would burn an LLM call per iteration for zero semantic benefit. Tool outcomes are *categorically* tool outcomes — the model can't disagree.

### Why This Separation Exists
The lecture's central memory insight: **only the LLM can decide semantic kind**. Whether "John's office is in HSR" is a fact or a preference (or scratchpad) needs judgement. But whether "fetch_url('https://x.com') → 256kb of markdown" is a tool outcome is structural. Spending an LLM call on it is wasteful and slow.

### What Would Break If Removed
- Cross-run memory disappears (every run starts blind).
- The lecture's mom-birthday demo can't work (Run 1 persists the fact, Run 2 retrieves it).
- Trace events still exist but provide no learning across runs.

---

## `perception.py` — Plan, Verify, Attach

### Responsibility
Three jobs:
1. **Plan** — decompose query into 1–5 atomic goals.
2. **Verify** — read history, flip prior goals' `done` flags when history shows decision answered them.
3. **Attach** — set `attach_artifact_ids` on each goal so decision gets the right bytes next iter.

### Contract / Interface
- **In:** `(query, hits, history, prior_goals, run_id)`.
- **Out:** `GoalList`.
- **Invariants:**
  - On iter 1 (`prior_goals` empty), every emitted goal has `done=False`.
  - On later iters, goal IDs and texts MUST match `prior_goals` exactly (the orchestrator's reconciler enforces this — perception is told but the structure backstops).
  - `attach_artifact_ids` MUST be a subset of `artifacts.catalog()` IDs — perception's parsing drops any ID not in the catalog.
  - Returned even if the LLM fails — parsing falls back to keeping `prior_goals` unchanged, or creating a single goal from the raw query.

### Internal Logic
```
build user_block:
  NOW: <date anchor>
  USER_QUERY
  RECALLED_MEMORY (descriptors only, never bytes)
  PRIOR_GOALS (id, done, text)
  HISTORY (last 8 events)
  ARTIFACT_CATALOGUE (id, descriptor, size — NEVER bytes)

for provider in ["g", "gr", "or"]:  # gemini → groq → openrouter
    try:
        reply = LLM.chat(provider=p, response_format=GoalListSchema)
        break
    except GatewayError as e:
        if _should_fallback(e): continue
        else: raise

parsed = reply["parsed"] or json.loads(reply["text"]) or default
sanitize:
    drop empty-text goals
    drop invented artifact_ids
    coerce singular attach_artifact_id → list (backward compat)
return GoalList(goals=sanitized)
```

The `_should_fallback(err)` predicate is a separate intelligence — it distinguishes recoverable failures (quota, JSON-mode glitches, rate limits) from real bugs (auth, missing tools, generic 500s).

### Why This Separation Exists
Perception is the only stage that's allowed to *reason about the plan as a whole*. Decision sees one goal at a time. Memory has no view of goals. Action has no LLM. Putting planning anywhere else would either re-couple stages or split planning across two places — both architecturally worse.

### What Would Break If Removed
- The agent could only execute single-call queries.
- Multi-step synthesis (search + search + recommend) becomes impossible — there's no module that can say "you need both artifacts attached."
- Loop termination becomes timer-based (MAX_ITERATIONS) rather than goal-based.

---

## `decision.py` — One Goal → One Step

### Responsibility
Given exactly one open `Goal` plus its attached artifact bytes plus history plus the tool catalog, emit either an `answer` string or a single `tool_call`. Never both. Never multiple tool calls.

### Contract / Interface
- **In:** `(goal, hits, attached, history, tools)`.
- **Out:** `DecisionOutput(is_answer=True, answer=str) XOR DecisionOutput(is_answer=False, tool_call=ToolCall)`.
- **Invariants:**
  - If the gateway returns multiple tool_calls, decision **takes the first** silently — the loop only handles one at a time.
  - If decision returns an answer, it has explicitly chosen *not* to call a tool; the orchestrator records this and marks the goal done.
  - The model is instructed: "Never call a tool with arguments you saw fail in recent history."

### Internal Logic
```
build user_block:
  NOW: <date anchor>
  CURRENT_GOAL (id, text, note)
  RECALLED_MEMORY
  HISTORY (last 6 events)
  ATTACHED_ARTIFACTS (full bytes for any goal.attach_artifact_ids, truncated to 16k chars each)

reply = LLM.chat(
    messages=[{role: user, content: user_block}],
    system=DECISION_SYSTEM,
    tools=tools, tool_choice="auto",
    auto_route="decision",   # gateway picks worker by tier
    temperature=1, reasoning="off"
)

if reply.tool_calls:
    return DecisionOutput(is_answer=False, tool_call=ToolCall(reply.tool_calls[0]))
else:
    return DecisionOutput(is_answer=True, answer=reply.text.strip())
```

### Why This Separation Exists
Decision is intentionally *narrow*. It doesn't plan, it doesn't verify, it doesn't choose what to remember. It picks one tool or answers one goal. This narrow contract is what lets us swap workers per-call (perception=Gemini, decision=router-picked) without coupling.

### What Would Break If Removed
The agent would have nothing to call tools with. Perception can decide "fetch URL X" but cannot actually emit the tool call — that's decision's exclusive job. The artifact wall would also collapse: action stashes bytes, perception attaches IDs, but decision is the only consumer of attached bytes.

---

## `action.py` — Pure Dispatch + Artifact Wall (Write Side)

### Responsibility
Execute exactly one MCP tool call. Decide whether the result fits inline (< 4 KB) or needs to cross the artifact wall (≥ 4 KB → store, return short head + ID). No LLM.

### Contract / Interface
- **In:** `(session, tool_call)`.
- **Out:** `(result_text: str, artifact_id: str | None)`.
- **Invariants:**
  - Wall-clock cap of 180 seconds per call. On timeout, returns `[tool_timeout]` text + `None` ID.
  - On any exception, returns `[tool_error] ...` text + `None`. Never raises into the orchestrator.
  - Artifact threshold of 4000 chars matches the lecture's specification.

### Internal Logic
```python
async def execute(session, tool_call):
    try:
        result = await asyncio.wait_for(session.call_tool(...), timeout=180)
    except asyncio.TimeoutError:
        return "[tool_timeout] ...", None
    except Exception as e:
        return f"[tool_error] {e}", None

    text = _extract_text(result)
    if len(text) <= 4000:
        return text, None
    art_id = artifacts.put_bytes(text.encode(), descriptor=...)
    inline = text[:1200] + f"\n[stashed as artifact {art_id} — {len(text)} chars]"
    return inline, art_id
```

### Why This Separation Exists
Action has no judgement responsibility. It's purely a transport layer. Splitting it out from decision means:
- Decision can be tested without an MCP server (mock action).
- Action's timeout and error handling are bounded — every failure becomes data, never an exception.
- The artifact-wall write side has exactly one author.

### What Would Break If Removed
- Decision would have to handle MCP transport directly, including timeouts and the artifact-stash decision.
- Errors in the MCP layer would propagate as exceptions, breaking the loop's "every iteration must complete" invariant.

---

## `artifacts.py` — In-RAM Integer-ID Blob Store

### Responsibility
Store byte blobs with monotonically increasing integer IDs (`"1"`, `"2"`, `"3"`…). Provide read-by-ID and a metadata catalog.

### Contract / Interface
- `put_bytes(data, *, descriptor, source) → str` — returns ID. Idempotent w.r.t. data (well, actually not — every put gets a new ID; deduplication is a deliberate non-feature here, unlike the original SHA-256 design).
- `exists(art_id) → bool`
- `get_bytes(art_id) → bytes`
- `catalog() → list[{id, descriptor, size}]` — what perception sees.
- `reset()` — clear for tests.

### Internal Logic
```python
class ArtifactStore:
    def __init__(self):
        self._next_id = 1
        self._blobs = {}   # id → bytes
        self._meta  = {}   # id → metadata dict
```

### Why This Separation Exists
The artifact wall depends on having a clear "store" with a clean read/write API. Mixing this into action.py or memory.py would muddy responsibilities. Integer IDs were chosen specifically because *weaker LLMs hallucinate long hex strings* — short integer tokens (1, 2, 3) are trackable.

### What Would Break If Removed
- Any tool output > 4 KB would have to be embedded in the LLM context.
- A Wikipedia fetch (~250 KB) would consume the V3 gateway's HUGE-tier rejection limit (>8000 tokens).
- Perception would have no `ARTIFACT_CATALOGUE` to reference; multi-step synthesis dies.

---

## `agent6.py` — The Orchestrator (the only artifact-wall crosser)

### Responsibility
Drive the run lifecycle, enforce goal immutability, materialize artifacts into decision's context, record events, terminate cleanly.

### Contract / Interface
- **In:** `query: str`, optional CLI arg.
- **Out:** Final answer string (printed and returned).
- **Invariants:**
  - `memory.remember(query)` runs once at top of run (captures fact-assertions before the loop).
  - First iter's `GoalList` is snapshotted to `locked_goals`. All later iters reconcile against it (drop new/removed goals; accept only `done` and `attach_artifact_ids` flips for known IDs).
  - The orchestrator is the ONLY caller of `artifacts.get_bytes()`. Perception/decision/memory never call it.
  - On `decision.is_answer=True`, the orchestrator flips the matching goal's `done=True` *before* the next iter — deterministic done-tracking instead of relying on perception.

### Internal Logic — full lifecycle
```
1. run_id = uuid4().hex[:8]
2. history = []; prior_goals = []
3. memory.remember(query) — best-effort, wrapped in try/except
4. open MCP stdio session, list tools
5. locked_goals = None
6. for it in 1..MAX_ITERATIONS:
       hits = memory.read(query, history)
       goal_list = perception.observe(...)
       if locked_goals is None:
           locked_goals = list(goal_list.goals)
       else:
           reconcile (done & attach only, for known IDs)
       prior_goals = goal_list.goals
       if goal_list.all_done: break
       goal = goal_list.next_unfinished()
       attached = materialize(goal.attach_artifact_ids)
       out = decision.next_step(...)
       if out.is_answer:
           history.append(answer_event)
           flip goal.done = True in prior_goals
           continue
       result_text, art_id = await action.execute(session, out.tool_call)
       memory.record_outcome(...)
       history.append(action_event)
7. answer = final_answer_from(history)
8. print FINAL ANSWER
```

### Why This Separation Exists
Orchestration is a system-level concern, not a cognitive one. It owns:
- Loop termination (iteration cap, all_done check).
- Goal lifecycle (freeze, reconcile, mark done).
- The artifact wall crossing.
- Resource management (MCP session via async context manager).
- Run-level metadata (run_id, history accumulation).

Putting any of these into perception or decision would couple cognitive logic to system logic.

### What Would Break If Removed
There would be no system. The four cognitive modules are tools; the orchestrator is the agent.

---

## `mcp_server.py` — 9-Tool MCP stdio Server

### Responsibility
Expose 9 callable tools over JSON-RPC stdio. Sandbox file operations under `./sandbox/`.

### Contract / Interface
- **In:** Tool call via MCP `call_tool(name, arguments)`.
- **Out:** Structured result (text/dict/list) per tool.
- **Invariants:**
  - All file paths are normalized through `_safe(path)` which validates non-escape from the sandbox.
  - `fetch_url` always returns `{status, content_type, length_bytes, text}` regardless of HTTP status (404s return `200`-shaped dict with `status=404`).
  - `web_search` caps at 5 results (Tavily pricing); Tavily primary with DDG fallback.

### Why It Lives in a Separate Process
MCP stdio decouples tool lifecycle from agent lifecycle. crawl4ai loads a headless Chromium; tavily holds an HTTP client; ddgs has its own subprocess. Putting all of this in the agent process would make a single tool crash kill the agent. Subprocess isolation is real-world correct.

---

# 3. Architecture Evolution

## Version 1 — Monolithic single-prompt loop (`agent5.py` style)

### Original Problem
Single file, single system prompt, single LLM call per turn. Native tool-use lets the model pick tools, but there's no plan, no verifier, no memory, no artifact wall. A Wikipedia fetch returned the entire markdown into the conversation, eating context and risking truncation. The "verifier" was a separate structured-output call after the loop ended.

### Change Introduced
Split into **Perception / Decision / Action / Memory** modules, each with its own LLM call shape (or no LLM at all). Introduced `Goal` as the atomic unit of work. Made the orchestrator the only artifact-wall crosser.

### Why This Was Better
- Each cognitive role can use a different worker tier (perception=Gemini, decision/memory=router-picked).
- Bugs in one role can't corrupt another (perception emits a bad GoalList → reconciler drops it; decision picks a wrong tool → action timeouts handle it).
- The artifact wall keeps context bounded regardless of tool output size.

### Example Scenario
**Before:** "Fetch Shannon Wikipedia and extract dates" — single LLM call sees 256 KB of markdown in its history, may truncate, certainly burns context budget.

**After:** Action stashes the 256 KB as artifact 1, decision sees a 1.2 KB head + ID, perception attaches ID 1 to the "extract dates" goal, decision now has full bytes for that one call. Context budget consumed only when needed.

### Hidden Benefit
**Composability for later sessions.** Session 9 adds parallel goal execution — that's a single change in the orchestrator (replace `next_unfinished()` with a DAG executor); none of the cognitive modules change.

---

## Version 2 — Pydantic contracts at every boundary

### Original Problem
Early versions passed dicts between modules. Type drift bugs surfaced as silent data loss — perception emits `attach_artifact_id` (singular), orchestrator reads `attach_artifact_ids` (plural), the attachment vanishes, decision can't synthesize, and the only symptom is a wrong final answer.

### Change Introduced
All cross-module values became Pydantic v2 models defined in `schemas.py`. Every boundary does `Model.model_validate(raw)` or `Model(...)`. The gateway's structured-output replies validate against `model_json_schema()` on the wire.

### Why This Was Better
- Type drift is now a `ValidationError`, not a silent data loss.
- The Pydantic `default_factory=list` idiom prevents the shared-default-list footgun.
- IDE / type-checker support across the codebase.

### Example Scenario
Perception's structured-output reply from Groq once returned `attach_artifact_id` (singular) because the worker had been trained on older schemas. The Pydantic schema validation rejected it; the gateway's corrective-retry got a valid reply; the loop continued.

### Hidden Benefit
**Testability.** Every module can be unit-tested by constructing Pydantic fixtures. No need for a live gateway to exercise perception's parsing or decision's tool selection.

---

## Version 3 — Integer artifact IDs (was SHA-256 hex)

### Original Problem
Iter 9 of the Shannon run showed perception attaching `880c6b6177fc` to one goal and `a0d13b1d258d` to a duplicate goal it had just invented. The two hex strings looked similar enough to the model that it couldn't track which was current. Decision then fetched the wrong artifact.

### Change Introduced
Replaced content-addressed SHA-256 prefixes with a monotonic integer counter. IDs become `"1"`, `"2"`, `"3"`.

### Why This Was Better
Weaker LLMs reliably handle short integer tokens. They hallucinate long hex digits.

### Example Scenario
**Before:** Perception emits `attach=880c6b6177fc` for the wrong artifact, decision fetches the wrong data.

**After:** Perception sees catalog `[{id: "1", desc: "fetch_url(en.wikipedia.org/Claude_Shannon)"}, {id: "2", desc: "fetch_url(en.wikipedia.org/Information_theory)"}]` — emits `attach=["1"]` deterministically.

### Hidden Benefit
**Debuggability.** A trace that says `attach=1,2` is readable; one that says `attach=880c...,a0d1...` requires cross-referencing the catalog mentally.

---

## Version 4 — Goal immutability via orchestrator-side reconciliation

### Original Problem
Perception, given a "stuck" open goal, would occasionally **invent a new goal** that paraphrased the stuck one. The agent would then have two near-duplicate goals competing for tool calls.

### Change Introduced
The orchestrator freezes the first iter's `GoalList` to `locked_goals`. All later perception replies are reconciled: only `done` and `attach_artifact_ids` updates for known IDs are kept; new/removed/reordered goals are dropped.

### Why This Was Better
The data structure enforces immutability; we don't trust the LLM to comply with a prompt rule.

### Example Scenario
**Before:** Iter 9 shows three goals where the original was two. The third is a paraphrase. Decision now has ambiguity about which goal to satisfy.

**After:** Iter 9 perception emits three goals; reconciler drops the third because its ID isn't in `locked_goals`. The original two-goal plan persists.

### Hidden Benefit
**Deterministic termination.** With immutable goals, `all_done` is a stable termination criterion. With mutable goals, perception could add a new goal mid-run and the loop would never settle.

---

## Version 5 — Per-call provider fallback chain (perception)

### Original Problem
Pinning perception to Gemini per the lecture's rule caused every Gemini outage (quota burned, rate-limit) to kill the run. Gateway treats explicit-provider failures as non-retryable 502s.

### Change Introduced
`PERCEPTION_PROVIDERS = ["g", "gr", "or"]` plus a `_should_fallback(err)` predicate. Perception loops through providers in order, only advancing when the error signature is recoverable.

### Why This Was Better
- Gemini is still tried first, honoring the lecture's preference.
- The run survives transient outages.
- Real bugs (auth errors, missing tools) still surface — fallback is conditional, not blanket.

### Example Scenario
**Before:** Gemini 429 quota → 502 from gateway → run dies.

**After:** Gemini 429 → predicate returns True → try Groq → Groq succeeds → run continues. Trace line `[perception] (fell back to provider=gr)` makes the degradation visible.

### Hidden Benefit
**Observable degradation.** The trace explicitly tells you when the agent is operating below ideal — useful for debugging and for users to understand why answers might be slightly less precise on quota-burned days.

---

## Version 6 — Multi-attach (single artifact → list of artifact IDs)

### Original Problem
The Tokyo synthesis goal needed both artifact 1 (activities) and artifact 2 (weather) but the schema allowed only one. Decision saw only weather and tried to re-fetch activities, hit 404, looped.

### Change Introduced
`Goal.attach_artifact_id: str` → `Goal.attach_artifact_ids: list[str]`. Updated schema, perception parsing, orchestrator iteration, decision rendering.

### Why This Was Better
Synthesis goals are a real pattern (recommend X given A and B; compare A vs B; summarize A through E). Single-attach is a fundamental design limit.

### Hidden Benefit
**Encodes the multi-input nature of synthesis explicitly.** A reviewer reading `attach_artifact_ids: list[str]` understands the agent supports multi-source synthesis at a glance.

---

## Version 7 — Date-anchor injection

### Original Problem
"This weekend" / "Saturday" / "tomorrow" queries failed because the LLM doesn't know today. It would do calendar arithmetic ("2024-01-01 was Monday...") and trip itself up.

### Change Introduced
Orchestrator stamps `NOW: Friday, 22 May 2026, 00:23 (Asia/Kolkata, ISO 2026-05-22)` at the top of every perception and decision user_block.

### Why This Was Better
- Free, no LLM call, no extra iteration.
- Always-on context; perception can't forget to ask.

### Example Scenario
**Before:** "Recommend an indoor activity for this Saturday" — agent guesses a Saturday from training data, picks irrelevant weather.

**After:** Agent sees `NOW: Friday 22 May 2026`, derives "this Saturday = 23 May 2026", looks up 23 May in the weather artifact, decides correctly.

### Hidden Benefit
**Context engineering pattern.** Encodes the principle that the orchestrator owns context derivation, not the model. Future temporal extensions (timezone-aware queries) plug into the same hook.

---

## Version 8 — Hardened MCP stdio (crawl4ai banner + UTF-8)

### Original Problem
crawl4ai's `[INIT] → Crawl4AI 0.8.6` banner printed at module-load corrupted the JSON-RPC channel. Then Rich's `→` character crashed on Windows `cp1252`.

### Change Introduced
- Module-load `os.dup2(2, 1)` around the crawl4ai import.
- `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` at MCP server start.

### Why This Was Better
The first byte the MCP client received was a valid JSON-RPC frame, not a Rich banner. The Unicode arrow no longer crashed the encoder.

### Hidden Benefit
Documents an MCP-stdio engineering principle: **anything you import that might write to stdout must be wrapped at import time, not at use time.**

---

# 4. Scenario-Based Execution Walkthroughs

## Scenario 1 — Simple cross-run memory retrieval ("When is my mom's birthday?")

**User Input:** `"When is my mom's birthday?"` (where Run 1 previously persisted "Mom's birthday is 15 May 2026" as a fact)

### Step 1 — Top of `run()`
`memory.remember(query)` classifier sees a question, returns `kind="scratchpad"` → not persisted. Returns `None`. Logged: `[memory.remember] skipped — classified scratchpad`.

### Step 2 — Iter 1: memory.read
Keyword tokens: `{mom, birthday}`. Overlaps with the persisted fact's keywords `{mom, birthday, may, 2026}` (score=2). Returns 1 hit.

### Step 3 — Iter 1: perception
With the sharpened prompt: "done means HISTORY contains evidence — memory is context, not evidence — iter 1 history is empty so every goal must start `done: false`."

Emits: `[Goal(id="abc12345", text="Provide mom's birthday date", done=False, attach_artifact_ids=[])]`.

### Step 4 — Iter 1: orchestrator
`locked_goals` = snapshot. `all_done` = False. `next_unfinished()` = the one goal. No artifact attachment.

### Step 5 — Iter 1: decision
Reads `RECALLED_MEMORY: [fact] Mom's birthday is 15 May 2026 (keywords: mom, birthday, may, 2026)`. Sees no artifact attached. System prompt rule: "If RECALLED_MEMORY contains the info for this goal, ANSWER from it."

Returns `DecisionOutput(is_answer=True, answer="Mom's birthday is 15 May 2026.")`.

### Step 6 — Iter 1: orchestrator post-decision
- `history.append({kind: "answer", text: ..., goal_id: "abc12345"})`.
- Mutate goal in `prior_goals` to `done=True`.
- continue.

### Step 7 — Iter 2: perception
`prior_goals` has the goal marked done. Perception returns the same list with `done=True`. Reconciler accepts the flip.

`all_done = True` → break.

### Step 8 — Final answer
`final_answer_from(history)` returns the most recent answer event's text.

**Contracts enforced:**
- Iter-1 `done=False` (perception prompt rule).
- Reconciler accepts only `done` and `attach` updates.
- Decision answers from memory when sufficient (system prompt rule).

**Edge case handled:** If memory had been empty, decision would have called a tool (e.g., `read_file` on a reminder file) and the loop would have done 2 iters instead of 1.

---

## Scenario 2 — Multi-step synthesis with artifact wall ("Tokyo activities + Saturday weather + recommend")

**User Input:** `"Find 3 family-friendly Tokyo activities + check Saturday weather + recommend best one"`

### Step 1 — Top of `run()`
`memory.remember(query)` classifies as `scratchpad` (question/request). Nothing persisted.

### Step 2 — Iter 1: perception (with date anchor)
Prompt includes `NOW: Friday, 22 May 2026`. Emits:
- Goal 1: "Retrieve 3 family-friendly Tokyo activities" — open
- Goal 2: "Get Saturday 23 May 2026 weather forecast for Tokyo" — open
- Goal 3: "Recommend best activity based on activities and weather" — open

### Step 3 — Iter 1: decision (Goal 1)
Calls `web_search({query: "family friendly Tokyo activities 23 May"})`.

### Step 4 — Iter 1: action
Tavily returns ~10 KB of structured results. > 4 KB threshold → stash as **artifact 1**. Returns 1.2 KB head + ID `"1"`.

### Step 5 — Iter 2: perception
Sees history has an action for Goal 1 with `artifact_id=1`. Flips Goal 1 → `done=True`. Sets `attach_artifact_ids=["1"]` on Goal 3 (anticipating synthesis). Goal 2 still open.

### Step 6 — Iter 2: decision (Goal 2)
Calls `web_search({query: "Tokyo weather Saturday 23 May 2026"})`.

### Step 7 — Iter 2: action
Returns ~9 KB of weather data → stash as **artifact 2**.

### Step 8 — Iter 3: perception
History now has both actions. Flips Goal 2 → `done=True`. Updates Goal 3's `attach_artifact_ids=["1", "2"]`.

### Step 9 — Iter 3: orchestrator materializes both artifacts
```python
for art_id in goal.attach_artifact_ids:  # ["1", "2"]
    attached.append((art_id, artifacts.get_bytes(art_id)))
print("[attach] 1 (10432 bytes)")
print("[attach] 2 (8694 bytes)")
```

### Step 10 — Iter 3: decision (Goal 3 with both artifacts)
User_block includes both artifact bodies (each truncated at 16 KB). Decision reads:
- Artifact 1: 3 activity options with descriptions.
- Artifact 2: weather table showing 23 May = "Patchy rain possible 21°/15°".

Returns `is_answer=True`, `answer="Given the patchy rain forecast for Saturday 23 May, I recommend Tokyo Skytree Observatory (indoor) over the Ueno Zoo (outdoor) or sumo experience (mixed)."`

### Step 11 — Iter 4: perception → all_done → break
Final answer printed.

**Contracts enforced:**
- Action threshold (4 KB) for artifact stash.
- Perception sees catalog (id/descriptor/size) but never bytes.
- Multi-attach delivers both artifacts to one decision call.
- Date anchor lets perception/decision agree on "Saturday = 23 May 2026."

**Edge case handled:** Iter 2 perception could have attached artifact 1 to Goal 3 even though it doesn't need it yet — that's fine; the loop only materializes when the orchestrator selects Goal 3 as `next_unfinished()`.

---

## Scenario 3 — Provider failure cascade (Gemini quota burned mid-run)

**Setup:** Gemini's 20-call daily quota is exhausted before this run starts. Groq and OpenRouter are healthy.

### Iter 1: perception attempt
- Try `provider="g"` → gateway responds `502: {"detail":"gemini failed: HTTP 429: Quota exceeded ..."}`.
- `_should_fallback(err)`:
  - status 502: yes (in allowed set).
  - body contains "quota": yes.
  - returns True.
- Try `provider="gr"` → Groq returns valid structured output.
- Print `[perception] (fell back to provider=gr)`.

### Iter 2-N: perception continues
Same cascade. Gemini is tried again on every call (it might recover); Groq picks up every time it's still 429. The run completes on Groq.

**Contracts enforced:**
- `_should_fallback` distinguishes recoverable (quota) from non-recoverable (auth 403).
- Gemini is preferred but never required.

**Decision/memory unaffected:** They use `auto_route` which the gateway handles internally with tier-based failover. They don't need their own fallback chain.

---

## Scenario 4 — Tool failure (404 URL) and decision adaptation

**Setup:** Decision called `fetch_url(https://example.com/dead-page)`. Server returns 404.

### Action layer
`crawl4ai.AsyncWebCrawler.arun(url)` returns `r.status_code = 404` with whatever 404-page text the server returned. Action treats it like any other result — extracts text, decides on artifact threshold, returns.

So the 404 isn't an exception. It's a normal-shaped result with `"status": 404` in the dict and the body text in `"text"`.

### Decision (next iter)
Sees in history:
```
iter N action fetch_url(https://example.com/dead-page) -> {"status": 404, "text": "<Page not found>..."}
```

Decision's system prompt rule: "Never call a tool with arguments you saw fail in recent history."

So decision picks a different tool or different URL. If perception's goal is "fetch X," decision might emit a `web_search` for X instead, or answer with a graceful failure note.

**Contracts enforced:**
- Action never raises; tool errors become data.
- Decision is told to read history for failed-tool signals.

**Edge case:** Early in the project decision did get stuck retrying the same 404. The system-prompt fix in Step 12 addressed it.

---

## Scenario 5 — Hallucinated artifact ID

**Setup:** Perception's LLM, under load, returns `attach_artifact_ids: ["7"]` when only artifacts `"1"` and `"2"` exist in the catalog.

### Perception parser (defensive)
```python
valid_artifact_ids = {c["id"] for c in artifact_store.catalog()}
arts = [str(a) for a in arts_raw if a and str(a) in valid_artifact_ids]
# "7" not in {"1", "2"} → dropped
```

Goal ends up with `attach_artifact_ids=[]`. Orchestrator attaches nothing. Decision sees no attachments and either calls a tool or asks for clarification.

**Contracts enforced:** Perception parser is the firewall against invented IDs.

**Hidden benefit:** This is why integer IDs matter — the model is far less likely to invent a small integer than a long hex string.

---

## Scenario 6 — Gateway unreachable

**Setup:** User starts `agent6.py` but forgets to start `llm_gatewayV3/main.py`.

### `_gateway.LLM.chat` first call
`httpx.post(localhost:8101/v1/chat)` raises `httpx.ConnectError: [WinError 10061] connection refused`.

Caught by `_gateway`:
```python
except (httpx.ConnectError, httpx.ConnectTimeout) as e:
    raise GatewayError(0, str(...), "gateway unreachable — is `python llm_gatewayV3/main.py` running on the expected port? underlying: ...")
```

Bubbles up through perception's first call. Perception's `_should_fallback` checks status 0 — not in `(400, 408, 429, 500, 502, 503, 504)` — returns False. Re-raises.

`agent6.run()` sees the exception bubble up out of the MCP context manager. Stack trace shows the friendly "gateway unreachable" message at the top.

**Contracts enforced:** Status `0` sentinel signals connection refused. Predicate explicitly does NOT fall back on it (different gateway URLs aren't going to help).

---

## Scenario 7 — Loop running to MAX_ITERATIONS without termination

**Setup:** Decision keeps emitting tool calls (never `is_answer=True`).

### Iters 1-12
Each iter calls a tool. Action stashes results, memory records outcomes, history grows. No answer event ever appears.

### After iter 12
`for it in range(1, MAX_ITERATIONS + 1)` exhausts. Python's `for-else` triggers:
```python
else:
    print(f"\n[stop] reached MAX_ITERATIONS={MAX_ITERATIONS}")
```

`final_answer_from(history)` returns `"(no answer was produced)"` because no answer event exists.

**Contracts enforced:** Hard iteration cap prevents infinite loops. Pristine termination message tells the user what happened.

**Improvement opportunity:** A "summarize history and answer" fallback at iter 12 would salvage some value from runs that almost-but-didn't terminate.

---

# 5. Responsibility Mapping

| Component | Responsibility | Depends On | Used By | Failure Impact |
|---|---|---|---|---|
| `schemas.py` | Pydantic contracts | Pydantic v2 only | every module | Compile-time everywhere; can't import any module |
| `_gateway.py` | LLM client adapter + error surface | `httpx`, `llm_gatewayV3/client.py` | `perception`, `decision`, `memory` | All LLM calls fail; loop dies on first call |
| `artifacts.py` | RAM blob store with integer IDs | nothing | `action`, `agent6`, `perception` | Artifact wall collapses; large tool outputs overflow context |
| `memory.py` | Persistent JSON memory; read=free, write=LLM | `_gateway`, `schemas`, JSON file | `agent6`, `perception` | No cross-run learning; no recall hits in perception |
| `perception.py` | Plan + verify + attach (Gemini-pinned) | `_gateway`, `artifacts.catalog`, `schemas` | `agent6` | Loop has no goal list; can't decompose query |
| `decision.py` | Single step: answer XOR tool_call | `_gateway`, `schemas` | `agent6` | No tools called; no answers produced |
| `action.py` | Pure MCP dispatch + artifact-wall write | `mcp.ClientSession`, `artifacts`, `schemas` | `agent6` | No tools execute; loop can't make progress |
| `agent6.py` | Orchestration + goal freezing + artifact crossing | every other module | user (CLI) | The system doesn't exist |
| `mcp_server.py` | 9-tool stdio MCP server | `crawl4ai`, `tavily`, `ddgs`, `fastmcp` | `agent6` (over stdio) | Tools unavailable; agent can only answer from memory |

## Ownership boundaries

- **Cognitive logic** (planning, deciding, classifying): perception, decision, memory.
- **System mechanics** (loop, MCP session, artifact crossing): agent6.
- **Transport** (HTTP, stdio, encoding): `_gateway`, action, `mcp_server`.
- **Contracts** (types): schemas, exclusively.

## Coupling map

```
schemas ◄──────────── (everyone)        loose; central type registry
_gateway ◄────────── perception, decision, memory     network adapter
agent6 ─────► everyone                  orchestration is the only authority
action ◄────► artifacts                 write side of the wall
perception ────► artifacts.catalog      read-only metadata (no bytes!)
```

The directionality matters. `schemas` is a dependency hub everyone imports from but nothing imports back into. `agent6` is the only module that touches every other module. No cognitive module depends on another cognitive module.

---

# 6. Data Flow & State Management

## State containers

| State Object | Lifetime | Persistence | Owner |
|---|---|---|---|
| `history: list[dict]` | One run | None (lost on exit) | `agent6.run()` |
| `prior_goals: list[Goal]` | One run | None | `agent6.run()` (mutated in place) |
| `locked_goals: list[Goal] \| None` | One run, set on iter 1 | None | `agent6.run()` |
| `memory.items: list[MemoryItem]` | Process lifetime + JSON | `./memory_store.json` | `memory.Memory` singleton |
| `artifacts._blobs: dict[str, bytes]` | Process lifetime | None (RAM only) | `artifacts.ArtifactStore` singleton |
| MCP session | One run (`async with`) | None | `agent6.mcp_session()` |
| Conversation messages in decision | One iter | None | constructed per `decision.next_step()` call |

## Data flow diagram

```
                                  ┌───────────────────┐
                                  │  memory_store.json │
                                  └─────────┬─────────┘
                                            │ load on import
                                            ▼
                                  ┌───────────────────┐
                                  │  Memory singleton │ ◄─── record_outcome()
                                  │  .items[]         │      remember()
                                  └─────────┬─────────┘
                                            │
                                            ▼ (read by every iter)
   user query                          hits[]
       │                                  │
       ▼                                  ▼
  ┌─── agent6.run() ───────────────────────────────────────────────────┐
  │                                                                   │
  │  history: list[dict]    ◄────────────┐                             │
  │  prior_goals: list[Goal] ◄──────────┐│                             │
  │  locked_goals: list[Goal] ◄────────┐││                             │
  │                                    │││                             │
  │      ┌── perception.observe(query, hits, history, prior_goals) ──┐ │
  │      │   reads everything,                                       │ │
  │      │   returns GoalList                                        │ │
  │      └────────────────────┬──────────────────────────────────────┘ │
  │                           │                                        │
  │                           ▼                                        │
  │   reconcile with locked_goals (iter > 1)                           │
  │                           │                                        │
  │                           ▼                                        │
  │   if all_done → break; else select goal                            │
  │                           │                                        │
  │                           ▼                                        │
  │   attach = [(art_id, artifacts.get_bytes(art_id))                  │
  │             for art_id in goal.attach_artifact_ids]                │
  │                           │                                        │
  │                           ▼                                        │
  │      ┌── decision.next_step(goal, hits, attach, history, tools)──┐ │
  │      │  returns DecisionOutput                                   │ │
  │      └────────────────────┬──────────────────────────────────────┘ │
  │                           │                                        │
  │              ┌────────────┴────────────┐                           │
  │              ▼                         ▼                           │
  │      is_answer=True              tool_call                         │
  │      append(answer event)        action.execute(...)               │
  │      flip goal.done             ──► (text, art_id?)                │
  │              │                   memory.record_outcome             │
  │              │                   append(action event)              │
  │              ▼                         │                           │
  │           continue ◄───────────────────┘                           │
  └─────────────────────────────────────────────────────────────────────┘
```

## Context propagation

Each LLM call gets a **purpose-built user_block** assembled by the calling module:

- **Perception user_block:** date anchor + USER_QUERY + RECALLED_MEMORY + PRIOR_GOALS + HISTORY (last 8) + ARTIFACT_CATALOGUE.
- **Decision user_block:** date anchor + CURRENT_GOAL + RECALLED_MEMORY + HISTORY (last 6) + ATTACHED_ARTIFACTS (full bytes, truncated at 16 KB each).
- **Memory classifier user_block:** "Classify this text:\n{text[:800]}".

Crucially, no module sees a state that contains both *bytes* and *catalog*. Perception sees the catalog but never the bytes. Decision sees only the bytes the orchestrator attached, never the full catalog.

## Consistency invariants

- `len(locked_goals) ≥ len(goal_list.goals)` after reconciliation — perception can never grow the goal count after iter 1.
- Every `attach_artifact_ids` element is in `artifacts.catalog()` IDs.
- `history` events are append-only — never mutated, never reordered.
- `Memory.items` are stamped with `created_at` UTC timestamp; sort order is meaningful.

---

# 7. Agent Behavior Intelligence

## Planning strategy

Perception emits 1–5 atomic goals. "Atomic" means *one tool call satisfies it*. A query like "find activities AND get weather AND recommend" naturally maps to 3 goals because each is one tool call OR one synthesis. The prompt enforces this: *"each goal the action stage can tackle with ONE tool call (or zero if already answered)."*

## Reasoning strategy

Decision is **non-reflective**. It looks at one goal, one attached blob, and recent history, and picks the next step. No tree-of-thought, no chain-of-thought, no self-critique. Reflection lives in perception's "verify by reading history" pass on the next iter.

This split is deliberate: reflection is expensive (it requires the strong model); execution is cheap (router-picked worker). Decision happens N times per run; perception happens N+1 times. Putting reflection in decision would multiply the cost of every tool call.

## Tool arbitration

The MCP server publishes 9 tools. Decision sees them as `ToolDef` envelopes (name + description + input_schema). The model picks via native tool-use. Two prompt-level guardrails:

1. *"Prefer one tool call over guessing when context genuinely lacks info."* — Combats the model's tendency to answer from training data.
2. *"Never call a tool with arguments you saw fail in recent history."* — Combats the 404-retry loop.

## Memory retrieval

Keyword overlap, no embeddings, no LLM. Score = `|query_tokens ∩ item_tokens|`. Tied scores break by `created_at` descending (newest wins). This is intentionally simple — RAG is Session 10+.

The hidden insight: **keywords are the LLM-classified output** of `memory.remember`. So the LLM does the semantic work *once*, at write time, and stores the result as plain tokens. Reads cost nothing.

## Hallucination prevention

Three layers:

1. **Schema validation** at perception/memory boundaries (Pydantic + JSON-schema).
2. **Artifact ID validation** in perception parser — invented IDs dropped before they reach the orchestrator.
3. **Date anchor injection** — model never has to guess today's date.

Plus the implicit fourth layer: **the artifact wall**. The model can't hallucinate that it read Wikipedia's content if Wikipedia isn't in its context. It either fetched the artifact (gets the bytes), or it didn't (works from catalog descriptor + acknowledges).

## Structured outputs

Perception and memory use JSON-schema-enforced structured output via the gateway. The gateway:
- Validates the worker's output against the schema.
- On validation failure, runs a single corrective retry: *"Your previous reply did not match the required JSON schema: {error}. Reply ONLY with valid JSON conforming to the schema."*
- On second failure, raises 503 — caught by our perception fallback chain → tries next worker.

## Confidence handling

Implicit. Decision either answers or doesn't. Perception either flips a goal done or doesn't. There is no `confidence: float` field anywhere — it would just be more data the model could lie about. Better to let outputs be deterministic and let the orchestrator's state machine drive termination.

---

# 8. Deep Technical Concepts Used

| Concept | Definition | Where used | Why valuable |
|---|---|---|---|
| **Pydantic v2 validation** | Runtime type checking via JSON schemas | Every boundary | Type drift becomes a fast-failing exception, not silent data loss |
| **Adapter pattern** | Wrapping a third-party class to fit a project's contract | `_gateway.LLM` wrapping `_RawLLM` | Lets us inject `GatewayError` translation without modifying the vendored client |
| **Singleton** | One instance per process, module-level | `memory.memory`, `artifacts.artifacts` | Single source of truth for state that's intrinsically global; testable via `reset()` |
| **Content addressing** (then abandoned) | Hash-based blob IDs | Original `artifacts.py` | Original design; abandoned because hex strings confused weak LLMs |
| **Monotonic counter** | Integer IDs | Current `artifacts.py` | LLMs reliably track short integer tokens; debuggable traces |
| **MCP (Model Context Protocol)** | stdio JSON-RPC for tool servers | `mcp_server.py` | Tool process isolation; tool crashes don't kill the agent |
| **Tool calling / native function call** | LLM emits structured tool invocations | `decision.next_step()` | No regex parsing; gateway translates per-provider dialects |
| **Structured generation** | LLM constrained to JSON schema | Perception + memory | Schema-validated outputs; corrective retry on failure |
| **Adaptive retry / fallback** | Provider chain with semantic predicate | `perception._should_fallback` | Recoverable failures fall through; real bugs surface |
| **Circuit-breaker-lite** | Status tracking + backoff | V3 gateway internal | Provider-specific cooldowns; quota burns trigger 60s backoff |
| **Async/await + TaskGroup** | Concurrent I/O via asyncio | Earlier `dispatch_tool_calls` (V5); now sequential | Sequential per Session-6 spec |
| **Async context managers** | `async with` for resource cleanup | `mcp_session()` | Guaranteed cleanup of subprocess + streams even on exception |
| **Sandboxed file operations** | `_safe(path)` non-escape check | `mcp_server.py` file tools | Prevents path traversal attacks |
| **File-path module loading** | `importlib.util.spec_from_file_location` | `_gateway.py` | Avoids `sys.path` collisions across trees |
| **Fd-level stdout redirection** | `os.dup2` | `mcp_server.py` crawl4ai import | Intercepts Rich library output that contextlib.redirect_stdout can't catch |
| **Defensive imports** | `try/except ImportError/ZoneInfoNotFoundError` | `perception._now_block` | Graceful degradation when optional deps missing |
| **State-machine loop** | Bounded iteration with explicit termination | `agent6.run()` | 12-iter cap + goal-based termination prevents infinite loops |
| **Immutable goal list** | Snapshot + reconcile | `agent6.run()` | LLM can't invent or rearrange goals mid-run |
| **Plan-and-execute** | Perception plans, decision executes | The PDA-M split itself | Separates expensive reasoning from cheap execution |
| **Best-effort persistence** | try/except around memory.remember | `agent6.run()` | A failed classifier doesn't lose the actual answer |
| **Trace events as append-only log** | `history.append(event)` | `agent6.run()` | History is the agent's ground truth for verification |

---

# 9. Codebase Design Critique

## Strengths

1. **The artifact wall is genuinely elegant.** Big-output handling is usually an afterthought (summarize, truncate, embed). Content-addressed blobs with explicit attachment is exact, simple, and survives any tool output size.

2. **Cognitive separation is real, not just file-level.** Each module would survive being unit-tested in isolation. The boundaries are well-chosen.

3. **The fallback predicate is auditable.** All failure signatures live in one function with named keywords. New worker failure modes can be added without touching the call sites.

4. **Diagnostic richness.** Every gateway error carries the actual response body; connection refused gets a specific status `0` sentinel; encoding errors print actionable advice. A user staring at a stack trace has the information they need to fix it.

5. **Constraints encoded in code, not just prompts.** Goal immutability via the reconciler. Artifact ID validation in the perception parser. Iter-1 always runs decision (the prompt rule + the orchestrator's tendency). These aren't suggestions to the model — they're enforced by the surrounding code.

## Weaknesses

1. **Memory grows unbounded.** `memory_store.json` accumulates every run's `tool_outcome` rows. Six runs in, `memory.read` is sifting through 100+ entries. Keyword overlap is O(N) per read; will be slow at 10⁴ entries. Mitigation: TTL on `tool_outcome` rows, periodic compaction, or move to SQLite.

2. **No retrieval ranking beyond keyword count.** Two items with 3 keyword matches tie-break only by recency. A semantically irrelevant outcome from yesterday outranks a more relevant fact from last week if their keyword counts are equal. RAG with embeddings would solve this; Session 10+.

3. **`MAX_ITERATIONS = 12` is arbitrary.** Some queries genuinely need 8 iters of search-fetch-synthesize; some get stuck at 3. A budget-based approach (token cost or wall-clock) would scale better than an iter count.

4. **Decision can still make weird first moves.** Iter 1 of one Tokyo run had decision calling `list_dir({'path': '.'})` — irrelevant to the query. The system prompt could be tightened with a "don't use file tools unless query mentions a file" rule.

5. **Synchronous LLM calls.** Each `llm.chat()` blocks the loop. Three perception fallback attempts × multiple iters × a network round-trip = noticeable latency. Async concurrency on independent LLM calls (e.g., parallel memory.read + perception in iter 1) would help, but adds complexity. Session 6 is intentionally sequential per the lecture.

6. **Memory store path is not configurable.** Hardcoded `Path(__file__).parent / "memory_store.json"`. Move to env var.

7. **No observability beyond stdout prints.** No structured tracing, no metrics, no spans. For a course project this is fine; for production this is a debugging nightmare.

8. **The fallback predicate is a living blob.** Each new failure mode adds a keyword. Over time it becomes a brittle catch-all. A more principled approach would tag errors at the source (gateway-side) with explicit "recoverable: yes/no" metadata.

9. **`Mock test surface is small`.** There are no unit tests in this codebase. Every "test" was an integration test (run the full agent and look at the output). For a system this rich, that's a real gap.

10. **No goal-rewrite mechanism.** Lecture-mandated, but a real-world limitation: if iter-1 perception under-decomposes ("do everything" as one goal), the run is stuck. Session 9+ adds goal-rewrite; for now, sub-optimal iter-1 plans = bad runs.

## Production hardening suggestions

- **Memory compaction.** Background job to delete `tool_outcome` rows older than N days; keep only `fact`/`preference`.
- **Structured logging.** Replace `print()` with `logging` configured for JSONL output → ELK / Loki.
- **Distributed tracing.** OpenTelemetry spans around each cognitive call → can profile where time/tokens are spent.
- **Token budget tracking.** Sum input/output tokens across the run; expose `run.token_cost` in the final summary.
- **Circuit-breaker on memory disk.** If JSON saves fail repeatedly (disk full, permissions), pause writes rather than crashing the loop.
- **Retry policy per stage.** Right now retry is implicit (fallback chain). Make it explicit: `RetryPolicy(max_attempts=3, backoff_seconds=[1, 2, 4])`.
- **Artifact LRU eviction.** RAM-only means a long-running daemon would accumulate artifacts forever. Cap at, say, 100 artifacts; evict by FIFO.
- **MCP server health check.** Before opening the session, ping the gateway and the MCP server; surface "gateway down" / "MCP unreachable" errors immediately, not on first tool call.
- **Test suite.** Pydantic fixtures for `Goal`, `MemoryItem`, etc. Mocks for `_gateway.LLM` and `ClientSession`. Unit tests per module; integration tests for canonical scenarios (Shannon, Tokyo, mom-birthday).

## Scaling architecture ideas

- **Parallel goal execution** (Session 9 territory). Topologically sort goals by `note` field describing dependencies; run independents in parallel via `asyncio.TaskGroup`.
- **Multi-MCP-server support.** Right now `mcp_server.py` is hardcoded. Allow `[mcp.servers]` config in `.env` for routing tool calls to specialized servers (web tools server, file tools server, etc.).
- **Persistent agent.** Long-running process with HTTP/WS frontend; multiple concurrent agent instances sharing the gateway pool.
- **Distributed memory.** Move from JSON file to Redis/Postgres so multiple agent instances share memory.

---

# 10. Final Architecture Summary

## What was ultimately built

A **6-iteration sequential agentic loop** built around four cognitive roles (Perception, Decision, Action, Memory), with the orchestrator as the only authority that crosses the artifact wall. The system:

- Decomposes free-form queries into atomic goals on iter 1, then freezes the plan.
- Calls one tool per iter via MCP, stashing large outputs as integer-ID artifacts in RAM.
- Materializes specific artifacts back into decision's context when perception requests them.
- Routes LLM calls through a multi-provider gateway, with perception pinned to Gemini and graceful fallback to Groq/OpenRouter.
- Persists semantic facts across runs via a keyword-indexed JSON memory; tool outcomes are persisted without an LLM call.
- Terminates on `all_done` (lecture-clean) or `MAX_ITERATIONS` (safety net).

## How architecture matured

| Maturity Axis | Initial | Final |
|---|---|---|
| Cognitive separation | None (monolith) | Four roles, four files, four contracts |
| State management | Implicit (function-local) | Explicit (`history`, `locked_goals`, singletons) |
| Error handling | `raise_for_status()` swallowed bodies | `GatewayError` carries full diagnostics |
| Provider strategy | Single pinned provider | Per-stage strategy (pinned + fallback OR auto_route) |
| Context engineering | None (model guesses) | Orchestrator injects NOW: anchor; artifact wall keeps context bounded |
| Failure recovery | Run dies on first error | Recoverable signatures fall through; real bugs surface |
| Determinism | LLM controls everything | Orchestrator enforces immutability via reconciliation |

## Why contracts matter

Every cross-module value is a Pydantic model. Every LLM call returns a JSON-schema-validated reply. Every error is a typed exception with structured fields. This isn't pedantry — each of these contracts caught at least one bug during the project's development:

- `Goal.attach_artifact_id` → `attach_artifact_ids` rename caught silent attachment loss.
- `_should_fallback` keyword set is a living registry of which provider failures we've seen.
- The `DecisionOutput` XOR contract prevents "decision answered AND called a tool" ambiguity.
- The `MemoryItem.kind="scratchpad"` semantic stops transient queries from polluting durable storage.

## How responsibilities became cleaner

The single biggest cleanup: **the orchestrator is the only artifact-wall crosser**. This rule alone:
- Bounds decision's context regardless of tool output size.
- Lets perception reason about *which* artifact decision needs without seeing bytes.
- Lets action focus purely on transport, not on stashing logic decisions.
- Makes the data flow auditable: `[attach] 1 (10432 bytes)` is the only point bytes re-enter the conversation.

## How execution became safer

- **Bounded iteration** (12-iter cap, 180-second per-tool timeout).
- **Goal immutability** (no mid-run plan drift).
- **Tool errors as data, never exceptions** (action layer catches and converts).
- **Per-stage fallback** without masking real bugs.
- **Best-effort memory writes** that don't kill the answer.

## Engineering maturity level

This system demonstrates **mid-senior maturity**:

- Strong contract discipline at boundaries.
- Real understanding of where to trust the model vs. enforce in code.
- Awareness of free-tier and Windows realities, not just happy-path.
- Diagnostic richness — every failure tells the user what's wrong.
- Architectural patience — the lecture's constraints (sequential, immutable goals) are respected even when they hurt because they unlock cleaner upgrades in later sessions.

The next maturity step would be observability + testability — structured logging, traces, and a real test suite. The codebase is *correct*; making it *operable at scale* is the work that remains.
