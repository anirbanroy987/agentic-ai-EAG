# SchemeContext

A multi-agent advisor for Indian government schemes that **grounds its
recommendations in real-time macro data** from the Government of India's
e-Sankhyiki MCP server.

Built as Assignment 4 for the EAGV3 course (Session 5: Planning and Reasoning
with Language Models).

---

## Honest framing: what's new and what isn't

**Citizens already have:**
- [myScheme.gov.in](https://www.myscheme.gov.in/) — the official government
  scheme discovery portal (form-based, 4,000+ schemes)
- [Jugalbandi](https://news.microsoft.com/source/asia/features/with-help-from-next-generation-ai-indian-villagers-gain-easier-access-to-government-services/)
  — Microsoft + AI4Bharat WhatsApp chatbot (FSM-based, 2023-era)
- [EasyGov](https://indiaai.gov.in/case-study/ai-for-providing-awareness-on-government-schemes)
  — civic-tech startup with regional language support

**What's different here, and only here:**
This project pairs each recommended scheme with **fresh statistical data from
MoSPI's e-Sankhyiki MCP server** (launched March 2026) to answer not just
"can you apply?" but "why is this scheme particularly relevant for your state
*right now*?" Nobody else is doing this.

It's also a deliberate demonstration of modern agentic patterns:
multi-agent orchestration with Pydantic-typed handoffs, MCP integration,
parallel tool dispatch via `asyncio.TaskGroup`, and adversarial verification.

---

## Architecture

```
                ┌──────────────────────────────┐
                │   User's free-text input     │
                └──────────────┬───────────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 1. Profile Parser             │  Cerebras
              └──────────────┬───────────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 2. State Resolver             │  Groq + pincode tool
              └──────────────┬───────────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 3. Scheme Matcher             │  Gemini + local search
              │    (local dataset + LLM)      │
              └──────────────┬───────────────┘
                               │ N candidates
        ┌──────────────────────┼─────────────────────┐
        ▼                      ▼                     ▼
  ┌──────────┐           ┌──────────┐         ┌──────────┐   PARALLEL
  │ Eligib.  │   ∥       │ Eligib.  │   ∥     │ Eligib.  │   (V5 pattern,
  │ Checker  │           │ Checker  │         │ Checker  │   asyncio.TaskGroup)
  └──────────┘           └──────────┘         └──────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 5. Macro Contextualizer       │  GPT-4.1
              │    ──> e-Sankhyiki MCP server │  + MCP calls
              │        list_datasets →        │  (parallel per scheme)
              │        get_indicators →       │
              │        get_metadata →         │
              │        get_data               │
              └──────────────┬───────────────┘
                               │
        ┌──────────────────────┼─────────────────────┐
        ▼                      ▼                     ▼
  ┌──────────┐           ┌──────────┐         ┌──────────┐   PARALLEL
  │ App.     │   ∥       │ App.     │   ∥     │ App.     │
  │ Drafter  │           │ Drafter  │         │ Drafter  │
  └──────────┘           └──────────┘         └──────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 7. Priority Ranker            │  GPT-4.1
              └──────────────┬───────────────┘
                               │
                               ▼
              ┌──────────────────────────────┐
              │ 8. Verifier (different LLM)   │  Gemini
              └──────────────┬───────────────┘
                               │
                               ▼
                  Final ranked recommendation
                  + verifier verdict + full trace
```

For a typical run with 5 candidates and 3 eligible: **~16 LLM calls + ~6 MCP
calls**, ~4 agents running in parallel at peak.

---

## How prompts satisfy the evaluator criteria

Every prompt in `src/prompts.py` is designed to pass all eight criteria from
the assignment's prompt-evaluator markdown:

| Criterion | Implementation |
|---|---|
| Explicit reasoning instructions | "How to reason" numbered section in every prompt |
| Structured output format | All outputs are Pydantic models passed via `response_format=json_schema` |
| Separation of reasoning and tools | `reasoning` field separate from action/output fields |
| Conversation loop support | Pydantic `AgentTrace` object captures every event |
| Instructional formatting | Markdown sections: Goal / How to reason / Rules / Output / Fallback |
| Internal self-checks | Every prompt mandates a self-check step before finalizing |
| Reasoning-time awareness | `reasoning="high"` passed to gateway for synthesis-heavy agents |
| Fallback / error handling | Every prompt has an explicit Fallback section |

Plus project-wide invariants:
- Every output schema has `reasoning`, `confidence`, `reasoning_type`
- Every uncertain field is `Optional` rather than fabricated
- Validation errors are fed back to the LLM with details (gateway retry loop)

---

## Data sources

| Source | Type | Used for | Coverage |
|---|---|---|---|
| myScheme dataset (HF: `shrijayan/gov_myscheme`) | Local CSV/JSON | Primary scheme catalog | ~4,000 central + state schemes |
| Built-in seed data (`src/scheme_data.py`) | Hardcoded | Fallback if HF dataset not downloaded | 10 major central schemes |
| e-Sankhyiki MCP server | **MCP** (FastMCP 3.0) | Macro context | 22 MoSPI statistical datasets |
| Offline pincode → state table | Local | Fast resolution | All 36 states/UTs |
| `api.postalpincode.in` | REST API | District resolution | All Indian pincodes |
| Tavily / DuckDuckGo | REST API | Last-resort web search | General |

---

## Multi-LLM justification

Different agents use different providers, deliberately:

| Agent | Provider | Why |
|---|---|---|
| Profile Parser | Cerebras | Fastest provider, extraction is latency-bound |
| State Resolver | Groq | Tiny task, cheapest viable model |
| Scheme Matcher | Gemini | Best at multi-candidate ranking with high reasoning budget |
| Eligibility Checker | Gemini-high | Adversarial verification needs reasoning |
| Macro Contextualizer | GitHub (GPT-4.1) | Best narrative synthesis |
| Application Drafter | Groq | Parallel-friendly, cheap, repetitive |
| Priority Ranker | GitHub (GPT-4.1) | Final output quality matters most here |
| Verifier | Gemini | Deliberately different from Ranker — adversarial check |

Auto-failover via the LLM Gateway handles outages without surfacing errors
to the agent.

---

## Requirements

- Python 3.11+
- UV package manager
- LLM Gateway V2 running on `http://localhost:8100` (or set `LLM_GATEWAY_URL`)
- API keys for at least 3 of: Gemini, Groq, Cerebras, GitHub Models,
  OpenRouter, Nvidia
- Outbound HTTPS to `https://mcp.mospi.gov.in/` (the e-Sankhyiki MCP server)

Optional:
- Tavily API key for higher-quality fallback web search

---

## Setup

```bash
# 1. Start your LLM Gateway V2 (port 8100 by default)
cd path/to/llm-gateway-v2
./run.sh

# 2. In a new terminal:
git clone <this repo>
cd scheme_context
cp .env.example .env
# Fill in TAVILY_API_KEY (optional) and adjust gateway URL if needed

# 3. Optional but recommended — download the full myScheme dataset
mkdir -p data
huggingface-cli download shrijayan/gov_myscheme \
    --repo-type dataset --local-dir data/

# 4. Run the demo
uv run main.py

# Or with a custom query:
uv run main.py --query "I'm a 28-year-old woman in rural Odisha, pincode 751001, 
                        my husband is a daily wage labourer, we have a 2-year-old child"
```

---

## Output

Three files are written to `output/`:

- `recommendation.md` — human-readable ranked recommendations
- `recommendation.json` — full structured Pydantic output
- `trace.json` — every LLM call, MCP call, tool call with timing/tokens
- `verdict.json` — the Verifier agent's evaluation

The `trace.json` is your telemetry/observability artifact — submit it
along with the README.

---

## Prompt evaluation

Run each prompt in `src/prompts.py` through the prompt-evaluator markdown
from the LMS. Save results as `docs/prompt_evaluation_results.md`. The
prompts are designed to pass; the evaluator output is your proof.

---

## Limitations (honest)

- The seed dataset is small (10 schemes). For real use, download the full
  HuggingFace dataset.
- The e-Sankhyiki MCP server's data schemas vary by dataset; the
  `_summarize_mcp_response` function in `agents.py` is permissive but won't
  always extract the perfect indicator. Inspect `raw_response` in the
  output JSON to see what came back.
- State-level scheme coverage in the HuggingFace dataset is uneven.
- We don't yet do longitudinal comparison (this quarter vs last). That's
  a natural next step.
- The Verifier itself is an LLM and can be wrong. A multi-verifier
  ensemble would be more robust.

---

## What's next

- Wrap the local myScheme dataset as its own MCP server and publish it.
  That's a small but real contribution to the Indian gov-tech ecosystem.
- Longitudinal mode: "what did the macro picture look like 6 months ago
  when this scheme was launched, and how has it changed?"
- Multi-language output via AI4Bharat Bhashini models.
- Coding sub-agent for computing derived metrics (poverty headcount,
  gini, etc.) from raw MCP responses.
