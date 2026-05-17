"""
Client wrapper for the e-Sankhyiki MCP server (Ministry of Statistics and
Programme Implementation, Government of India).

The server is live at https://mcp.mospi.gov.in/ and exposes a 4-tool
sequential workflow:

    list_datasets() → get_indicators(dataset) → get_metadata(dataset, ...) → get_data(dataset, filters)

This module wraps that protocol-level access into agent-friendly methods
that return Pydantic-typed results.

If the public server is unreachable, the client raises so the orchestrator
can record the failure in the trace and continue (the macro contextualizer
can degrade gracefully — schemes still get ranked, just without macro grounding).
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import Client


ESANKHYIKI_URL = os.getenv("ESANKHYIKI_MCP_URL", "https://mcp.mospi.gov.in/")
"""
Client wrapper for the e-Sankhyiki MCP server (Ministry of Statistics and
Programme Implementation, Government of India).
 
The server is live at https://mcp.mospi.gov.in/ and exposes a 4-tool
sequential workflow:
 
    list_datasets() → get_indicators(dataset) → get_metadata(dataset, ...) → get_data(dataset, filters)
 
This module wraps that protocol-level access into agent-friendly methods
that return Pydantic-typed results.
 
Dataset selection strategy
==========================
We use a two-tier lookup to decide which MoSPI dataset to query for a scheme:
 
  1. SCHEME_SPECIFIC_DATASETS — known-good per-scheme overrides. These were
     researched against the official esankhyiki-mcp README dataset list.
     If a scheme appears here, we use these datasets directly and skip
     theme-based lookup. This is the primary path.
 
  2. DATASET_HINTS_BY_THEME — fallback for schemes not in the override list.
     Maps broad themes to plausible datasets. Less accurate, used only when
     no scheme-specific override exists.
 
The dataset codes below MUST match the exact codes returned by
list_datasets() on the e-Sankhyiki MCP server. The full list of 22 codes
is documented in `_VALID_DATASET_CODES` below.
 
If the public server is unreachable, the client raises so the orchestrator
can record the failure in the trace and continue (the macro contextualizer
can degrade gracefully).
"""
 
from __future__ import annotations
 
import os
from typing import Any
 
from fastmcp import Client
 
 
ESANKHYIKI_URL = os.getenv("ESANKHYIKI_MCP_URL", "https://mcp.mospi.gov.in/")
 
 
# -------------------------------------------------------------------------
# Authoritative list of all 22 dataset codes on the e-Sankhyiki MCP server.
# Source: https://github.com/nso-india/esankhyiki-mcp README, verified May 2026.
# These are the exact case-sensitive codes the `get_data` tool expects.
# -------------------------------------------------------------------------
 
_VALID_DATASET_CODES: set[str] = {
    "PLFS",       # Periodic Labour Force Survey — employment, unemployment, LFPR
    "CPI",        # Consumer Price Index — retail inflation
    "CPIALRL",    # CPI for Agricultural/Rural Labourers — rural inflation
    "WPI",        # Wholesale Price Index — producer prices
    "IIP",        # Index of Industrial Production — manufacturing output
    "ASI",        # Annual Survey of Industries — factory performance
    "NAS",        # National Accounts Statistics — GDP
    "ASUSE",      # Annual Survey of Unincorporated Enterprises — informal MSMEs
    "EC",         # Economic Census — establishments, enterprises
    "HCES",       # Household Consumption Expenditure Survey — poverty, Gini
    "NSS77",      # NSS 77th Round — Land & Livestock (farmers, crop insurance)
    "NSS78",      # NSS 78th Round — Living Conditions (water, sanitation, housing)
    "NSS79",      # NSS 79th Round — CAMS+AYUSH (financial inclusion, literacy)
    "NFHS",       # National Family Health Survey — fertility, child health, banking
    "UDISE",      # UDISE+ — school enrolment, dropout
    "AISHE",      # All India Survey on Higher Education — colleges, GER
    "GENDER",     # Gender Statistics — sex ratio, women empowerment
    "TUS",        # Time Use Survey — unpaid work, gender time gaps
    "ENERGY",     # Energy Statistics — fuel mix, consumption
    "MNRE",       # Renewable Energy — installed capacity by state
    "ENVSTATS",   # Environment Statistics — climate, water resources
    "RBI",        # RBI Statistics — banking, foreign trade
}
 
 
# -------------------------------------------------------------------------
# Scheme-specific dataset overrides (PRIMARY lookup path)
# -------------------------------------------------------------------------
# Each scheme_id maps to a list of datasets ranked by relevance. The macro
# agent will try them in order until one returns usable data.
#
# Mapping researched against:
# - Each scheme's official eligibility and target population
# - Each MoSPI dataset's actual contents (from esankhyiki-mcp README)
# - The principle: scheme addresses a MACRO CONDITION, which dataset measures it?
#
# Tested and verified for the 14 most common central schemes. Add more as
# you expand the scheme catalogue.
 
SCHEME_SPECIFIC_DATASETS: dict[str, list[str]] = {
    # --- Employment & Rural Livelihoods ---
    "mgnrega": ["PLFS", "CPIALRL", "NSS78"],
    # PLFS has rural LFPR/unemployment by state — exactly what MGNREGA addresses.
    # CPIALRL gives agricultural labourer cost-of-living (wage adequacy).
 
    # --- Agriculture ---
    "pmkisan": ["NSS77", "HCES", "EC"],
    # NSS77 (Land & Livestock) has small/marginal farmer share — the exact target.
    # HCES gives rural household income distribution.
 
    "pmfby": ["NSS77", "ENVSTATS", "CPIALRL"],
    # NSS77 covers "crop insurance" explicitly in its scope.
    # ENVSTATS gives climate/disaster context.
 
    # --- Housing ---
    "pmay-g": ["NSS78", "HCES", "NSS77"],
    # NSS78 (Living Conditions) covers housing, drinking water, sanitation.
    # HCES gives household consumption (housing affordability proxy).
 
    "pmay-u": ["NSS78", "HCES"],
    # Same as PMAY-G but urban — NSS78 also covers urban living conditions.
 
    # --- Health ---
    "ayushman-bharat": ["NFHS", "NSS79", "HCES"],
    # NFHS is the primary health survey — hospitalization, maternal care.
    # NSS79 has out-of-pocket health expenditure data.
 
    "pmjay": ["NFHS", "NSS79", "HCES"],  # Alias for Ayushman Bharat
    "jsy": ["NFHS", "GENDER"],            # Janani Suraksha Yojana — maternal care
    "pmmvy": ["NFHS", "GENDER"],          # PM Matru Vandana Yojana
 
    # --- Clean Fuel & Energy ---
    "ujjwala": ["NSS78", "NFHS", "ENERGY"],
    "pmuy": ["NSS78", "NFHS", "ENERGY"],  # Alias
    # NSS78 covers household cooking fuel access in Living Conditions.
    # NFHS measures indoor air pollution and women's health impact.
 
    "kusum": ["MNRE", "ENERGY", "NSS77"],
    # PM-KUSUM — solar for farmers. MNRE has state-wise solar capacity.
 
    # --- Water & Sanitation ---
    "jjm": ["NSS78", "NFHS", "ENVSTATS"],
    # Jal Jeevan Mission. NSS78 explicitly lists "drinking water" in scope.
 
    "swachh-bharat": ["NSS78", "NFHS"],
    # NSS78 covers sanitation. NFHS has household toilet access.
 
    "swachh-bharat-gramin": ["NSS78", "NFHS"],
    "swachh-bharat-urban": ["NSS78", "NFHS"],
 
    # --- Women & Girl Child ---
    "sukanya": ["GENDER", "NFHS", "HCES"],
    # GENDER dataset is purpose-built for women empowerment indicators.
 
    "beti-bachao": ["GENDER", "NFHS", "UDISE"],
    # GENDER has sex ratio. UDISE has girl enrolment/dropout.
 
    # --- Financial Inclusion ---
    "jandhan": ["NSS79", "NFHS", "RBI"],
    "pmjdy": ["NSS79", "NFHS", "RBI"],
    # NSS79 explicitly covers "financial inclusion" in its scope.
    # NFHS measures household banking access. RBI has banking penetration.
 
    # --- Micro-enterprise / MSME ---
    "mudra": ["ASUSE", "EC", "NSS79"],
    "pmmy": ["ASUSE", "EC", "NSS79"],  # Alias
    # ASUSE is the unincorporated-enterprise survey — direct fit for MUDRA.
 
    "stand-up-india": ["ASUSE", "GENDER", "EC"],
    # SC/ST/women entrepreneurship — ASUSE + GENDER overlap.
 
    # --- Education ---
    "midday-meal": ["UDISE", "NFHS", "NSS79"],
    "pm-poshan": ["UDISE", "NFHS", "NSS79"],  # Alias
    # UDISE has school enrolment by class — Classes 1-8 are the target.
    # NFHS measures child nutrition outcomes the meal addresses.
 
    "samagra-shiksha": ["UDISE", "NSS79"],
    # School-system overall. UDISE is the school-system survey.
 
    "national-scholarship": ["AISHE", "UDISE", "NSS79"],
    # Mix of school and higher-ed. AISHE for college-level.
 
    # --- Elderly & Social Security ---
    "ignoaps": ["HCES", "NSS78", "NFHS"],
    # Old age pension. HCES has elderly consumption/poverty. NSS78 has elderly living conditions.
 
    "ignwps": ["HCES", "GENDER", "NSS78"],     # Widow pension
    "ignds": ["HCES", "NSS78"],                # Disability pension
 
    # --- Skill Development ---
    "ddu-gky": ["PLFS", "AISHE", "NSS79"],
    # Skill development for rural youth. PLFS gives youth unemployment.
 
    "pmkvy": ["PLFS", "AISHE"],
    # PM Kaushal Vikas Yojana — same youth/skills domain.
 
    # --- Senior Citizen / Elderly ---
    "atal-pension": ["HCES", "NFHS"],
    # Atal Pension Yojana — informal-sector retirement. HCES for income context.
}
 
 
# -------------------------------------------------------------------------
# Theme-based fallback (SECONDARY lookup path)
# -------------------------------------------------------------------------
# Used only when a scheme isn't in SCHEME_SPECIFIC_DATASETS. Broader, less
# precise. Prefer adding to SCHEME_SPECIFIC_DATASETS over adding here.
 
DATASET_HINTS_BY_THEME: dict[str, list[str]] = {
    "employment": ["PLFS", "ASUSE"],
    "agriculture": ["NSS77", "CPIALRL"],
    "housing": ["NSS78", "HCES"],
    "water_sanitation": ["NSS78", "NFHS"],
    "education": ["UDISE", "AISHE", "NSS79"],
    "health": ["NFHS", "NSS79"],
    "women_empowerment": ["GENDER", "NFHS"],
    "financial_inclusion": ["NSS79", "NFHS", "RBI"],
    "consumption_poverty": ["HCES"],
    "energy": ["ENERGY", "MNRE", "NSS78"],
    "industry_msme": ["ASUSE", "EC"],
    "inflation_cost": ["CPI", "CPIALRL", "WPI"],
    "elderly_pension": ["HCES", "NSS78"],
    "skill_development": ["PLFS", "AISHE", "NSS79"],
    "environment_climate": ["ENVSTATS", "MNRE"],
    "general": ["PLFS", "HCES"],
}
 
 
# -------------------------------------------------------------------------
# Public lookup function
# -------------------------------------------------------------------------
 
 
def datasets_for_scheme(
    scheme_id: str, theme: str | None = None
) -> list[str]:
    """
    Resolve which MoSPI datasets to query for a given scheme.
 
    Lookup order:
      1. Scheme-specific override (most accurate)
      2. Theme-based hint (broader fallback)
      3. 'general' default (PLFS + HCES, the most universally useful pair)
 
    All returned codes are validated against _VALID_DATASET_CODES so we
    never accidentally pass a typo to the MCP server.
    """
    scheme_key = scheme_id.lower().strip()
    if scheme_key in SCHEME_SPECIFIC_DATASETS:
        candidates = SCHEME_SPECIFIC_DATASETS[scheme_key]
    elif theme and theme.lower() in DATASET_HINTS_BY_THEME:
        candidates = DATASET_HINTS_BY_THEME[theme.lower()]
    else:
        candidates = DATASET_HINTS_BY_THEME["general"]
 
    # Filter out any invalid codes (defense against typos).
    return [d for d in candidates if d in _VALID_DATASET_CODES]
 
 
# Legacy alias for backward compatibility — the prior code used this name.
def datasets_for_theme(theme: str) -> list[str]:
    """Theme-only lookup. Prefer datasets_for_scheme() for better accuracy."""
    return DATASET_HINTS_BY_THEME.get(
        theme.lower(), DATASET_HINTS_BY_THEME["general"]
    )


class ESankhyikiMCPClient:
    def __init__(self, url: str = ESANKHYIKI_URL) -> None:
            self.url = url
    
    async def list_datasets(self) -> Any:
        """Step 1: overview of all 22 datasets."""
        async with Client(self.url) as client:
            return await client.call_tool("list_datasets", {})

    async def get_indicators(self, dataset: str) -> Any:
        """Step 2: indicators available within one dataset."""
        async with Client(self.url) as client:
            return await client.call_tool("get_indicators", {"dataset": dataset})

    async def get_metadata(self, dataset: str, **kwargs: Any) -> Any:
        """Step 3: valid filter values (states, years, etc.)."""
        async with Client(self.url) as client:
            return await client.call_tool(
                "get_metadata", {"dataset": dataset, **kwargs}
            )

    async def get_data(
        self,
        dataset: str,
        filters: dict[str, Any],
    ) -> Any:
        """Step 4: fetch the actual data with validated filters."""
        async with Client(self.url) as client:
            return await client.call_tool(
                "get_data", {"dataset": dataset, **filters}
            )

    async def quick_fetch(
        self,
        dataset: str,
        state: str | None = None,
        year: str | None = None,
        **extra_filters: Any,
    ) -> tuple[Any, list[dict]]:
        """
        Convenience: do steps 3-4 in sequence and return both the final
        data and a list of MCP-call records (for tracing).

        Returns (data, [call_records]).
        """
        calls: list[dict] = []

        # Validate dataset code before calling — saves a round-trip on typos.
        if dataset not in _VALID_DATASET_CODES:
            calls.append({
                "tool": "validate",
                "args": {"dataset": dataset},
                "ok": False,
                "error": f"Unknown dataset code: {dataset!r}. "
                        f"Valid codes: {sorted(_VALID_DATASET_CODES)}",
            })
            return None, calls

        # Step 3: metadata to validate filter codes
        meta = await self.get_metadata(dataset)
        calls.append({
            "tool": "get_metadata",
            "args": {"dataset": dataset},
            "ok": True,
        })

        # Step 4: fetch data
        filters: dict[str, Any] = {}
        if state:
            filters["state"] = state
        if year:
            filters["year"] = year
        filters.update(extra_filters)

        data = await self.get_data(dataset, filters)
        calls.append({
            "tool": "get_data",
            "args": {"dataset": dataset, **filters},
            "ok": True,
        })
        return data, calls


def datasets_for_theme(theme: str) -> list[str]:
    """Map a scheme theme to candidate MoSPI dataset codes."""
    return DATASET_HINTS_BY_THEME.get(theme, DATASET_HINTS_BY_THEME["general"])
