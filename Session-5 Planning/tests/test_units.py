"""
Unit tests for the pure (non-LLM, non-network) modules.

Run with:
    python -m pytest tests/test_units.py -v
or simply:
    python -m tests.test_units
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure we can import from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.schemas import (
    INDIAN_STATES,
    EligibilityCheck,
    FinalRecommendation,
    ParsedProfile,
    RankedScheme,
    SchemeCandidate,
    SchemeMatchResult,
    UserProfile,
)
from src.scheme_data import (
    SEED_SCHEMES,
    get_scheme_by_id,
    load_schemes,
    search_schemes,
)
from src.tools import pincode_to_state_offline
from src.mcp_client import datasets_for_theme


# -------------------------------------------------------------------------
# Schema validation tests
# -------------------------------------------------------------------------


def test_user_profile_valid_pincode():
    p = UserProfile(raw_input="test", pincode="800001")
    assert p.pincode == "800001"


def test_user_profile_rejects_bad_pincode():
    """Pincode must be exactly 6 digits."""
    try:
        UserProfile(raw_input="test", pincode="12345")
        raise AssertionError("Should have rejected 5-digit pincode")
    except Exception as e:
        assert "string_pattern_mismatch" in str(e) or "pattern" in str(e).lower()


def test_user_profile_rejects_alphabetic_pincode():
    try:
        UserProfile(raw_input="test", pincode="abcdef")
        raise AssertionError("Should have rejected alphabetic pincode")
    except Exception:
        pass  # expected


def test_parsed_profile_requires_reasoning():
    """Every output schema enforces a `reasoning` field."""
    try:
        ParsedProfile(
            profile=UserProfile(raw_input="x"),
            confidence=0.5,
            # reasoning is missing → must fail
        )
        raise AssertionError("Should have required reasoning field")
    except Exception as e:
        assert "reasoning" in str(e).lower()


def test_eligibility_check_verdict_constrained():
    """Verdict must be one of the Literal values."""
    try:
        EligibilityCheck(
            scheme_id="x",
            scheme_name="x",
            reasoning="r",
            clauses_evaluated=["c"],
            clauses_satisfied=[],
            clauses_failed=[],
            verdict="maybe",  # not in Literal
            confidence=0.5,
        )
        raise AssertionError("Should have rejected invalid verdict")
    except Exception:
        pass


def test_confidence_must_be_between_0_and_1():
    try:
        UserProfile(raw_input="x")  # this is fine
        # but for parsed profile, confidence must be 0..1
        ParsedProfile(
            reasoning="r",
            profile=UserProfile(raw_input="x"),
            confidence=1.5,  # out of range
        )
        raise AssertionError("Should have rejected confidence > 1")
    except Exception:
        pass


def test_pydantic_json_schema_generation():
    """The killer feature: model_json_schema() must produce valid JSON Schema."""
    schema = ParsedProfile.model_json_schema()
    assert schema["type"] == "object"
    assert "reasoning" in schema["required"]
    assert "confidence" in schema["required"]


def test_pydantic_round_trip():
    """Serialize → deserialize must preserve the object."""
    original = ParsedProfile(
        reasoning="test",
        profile=UserProfile(raw_input="x", state="Bihar"),
        confidence=0.7,
    )
    json_str = original.model_dump_json()
    restored = ParsedProfile.model_validate_json(json_str)
    assert restored.profile.state == "Bihar"
    assert restored.confidence == 0.7


# -------------------------------------------------------------------------
# Scheme data tests
# -------------------------------------------------------------------------


def test_load_schemes_returns_seed_when_no_file():
    schemes = load_schemes()
    assert len(schemes) >= 5
    assert all(s.name for s in schemes)


def test_seed_data_has_required_schemes():
    """Spot-check: the most important central schemes are present."""
    schemes = load_schemes()
    names_lower = [s.name.lower() for s in schemes]
    must_have_substrings = ["mgnrega", "pm-kisan", "pmay", "ayushman", "ujjwala"]
    for needle in must_have_substrings:
        assert any(needle in n for n in names_lower), (
            f"Missing scheme containing {needle!r}"
        )


def test_search_schemes_returns_relevant():
    schemes = load_schemes()
    results = search_schemes(schemes, query="farmer", categories=["agriculture"])
    assert len(results) > 0
    # PM-KISAN should rank high for farmer + agriculture
    assert any("kisan" in s.name.lower() for s in results)


def test_search_schemes_respects_limit():
    schemes = load_schemes()
    results = search_schemes(schemes, query="rural", limit=2)
    assert len(results) <= 2


def test_get_scheme_by_id():
    schemes = load_schemes()
    scheme = get_scheme_by_id(schemes, "pmkisan")
    assert scheme is not None
    assert "kisan" in scheme.name.lower()


def test_get_scheme_by_id_returns_none_for_unknown():
    schemes = load_schemes()
    assert get_scheme_by_id(schemes, "no-such-scheme") is None


# -------------------------------------------------------------------------
# Pincode lookup tests
# -------------------------------------------------------------------------


def test_pincode_known_cities():
    cases = {
        "110001": "Delhi",
        "400001": "Maharashtra",  # Mumbai
        "560001": "Karnataka",  # Bangalore
        "600001": "Tamil Nadu",  # Chennai
        "700001": "West Bengal",  # Kolkata
        "800001": "Bihar",  # Patna
        "500001": "Telangana",  # Hyderabad
        "380001": "Gujarat",  # Ahmedabad
        "302001": "Rajasthan",  # Jaipur
        "226001": "Uttar Pradesh",  # Lucknow
    }
    for pin, expected in cases.items():
        got = pincode_to_state_offline(pin)
        assert got == expected, f"{pin}: expected {expected}, got {got}"


def test_pincode_rejects_bad_format():
    assert pincode_to_state_offline("abc") is None
    assert pincode_to_state_offline("12345") is None
    assert pincode_to_state_offline("1234567") is None


def test_pincode_handles_unknown_range():
    # 999xxx is not assigned
    assert pincode_to_state_offline("999999") is None


# -------------------------------------------------------------------------
# MCP theme mapping tests
# -------------------------------------------------------------------------


def test_datasets_for_known_themes():
    assert "PLFS" in datasets_for_theme("employment")
    assert "NFHS" in datasets_for_theme("health")
    assert "NSS78" in datasets_for_theme("housing")


def test_datasets_for_unknown_theme_falls_back():
    """Unknown themes should fall back to general — never None."""
    result = datasets_for_theme("flying_unicorns")
    assert isinstance(result, list)
    assert len(result) > 0


# -------------------------------------------------------------------------
# Indian states list integrity
# -------------------------------------------------------------------------


def test_indian_states_count():
    """28 states + 8 UTs = 36 entries."""
    assert len(INDIAN_STATES) == 36


def test_indian_states_no_duplicates():
    assert len(INDIAN_STATES) == len(set(INDIAN_STATES))


# -------------------------------------------------------------------------
# Runner
# -------------------------------------------------------------------------


def run_all():
    tests = [
        ("user_profile_valid_pincode", test_user_profile_valid_pincode),
        ("user_profile_rejects_bad_pincode", test_user_profile_rejects_bad_pincode),
        ("user_profile_rejects_alphabetic_pincode", test_user_profile_rejects_alphabetic_pincode),
        ("parsed_profile_requires_reasoning", test_parsed_profile_requires_reasoning),
        ("eligibility_check_verdict_constrained", test_eligibility_check_verdict_constrained),
        ("confidence_must_be_between_0_and_1", test_confidence_must_be_between_0_and_1),
        ("pydantic_json_schema_generation", test_pydantic_json_schema_generation),
        ("pydantic_round_trip", test_pydantic_round_trip),
        ("load_schemes_returns_seed", test_load_schemes_returns_seed_when_no_file),
        ("seed_data_has_required", test_seed_data_has_required_schemes),
        ("search_schemes_returns_relevant", test_search_schemes_returns_relevant),
        ("search_schemes_respects_limit", test_search_schemes_respects_limit),
        ("get_scheme_by_id", test_get_scheme_by_id),
        ("get_scheme_by_id_unknown", test_get_scheme_by_id_returns_none_for_unknown),
        ("pincode_known_cities", test_pincode_known_cities),
        ("pincode_rejects_bad_format", test_pincode_rejects_bad_format),
        ("pincode_handles_unknown_range", test_pincode_handles_unknown_range),
        ("datasets_for_known_themes", test_datasets_for_known_themes),
        ("datasets_for_unknown_theme", test_datasets_for_unknown_theme_falls_back),
        ("indian_states_count", test_indian_states_count),
        ("indian_states_no_duplicates", test_indian_states_no_duplicates),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}")
            print(f"      {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"{passed}/{len(tests)} tests passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
