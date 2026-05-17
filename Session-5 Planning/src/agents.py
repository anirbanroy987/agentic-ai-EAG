"""
Agent implementations.

Each agent:
- builds its prompt and user message from upstream context
- calls the LLM Gateway with the right Pydantic schema
- records a TraceEvent (provider, model, latency, tokens, cache_hit)
- returns the parsed result

The macro contextualizer additionally calls the e-Sankhyiki MCP server
directly (not through the LLM) to fetch real statistical data, then asks
the LLM to interpret it.

Parallel calls use asyncio.TaskGroup — the V5 pattern.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from .gateway_client import GatewayClient, GatewayError
from .mcp_client import ESankhyikiMCPClient, datasets_for_theme
from .prompts import (
    APPLICATION_DRAFTER_PROMPT,
    ELIGIBILITY_CHECKER_PROMPT,
    MACRO_CONTEXTUALIZER_PROMPT,
    PRIORITY_RANKER_PROMPT,
    PROFILE_PARSER_PROMPT,
    SCHEME_MATCHER_PROMPT,
    STATE_RESOLVER_PROMPT,
    VERIFIER_PROMPT,
)
from .schemas import (
    AgentTrace,
    ApplicationGuide,
    EligibilityCheck,
    EligibilityResults,
    FinalRecommendation,
    MacroContextForScheme,
    MacroContextResults,
    MacroDataPoint,
    ParsedProfile,
    SchemeCandidate,
    SchemeMatchResult,
    SchemeRecord,
    StateResolution,
    TraceEvent,
    UserProfile,
    VerifierVerdict,
)
from .scheme_data import search_schemes
from .tools import resolve_pincode

# -------------------------------------------------------------------------
# Provider routing — different LLMs for different tasks (V5 pattern)
# -------------------------------------------------------------------------

PROVIDER_FOR_AGENT = {
    "profile_parser": "cerebras",      # fast extraction
    "state_resolver": "groq",          # tiny task
    "scheme_matcher": "gemini",        # multi-candidate reasoning
    "eligibility_checker": "gemini",   # adversarial verification, needs reasoning budget
    "macro_contextualizer": "github",  # GPT-4.1 strong at narrative synthesis
    "application_drafter": "groq",     # parallel-friendly, cheap
    "priority_ranker": "github",       # final synthesis quality matters
    "verifier": "gemini",              # adversarial, deliberately not GPT-4.1
}


async def _record_llm_call(
    trace: AgentTrace, agent_name: str, response
) -> None:
    trace.add(
        TraceEvent(
            turn=0,
            kind="llm_call",
            timestamp=datetime.now(),
            agent_name=agent_name,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_hit=response.cache_hit,
        )
    )


# -------------------------------------------------------------------------
# Agent 1: Profile Parser
# -------------------------------------------------------------------------


async def run_profile_parser(
    raw_input: str,
    client: GatewayClient,
    trace: AgentTrace,
) -> ParsedProfile:
    user_message = (
        f"User input:\n{raw_input}\n\n"
        f"Extract the structured profile. Be conservative about inferences."
    )
    response = await client.call(
        system_prompt=PROFILE_PARSER_PROMPT,
        user_message=user_message,
        response_schema=ParsedProfile,
        preferred_provider=PROVIDER_FOR_AGENT["profile_parser"],
        reasoning="low",
    )
    await _record_llm_call(trace, "profile_parser", response)
    return response.parsed


# -------------------------------------------------------------------------
# Agent 2: State Resolver — uses pincode tool + LLM
# -------------------------------------------------------------------------


async def run_state_resolver(
    profile: UserProfile,
    client: GatewayClient,
    trace: AgentTrace,
) -> StateResolution:
    pincode_result: dict = {}
    if profile.pincode:
        trace.add(
            TraceEvent(
                turn=0,
                kind="tool_call",
                timestamp=datetime.now(),
                agent_name="state_resolver",
                tool_name="resolve_pincode",
                tool_args={"pincode": profile.pincode},
            )
        )
        pincode_result = await resolve_pincode(profile.pincode)

    user_message = (
        f"User-provided state: {profile.state or 'none'}\n"
        f"User-provided pincode: {profile.pincode or 'none'}\n"
        f"Pincode lookup result: {json.dumps(pincode_result)}\n"
        f"User mentioned rural: {profile.is_rural}\n\n"
        f"Resolve the final state and rural-flag for scheme matching."
    )
    response = await client.call(
        system_prompt=STATE_RESOLVER_PROMPT,
        user_message=user_message,
        response_schema=StateResolution,
        preferred_provider=PROVIDER_FOR_AGENT["state_resolver"],
        reasoning="low",
    )
    await _record_llm_call(trace, "state_resolver", response)
    return response.parsed


# -------------------------------------------------------------------------
# Agent 3: Scheme Matcher — local search + LLM ranking
# -------------------------------------------------------------------------


async def run_scheme_matcher(
    profile: UserProfile,
    state: str | None,
    all_schemes: list[SchemeRecord],
    client: GatewayClient,
    trace: AgentTrace,
) -> tuple[SchemeMatchResult, list[SchemeRecord]]:
    """
    Returns (LLM-ranked matches, full SchemeRecords used for downstream agents).
    """
    # Step 1: deterministic pre-filter from local dataset.
    query_parts = []
    if profile.occupation:
        query_parts.append(profile.occupation)
    if profile.is_rural:
        query_parts.append("rural")
    if profile.has_disability:
        query_parts.append("disability")
    query = " ".join(query_parts)

    # Theme hints based on profile.
    inferred_categories = []
    if profile.occupation and "farm" in (profile.occupation or "").lower():
        inferred_categories.extend(["agriculture", "farmers", "income_support"])
    if profile.is_rural:
        inferred_categories.extend(["rural", "employment", "housing"])
    if profile.annual_income_inr is not None and profile.annual_income_inr < 250000:
        inferred_categories.extend(["financial_inclusion", "consumption_poverty", "health"])
    if profile.gender == "female":
        inferred_categories.extend(["women_empowerment"])

    pre_filtered = search_schemes(
        all_schemes,
        query=query,
        state=state,
        categories=inferred_categories or None,
        limit=15,
    )

    trace.add(
        TraceEvent(
            turn=0,
            kind="tool_call",
            timestamp=datetime.now(),
            agent_name="scheme_matcher",
            tool_name="search_schemes_local",
            tool_args={
                "query": query,
                "state": state,
                "categories": inferred_categories,
                "returned": len(pre_filtered),
            },
        )
    )

    if not pre_filtered:
        # Honest fallback.
        empty = SchemeMatchResult(
            reasoning="No candidates matched the profile against the local dataset.",
            candidates=[],
            rejected_categories=[],
            confidence=0.0,
        )
        return empty, []

    # Step 2: LLM ranks the pre-filtered candidates.
    user_message = (
        f"User profile:\n{profile.model_dump_json(indent=2)}\n\n"
        f"Pre-filtered candidate schemes (from local dataset):\n"
        + "\n\n".join(
            f"[{s.scheme_id}] {s.name}\n  Level: {s.level}\n  "
            f"Categories: {s.category}\n  "
            f"Description: {s.description[:240]}\n  "
            f"Eligibility: {s.eligibility_text[:240]}"
            for s in pre_filtered
        )
        + "\n\nRank and reason about which schemes actually fit this user."
    )

    response = await client.call(
        system_prompt=SCHEME_MATCHER_PROMPT,
        user_message=user_message,
        response_schema=SchemeMatchResult,
        preferred_provider=PROVIDER_FOR_AGENT["scheme_matcher"],
        reasoning="medium",
    )
    await _record_llm_call(trace, "scheme_matcher", response)
    return response.parsed, pre_filtered


# -------------------------------------------------------------------------
# Agent 4: Eligibility Checker — runs PARALLEL per candidate (V5 pattern)
# -------------------------------------------------------------------------


async def _check_one_scheme(
    profile: UserProfile,
    scheme: SchemeRecord,
    client: GatewayClient,
    trace: AgentTrace,
) -> EligibilityCheck:
    user_message = (
        f"User profile:\n{profile.model_dump_json(indent=2)}\n\n"
        f"Scheme: {scheme.name} ({scheme.scheme_id})\n"
        f"Eligibility text:\n{scheme.eligibility_text}\n\n"
        f"Perform the adversarial clause-by-clause check."
    )
    # Wrap with EligibilityResults schema but for one scheme — we want the
    # check inside the list. Use the single-scheme schema directly:
    response = await client.call(
        system_prompt=ELIGIBILITY_CHECKER_PROMPT,
        user_message=user_message,
        response_schema=EligibilityCheck,
        preferred_provider=PROVIDER_FOR_AGENT["eligibility_checker"],
        reasoning="high",
    )
    await _record_llm_call(trace, f"eligibility.{scheme.scheme_id}", response)
    return response.parsed


async def run_eligibility_checkers_parallel(
    profile: UserProfile,
    matched: list[SchemeRecord],
    client: GatewayClient,
    trace: AgentTrace,
) -> EligibilityResults:
    """Fan out one checker per scheme."""
    if not matched:
        return EligibilityResults(
            reasoning="No matched schemes to verify.",
            checks=[],
            self_check_passed=True,
            confidence=1.0,
        )

    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(_check_one_scheme(profile, s, client, trace))
            for s in matched
        ]
    checks = [t.result() for t in tasks]

    # No additional LLM call here — checks are already from LLM, we just aggregate.
    return EligibilityResults(
        reasoning=(
            f"Ran adversarial eligibility checks in parallel for "
            f"{len(checks)} schemes. Aggregating results."
        ),
        checks=checks,
        self_check_passed=all(c.confidence > 0 for c in checks),
        confidence=sum(c.confidence for c in checks) / len(checks) if checks else 0.0,
    )


# -------------------------------------------------------------------------
# Agent 5: Macro Contextualizer — calls e-Sankhyiki MCP, then LLM
# -------------------------------------------------------------------------


async def _fetch_macro_for_scheme(
    scheme: SchemeRecord,
    state: str | None,
    mcp: ESankhyikiMCPClient,
    trace: AgentTrace,
) -> tuple[list[MacroDataPoint], int]:
    """Make MCP calls for one scheme, return data points and call count."""
    # Pick a theme from the scheme's category. Fall back to 'general'.
    theme = (scheme.category[0] if scheme.category else "general").lower()
    datasets = datasets_for_theme(theme)

    data_points: list[MacroDataPoint] = []
    call_count = 0

    for ds_code in datasets[:2]:  # Cap at 2 datasets per scheme for latency.
        trace.add(
            TraceEvent(
                turn=0,
                kind="mcp_call",
                timestamp=datetime.now(),
                agent_name=f"macro.{scheme.scheme_id}",
                mcp_server="esankhyiki",
                mcp_tool="quick_fetch",
                tool_args={"dataset": ds_code, "state": state},
            )
        )
        try:
            data, _calls = await mcp.quick_fetch(ds_code, state=state)
            call_count += len(_calls)
            # Convert MCP response to MacroDataPoint. Shape varies per dataset,
            # so we keep it permissive and store the raw payload.
            summary = _summarize_mcp_response(data, ds_code, state)
            if summary:
                data_points.append(summary)
        except Exception as e:
            trace.add(
                TraceEvent(
                    turn=0,
                    kind="mcp_call",
                    timestamp=datetime.now(),
                    agent_name=f"macro.{scheme.scheme_id}",
                    mcp_server="esankhyiki",
                    mcp_tool="quick_fetch",
                    error=f"{ds_code}: {e!r}",
                )
            )

    return data_points, call_count


def _summarize_mcp_response(
    data: Any, dataset: str, state: str | None
) -> MacroDataPoint | None:
    """
    Convert the MCP response into a single MacroDataPoint we can show to the
    LLM. MCP responses vary; we extract whatever is most informative.
    """
    if data is None:
        return None
    # FastMCP returns a CallToolResult-like object; convert to dict.
    try:
        payload = data.structured_content if hasattr(data, "structured_content") else data
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        elif not isinstance(payload, (dict, list)):
            payload = json.loads(str(payload))
    except Exception:
        payload = {"raw": str(data)}

    return MacroDataPoint(
        indicator=f"{dataset} data",
        value=str(payload)[:240],
        state=state,
        source_dataset=dataset,
        raw_response=payload if isinstance(payload, dict) else {"data": payload},
    )


async def run_macro_contextualizer(
    eligible_schemes: list[SchemeRecord],
    state: str | None,
    client: GatewayClient,
    mcp: ESankhyikiMCPClient,
    trace: AgentTrace,
) -> MacroContextResults:
    """Fetch macro data per scheme (parallel) then ask LLM to narrate."""
    if not eligible_schemes:
        return MacroContextResults(
            reasoning="No eligible schemes to contextualize.",
            contexts=[],
            confidence=1.0,
        )

    # Fetch macro data for each scheme in parallel.
    async with asyncio.TaskGroup() as tg:
        fetch_tasks = [
            tg.create_task(_fetch_macro_for_scheme(s, state, mcp, trace))
            for s in eligible_schemes
        ]
    fetched = [t.result() for t in fetch_tasks]

    # Build the per-scheme macro_data summary for the LLM.
    schemes_with_data = []
    for scheme, (points, count) in zip(eligible_schemes, fetched):
        schemes_with_data.append(
            {
                "scheme_id": scheme.scheme_id,
                "scheme_name": scheme.name,
                "scheme_purpose": scheme.description[:300],
                "macro_data_points": [p.model_dump() for p in points],
                "mcp_calls_made": count,
            }
        )

    user_message = (
        f"User state: {state or 'unknown (only central schemes are contextualized)'}\n\n"
        f"Per-scheme macro data (from e-Sankhyiki MCP):\n"
        f"{json.dumps(schemes_with_data, indent=2, default=str)}\n\n"
        f"For each scheme, write a 'why this matters now' narrative that "
        f"references specific numbers from the macro data."
    )

    response = await client.call(
        system_prompt=MACRO_CONTEXTUALIZER_PROMPT,
        user_message=user_message,
        response_schema=MacroContextResults,
        preferred_provider=PROVIDER_FOR_AGENT["macro_contextualizer"],
        reasoning="high",
    )
    await _record_llm_call(trace, "macro_contextualizer", response)
    return response.parsed


# -------------------------------------------------------------------------
# Agent 6: Application Drafter — PARALLEL per scheme
# -------------------------------------------------------------------------


async def _draft_one(
    scheme: SchemeRecord,
    client: GatewayClient,
    trace: AgentTrace,
) -> ApplicationGuide:
    user_message = (
        f"Scheme: {scheme.name}\n"
        f"Description: {scheme.description}\n"
        f"Application URL: {scheme.application_url or 'unknown'}\n"
        f"Documents required (per scheme metadata): "
        f"{', '.join(scheme.documents_required) or 'unknown'}\n\n"
        f"Write the step-by-step application guide."
    )
    response = await client.call(
        system_prompt=APPLICATION_DRAFTER_PROMPT,
        user_message=user_message,
        response_schema=ApplicationGuide,
        preferred_provider=PROVIDER_FOR_AGENT["application_drafter"],
        reasoning="low",
    )
    await _record_llm_call(trace, f"app_drafter.{scheme.scheme_id}", response)
    return response.parsed


async def run_application_drafters_parallel(
    schemes: list[SchemeRecord],
    client: GatewayClient,
    trace: AgentTrace,
) -> list[ApplicationGuide]:
    if not schemes:
        return []
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_draft_one(s, client, trace)) for s in schemes]
    return [t.result() for t in tasks]


# -------------------------------------------------------------------------
# Agent 7: Priority Ranker
# -------------------------------------------------------------------------


async def run_priority_ranker(
    eligibility: EligibilityResults,
    macro: MacroContextResults,
    guides: list[ApplicationGuide],
    client: GatewayClient,
    trace: AgentTrace,
) -> FinalRecommendation:
    user_message = (
        f"Eligibility results:\n{eligibility.model_dump_json(indent=2)}\n\n"
        f"Macro context:\n{macro.model_dump_json(indent=2)}\n\n"
        f"Application guides:\n{json.dumps([g.model_dump() for g in guides], indent=2)}\n\n"
        f"Produce the final ranked recommendation."
    )
    response = await client.call(
        system_prompt=PRIORITY_RANKER_PROMPT,
        user_message=user_message,
        response_schema=FinalRecommendation,
        preferred_provider=PROVIDER_FOR_AGENT["priority_ranker"],
        reasoning="high",
    )
    await _record_llm_call(trace, "priority_ranker", response)
    return response.parsed


# -------------------------------------------------------------------------
# Agent 8: Verifier
# -------------------------------------------------------------------------


async def run_verifier(
    recommendation: FinalRecommendation,
    eligibility: EligibilityResults,
    macro: MacroContextResults,
    client: GatewayClient,
    trace: AgentTrace,
) -> VerifierVerdict:
    user_message = (
        f"Final recommendation:\n{recommendation.model_dump_json(indent=2)}\n\n"
        f"Eligibility ground truth:\n{eligibility.model_dump_json(indent=2)}\n\n"
        f"Macro ground truth:\n{macro.model_dump_json(indent=2)}\n\n"
        f"Run every check and return your verdict."
    )
    response = await client.call(
        system_prompt=VERIFIER_PROMPT,
        user_message=user_message,
        response_schema=VerifierVerdict,
        preferred_provider=PROVIDER_FOR_AGENT["verifier"],
        reasoning="high",
    )
    await _record_llm_call(trace, "verifier", response)
    return response.parsed
