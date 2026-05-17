"""
Pydantic schemas for the SchemeContext multi-agent advisor.

Every LLM input and output is typed. Every model has:
- `reasoning` (so the model has a place to think inside structured JSON)
- `confidence` (forces self-evaluation)
- `reasoning_type` (categorical tag for the cognitive task)

This is the spine of the project. Read this file first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# -------------------------------------------------------------------------
# User profile (input)
# -------------------------------------------------------------------------

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand",
    "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]


class UserProfile(BaseModel):
    """The citizen's raw input — what they tell us about themselves."""

    raw_input: str = Field(
        ...,
        description="Free-text description from the user. The parser agent extracts structured fields from this.",
    )
    pincode: Optional[str] = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="Indian pincode if provided (used to resolve state).",
    )
    state: Optional[str] = Field(default=None, description="Explicit state if given.")
    age: Optional[int] = Field(default=None, ge=0, le=120)
    annual_income_inr: Optional[float] = Field(default=None, ge=0)
    occupation: Optional[str] = None
    education_level: Optional[str] = None
    gender: Optional[Literal["male", "female", "other"]] = None
    is_rural: Optional[bool] = None
    has_disability: Optional[bool] = None
    social_category: Optional[Literal["general", "obc", "sc", "st"]] = None
    dependents: Optional[int] = Field(default=None, ge=0)


# -------------------------------------------------------------------------
# Agent 1: Profile Parser
# -------------------------------------------------------------------------


class ParsedProfile(BaseModel):
    """Structured profile extracted from the user's free-text input."""

    reasoning: str = Field(
        ...,
        description="Walk through how each field was inferred or marked unknown.",
    )
    profile: UserProfile
    inferred_fields: List[str] = Field(
        default_factory=list,
        description="Fields the agent inferred rather than read directly (e.g., 'rural' from 'village').",
    )
    missing_critical_fields: List[str] = Field(
        default_factory=list,
        description="Fields that would meaningfully change recommendations if known.",
    )
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["extraction"] = "extraction"

    @field_validator("profile")
    @classmethod
    def state_must_be_valid(cls, v: UserProfile) -> UserProfile:
        if v.state and v.state not in INDIAN_STATES:
            # Don't reject — let the State Resolver agent normalize it.
            pass
        return v


# -------------------------------------------------------------------------
# Agent 2: State Resolver (pincode → state)
# -------------------------------------------------------------------------


class StateResolution(BaseModel):
    reasoning: str
    pincode: Optional[str] = None
    resolved_state: Optional[str] = None
    resolved_district: Optional[str] = None
    is_rural_likely: Optional[bool] = Field(
        default=None,
        description="Heuristic from pincode — rural pincodes have known patterns.",
    )
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["lookup"] = "lookup"


# -------------------------------------------------------------------------
# Scheme data (loaded from local dataset, NOT LLM output)
# -------------------------------------------------------------------------


class SchemeRecord(BaseModel):
    """One scheme from the myScheme dataset."""

    scheme_id: str
    name: str
    ministry: Optional[str] = None
    state: Optional[str] = Field(default=None, description="State if state-specific; None for central.")
    level: Literal["central", "state", "ut"] = "central"
    category: List[str] = Field(default_factory=list)
    description: str
    eligibility_text: str
    benefits_text: str
    application_url: Optional[str] = None
    documents_required: List[str] = Field(default_factory=list)
    last_updated: Optional[str] = None


# -------------------------------------------------------------------------
# Agent 3: Scheme Matcher — finds candidates
# -------------------------------------------------------------------------


class SchemeCandidate(BaseModel):
    """A scheme the matcher thinks could be relevant."""

    scheme_id: str
    name: str
    match_reasoning: str = Field(
        ...,
        description="Specifically why this scheme is a candidate for THIS profile.",
    )
    initial_relevance_score: float = Field(ge=0, le=1)
    likely_category_match: str
    needs_eligibility_verification: bool = True


class SchemeMatchResult(BaseModel):
    reasoning: str = Field(
        ...,
        description="How the matcher narrowed thousands of schemes down to these candidates.",
    )
    candidates: List[SchemeCandidate] = Field(
        ...,
        max_length=15,
        description="Top scheme candidates before eligibility verification.",
    )
    rejected_categories: List[str] = Field(
        default_factory=list,
        description="Categories explicitly ruled out and why.",
    )
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["filtering_and_matching"] = "filtering_and_matching"


# -------------------------------------------------------------------------
# Agent 4: Eligibility Checker — verifies each candidate
# -------------------------------------------------------------------------


class EligibilityCheck(BaseModel):
    """Per-scheme verification of every clause."""

    scheme_id: str
    scheme_name: str
    reasoning: str = Field(
        ...,
        description="Walk through each eligibility clause and verify against the profile.",
    )
    clauses_evaluated: List[str] = Field(
        ...,
        description="Each eligibility clause from the scheme text.",
    )
    clauses_satisfied: List[str]
    clauses_failed: List[str]
    clauses_unknown: List[str] = Field(
        default_factory=list,
        description="Clauses we cannot verify without more user information.",
    )
    verdict: Literal["eligible", "likely_eligible", "needs_info", "ineligible"]
    confidence: float = Field(ge=0, le=1)
    follow_up_questions: List[str] = Field(
        default_factory=list,
        description="Questions to ask the user to resolve 'needs_info'.",
    )


class EligibilityResults(BaseModel):
    reasoning: str
    checks: List[EligibilityCheck]
    self_check_passed: bool = Field(
        ...,
        description="Did the agent verify each clause is actually grounded in the scheme text?",
    )
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["adversarial_verification"] = "adversarial_verification"


# -------------------------------------------------------------------------
# Agent 5: Macro Contextualizer — calls e-Sankhyiki MCP
# -------------------------------------------------------------------------


class MacroDataPoint(BaseModel):
    """One macro statistic relevant to a scheme."""

    indicator: str = Field(description="e.g. 'Rural unemployment rate'")
    value: str = Field(description="e.g. '8.2%' — keep as string since MCP returns mixed types.")
    state: Optional[str] = None
    period: Optional[str] = None
    source_dataset: str = Field(description="MoSPI dataset code, e.g. PLFS, CPI, HCES.")
    raw_response: Optional[dict] = Field(
        default=None,
        description="Raw MCP response for traceability.",
    )


class MacroContextForScheme(BaseModel):
    """Why a scheme is relevant given current macro data."""

    scheme_id: str
    scheme_name: str
    reasoning: str
    data_points: List[MacroDataPoint] = Field(default_factory=list)
    relevance_narrative: str = Field(
        ...,
        description="One paragraph explaining why this scheme is relevant NOW given the data. Must reference specific numbers.",
    )
    urgency: Literal["high", "medium", "low"]
    caveats: List[str] = Field(
        default_factory=list,
        description="Why the macro inference could be misleading.",
    )
    mcp_calls_made: int = Field(ge=0, description="How many calls to the e-Sankhyiki MCP server.")
    confidence: float = Field(ge=0, le=1)


class MacroContextResults(BaseModel):
    reasoning: str
    contexts: List[MacroContextForScheme]
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["macro_grounding"] = "macro_grounding"


# -------------------------------------------------------------------------
# Agent 6: Application Drafter
# -------------------------------------------------------------------------


class ApplicationGuide(BaseModel):
    scheme_id: str
    scheme_name: str
    reasoning: str
    steps: List[str] = Field(
        ...,
        min_length=1,
        description="Concrete step-by-step application guide.",
    )
    documents_needed: List[str]
    likely_blockers: List[str] = Field(
        default_factory=list,
        description="Common reasons applications fail for this scheme.",
    )
    estimated_time_to_apply: Optional[str] = None
    application_url: Optional[str] = None


# -------------------------------------------------------------------------
# Agent 7: Priority Ranker — final synthesis
# -------------------------------------------------------------------------


class RankedScheme(BaseModel):
    rank: int = Field(ge=1)
    scheme_id: str
    scheme_name: str
    one_line_pitch: str = Field(
        ...,
        description="Single sentence: why this scheme, why now.",
    )
    macro_context_summary: str
    eligibility_status: Literal["eligible", "likely_eligible", "needs_info"]
    estimated_benefit_inr: Optional[str] = Field(
        default=None,
        description="If quantifiable from the scheme text.",
    )
    why_this_rank: str


class FinalRecommendation(BaseModel):
    reasoning_trace: str = Field(
        ...,
        description="Overall reasoning for the ranking choices.",
    )
    ranked_schemes: List[RankedScheme]
    top_pick_justification: str
    follow_up_questions_for_user: List[str] = Field(default_factory=list)
    confidence_overall: float = Field(ge=0, le=1)
    reasoning_type: Literal["synthesis_and_ranking"] = "synthesis_and_ranking"


# -------------------------------------------------------------------------
# Agent 8: Verifier — adversarial review
# -------------------------------------------------------------------------


class VerifierVerdict(BaseModel):
    reasoning: str
    checks_performed: List[str]
    issues_found: List[str]
    suggested_revisions: List[str]
    final_verdict: Literal["approved", "needs_revision", "rejected"]
    confidence: float = Field(ge=0, le=1)
    reasoning_type: Literal["evaluation"] = "evaluation"


# -------------------------------------------------------------------------
# Trace (V5-style telemetry)
# -------------------------------------------------------------------------


class TraceEvent(BaseModel):
    turn: int
    kind: Literal[
        "llm_call",
        "tool_call",
        "mcp_call",
        "tool_result",
        "agent_handoff",
        "verdict",
    ]
    timestamp: datetime
    agent_name: str
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_hit: Optional[bool] = None
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    mcp_server: Optional[str] = None
    mcp_tool: Optional[str] = None
    text_payload: Optional[str] = None
    error: Optional[str] = None


class AgentTrace(BaseModel):
    user_input: str
    started_at: datetime
    events: List[TraceEvent] = Field(default_factory=list)

    def add(self, event: TraceEvent) -> None:
        event.turn = len(self.events)
        self.events.append(event)

    def summary(self) -> dict:
        return {
            "total_events": len(self.events),
            "llm_calls": sum(1 for e in self.events if e.kind == "llm_call"),
            "mcp_calls": sum(1 for e in self.events if e.kind == "mcp_call"),
            "tool_calls": sum(1 for e in self.events if e.kind == "tool_call"),
            "total_input_tokens": sum(e.input_tokens or 0 for e in self.events),
            "total_output_tokens": sum(e.output_tokens or 0 for e in self.events),
            "total_latency_ms": sum(e.latency_ms or 0 for e in self.events),
            "providers_used": sorted(
                {e.provider for e in self.events if e.provider}
            ),
            "mcp_servers_used": sorted(
                {e.mcp_server for e in self.events if e.mcp_server}
            ),
        }
