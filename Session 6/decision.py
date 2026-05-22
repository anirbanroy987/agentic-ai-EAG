"""
Decision — one goal in, one step out. Native tool-use via V3, auto_route="decision".

The orchestrator hands us exactly one Goal per call, plus the memory hits,
any artifact bytes perception explicitly attached, the run history, and the
tool catalogue. We return a DecisionOutput that is either:
    • an `answer` (string), or
    • a `tool_call` (the first one if the model emitted several — the loop
      runs one tool per iteration so extra calls would be wasted anyway).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _gateway import LLM
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall, ToolDef


def _now_block(tz_name: str = "Asia/Kolkata") -> str:
    """Same anchor as perception — see perception._now_block() for rationale,
    including the tzdata fallback for Windows hosts without the package."""
    try:
        now = datetime.now(ZoneInfo(tz_name))
        label = tz_name
    except ZoneInfoNotFoundError:
        now = datetime.now()
        label = "local"
    return (
        f"NOW: {now.strftime('%A, %d %B %Y, %H:%M')} "
        f"({label}, ISO {now.date().isoformat()})"
    )


DECISION_SYSTEM = (
    "You are the decision stage of an agentic loop. You will be given ONE "
    "goal to tackle this iteration plus context (memory hits, attached "
    "artifact bytes, recent history, tool catalogue).\n\n"
    "Do exactly one of:\n"
    "  (a) emit a single tool call that gathers the missing information, OR\n"
    "  (b) reply with plain text containing the answer to the goal — when "
    "      the attached artifacts, history, and memory already contain "
    "      what you need.\n\n"
    "Rules:\n"
    "  • If ATTACHED_ARTIFACTS contains the information for this goal, "
    "    ANSWER from it. Do NOT call a tool to re-fetch information already "
    "    in front of you. This is the most common mistake — the artifacts "
    "    are attached precisely because perception decided they're "
    "    sufficient.\n"
    "  • Never call a tool with arguments you saw fail in the recent "
    "    history (same URL that returned 404, same query that returned no "
    "    results, etc.). Try a different argument or answer with what you "
    "    have.\n"
    "  • Prefer one tool call over guessing when the context genuinely "
    "    lacks the information. Tools are cheap; hallucinations are not.\n"
    "  • When you DO answer, be terse and structured — the orchestrator "
    "    just captures the text verbatim."
)


# Cap each attached artifact at ~16k chars (~4k tokens) so multiple attached
# artifacts on the same turn still fit under V3's 8000-token HUGE ceiling.
ATTACHED_TRUNCATE = 16_000


def mcp_tools_for_decision(mcp_tools) -> list[dict[str, Any]]:
    """Reshape an MCP tool list into the gateway's ToolDef envelopes."""
    return [
        ToolDef(
            name=t.name,
            description=t.description or "",
            input_schema=t.inputSchema or {"type": "object", "properties": {}},
        ).model_dump()
        for t in mcp_tools
    ]


def _render_hits(hits: list[MemoryItem]) -> str:
    if not hits:
        return "(none)"
    out = []
    for h in hits:
        art = f"  artifact={h.artifact_id}" if h.artifact_id else ""
        out.append(f"  [{h.kind}] {h.descriptor[:200]}{art}")
    return "\n".join(out)


def _render_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(empty)"
    out = []
    for ev in history[-6:]:
        if ev.get("kind") == "action":
            out.append(
                f"  iter {ev.get('iter')} {ev.get('tool')}({ev.get('arguments')}) "
                f"-> {(ev.get('result_descriptor') or '')[:160]}"
            )
        elif ev.get("kind") == "answer":
            out.append(f"  iter {ev.get('iter')} answer: {ev.get('text','')[:160]}")
    return "\n".join(out)


def _render_attached(attached: list[tuple[str, bytes]]) -> str:
    if not attached:
        return ""
    parts = ["", "ATTACHED_ARTIFACTS (full bytes pulled by the loop):"]
    for art_id, blob in attached:
        text = blob.decode("utf-8", errors="replace")
        truncated = ""
        if len(text) > ATTACHED_TRUNCATE:
            text = (text[: ATTACHED_TRUNCATE - 80]
                    + f"\n[...truncated; original was {len(blob)} bytes...]")
            truncated = "  (truncated)"
        parts.append(f"--- artifact {art_id}{truncated} ---")
        parts.append(text)
        parts.append(f"--- end artifact {art_id} ---")
    return "\n".join(parts)


def _build_user_block(goal: Goal, hits: list[MemoryItem],
                      attached: list[tuple[str, bytes]],
                      history: list[dict[str, Any]]) -> str:
    return (
        f"{_now_block()}\n\n"
        f"CURRENT_GOAL (id={goal.id}):\n  {goal.text}\n"
        + (f"NOTE: {goal.note}\n" if goal.note else "")
        + f"\nRECALLED_MEMORY:\n{_render_hits(hits)}\n"
        + f"\nHISTORY (last 6):\n{_render_history(history)}"
        + _render_attached(attached)
    )


def next_step(goal: Goal, hits: list[MemoryItem],
              attached: list[tuple[str, bytes]],
              history: list[dict[str, Any]],
              tools: list[dict[str, Any]]) -> DecisionOutput:
    """One gateway call. Returns DecisionOutput (answer XOR tool_call)."""
    user_block = _build_user_block(goal, hits, attached, history)

    llm = LLM()
    reply = llm.chat(
        messages=[{"role": "user", "content": user_block}],
        system=DECISION_SYSTEM,
        # Lecture: cache flags are fake on free tiers — leave them off.
        tools=tools,
        tool_choice="auto",
        auto_route="decision",  # router picks worker by tier
        reasoning="off",
        # Lecture: temperature=1 with free providers.
        temperature=1,
        max_tokens=1024,
    )

    raw_calls = reply.get("tool_calls") or []
    if raw_calls:
        # Take only the first; the loop runs one per iteration.
        tc = raw_calls[0]
        return DecisionOutput(
            is_answer=False,
            tool_call=ToolCall(
                id=str(tc.get("id") or "") or ToolCall(name=tc["name"]).id,
                name=tc["name"],
                arguments=dict(tc.get("arguments") or {}),
            ),
            rationale=(reply.get("text") or "")[:300],
        )

    return DecisionOutput(
        is_answer=True,
        answer=(reply.get("text") or "").strip(),
        rationale="",
    )
