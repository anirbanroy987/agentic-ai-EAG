"""
Orchestrator: pipeline coordinator.

Pipeline (each line is one stage; → means sequential, ∥ means parallel):

  1. Profile Parser
  2. State Resolver
  3. Scheme Matcher (local pre-filter + LLM ranking)
  4. Eligibility Checkers          ∥ (one per candidate, parallel)
  5. Macro Contextualizer (calls e-Sankhyiki MCP per scheme, parallel inside)
  6. Application Drafters          ∥ (one per eligible scheme, parallel)
  7. Priority Ranker (synthesis)
  8. Verifier (different LLM than ranker — adversarial)

Total LLM calls: ~6 + N_eligible*2 (eligibility + drafter), plus MCP calls.
For a typical run with 5 candidates and 3 eligible, that's ~16 LLM calls
and ~6 MCP calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from rich.console import Console
from rich.panel import Panel

from .agents import (
    run_application_drafters_parallel,
    run_eligibility_checkers_parallel,
    run_macro_contextualizer,
    run_priority_ranker,
    run_profile_parser,
    run_scheme_matcher,
    run_state_resolver,
    run_verifier,
)
from .gateway_client import GatewayClient
from .mcp_client import ESankhyikiMCPClient
from .schemas import (
    AgentTrace,
    FinalRecommendation,
    SchemeRecord,
    VerifierVerdict,
)
from .scheme_data import get_scheme_by_id, load_schemes

console = Console()


async def generate_recommendation(
    raw_user_input: str,
    progress: Optional[Callable[[int, str], None]] = None,
) -> tuple[FinalRecommendation, VerifierVerdict, AgentTrace]:
    """
    Run the full pipeline.

    ``progress`` is an optional callback ``progress(stage_number, stage_name)``
    fired as each of the 8 stages starts — used by non-CLI front-ends (e.g.
    the Streamlit app) to show live progress. Defaults to None, so the CLI
    path is unchanged. Callback exceptions are swallowed so a flaky UI can
    never break the pipeline.
    """

    trace = AgentTrace(user_input=raw_user_input, started_at=datetime.now())
    client = GatewayClient()
    mcp = ESankhyikiMCPClient()
    all_schemes = load_schemes()

    def _stage(n: int, name: str) -> None:
        console.rule(f"[bold cyan]{n}/8 {name}")
        if progress is not None:
            try:
                progress(n, name)
            except Exception:
                pass

    # Stage 1
    _stage(1, "Profile Parser")
    parsed = await run_profile_parser(raw_user_input, client, trace)
    console.print(
        Panel(
            f"Parsed profile:\n{parsed.profile.model_dump_json(indent=2)}\n\n"
            f"Inferred: {parsed.inferred_fields}\n"
            f"Missing: {parsed.missing_critical_fields}\n"
            f"Confidence: {parsed.confidence:.2f}",
            title="Profile",
        )
    )

    # Stage 2
    _stage(2, "State Resolver")
    state_res = await run_state_resolver(parsed.profile, client, trace)
    final_state = state_res.resolved_state or parsed.profile.state
    console.print(
        Panel(
            f"Resolved state: {final_state or '(unknown — only central schemes)'}\n"
            f"Confidence: {state_res.confidence:.2f}",
            title="State",
        )
    )

    # Sync the profile with resolved data for downstream agents.
    if final_state:
        parsed.profile.state = final_state
    if state_res.is_rural_likely is not None and parsed.profile.is_rural is None:
        parsed.profile.is_rural = state_res.is_rural_likely

    # Stage 3
    _stage(3, "Scheme Matcher")
    match_result, matched_records = await run_scheme_matcher(
        parsed.profile, final_state, all_schemes, client, trace
    )
    console.print(
        Panel(
            f"Candidates: {len(match_result.candidates)}\n"
            + "\n".join(
                f"  • {c.name} ({c.initial_relevance_score:.2f})"
                for c in match_result.candidates
            ),
            title="Matched Schemes",
        )
    )

    # Re-resolve to scheme records (the matcher returned candidate IDs).
    candidate_records: list[SchemeRecord] = []
    candidate_ids = {c.scheme_id for c in match_result.candidates}
    for rec in matched_records:
        if rec.scheme_id in candidate_ids:
            candidate_records.append(rec)

    if not candidate_records:
        console.print("[yellow]No matched schemes found — stopping pipeline.[/]")
        empty_rec = FinalRecommendation(
            reasoning_trace=match_result.reasoning,
            ranked_schemes=[],
            top_pick_justification="No schemes matched this profile.",
            follow_up_questions_for_user=parsed.missing_critical_fields,
            confidence_overall=0.0,
        )
        empty_verdict = VerifierVerdict(
            reasoning="Nothing to verify — empty recommendation.",
            checks_performed=[],
            issues_found=[],
            suggested_revisions=[],
            final_verdict="approved",
            confidence=1.0,
        )
        return empty_rec, empty_verdict, trace

    # Stage 4 — PARALLEL
    _stage(4, "Eligibility Checkers (parallel)")
    eligibility = await run_eligibility_checkers_parallel(
        parsed.profile, candidate_records, client, trace
    )
    eligible_records = [
        get_scheme_by_id(all_schemes, c.scheme_id)
        for c in eligibility.checks
        if c.verdict in ("eligible", "likely_eligible")
    ]
    eligible_records = [r for r in eligible_records if r is not None]
    console.print(
        Panel(
            "\n".join(
                f"  {c.verdict:18s} {c.scheme_name}"
                for c in eligibility.checks
            ),
            title=f"Eligibility ({len(eligible_records)}/{len(eligibility.checks)} eligible)",
        )
    )

    # Stage 5 — MCP-heavy
    _stage(5, "Macro Contextualizer (e-Sankhyiki MCP)")
    macro = await run_macro_contextualizer(
        eligible_records, final_state, client, mcp, trace
    )
    console.print(
        Panel(
            "\n".join(
                f"  [{ctx.urgency:6s}] {ctx.scheme_name}: "
                f"{len(ctx.data_points)} data points"
                for ctx in macro.contexts
            ),
            title="Macro Context",
        )
    )

    # Stage 6 — PARALLEL
    _stage(6, "Application Drafters (parallel)")
    guides = await run_application_drafters_parallel(
        eligible_records, client, trace
    )

    # Stage 7
    _stage(7, "Priority Ranker")
    recommendation = await run_priority_ranker(
        eligibility, macro, guides, client, trace
    )

    # Stage 8
    _stage(8, "Verifier")
    verdict = await run_verifier(recommendation, eligibility, macro, client, trace)
    console.print(
        Panel(
            f"Verdict: [bold]{verdict.final_verdict}[/]\n"
            f"Issues: {len(verdict.issues_found)}\n"
            + ("\n".join(f"  • {i}" for i in verdict.issues_found[:5])),
            title="Verifier",
        )
    )

    return recommendation, verdict, trace


def render_markdown(
    recommendation: FinalRecommendation,
    user_input: str,
) -> str:
    """Render the final recommendation as user-readable markdown."""
    lines = [
        "# Your Scheme Recommendations",
        "",
        f"*Based on: \"{user_input[:120]}{'...' if len(user_input) > 120 else ''}\"*",
        "",
        f"> **Top pick:** {recommendation.top_pick_justification}",
        "",
    ]

    for r in recommendation.ranked_schemes:
        lines.append(f"## {r.rank}. {r.scheme_name}")
        lines.append("")
        lines.append(f"**{r.one_line_pitch}**")
        lines.append("")
        lines.append(f"- **Why now:** {r.macro_context_summary}")
        lines.append(f"- **Eligibility:** {r.eligibility_status}")
        if r.estimated_benefit_inr:
            lines.append(f"- **Estimated benefit:** {r.estimated_benefit_inr}")
        lines.append(f"- **Reasoning:** {r.why_this_rank}")
        lines.append("")

    if recommendation.follow_up_questions_for_user:
        lines.append("---")
        lines.append("## To refine these recommendations")
        for q in recommendation.follow_up_questions_for_user:
            lines.append(f"- {q}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"<small>Overall confidence: {recommendation.confidence_overall:.0%} · "
        f"Reasoning: {recommendation.reasoning_trace[:200]}{'...' if len(recommendation.reasoning_trace) > 200 else ''}</small>"
    )
    return "\n".join(lines)
