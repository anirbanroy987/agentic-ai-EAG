"""
Additional tools used by the agents.

- pincode_to_state: maps Indian PINCODEs to state. Uses an offline lookup
  table; falls back to api.postalpincode.in if not found locally.
- web_search: only used for "is there a newer scheme" fallback queries.

All tools return strings or simple dicts — MCP convention. They never raise;
on failure they return error strings the agent can reason about.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx


USER_AGENT = "SchemeContext/0.1 (assignment-project)"


# -------------------------------------------------------------------------
# Pincode → state
# -------------------------------------------------------------------------
# We use the first 2-3 digits of an Indian pincode to identify state. This is
# a deterministic mapping defined by India Post; the table below covers all
# states/UTs by their canonical prefix ranges. Source: India Post pincode
# directory; encoded by ranges to keep the file compact.

# Each tuple is (range_start, range_end_inclusive, state_name).
PINCODE_STATE_RANGES: list[tuple[int, int, str]] = [
    # 11xxxx — Delhi
    (110000, 110999, "Delhi"),
    # 12-13xxxx — Haryana
    (121000, 136999, "Haryana"),
    # 14-15xxxx, 16xxxx (parts) — Punjab
    (140000, 160099, "Punjab"),
    # 16xxxx (parts) — Chandigarh
    (160100, 160101, "Chandigarh"),
    (160102, 160199, "Punjab"),
    # 17xxxx — Himachal Pradesh
    (170000, 177999, "Himachal Pradesh"),
    # 18-19xxxx — Jammu and Kashmir / Ladakh
    (180000, 194999, "Jammu and Kashmir"),
    (194100, 194999, "Ladakh"),
    # 20-28xxxx — Uttar Pradesh / Uttarakhand
    (200000, 249999, "Uttar Pradesh"),
    (244700, 249999, "Uttarakhand"),
    (263000, 263999, "Uttarakhand"),
    # 30-34xxxx — Rajasthan
    (301000, 345999, "Rajasthan"),
    # 36-39xxxx — Gujarat / Dadra
    (360000, 396999, "Gujarat"),
    (396200, 396299, "Dadra and Nagar Haveli and Daman and Diu"),
    # 40-44xxxx — Maharashtra / Goa
    (400000, 445999, "Maharashtra"),
    (403000, 403999, "Goa"),
    # 45-48xxxx — Madhya Pradesh / Chhattisgarh
    (450000, 488999, "Madhya Pradesh"),
    (490000, 497999, "Chhattisgarh"),
    # 50-53xxxx — Andhra Pradesh / Telangana
    (500000, 509999, "Telangana"),
    (515000, 535999, "Andhra Pradesh"),
    # 56-59xxxx — Karnataka
    (560000, 591999, "Karnataka"),
    # 60-64xxxx — Tamil Nadu / Puducherry
    (600000, 643999, "Tamil Nadu"),
    (605000, 605999, "Puducherry"),
    # 67-69xxxx — Kerala / Lakshadweep
    (670000, 695999, "Kerala"),
    (682500, 682999, "Lakshadweep"),
    # 70-74xxxx — West Bengal / Andaman
    (700000, 743999, "West Bengal"),
    (744100, 744399, "Andaman and Nicobar Islands"),
    # 75-77xxxx — Odisha
    (750000, 770999, "Odisha"),
    # 78xxxx — Assam / Northeast
    (780000, 788999, "Assam"),
    (790000, 792999, "Arunachal Pradesh"),
    (793000, 794999, "Meghalaya"),
    (795000, 795999, "Manipur"),
    (796000, 796999, "Mizoram"),
    (797000, 798999, "Nagaland"),
    (799000, 799999, "Tripura"),
    # 80-85xxxx — Bihar / Jharkhand
    (800000, 855999, "Bihar"),
    (813000, 814999, "Jharkhand"),
    (815000, 835999, "Jharkhand"),
    # 73-737xxx — Sikkim
    (737100, 737999, "Sikkim"),
]


def pincode_to_state_offline(pincode: str) -> Optional[str]:
    """Pure offline pincode → state lookup. Returns None if not matched."""
    if not pincode.isdigit() or len(pincode) != 6:
        return None
    p = int(pincode)
    # Iterate ranges; later, more specific ranges override earlier broad ones.
    matched: Optional[str] = None
    for start, end, state in PINCODE_STATE_RANGES:
        if start <= p <= end:
            matched = state
    return matched


async def pincode_to_state_online(pincode: str) -> Optional[dict]:
    """Fallback that hits the free api.postalpincode.in service."""
    try:
        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(f"https://api.postalpincode.in/pincode/{pincode}")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    if not data or data[0].get("Status") != "Success":
        return None
    offices = data[0].get("PostOffice") or []
    if not offices:
        return None
    first = offices[0]
    return {
        "state": first.get("State"),
        "district": first.get("District"),
        "block": first.get("Block"),
        "name": first.get("Name"),
    }


async def resolve_pincode(pincode: str) -> dict:
    """Best-effort pincode resolution. Always returns a dict, possibly partial."""
    state = pincode_to_state_offline(pincode)
    result: dict = {"pincode": pincode, "state": state, "source": "offline"}

    online = await pincode_to_state_online(pincode)
    if online:
        result.update(
            {
                "state": online.get("state") or state,
                "district": online.get("district"),
                "block": online.get("block"),
                "post_office": online.get("name"),
                "source": "online",
            }
        )

    return result


# -------------------------------------------------------------------------
# Web search (used sparingly — most data comes from the local dataset + MCP)
# -------------------------------------------------------------------------


async def web_search(query: str, max_results: int = 3) -> str:
    """
    Search the web. Tavily if key is set, else DuckDuckGo HTML.

    Returns formatted result string. On failure, returns an error string —
    never raises.
    """
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            return await _search_tavily(query, max_results, tavily_key)
        except Exception as e:
            print(f"[tool] tavily failed: {e}; falling back")

    try:
        return await _search_duckduckgo(query, max_results)
    except Exception as e:
        return f"WEB_SEARCH_FAILED: {e!r}"


async def _search_tavily(query: str, max_results: int, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    return "\n\n".join(
        f"[{i+1}] {r.get('title','')}\n    URL: {r.get('url','')}\n    {r.get('content','')[:240]}"
        for i, r in enumerate(results[:max_results])
    ) or f"No results for {query!r}"


async def _search_duckduckgo(query: str, max_results: int) -> str:
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/", params={"q": query}
        )
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    lines = []
    for i, div in enumerate(soup.select("div.result")[:max_results], 1):
        title_el = div.select_one("a.result__a")
        snippet_el = div.select_one("a.result__snippet")
        if not title_el:
            continue
        lines.append(
            f"[{i}] {title_el.get_text(strip=True)}\n"
            f"    URL: {title_el.get('href','')}\n"
            f"    {(snippet_el.get_text(strip=True) if snippet_el else '')[:240]}"
        )
    return "\n\n".join(lines) or f"No results for {query!r}"
