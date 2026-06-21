"""Layer 3 — action sequencing: the scan → act → verify loop, with the two
cache invariants from CUA_DRIVER_GUIDE §5 baked in so they cannot be
forgotten:

  Invariant A — every element-indexed action is preceded, THIS turn, by a
                get_window_state that built the cache.
  Invariant B — every state-changing action is followed by a re-scan; an
                index from a previous scan is never reused.

"Verify is mandatory" (brief §2): a tool returning success means the call
*dispatched*, not that intent was achieved. After each action we re-scan and
check one concrete post-condition; only the judge's `done` ends the subgoal.
"""
from __future__ import annotations

from . import driver, layers, perception, recovery
from .driver import WindowState
from .layers import LayerResult


def post_condition_met(before: WindowState, after: WindowState, *,
                       expect: str | None = None) -> bool:
    """Did the action have a visible effect? With an `expect` token, check it
    appears in the post-action tree (the strong check). Without one, fall back
    to 'the tree changed at all' — weak, but it still catches a click that hit
    a disabled/no-op element (guide's closing warning)."""
    if expect:
        return expect.lower() in after.tree_markdown.lower()
    return after.tree_markdown.strip() != before.tree_markdown.strip()


def _history(steps: list[dict], limit: int = 5) -> str:
    if not steps:
        return "(none yet)"
    return "\n".join(
        f"turn {s['turn']}: {s.get('action', {})} → {s['outcome']}"
        for s in steps[-limit:]
    )


async def run_subgoal(pid: int, window_id: int, subgoal: str, client, *,
                      app: str | None = None, expect: str | None = None,
                      query: str | None = None, provider: str | None = None,
                      max_steps: int = 12, max_failures: int = 3) -> LayerResult:
    """Drive one subgoal to completion via the scan-act-verify loop. Returns a
    LayerResult; `escalate=True` signals the cascade to try vision (L3) or the
    Electron path, depending on the route recovery picked."""
    steps: list[dict] = []
    failures = 0

    for turn in range(1, max_steps + 1):
        # ── SCAN (Invariant A) ──────────────────────────────────────────────
        state = driver.get_window_state(pid, window_id, capture_mode="ax", query=query)
        if state.empty:
            res = recovery.handle_empty_tree(pid, window_id, state, app=app)
            if res.resolved and res.state is not None:
                state = res.state
            else:
                # Not auto-recoverable at this layer — bubble up the route so
                # the cascade can relaunch-for-page (electron) or go vision.
                return LayerResult(False, "a11y", note=f"empty tree → {res.route}: {res.note}",
                                   turns=turn, actions=steps, escalate=True)

        view = perception.filter(state, subgoal)

        # ── DECIDE (cheap text LLM) ─────────────────────────────────────────
        verdict = await layers.judge(view, subgoal, client,
                                     history=_history(steps), provider=provider)
        v = verdict.get("verdict")
        if v == "escalate":
            return LayerResult(False, "a11y", note=f"judge escalate: {verdict.get('reason','')}",
                               turns=turn, actions=steps, escalate=True)
        if v == "done":
            return LayerResult(bool(verdict.get("success", True)), "a11y",
                               note="judge done", turns=turn, actions=steps)

        action = verdict.get("action") or {}
        if not action.get("type"):
            steps.append({"turn": turn, "outcome": "error: act verdict with no action"})
            failures += 1
            if failures >= max_failures:
                return LayerResult(False, "a11y", note="judge gave no actionable step",
                                   turns=turn, actions=steps)
            continue

        # ── ACT (this scan's indices only) ──────────────────────────────────
        outcome = driver.dispatch_action(pid, window_id, action)
        steps.append({"turn": turn, "action": action, "outcome": outcome,
                      "thinking": verdict.get("thinking", "")})

        if outcome.startswith("error"):
            # Stale index = Invariant B violation: just re-scan next turn.
            if recovery.is_stale_index_error(outcome):
                continue
            failures += 1
            if failures >= max_failures:
                return LayerResult(False, "a11y", note=f"giveup after {failures} failures: {outcome}",
                                   turns=turn, actions=steps)
            continue
        failures = 0

        # ── VERIFY (Invariant B: re-scan rebuilds the map) ──────────────────
        after = driver.get_window_state(pid, window_id, capture_mode="ax")
        if expect and post_condition_met(state, after, expect=expect):
            return LayerResult(True, "a11y", note=f"post-condition met: {expect!r}",
                               turns=turn, actions=steps)
        if not post_condition_met(state, after, expect=None):
            # Action dispatched but nothing changed — recover (fresh scan) and
            # let the next turn re-resolve against the new map.
            recovery.on_verify_fail(pid, window_id)

    return LayerResult(False, "a11y", note=f"step cap reached ({max_steps})",
                       turns=max_steps, actions=steps)
