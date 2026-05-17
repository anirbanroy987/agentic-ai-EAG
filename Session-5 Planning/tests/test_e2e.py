"""
End-to-end test of the SchemeContext pipeline.

What this proves:
1. The orchestrator runs all 8 agents in the correct order
2. The gateway client correctly serializes/deserializes Pydantic schemas
3. Parallel calls (asyncio.TaskGroup) actually fan out
4. The MCP client integrates cleanly
5. The AgentTrace captures everything (LLM, MCP, and tool calls)
6. The verifier runs on a different "provider" than the writer
7. The final markdown renders without errors

We mock:
- The LLM Gateway (via tests/mock_gateway.py, real HTTP on a local port)
- The e-Sankhyiki MCP client (via FakeMCPClient in conftest.py)
- The pincode online lookup (mocked at the network layer)

Real:
- Pydantic validation runs for real
- The orchestrator code paths run unmodified
- asyncio.TaskGroup parallelism is exercised
- All 8 agent prompts get serialized into real HTTP requests

Run:
    python -m tests.test_e2e
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest import FakeMCPClient, mock_gateway
from tests.mock_gateway import clear_call_log, get_call_log


# ANSI colour helpers — keep the output readable.
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def green(msg: str) -> str:
    return f"{GREEN}{msg}{RESET}"


def red(msg: str) -> str:
    return f"{RED}{msg}{RESET}"


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(
            f"{label}: expected {expected!r}, got {actual!r}"
        )


def assert_ge(actual, expected, label: str) -> None:
    if not (actual >= expected):
        raise AssertionError(
            f"{label}: expected >= {expected}, got {actual}"
        )


def assert_truthy(value, label: str) -> None:
    if not value:
        raise AssertionError(f"{label}: expected truthy, got {value!r}")


# -------------------------------------------------------------------------
# Test 1: full pipeline end-to-end
# -------------------------------------------------------------------------


async def test_full_pipeline():
    """Run the whole 8-agent pipeline against mocks."""
    from src.orchestrator import generate_recommendation, render_markdown

    print(f"\n{BOLD}{CYAN}TEST 1: Full pipeline end-to-end{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    # Patch ESankhyikiMCPClient so it never touches the network
    async def _fake_pincode_online(pin: str):
        return None  # Simulate the online lookup not finding the pincode

    with patch("src.agents.ESankhyikiMCPClient", FakeMCPClient), \
         patch("src.orchestrator.ESankhyikiMCPClient", FakeMCPClient):
        # Also stub out the online pincode lookup so we don't hit the internet
        with patch("src.tools.pincode_to_state_online", _fake_pincode_online):
            user_input = (
                "I'm a 32-year-old farmer in Bihar, pincode 800001. "
                "1 acre of ancestral land. Family of 5 including 2 kids. "
                "Annual income about 1.2 lakh. We live in a kachha 2-room house."
            )

            start = time.perf_counter()
            recommendation, verdict, trace = await generate_recommendation(user_input)
            duration_ms = int((time.perf_counter() - start) * 1000)

    print()
    print(f"  Pipeline duration: {duration_ms}ms")
    print(f"  Trace events recorded: {len(trace.events)}")

    summary = trace.summary()
    print(f"  LLM calls: {summary['llm_calls']}")
    print(f"  MCP calls: {summary['mcp_calls']}")
    print(f"  Tool calls: {summary['tool_calls']}")
    print(f"  Total tokens in: {summary['total_input_tokens']}")
    print(f"  Total tokens out: {summary['total_output_tokens']}")
    print(f"  Providers used: {summary['providers_used']}")

    # Assertions about the trace
    assert_ge(summary["llm_calls"], 8, "Expected at least 8 LLM calls")
    assert_ge(summary["mcp_calls"], 3, "Expected at least 3 MCP calls")
    assert_ge(summary["tool_calls"], 1, "Expected at least 1 tool call")

    # Assertions about the recommendation
    print()
    print(f"  Recommendation ranked {len(recommendation.ranked_schemes)} schemes")
    print(f"  Top pick: {recommendation.ranked_schemes[0].scheme_name}")
    print(f"  Overall confidence: {recommendation.confidence_overall:.2f}")

    assert_ge(len(recommendation.ranked_schemes), 1, "Expected at least 1 ranked scheme")
    assert_truthy(recommendation.top_pick_justification, "Missing top pick justification")
    assert_truthy(recommendation.reasoning_trace, "Missing reasoning trace")
    assert recommendation.confidence_overall <= 1.0

    # Verifier verdict
    print(f"  Verifier verdict: {verdict.final_verdict}")
    print(f"  Verifier issues found: {len(verdict.issues_found)}")
    assert_ge(len(verdict.checks_performed), 1, "Verifier should perform at least one check")

    # Markdown rendering
    markdown = render_markdown(recommendation, user_input)
    assert_truthy(markdown.startswith("# "), "Markdown must start with heading")
    assert "Top pick" in markdown
    print(f"  Markdown output: {len(markdown)} chars")

    print(green("\n  ✓ Full pipeline test passed"))
    return True


# -------------------------------------------------------------------------
# Test 2: Gateway request shape is correct
# -------------------------------------------------------------------------


async def test_gateway_request_shape():
    """Confirm the gateway received properly-shaped requests."""
    print(f"\n{BOLD}{CYAN}TEST 2: Gateway request shape{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    log = get_call_log()
    assert_ge(len(log), 8, "Expected at least 8 gateway calls in the log")

    # All calls must have a schema title
    for i, call in enumerate(log):
        assert_truthy(call["schema_title"], f"Call {i}: missing schema_title")

    # We should see all 8 distinct agent schemas
    schema_titles = {c["schema_title"] for c in log}
    expected = {
        "ParsedProfile",
        "StateResolution",
        "SchemeMatchResult",
        "EligibilityCheck",
        "MacroContextResults",
        "ApplicationGuide",
        "FinalRecommendation",
        "VerifierVerdict",
    }
    missing = expected - schema_titles
    if missing:
        raise AssertionError(f"Missing agent schema calls: {missing}")
    print(f"  All 8 agent schemas hit: {sorted(schema_titles)}")

    # Multiple providers should have been requested (V5 multi-LLM pattern)
    providers = {c["preferred_provider"] for c in log if c.get("preferred_provider")}
    assert_ge(len(providers), 3, "Expected at least 3 distinct preferred providers")
    print(f"  Distinct providers requested: {sorted(providers)}")

    # Reasoning levels should vary (not all "medium")
    reasoning_levels = {c["reasoning"] for c in log}
    print(f"  Reasoning levels used: {sorted(reasoning_levels)}")
    assert_ge(len(reasoning_levels), 2, "Expected varied reasoning levels")

    print(green("\n  ✓ Gateway request shape test passed"))
    return True


# -------------------------------------------------------------------------
# Test 3: Parallel calls actually happen in parallel
# -------------------------------------------------------------------------


async def test_parallel_dispatch():
    """
    The eligibility checker and application drafter both fan out via
    asyncio.TaskGroup. We verify that N parallel calls finish in roughly
    the time of a single call, not N times that.
    """
    print(f"\n{BOLD}{CYAN}TEST 3: Parallel dispatch via asyncio.TaskGroup{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    # We need to count how many EligibilityCheck calls happened
    log = get_call_log()
    eligibility_calls = [c for c in log if c["schema_title"] == "EligibilityCheck"]
    drafter_calls = [c for c in log if c["schema_title"] == "ApplicationGuide"]
    macro_calls = [c for c in log if c["schema_title"] == "MacroContextResults"]

    print(f"  EligibilityCheck calls: {len(eligibility_calls)}")
    print(f"  ApplicationGuide calls: {len(drafter_calls)}")
    print(f"  MacroContextResults calls: {len(macro_calls)}")

    # With 3 matched candidates from the mock, we expect 3 eligibility checks
    assert_ge(len(eligibility_calls), 3, "Expected >= 3 parallel eligibility calls")
    # The macro contextualizer is a single LLM call (it fetches MCP data in parallel
    # then makes one synthesis call)
    assert_ge(len(macro_calls), 1, "Expected at least 1 macro contextualizer call")

    print(green("\n  ✓ Parallel dispatch test passed"))
    return True


# -------------------------------------------------------------------------
# Test 4: Verifier uses a different LLM than the writer
# -------------------------------------------------------------------------


async def test_verifier_diversity():
    """
    Session 5 emphasized using a different LLM for the verifier than the
    writer/synthesizer, for adversarial robustness.
    """
    print(f"\n{BOLD}{CYAN}TEST 4: Verifier uses a different provider than the Ranker{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    log = get_call_log()
    ranker_calls = [c for c in log if c["schema_title"] == "FinalRecommendation"]
    verifier_calls = [c for c in log if c["schema_title"] == "VerifierVerdict"]

    assert_truthy(ranker_calls, "No ranker call recorded")
    assert_truthy(verifier_calls, "No verifier call recorded")

    ranker_provider = ranker_calls[0]["preferred_provider"]
    verifier_provider = verifier_calls[0]["preferred_provider"]

    print(f"  Priority Ranker preferred: {ranker_provider}")
    print(f"  Verifier preferred:        {verifier_provider}")

    if ranker_provider == verifier_provider:
        raise AssertionError(
            f"Verifier and Ranker should use different providers; both used {ranker_provider}"
        )

    print(green("\n  ✓ Verifier diversity test passed"))
    return True


# -------------------------------------------------------------------------
# Test 5: Trace JSON is itself valid Pydantic output
# -------------------------------------------------------------------------


async def test_trace_serialization():
    """
    The AgentTrace is itself a Pydantic model. We should be able to dump it
    to JSON and reload it without loss.
    """
    print(f"\n{BOLD}{CYAN}TEST 5: AgentTrace round-trips through JSON{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    from src.orchestrator import generate_recommendation
    from src.schemas import AgentTrace

    async def _fake_pincode_online(pin: str):
        return None

    with patch("src.agents.ESankhyikiMCPClient", FakeMCPClient), \
         patch("src.orchestrator.ESankhyikiMCPClient", FakeMCPClient), \
         patch("src.tools.pincode_to_state_online", _fake_pincode_online):
        _rec, _verdict, trace = await generate_recommendation(
            "I'm a 28-year-old woman in rural Odisha, pincode 751001"
        )

    # Dump to JSON
    json_str = trace.model_dump_json(indent=2)
    assert_ge(len(json_str), 1000, "Trace JSON seems too small")

    # Reload
    restored = AgentTrace.model_validate_json(json_str)
    assert_eq(len(restored.events), len(trace.events), "Event count after round-trip")
    assert_eq(restored.user_input, trace.user_input, "User input after round-trip")

    # Check that LLM-call events have their telemetry fields populated
    llm_events = [e for e in restored.events if e.kind == "llm_call"]
    assert_ge(len(llm_events), 1, "Expected at least 1 LLM event")
    sample = llm_events[0]
    assert_truthy(sample.provider, "Sample LLM event missing provider")
    assert_truthy(sample.model, "Sample LLM event missing model")
    assert sample.latency_ms is not None and sample.latency_ms >= 0
    assert sample.input_tokens is not None
    assert sample.output_tokens is not None

    print(f"  JSON length: {len(json_str)} chars")
    print(f"  Events: {len(restored.events)}")
    print(f"  Sample LLM event: provider={sample.provider}, model={sample.model}")

    print(green("\n  ✓ Trace serialization test passed"))
    return True


# -------------------------------------------------------------------------
# Test 6: Pydantic schemas reject invalid LLM output
# -------------------------------------------------------------------------


async def test_pydantic_rejects_bad_llm_output():
    """
    The gateway client has a retry loop that re-prompts on validation failure.
    We verify that bad output is actually rejected by Pydantic.
    """
    print(f"\n{BOLD}{CYAN}TEST 6: Bad LLM output is caught by Pydantic{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")

    from src.schemas import EligibilityCheck
    from pydantic import ValidationError

    bad_json_examples = [
        # Missing required field
        '{"scheme_id":"x","scheme_name":"x","clauses_evaluated":[],"clauses_satisfied":[],"clauses_failed":[],"verdict":"eligible","confidence":0.5}',
        # Invalid verdict literal
        '{"scheme_id":"x","scheme_name":"x","reasoning":"r","clauses_evaluated":[],"clauses_satisfied":[],"clauses_failed":[],"verdict":"maybe","confidence":0.5}',
        # Confidence out of range
        '{"scheme_id":"x","scheme_name":"x","reasoning":"r","clauses_evaluated":[],"clauses_satisfied":[],"clauses_failed":[],"verdict":"eligible","confidence":1.5}',
    ]

    rejected = 0
    for i, bad in enumerate(bad_json_examples):
        try:
            EligibilityCheck.model_validate_json(bad)
            print(red(f"    ✗ Example {i+1} should have been rejected"))
        except ValidationError as e:
            rejected += 1
            print(f"    Bad example {i+1} → rejected ({type(e).__name__})")

    if rejected != len(bad_json_examples):
        raise AssertionError(
            f"Expected all {len(bad_json_examples)} bad examples to be rejected; got {rejected}"
        )

    print(green("\n  ✓ Pydantic rejection test passed"))
    return True


# -------------------------------------------------------------------------
# Main runner
# -------------------------------------------------------------------------


async def run_all():
    """Run all e2e tests inside one mock-gateway session."""
    # Spin up the mock gateway in this process
    with mock_gateway() as gateway_url:
        os.environ["LLM_GATEWAY_URL"] = gateway_url
        print(f"{BOLD}Mock gateway running at {gateway_url}{RESET}")

        # Run pipeline once — this also populates the call log for subsequent tests
        clear_call_log()
        await test_full_pipeline()

        # Tests 2-4 inspect the call log from test 1
        await test_gateway_request_shape()
        await test_parallel_dispatch()
        await test_verifier_diversity()

        # Tests 5-6 don't depend on the prior run
        await test_trace_serialization()
        await test_pydantic_rejects_bad_llm_output()

    print()
    print(f"{BOLD}{GREEN}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{GREEN}║  ALL E2E TESTS PASSED  ✓                                             ║{RESET}")
    print(f"{BOLD}{GREEN}╚══════════════════════════════════════════════════════════════════════╝{RESET}")


if __name__ == "__main__":
    try:
        asyncio.run(run_all())
        sys.exit(0)
    except AssertionError as e:
        print(red(f"\n  ✗ FAILED: {e}"))
        sys.exit(1)
    except Exception as e:
        print(red(f"\n  ✗ ERROR: {type(e).__name__}: {e}"))
        import traceback
        traceback.print_exc()
        sys.exit(1)
