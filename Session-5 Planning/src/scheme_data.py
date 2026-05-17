"""
Loader for the myScheme dataset.

The HuggingFace dataset `shrijayan/gov_myscheme` is the practical primary
source (the official myScheme API on API Setu is gated). Download it once
into data/myscheme.csv, then this module provides:

- a vector-free search using category + state + keyword filters + RapidFuzz
- conversion to Pydantic SchemeRecord objects

Why no vector embeddings? Because for ~4000 schemes with structured fields,
deterministic filter+fuzz is faster, free, and produces auditable matches
the agent can reason about. Add embeddings later if needed.

To download the dataset:

    huggingface-cli download shrijayan/gov_myscheme \\
        --repo-type dataset \\
        --local-dir data/

Or use the included `python -m src.scheme_data --download` helper.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz

from .schemas import SchemeRecord


DATA_DIR = Path(__file__).parent.parent / "data"
DEFAULT_CSV = DATA_DIR / "myscheme.csv"
DEFAULT_JSON = DATA_DIR / "myscheme.json"


# -------------------------------------------------------------------------
# Synthetic seed data — so the project runs even without the HF download.
# These are REAL central schemes summarized in our own words from public
# information. Replace with the full HuggingFace dataset for production.
# -------------------------------------------------------------------------

SEED_SCHEMES: list[dict] = [
    {
        "scheme_id": "mgnrega",
        "name": "Mahatma Gandhi National Rural Employment Guarantee Act (MGNREGA)",
        "ministry": "Ministry of Rural Development",
        "level": "central",
        "state": None,
        "category": ["employment", "rural", "wages"],
        "theme": "employment",
        "description": (
            "Guarantees 100 days of wage employment in a financial year to "
            "every rural household whose adult members volunteer to do "
            "unskilled manual work."
        ),
        "eligibility_text": (
            "Must be a rural Indian household. Must be willing to do "
            "unskilled manual labour. Members must be 18 years or older. "
            "Must have a job card from the Gram Panchayat."
        ),
        "benefits_text": (
            "Up to 100 days of guaranteed wage employment per household "
            "per year at notified state-wise minimum wages. Payment within "
            "15 days of work completion."
        ),
        "application_url": "https://nrega.nic.in/",
        "documents_required": ["Aadhaar", "Bank account", "Photograph"],
    },
    {
        "scheme_id": "pmkisan",
        "name": "Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
        "ministry": "Ministry of Agriculture and Farmers Welfare",
        "level": "central",
        "state": None,
        "category": ["agriculture", "farmers", "income_support"],
        "theme": "agriculture",
        "description": (
            "Income support of ₹6,000 per year to landholding farmer "
            "families, paid in three equal instalments of ₹2,000."
        ),
        "eligibility_text": (
            "Must be a small or marginal landholding farmer family. Family "
            "is defined as husband, wife and minor children. Excludes: "
            "institutional landholders, families with any member who is or "
            "was a constitutional post holder, central/state government "
            "employees (with some exceptions), pensioners drawing monthly "
            "pension above ₹10,000, income-tax payers, and professionals."
        ),
        "benefits_text": "₹6,000 per year direct benefit transfer in three instalments.",
        "application_url": "https://pmkisan.gov.in/",
        "documents_required": ["Aadhaar", "Land records", "Bank account"],
    },
    {
        "scheme_id": "pmay-g",
        "name": "Pradhan Mantri Awas Yojana - Gramin (PMAY-G)",
        "ministry": "Ministry of Rural Development",
        "level": "central",
        "state": None,
        "category": ["housing", "rural"],
        "theme": "housing",
        "description": (
            "Pucca house with basic amenities to all rural households who "
            "are houseless or living in dilapidated houses."
        ),
        "eligibility_text": (
            "Must be rural. Must be houseless OR living in 0-room or "
            "1-room kachha house. Selection based on SECC-2011 data with "
            "Gram Sabha verification. Excludes households owning motor "
            "vehicles, mechanised farm equipment, Kisan Credit Card with "
            "limit above ₹50,000, or income-tax-paying members."
        ),
        "benefits_text": (
            "Financial assistance of ₹1.20 lakh in plain areas and ₹1.30 "
            "lakh in hilly/difficult areas/IAP districts, plus 90-95 days "
            "of unskilled MGNREGA labour and toilet support under SBM-G."
        ),
        "application_url": "https://pmayg.nic.in/",
        "documents_required": ["Aadhaar", "Bank account", "SECC-2011 verification"],
    },
    {
        "scheme_id": "ayushman-bharat",
        "name": "Ayushman Bharat Pradhan Mantri Jan Arogya Yojana (AB PM-JAY)",
        "ministry": "Ministry of Health and Family Welfare",
        "level": "central",
        "state": None,
        "category": ["health", "insurance"],
        "theme": "health",
        "description": (
            "Health cover of ₹5 lakh per family per year for secondary and "
            "tertiary care hospitalization to over 12 crore poor and "
            "vulnerable families."
        ),
        "eligibility_text": (
            "Identified based on the deprivation criteria of SECC-2011 in "
            "rural areas and occupational categories in urban areas. No "
            "cap on family size, age or gender. Includes all families "
            "listed in Rashtriya Swasthya Bima Yojana (RSBY)."
        ),
        "benefits_text": (
            "Cashless hospitalization up to ₹5 lakh per family per year "
            "across empanelled public and private hospitals. Covers 1,949 "
            "medical packages."
        ),
        "application_url": "https://pmjay.gov.in/",
        "documents_required": ["Aadhaar", "Ration card", "SECC verification"],
    },
    {
        "scheme_id": "ujjwala",
        "name": "Pradhan Mantri Ujjwala Yojana (PMUY)",
        "ministry": "Ministry of Petroleum and Natural Gas",
        "level": "central",
        "state": None,
        "category": ["energy", "women", "rural"],
        "theme": "energy",
        "description": (
            "Free LPG connections to women from Below Poverty Line (BPL) "
            "households to reduce reliance on traditional cooking fuels."
        ),
        "eligibility_text": (
            "Applicant must be an adult woman. Family must not have any "
            "other LPG connection in the same household. SECC-2011 BPL "
            "list or other identified poor categories (SC/ST, PMAY-G "
            "beneficiaries, Antyodaya, forest dwellers, residents of "
            "islands, etc.)."
        ),
        "benefits_text": (
            "Free LPG connection deposit (₹1,600), free first refill and "
            "stove, then targeted subsidy on refills."
        ),
        "application_url": "https://pmuy.gov.in/",
        "documents_required": ["Aadhaar", "Bank account", "BPL/SECC proof"],
    },
    {
        "scheme_id": "jjm",
        "name": "Jal Jeevan Mission (Har Ghar Jal)",
        "ministry": "Ministry of Jal Shakti",
        "level": "central",
        "state": None,
        "category": ["water_sanitation", "rural", "infrastructure"],
        "theme": "water_sanitation",
        "description": (
            "Functional tap water connection to every rural household by 2024."
        ),
        "eligibility_text": (
            "Rural households without functional tap water connection. "
            "Implementation through state governments and village water "
            "and sanitation committees."
        ),
        "benefits_text": (
            "Free or heavily subsidised functional household tap water "
            "connection with assured supply of 55 litres per capita per day."
        ),
        "application_url": "https://jaljeevanmission.gov.in/",
        "documents_required": ["Aadhaar", "Household identification"],
    },
    {
        "scheme_id": "sukanya",
        "name": "Sukanya Samriddhi Yojana",
        "ministry": "Ministry of Finance",
        "level": "central",
        "state": None,
        "category": ["women_empowerment", "savings", "girl_child"],
        "theme": "women_empowerment",
        "description": (
            "Small deposit scheme for the girl child as part of Beti Bachao "
            "Beti Padhao."
        ),
        "eligibility_text": (
            "Account can be opened by parent/legal guardian of a girl "
            "child below 10 years of age. Only one account per girl child, "
            "maximum two accounts per family (exceptions for twins/triplets)."
        ),
        "benefits_text": (
            "Tax-free interest at notified rate (currently 8.2% p.a.), "
            "tax deduction under 80C, partial withdrawal at age 18, full "
            "maturity at 21."
        ),
        "application_url": "https://www.indiapost.gov.in/",
        "documents_required": ["Birth certificate of girl child", "Aadhaar of guardian"],
    },
    {
        "scheme_id": "mudra",
        "name": "Pradhan Mantri Mudra Yojana (PMMY)",
        "ministry": "Ministry of Finance",
        "level": "central",
        "state": None,
        "category": ["industry_msme", "entrepreneurship", "loans"],
        "theme": "industry_msme",
        "description": (
            "Loans up to ₹10 lakh to non-corporate, non-farm small/micro "
            "enterprises through Member Lending Institutions (banks, NBFCs, MFIs)."
        ),
        "eligibility_text": (
            "Any Indian citizen with a business plan for a non-farm "
            "income-generating activity in manufacturing, processing, "
            "trading or services sector. No collateral required up to ₹10 lakh."
        ),
        "benefits_text": (
            "Shishu loans up to ₹50,000; Kishore loans ₹50,001–₹5 lakh; "
            "Tarun loans ₹5–10 lakh. No collateral, no processing fee for "
            "Shishu category."
        ),
        "application_url": "https://www.mudra.org.in/",
        "documents_required": ["Aadhaar", "PAN", "Business plan/proof", "Bank statements"],
    },
    {
        "scheme_id": "jandhan",
        "name": "Pradhan Mantri Jan Dhan Yojana (PMJDY)",
        "ministry": "Ministry of Finance",
        "level": "central",
        "state": None,
        "category": ["financial_inclusion", "banking"],
        "theme": "financial_inclusion",
        "description": (
            "Zero-balance bank account with RuPay debit card, accident "
            "insurance, and overdraft facility — the foundation of India's "
            "financial inclusion push."
        ),
        "eligibility_text": (
            "Any Indian resident, including minors above 10 years with a "
            "guardian. No minimum balance requirement."
        ),
        "benefits_text": (
            "Zero-balance bank account, free RuPay debit card, accident "
            "insurance cover of ₹2 lakh, overdraft of ₹10,000 after six "
            "months of satisfactory operation."
        ),
        "application_url": "https://pmjdy.gov.in/",
        "documents_required": ["Aadhaar OR any officially valid document"],
    },
    {
        "scheme_id": "midday-meal",
        "name": "PM POSHAN (Mid-Day Meal Scheme)",
        "ministry": "Ministry of Education",
        "level": "central",
        "state": None,
        "category": ["education", "nutrition", "children"],
        "theme": "education",
        "description": (
            "Hot cooked meal to all school children in government and "
            "government-aided schools from Classes 1 to 8."
        ),
        "eligibility_text": (
            "All children enrolled in Classes 1 to 8 in government, "
            "government-aided, local body schools, and special training "
            "centres."
        ),
        "benefits_text": (
            "One hot cooked meal per school day with prescribed nutritional "
            "content (450 kcal and 12g protein for primary; 700 kcal and "
            "20g protein for upper primary)."
        ),
        "application_url": "https://pmposhan.education.gov.in/",
        "documents_required": ["School enrollment"],
    },
]


def load_schemes() -> list[SchemeRecord]:
    """Load schemes from disk if present, else fall back to seed data."""
    if DEFAULT_JSON.exists():
        records = json.loads(DEFAULT_JSON.read_text(encoding="utf-8"))
        return [_record_from_dict(r) for r in records]

    if DEFAULT_CSV.exists():
        records = []
        with DEFAULT_CSV.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(_record_from_dict(row))
        return records

    # Fall back to seeds — fine for development and demos.
    return [_record_from_dict(s) for s in SEED_SCHEMES]


def _record_from_dict(d: dict) -> SchemeRecord:
    return SchemeRecord(
        scheme_id=str(d.get("scheme_id") or d.get("id") or d.get("name", "")).lower().replace(" ", "-"),
        name=d.get("name", ""),
        ministry=d.get("ministry"),
        state=d.get("state"),
        level=d.get("level", "central"),
        category=d.get("category") if isinstance(d.get("category"), list)
                 else [c.strip() for c in str(d.get("category", "")).split(",") if c.strip()],
        description=d.get("description", ""),
        eligibility_text=d.get("eligibility_text", ""),
        benefits_text=d.get("benefits_text", ""),
        application_url=d.get("application_url"),
        documents_required=d.get("documents_required") if isinstance(d.get("documents_required"), list)
                           else [doc.strip() for doc in str(d.get("documents_required", "")).split(",") if doc.strip()],
    )


def search_schemes(
    schemes: list[SchemeRecord],
    *,
    query: str = "",
    state: str | None = None,
    categories: list[str] | None = None,
    limit: int = 20,
) -> list[SchemeRecord]:
    """
    Filter + fuzzy-rank schemes without LLM involvement.

    The LLM will further reason about these candidates — this is just the
    coarse retrieval step. Returns top-`limit` schemes by combined score.
    """
    candidates: list[tuple[float, SchemeRecord]] = []

    for s in schemes:
        score = 0.0

        # Hard filter: state schemes must match if state is known.
        if s.level in ("state", "ut") and state and s.state and s.state != state:
            continue

        # Category overlap.
        if categories:
            overlap = len(set(c.lower() for c in s.category) & set(c.lower() for c in categories))
            if overlap == 0:
                # Don't skip — but heavily penalize.
                score -= 0.3
            else:
                score += 0.4 * overlap

        # Query similarity.
        if query:
            haystack = f"{s.name} {s.description} {s.eligibility_text}"
            score += fuzz.partial_ratio(query.lower(), haystack.lower()) / 100.0

        # Central schemes are universally available — small bonus when no state filter.
        if s.level == "central":
            score += 0.1

        if score > 0:
            candidates.append((score, s))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in candidates[:limit]]


def get_scheme_by_id(schemes: list[SchemeRecord], scheme_id: str) -> SchemeRecord | None:
    for s in schemes:
        if s.scheme_id == scheme_id:
            return s
    return None
