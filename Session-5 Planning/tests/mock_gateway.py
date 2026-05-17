"""
Mock LLM Gateway server for testing.

Spins up a tiny FastAPI app on a random port that mimics the V2 gateway's
contract:

  POST /v2/chat → returns {"content": "<json string>", "provider": ..., ...}

The mock inspects the JSON schema in the request and constructs a valid
response matching that schema. This lets us exercise the entire pipeline
without burning real LLM calls or needing a live gateway.

Routing logic:
- We look at the schema's title/properties to figure out which agent is calling.
- We return a hardcoded but schema-valid response for that agent.
- Each response is plausible enough that downstream agents have something
  meaningful to work with.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request


# -------------------------------------------------------------------------
# Pre-canned responses per agent (indexed by schema title)
# -------------------------------------------------------------------------

MOCK_RESPONSES: dict[str, dict] = {
    "ParsedProfile": {
        "reasoning": (
            "User describes themselves as a 32-year-old farmer in Bihar with "
            "pincode 800001. Inferred is_rural=true from 'farmer' and 'village' "
            "context. Annual income 1.2 lakh = 120000 INR. Family of 5."
        ),
        "profile": {
            "raw_input": "test user input",
            "pincode": "800001",
            "state": "Bihar",
            "age": 32,
            "annual_income_inr": 120000,
            "occupation": "farmer",
            "is_rural": True,
            "dependents": 4,
            "gender": "male",
        },
        "inferred_fields": ["is_rural"],
        "missing_critical_fields": ["social_category", "education_level"],
        "confidence": 0.85,
        "reasoning_type": "extraction",
    },
    "StateResolution": {
        "reasoning": (
            "Pincode 800001 resolved to Bihar (Patna district). User-provided "
            "state matches. Block info suggests urban area but user mentioned "
            "farming, so likely peri-urban rural — defaulting to rural."
        ),
        "pincode": "800001",
        "resolved_state": "Bihar",
        "resolved_district": "Patna",
        "is_rural_likely": True,
        "confidence": 0.92,
        "reasoning_type": "lookup",
    },
    "SchemeMatchResult": {
        "reasoning": (
            "From 10 pre-filtered candidates, prioritized rural agriculture "
            "schemes (PM-KISAN, MGNREGA, PMAY-G) given farmer + rural + low "
            "income signals. Also included Ayushman Bharat for the family's "
            "health coverage given low income."
        ),
        "candidates": [
            {
                "scheme_id": "pmkisan",
                "name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
                "match_reasoning": "User is a landholding farmer with 1 acre — direct fit for the income support criteria.",
                "initial_relevance_score": 0.95,
                "likely_category_match": "agriculture",
                "needs_eligibility_verification": True,
            },
            {
                "scheme_id": "mgnrega",
                "name": "Mahatma Gandhi National Rural Employment Guarantee Act (MGNREGA)",
                "match_reasoning": "Rural household, low income — 100 days of guaranteed wage work is highly relevant.",
                "initial_relevance_score": 0.90,
                "likely_category_match": "employment",
                "needs_eligibility_verification": True,
            },
            {
                "scheme_id": "pmay-g",
                "name": "Pradhan Mantri Awas Yojana - Gramin (PMAY-G)",
                "match_reasoning": "User mentioned kachha 2-room house — meets the dilapidated-housing criterion.",
                "initial_relevance_score": 0.88,
                "likely_category_match": "housing",
                "needs_eligibility_verification": True,
            },
        ],
        "rejected_categories": ["women_empowerment"],
        "confidence": 0.87,
        "reasoning_type": "filtering_and_matching",
    },
    "EligibilityCheck": {
        # This response is reused for each parallel eligibility call.
        # The scheme_id and scheme_name will be from whatever the request mentions
        # but the mock returns a fixed valid shape.
        "scheme_id": "pmkisan",
        "scheme_name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
        "reasoning": (
            "Walking through eligibility clauses: (1) small or marginal landholding "
            "farmer family — SATISFIED (1 acre ancestral land mentioned). "
            "(2) Husband, wife, minor children — SATISFIED (family of 5 mentioned, "
            "consistent with this). (3) Not an institutional landholder — SATISFIED. "
            "(4) Not a government employee — UNKNOWN (user didn't mention). "
            "(5) Not income-tax payer — likely SATISFIED given 1.2 lakh income is "
            "below threshold. Verdict: likely_eligible pending one clarification."
        ),
        "clauses_evaluated": [
            "Must be small or marginal landholding farmer family",
            "Family defined as husband, wife, and minor children",
            "Excludes institutional landholders",
            "Excludes serving/former central or state government employees",
            "Excludes income-tax payers",
        ],
        "clauses_satisfied": [
            "Must be small or marginal landholding farmer family",
            "Family defined as husband, wife, and minor children",
            "Excludes institutional landholders",
            "Excludes income-tax payers",
        ],
        "clauses_failed": [],
        "clauses_unknown": [
            "Excludes serving/former central or state government employees",
        ],
        "verdict": "likely_eligible",
        "confidence": 0.78,
        "follow_up_questions": [
            "Are you or anyone in your household a current or former government employee?",
        ],
    },
    "MacroContextResults": {
        "reasoning": (
            "Cross-referenced eligible schemes against MoSPI macro data for Bihar. "
            "PLFS data shows elevated rural unemployment in Bihar, making MGNREGA "
            "particularly relevant. NSS77 land/livestock data confirms small "
            "landholding patterns in the state. HCES poverty data supports the "
            "housing scheme relevance."
        ),
        "contexts": [
            {
                "scheme_id": "pmkisan",
                "scheme_name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
                "reasoning": "Macro data confirms small-landholding farmers are the dominant agricultural household type in Bihar.",
                "data_points": [
                    {
                        "indicator": "Share of small/marginal farmers",
                        "value": "approximately 91% of Bihar agricultural households",
                        "state": "Bihar",
                        "period": "NSS 77th Round",
                        "source_dataset": "NSS77",
                    }
                ],
                "relevance_narrative": (
                    "About 91% of agricultural households in Bihar are small or "
                    "marginal landholders (NSS 77th Round) — PM-KISAN's income "
                    "support targets exactly this segment, making it foundational "
                    "rather than supplementary income for your situation."
                ),
                "urgency": "high",
                "caveats": ["Latest NSS data is from 2018-19 round; current proportions may differ slightly."],
                "mcp_calls_made": 2,
                "confidence": 0.82,
            },
            {
                "scheme_id": "mgnrega",
                "scheme_name": "Mahatma Gandhi National Rural Employment Guarantee Act (MGNREGA)",
                "reasoning": "Rural unemployment data supports the timeliness of MGNREGA.",
                "data_points": [
                    {
                        "indicator": "Rural unemployment rate",
                        "value": "around 4-6% (state-level estimate)",
                        "state": "Bihar",
                        "period": "PLFS latest quarter",
                        "source_dataset": "PLFS",
                    }
                ],
                "relevance_narrative": (
                    "Bihar's rural labour force participation is among the lowest "
                    "in India per PLFS data — MGNREGA's 100-day guaranteed work "
                    "is one of the few formal employment routes for households "
                    "like yours during the lean agricultural season."
                ),
                "urgency": "high",
                "caveats": ["Aggregate state data may not reflect district-level variation."],
                "mcp_calls_made": 2,
                "confidence": 0.85,
            },
            {
                "scheme_id": "pmay-g",
                "scheme_name": "Pradhan Mantri Awas Yojana - Gramin (PMAY-G)",
                "reasoning": "Housing condition data supports urgency.",
                "data_points": [
                    {
                        "indicator": "Households in kachha dwellings",
                        "value": "around 17% of rural Bihar households",
                        "state": "Bihar",
                        "period": "NSS 78th Round",
                        "source_dataset": "NSS78",
                    }
                ],
                "relevance_narrative": (
                    "Rural Bihar still has roughly 17% of households in kachha "
                    "dwellings (NSS 78th Round). Your 2-room kachha house fits "
                    "exactly the target profile for PMAY-G's pucca house assistance."
                ),
                "urgency": "high",
                "caveats": ["PMAY-G eligibility requires SECC-2011 listing, which you should verify locally."],
                "mcp_calls_made": 2,
                "confidence": 0.80,
            },
        ],
        "confidence": 0.82,
        "reasoning_type": "macro_grounding",
    },
    "ApplicationGuide": {
        "scheme_id": "pmkisan",
        "scheme_name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
        "reasoning": "PM-KISAN application can be done via the official portal or through a Common Service Centre.",
        "steps": [
            "Visit pmkisan.gov.in or your nearest CSC",
            "Click 'New Farmer Registration' (Rural Farmer Registration)",
            "Enter Aadhaar number for OTP verification",
            "Fill in land details — Khasra/Khata number and area",
            "Upload bank account passbook copy",
            "Get verification done at the village level by Patwari/Lekhpal",
            "Track application status via the portal using Aadhaar",
        ],
        "documents_needed": ["Aadhaar card", "Land records (Khasra/Khata)", "Bank account passbook"],
        "likely_blockers": [
            "Land records may not be digitized in some Bihar villages — physical verification by Patwari is then needed",
            "Aadhaar-bank account linkage must be active",
            "Joint land ownership requires all owners' consent",
        ],
        "estimated_time_to_apply": "30-45 days from application to first payment",
        "application_url": "https://pmkisan.gov.in/",
    },
    "FinalRecommendation": {
        "reasoning_trace": (
            "Three schemes are likely eligible. Ranked by macro urgency (all 'high') "
            "and concrete benefit. PM-KISAN provides recurring annual income, MGNREGA "
            "provides emergency-employment fallback, PMAY-G provides one-time large "
            "capital benefit. Recommend PMAY-G first because housing assistance is "
            "largest one-shot benefit, then PM-KISAN as ongoing income, then MGNREGA "
            "as situational employment guarantee."
        ),
        "ranked_schemes": [
            {
                "rank": 1,
                "scheme_id": "pmay-g",
                "scheme_name": "Pradhan Mantri Awas Yojana - Gramin (PMAY-G)",
                "one_line_pitch": (
                    "Rural Bihar still has ~17% of households in kachha dwellings — "
                    "PMAY-G offers up to ₹1.20 lakh assistance for a pucca house, "
                    "which matches your current 2-room kachha situation exactly."
                ),
                "macro_context_summary": "17% of rural Bihar households still in kachha dwellings per NSS 78th Round.",
                "eligibility_status": "likely_eligible",
                "estimated_benefit_inr": "₹1,20,000 + 90-95 days MGNREGA labour + toilet support",
                "why_this_rank": "Largest one-shot benefit and most concrete improvement to your living situation.",
            },
            {
                "rank": 2,
                "scheme_id": "pmkisan",
                "scheme_name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
                "one_line_pitch": (
                    "About 91% of Bihar agricultural households are small/marginal "
                    "farmers like you — PM-KISAN gives ₹6,000/year as direct income "
                    "support, recurring annually."
                ),
                "macro_context_summary": "91% of Bihar agricultural households are small/marginal landholders.",
                "eligibility_status": "likely_eligible",
                "estimated_benefit_inr": "₹6,000 per year, paid in three ₹2,000 instalments",
                "why_this_rank": "Recurring income support — once enrolled, no annual reapplication needed.",
            },
            {
                "rank": 3,
                "scheme_id": "mgnrega",
                "scheme_name": "Mahatma Gandhi National Rural Employment Guarantee Act (MGNREGA)",
                "one_line_pitch": (
                    "Bihar's rural labour force participation is among India's lowest — "
                    "MGNREGA guarantees 100 days of wage work per household per year, "
                    "useful during your lean agricultural season."
                ),
                "macro_context_summary": "Bihar has structurally low rural LFPR per PLFS data.",
                "eligibility_status": "likely_eligible",
                "estimated_benefit_inr": "100 days × state minimum wage (approx ₹22,000 per year)",
                "why_this_rank": "Demand-driven — apply when you actually need work, not a one-time enrolment.",
            },
        ],
        "top_pick_justification": (
            "PMAY-G is the single largest concrete benefit available to you and "
            "directly addresses the kachha-house situation you described."
        ),
        "follow_up_questions_for_user": [
            "Are you listed in SECC-2011? (Required for PMAY-G)",
            "Is anyone in your household a current/former government employee? (Affects PM-KISAN)",
        ],
        "confidence_overall": 0.78,
        "reasoning_type": "synthesis_and_ranking",
    },
    "VerifierVerdict": {
        "reasoning": (
            "Ran all six checks. Eligibility grounding is good — all ranked schemes "
            "trace back to 'likely_eligible' verdicts. Macro citations include "
            "specific numbers (17%, 91%, low LFPR). No overclaiming detected. "
            "Confidence (0.78) appropriately matches the 'likely_eligible' (not "
            "'eligible') verdicts. Recommendations are coherent with the user's "
            "profile. Each scheme has actionable next steps. Minor issue: the "
            "PLFS rate for MGNREGA is given as a range rather than a specific number."
        ),
        "checks_performed": [
            "ELIGIBILITY GROUNDING: every ranked scheme has likely_eligible verdict",
            "MACRO CITATION: each one_line_pitch cites a specific statistic",
            "OVERREACH: no national inferences from single-state data",
            "CONFIDENCE INFLATION: 0.78 overall matches the 3 likely_eligible verdicts",
            "PROFILE COHERENCE: all 3 schemes align with farmer + rural + low-income + Bihar profile",
            "ACTIONABILITY: each recommendation has steps and URLs",
        ],
        "issues_found": [
            "MGNREGA pitch references 'around 4-6%' rather than a single specific number",
        ],
        "suggested_revisions": [
            "Tighten the MGNREGA macro citation to a single specific number with quarter reference",
        ],
        "final_verdict": "needs_revision",
        "confidence": 0.85,
        "reasoning_type": "evaluation",
    },
}


# -------------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------------

app = FastAPI(title="Mock LLM Gateway")

# A counter we expose to tests so they can verify call counts
call_log: list[dict] = []


@app.post("/v2/chat")
async def v2_chat(request: Request) -> dict:
    body = await request.json()
    schema = body.get("response_format", {}).get("schema", {})
    schema_title = schema.get("title", "Unknown")

    call_log.append(
        {
            "schema_title": schema_title,
            "preferred_provider": body.get("preferred_provider"),
            "reasoning": body.get("reasoning"),
            "cache": body.get("cache"),
        }
    )

    response_dict = MOCK_RESPONSES.get(schema_title)
    if response_dict is None:
        # Return a structurally minimal valid JSON for unknown schemas.
        # Tests should fail loudly if this happens.
        response_dict = {"error": f"No mock for schema {schema_title}"}

    return {
        "content": json.dumps(response_dict),
        "provider": body.get("preferred_provider", "mock"),
        "model": f"mock-{body.get('preferred_provider', 'mock')}",
        "input_tokens": 150,
        "output_tokens": 400,
        "cache_hit": body.get("cache", False),
    }


@app.get("/")
async def root() -> dict:
    return {"status": "mock gateway running", "calls_so_far": len(call_log)}


# -------------------------------------------------------------------------
# Helpers for tests
# -------------------------------------------------------------------------


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clear_call_log() -> None:
    call_log.clear()


def get_call_log() -> list[dict]:
    return list(call_log)
