"""The layer building blocks + the L2b judge contract.

The cascade itself (which layer to try, when to escalate) lives in
skill.py, mirroring how BrowserSkill.run owns its cascade. This module
provides the reusable pieces each layer is built from:

  LayerResult         — the uniform return type across L1/2a/2b/3/page
  extract_value()     — L1: read a value straight from the tree (no LLM)
  run_deterministic() — L2a: caller-supplied key/click sequence (no LLM)
  JUDGE_SCHEMA + judge() — L2b: perception view → single-action verdict (cheap text LLM)

The judge's verdict is the routing seam (brief §4): verdict "act" →
dispatch by element_index; "escalate" → vision fallback; "done" → finish.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import driver, perception
from . import platform as plat


# ── uniform layer result ──────────────────────────────────────────────────────
@dataclass
class LayerResult:
    success: bool
    path: str                      # extract | deterministic | a11y | vision | page
    note: str = ""
    turns: int = 0
    content: str | None = None
    actions: list[dict] = field(default_factory=list)
    escalate: bool = False         # L2b sets this so the cascade knows to try L3


# ── Layer 1: extract a value from the tree (no LLM) ───────────────────────────
_READ_VERBS = ("read", "what is", "what's", "value of", "show me", "get the value")
# An action goal ("compute X then read it") is NOT a pure read — taking the L1
# short-circuit would read the pre-action state and falsely report success.
_ACTION_VERBS = (
    "compute", "calculate", "click", "type", "open ", "launch", "set ", "add ",
    "draw", "multiply", "divide", "subtract", "plus", "press", "enter ",
    "create", "select", "toggle", "search", "save", "fill", "navigate", "run ",
)


def looks_like_read(goal: str) -> bool:
    g = (goal or "").lower()
    if any(a in g for a in _ACTION_VERBS):
        return False
    return any(v in g for v in _READ_VERBS)


# Canvas verbs: a freehand stroke/shape on a pixel surface. No AX element can
# perform these — the judge only ever sees the toolbar — so a draw goal must
# skip L2b and go straight to L3 vision rather than burning the step budget
# clicking toolbar buttons.
_DRAW_VERBS = ("draw", "sketch", "paint", "doodle", "scribble", "stroke", "trace ")


def looks_like_draw(goal: str) -> bool:
    """True for canvas/drawing goals that only the vision layer can satisfy."""
    return any(v in (goal or "").lower() for v in _DRAW_VERBS)


def extract_value(view: perception.PerceptionView, goal: str) -> LayerResult:
    """L1: answer a read-only goal directly from the AX/UIA text rows. Used for
    'read the calculator result' style goals — zero LLM, zero vision."""
    texts = view.read_text()
    if not texts:
        return LayerResult(False, "extract", note="no text rows to read")
    # Heuristic: the most specific display value is usually the last/long text
    # row; return all of them so the caller can pick.
    content = " | ".join(texts[-5:])
    return LayerResult(True, "extract", note="read from tree", content=content)


# ── Layer 2a: deterministic sequence (no LLM) ─────────────────────────────────
def _resolve_step(pid: int, window_id: int, step: dict) -> tuple[dict, str]:
    """Turn one caller step into a normalized action dict for
    driver.dispatch_action. Label-addressed steps trigger a fresh scan +
    perception resolve (still LLM-free) and honour Invariant B between steps."""
    action = step.get("action", step.get("type", ""))
    # Full scan (no query trim) before each label step: re-resolves the index
    # against a fresh map (Invariant B) and is robust to platforms where the
    # `query` filter behaves differently. Trees for known UIs are small.
    if action in ("click", "click_label") and "label" in step:
        view = perception.filter(driver.get_window_state(pid, window_id, capture_mode="ax"))
        row = view.find_label(str(step["label"]), exact=step.get("exact", False))
        if not row:
            return {}, f"error: label {step['label']!r} not found in tree"
        return {"type": "click", "element_index": row.index}, "ok"
    if action in ("click", "click_index") and "element_index" in step:
        return {"type": "click", "element_index": int(step["element_index"])}, "ok"
    if action == "type":
        a = {"type": "type", "value": str(step.get("value", ""))}
        if "label" in step:
            row = perception.filter(
                driver.get_window_state(pid, window_id, capture_mode="ax")
            ).find_label(str(step["label"]))
            if row:
                a["element_index"] = row.index
        return a, "ok"
    if action == "key":
        return {"type": "key", "value": str(step.get("value", "Return"))}, "ok"
    if action == "hotkey":
        return {"type": "hotkey", "keys": plat.hotkey(*step.get("keys", []))}, "ok"
    return {}, f"error: unrecognised deterministic step {step!r}"


def run_deterministic(pid: int, window_id: int, sequence: list[dict], *,
                      pause: float = 0.25) -> LayerResult:
    """L2a: execute a caller-supplied, LLM-free step sequence. Each step is
    {action, label|element_index|value|keys}. Re-scans per label step so
    indices are always turn-fresh (Invariant B)."""
    recorded: list[dict] = []
    for i, step in enumerate(sequence, start=1):
        action, status = _resolve_step(pid, window_id, step)
        if status.startswith("error"):
            recorded.append({"step": i, "outcome": status})
            return LayerResult(False, "deterministic", note=status,
                               turns=i, actions=recorded)
        outcome = driver.dispatch_action(pid, window_id, action)
        recorded.append({"step": i, "action": action, "outcome": outcome})
        if outcome.startswith("error"):
            return LayerResult(False, "deterministic", note=outcome,
                               turns=i, actions=recorded)
        time.sleep(pause)
    return LayerResult(True, "deterministic", note="sequence completed",
                       turns=len(sequence), actions=recorded)


# ── Layer 2b: the judge (cheap text LLM over the filtered view) ───────────────
JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "verdict"],
    "properties": {
        "thinking": {"type": "string", "description": "1-2 sentences"},
        "verdict": {"type": "string", "enum": ["act", "done", "escalate"]},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string",
                         "enum": ["click", "type", "key", "hotkey", "set_value"]},
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
            },
        },
        "success": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

SYSTEM_PROMPT_JUDGE = (
    "You drive a desktop app through an accessibility tree. Each turn you get "
    "the goal, a numbered legend of addressable elements as [index] <Role> "
    '"Label", and recent actions. Emit ONE decision:\n'
    "  verdict=act  + action{type,element_index,value?,keys?} — take a single step\n"
    "  verdict=done + success — the goal is already satisfied in this legend\n"
    "  verdict=escalate + reason — the target is NOT in the legend or is "
    "inherently visual (canvas/game); the caller will fall back to vision.\n"
    "Address elements ONLY by an index that appears in THIS legend — indices "
    "are turn-scoped and change after every action. Prefer the element whose "
    "Role/Label best fits the next step. Be terse in `thinking`."
)


async def judge(view: perception.PerceptionView, goal: str, client, *,
                history: str = "", provider: str | None = None,
                model: str | None = None) -> dict:
    """L2b decision. Returns the parsed verdict dict (or an escalate verdict if
    the model produced nothing parseable). `client` is a browser.client.V9Client."""
    prompt = (
        f"GOAL: {goal}\n\n"
        f"ADDRESSABLE ELEMENTS ({len(view.rows)}):\n{view.legend()}\n\n"
        f"RECENT ACTIONS:\n{history or '(none yet)'}\n\n"
        f"What is the next single decision?"
    )
    result = await client.chat(
        prompt, system=SYSTEM_PROMPT_JUDGE,
        schema=JUDGE_SCHEMA, schema_name="ComputerVerdict",
        max_tokens=512, provider=provider, model=model,
    )
    parsed = result.parsed
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        return {"verdict": "escalate", "reason": "judge produced no parseable verdict",
                "_meta": {"provider": result.provider, "model": result.model}}
    parsed["_meta"] = {"provider": result.provider, "model": result.model,
                       "tokens_in": result.input_tokens, "tokens_out": result.output_tokens}
    return parsed
