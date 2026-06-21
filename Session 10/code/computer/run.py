"""Safe standalone runner for the computer skill (brief §11).

Lets you drive a single task without the orchestrator — used for the staged
per-app tests. Wraps the run with: daemon up, the recording context (so even
a crash is captured), and kill switches (undo + kill_app + daemon shutdown)
wired to Ctrl-C and to a panic() helper you can call from a REPL.

    uv run python -m computer.run --app Calculator \
        --goal "compute 42x18 then read the result" \
        --sequence '[{"action":"click","label":"Four"},{"action":"click","label":"Two"},
                     {"action":"click","label":"Multiply by"},{"action":"click","label":"One"},
                     {"action":"click","label":"Eight"},{"action":"click","label":"Equals"}]' \
        --post 756

    # NOTE: Windows Calculator UIA labels are spelled out — "Four","Multiply by",
    # "Equals" — not "4","x","=". The result lands in the "Display is 756" Text.
    #
    # PowerShell 5.1 WARNING: it strips the quotes around JSON keys before they
    # reach Python, so `--sequence '[...]'` fails to parse. For any run with a
    # sequence/subgoals use the predefined tasks instead (metadata in Python):
    #     uv run python -m computer.tasks calc
    # run.py is reliable for flag-only runs (--app/--goal/--force).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from schemas import NodeSpec

from . import daemon, driver, enable_utf8_console
from . import platform as plat
from .skill import ComputerSkill


# ── kill switches (test these BEFORE trusting a recording — brief §11) ────────
def undo(pid: int) -> None:
    """Ctrl-Z / Cmd-Z — the cheapest reversal primitive."""
    driver.hotkey(plat.hotkey("mod", "z"), pid=pid)


def panic(pid: int | None = None) -> None:
    """Emergency stop: undo (if a pid is known), force-kill the app, shut the
    daemon down. Best-effort and never raises."""
    try:
        if pid is not None:
            undo(pid)
            driver.kill_app(pid)
    except Exception:  # noqa: BLE001
        pass
    daemon.shutdown()


async def safe_run(app: str, goal: str, **metadata) -> dict:
    daemon.ensure_daemon()
    skill = ComputerSkill(artifacts_root=metadata.pop("artifacts_root", None))
    node = NodeSpec(skill="computer", inputs=[], metadata={"app": app, "goal": goal, **metadata})
    result = await skill.run(node)
    return result.model_dump()


def _json_arg(s: str | None):
    return json.loads(s) if s else None


def main() -> int:
    enable_utf8_console()
    ap = argparse.ArgumentParser(description="Run one computer-skill task safely.")
    ap.add_argument("--app", required=True, help="App name (Win/Linux) or bundle_id (macOS)")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--sequence", help="JSON list of deterministic steps (L2a)")
    ap.add_argument("--subgoals", help="JSON list of subgoals (or CDP steps for --force page)")
    ap.add_argument("--force", choices=["a11y", "vision", "page"], help="pin a layer")
    ap.add_argument("--post", help="post-condition text to verify")
    ap.add_argument("--electron", action="store_true", help="force the Electron page path")
    ap.add_argument("--port", type=int, help="Electron debugging port")
    args = ap.parse_args()

    meta: dict = {}
    if args.sequence: meta["sequence"] = _json_arg(args.sequence)
    if args.subgoals: meta["subgoals"] = _json_arg(args.subgoals)
    if args.force:    meta["force_path"] = args.force
    if args.post:     meta["post_condition"] = args.post
    if args.electron: meta["electron"] = True
    if args.port:     meta["debugging_port"] = args.port

    try:
        result = asyncio.run(safe_run(args.app, args.goal, **meta))
    except KeyboardInterrupt:
        print("\n[run] interrupted — invoking panic()", file=sys.stderr)
        panic()
        return 130
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
