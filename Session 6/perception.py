"""
Perception — decompose / verify / attach.

Called once per iteration. Emits a GoalList:
  • new goals if there are none yet,
  • prior goals marked `done` when history shows they're satisfied,
  • optional `attach_artifact_id` on the next-to-tackle goal, asking the
    orchestrator to materialize stored bytes into the next decision turn.

One LLM call, auto_route="perception" — V3's router picks a TINY worker.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _gateway import LLM, GatewayError
from artifacts import artifacts as artifact_store
from schemas import Goal, GoalList, MemoryItem


def _now_block(tz_name: str = "Asia/Kolkata") -> str:
    """A short anchor the orchestrator stamps onto every perception/decision
    call. Most agent queries that mention 'today', 'tomorrow', 'this
    weekend', 'Saturday', etc. fail silently without this — the LLM can't
    derive what day it is from its training data. We resolve it in the
    orchestrator (free, no tool call) rather than burning an iteration on
    get_time.

    Windows installs of Python don't ship IANA tzdata; the user can `pip
    install tzdata` for proper named-zone support, but we degrade
    gracefully to local naive time so a missing dependency never crashes
    the run — the calendar date is the same in any zone ±1 day, which is
    plenty for queries like 'this Saturday'."""
    try:
        now = datetime.now(ZoneInfo(tz_name))
        label = tz_name
    except ZoneInfoNotFoundError:
        now = datetime.now()  # naive, host local time
        label = "local (tzdata not installed; `pip install tzdata`)"
    return (
        f"NOW: {now.strftime('%A, %d %B %Y, %H:%M')} "
        f"({label}, ISO {now.date().isoformat()})"
    )


# Lecture rule: perception always goes to Gemini. Reality: Gemini's free
# tier is 20 generate_content requests/day, which a single non-trivial run
# (with retries / multiple iters / structured-output validation retries)
# can blow through. We try Gemini first; if the gateway tells us Gemini's
# quota is burned, we degrade to the next structured-capable worker for
# THIS call only. The preference is preserved; the run doesn't die.
PERCEPTION_PROVIDERS: list[str] = ["g", "gr", "or"]  # gemini → groq → openrouter


def _should_fallback(err: GatewayError) -> bool:
    """Decide whether to try the next worker.

    Recoverable iff status is in the recoverable set AND the body carries
    a signature we know another worker might handle. Keeping this two-axis
    check (status + body keyword) means a generic 500 with no signature
    won't be silently retried, but a 502 wrapping a known JSON failure
    will. Status mismatches keep tripping us because the gateway wraps
    explicit-provider errors as 502 regardless of the upstream code, so we
    cast the status net wide and rely on body keywords to distinguish.

    Recoverable families:
      • quota / rate-limit / unavailable
        (e.g. Gemini free-tier 20/day burned, with body containing 'quota'
        wrapped as gateway 502)
      • worker-side structured-output / JSON validation failures
        (Groq's `json_validate_failed`, gateway's own
        `structured output failed validation: output is not JSON`, output
        truncated by max_tokens, etc.)

    Auth errors, missing-tool errors, real 500s with no signature → NOT
    fallback. They indicate a real bug and we want them surfaced rather
    than masked by retrying through every provider.
    """
    if err.status not in (400, 408, 429, 500, 502, 503, 504):
        return False
    body = (err.body or "").lower()
    keywords = (
        # Quota / rate / availability
        "quota", "rate limit", "rpm", "rpd", "exceeded", "unavailable",
        "overloaded", "try again",
        # Worker-side structured-output failures
        "json_validate_failed", "failed to validate json",
        "failed to generate json", "failed_generation",
        "max completion tokens reached",
        # Gateway-side validation phrasing (this is what bit us today)
        "structured output failed validation", "output is not json",
        "did not match the required json schema",
        # Generic JSON-mode hiccups some providers emit
        "invalid json", "malformed json",
    )
    return any(s in body for s in keywords)


PERCEPTION_SYSTEM = (
    "You are the perception stage of an agentic loop. Read the user's query, "
    "the recalled memory, the recent action history, and any prior goals. "
    "Output a GoalList JSON object describing the current plan.\n\n"
    "Rules:\n"
    "  • Decompose the query into 1-5 atomic goals the action stage can "
    "    each tackle with ONE tool call (or zero — if it's already answered).\n"
    "  • If `prior_goals` is non-empty, KEEP each prior goal's id and text "
    "    intact.\n"
    "  • `done: true` MEANS 'the decision stage already produced an answer "
    "    or tool result for this goal in a previous iteration, and HISTORY "
    "    contains the evidence.' It does NOT mean 'memory hints at the "
    "    answer' or 'this looks easy.' On the very first iteration HISTORY "
    "    is empty, so EVERY goal must start `done: false`. A memory hit is "
    "    context for decision to use; it is not evidence the goal is "
    "    finished — decision must still produce the answer text.\n"
    "  • `attach_artifact_ids` is a LIST of artifact ids the next decision "
    "    turn needs in front of it to finish this goal. A synthesis goal "
    "    ('recommend X given A and B') typically needs BOTH A and B "
    "    attached — list every artifact id required, in order. Use [] for "
    "    none. Only use ids that appear in the ARTIFACT_CATALOGUE; never "
    "    invent ids.\n"
    "  • Be terse. Goal text should be one short imperative sentence."
)


# Schema kept deliberately conservative: no nullable union types, no optional
# fields. Some providers' structured-output endpoints (Gemini JSON mode,
# OpenAI strict mode) reject `"type": ["string","null"]` and partial-required
# lists. Empty list for `attach_artifact_ids` means "no attachments".
GOALLIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "done": {"type": "boolean"},
                    "attach_artifact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "note": {"type": "string"},
                },
                "required": ["id", "text", "done", "attach_artifact_ids", "note"],
            },
        },
    },
    "required": ["goals"],
}


def _render_hits(hits: list[MemoryItem]) -> str:
    if not hits:
        return "(none)"
    lines = []
    for h in hits:
        tag = f"[{h.kind}]"
        art = f"  artifact={h.artifact_id}" if h.artifact_id else ""
        lines.append(f"  {tag} {h.descriptor[:160]}{art}")
    return "\n".join(lines)


def _render_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(empty)"
    lines = []
    for ev in history[-8:]:
        kind = ev.get("kind", "?")
        if kind == "action":
            desc = (ev.get("result_descriptor") or "")[:140]
            art = f"  artifact={ev.get('artifact_id')}" if ev.get("artifact_id") else ""
            lines.append(f"  iter {ev.get('iter')} action {ev.get('tool')} -> {desc}{art}")
        elif kind == "answer":
            lines.append(f"  iter {ev.get('iter')} answer: {ev.get('text','')[:120]}")
        else:
            lines.append(f"  iter {ev.get('iter')} {kind}")
    return "\n".join(lines)


def _render_prior_goals(prior_goals: list[Goal]) -> str:
    if not prior_goals:
        return "(none)"
    return "\n".join(
        f"  - id={g.id}  done={g.done}  text={g.text}"
        for g in prior_goals
    )


def _render_catalog() -> str:
    cat = artifact_store.catalog()
    if not cat:
        return "(empty)"
    return "\n".join(
        f"  - {c['id']}  ({c['size']} bytes)  {c['descriptor'][:120]}"
        for c in cat
    )


def observe(query: str, hits: list[MemoryItem], history: list[dict[str, Any]],
            prior_goals: list[Goal], run_id: str) -> GoalList:
    """One LLM call. Returns a freshly assembled GoalList."""
    user_block = (
        f"{_now_block()}\n\n"
        f"USER_QUERY:\n{query}\n\n"
        f"RECALLED_MEMORY:\n{_render_hits(hits)}\n\n"
        f"PRIOR_GOALS:\n{_render_prior_goals(prior_goals)}\n\n"
        f"HISTORY (last 8 events):\n{_render_history(history)}\n\n"
        f"ARTIFACT_CATALOGUE:\n{_render_catalog()}\n"
    )

    llm = LLM()
    # Lecture: "Perception always goes to Gemini." Perception is the critical
    # reasoning stage — decomposition + verification both live here, so it
    # gets the strongest worker. Failover only fires when Gemini is *out*
    # (quota burned, rate-limited); ordinary 200s never trigger fallback.
    reply = None
    last_err: GatewayError | None = None
    for provider in PERCEPTION_PROVIDERS:
        try:
            reply = llm.chat(
                prompt=user_block,
                system=PERCEPTION_SYSTEM,
                # Lecture: cache flags are fake on free tiers — leave off.
                response_format={
                    "type": "json_schema", "schema": GOALLIST_SCHEMA,
                    "name": "GoalList", "strict": True,
                },
                provider=provider,
                reasoning="off",
                # Lecture: temperature=1 with free providers.
                temperature=1,
                max_tokens=768,
            )
            if provider != PERCEPTION_PROVIDERS[0]:
                # Be loud about fallback — it's a degradation worth seeing.
                print(f"[perception]    (fell back to provider={provider})")
            break
        except GatewayError as e:
            last_err = e
            if _should_fallback(e):
                continue  # try the next worker
            raise  # real error — don't paper over it
    if reply is None:
        assert last_err is not None
        raise last_err

    parsed = reply.get("parsed")
    if not parsed:
        try:
            parsed = json.loads(reply.get("text") or "")
        except Exception:
            parsed = None

    if not parsed or "goals" not in parsed:
        # Fallback: keep prior_goals if any, else create a single goal from
        # the raw query. Better than crashing the run.
        return GoalList(
            goals=list(prior_goals) if prior_goals
            else [Goal(text=query.strip())]
        )

    goals: list[Goal] = []
    valid_artifact_ids = {c["id"] for c in artifact_store.catalog()}
    for raw in parsed["goals"]:
        # The LLM occasionally invents artifact IDs — drop unknown ones
        # rather than letting the orchestrator chase a phantom blob.
        # Also accept the singular form for backward-compat in case a worker
        # echoes the old field name; coerce everything to a clean list.
        arts_raw = raw.get("attach_artifact_ids")
        if arts_raw is None:
            singular = raw.get("attach_artifact_id")
            arts_raw = [singular] if singular else []
        arts = [
            str(a) for a in (arts_raw or [])
            if a and str(a) in valid_artifact_ids
        ]
        goals.append(Goal(
            id=str(raw.get("id") or "")[:32] or Goal().id,
            text=str(raw.get("text") or "").strip(),
            done=bool(raw.get("done", False)),
            attach_artifact_ids=arts,
            note=str(raw.get("note") or ""),
        ))
    # Drop empty-text goals.
    goals = [g for g in goals if g.text]
    return GoalList(goals=goals or [Goal(text=query.strip())])
