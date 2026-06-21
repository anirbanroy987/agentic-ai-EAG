"""The three demo tasks — runnable without fighting shell JSON quoting.

PowerShell 5.1 mangles JSON passed on the command line (it strips the quotes
around object keys), so `run.py --sequence '[...]'` is fragile. These tasks
define the metadata in Python instead, so they always run cleanly:

    uv run python -m computer.tasks calc      # Task 1: zero-LLM compute (L1+L2a) — no gateway
    uv run python -m computer.tasks notepad   # L2b a11y judge — needs the V9 gateway
    uv run python -m computer.tasks paint     # Task 3: vision fallback (L3) — needs the gateway
    uv run python -m computer.tasks vscode    # Task 2: Electron page path (CDP)
    uv run python -m computer.tasks list

Set CUA_DEBUG=1 first to see the per-call trace.
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import enable_utf8_console
from .run import safe_run

# Windows Calculator UIA labels are spelled out (Four, Multiply by, Equals).
_CALC_42x18 = [{"action": "click", "label": l} for l in
               ("Four", "Two", "Multiply by", "One", "Eight", "Equals")]

TASKS: dict[str, dict] = {
    # Task 1 — zero LLM, zero vision: deterministic clicks (L2a) + read (L1).
    "calc": dict(app="Calculator", goal="compute 42x18 then read the result",
                 sequence=_CALC_42x18, post_condition="756"),
    # L2b — a11y judge drives the app turn-by-turn (no sequence given).
    "notepad": dict(app="Notepad", goal="type the words hello world into the document"),
    # Task 3 — vision (L3) on a canvas; force the vision layer.
    "paint": dict(app="Paint", goal="draw one short horizontal line near the center",
                  force_path="vision"),
    # Task 2 — Electron page path. `get_text`/`query_dom` work via UIA on a
    # running Electron app (no debug port). `execute_javascript`/`click_element`
    # need a relaunch with --remote-debugging-port (CDP) — see vscode_cdp.
    "vscode": dict(app="Code", goal="read the VS Code window content via the Electron page tool",
                   electron=True, subgoals=[{"action": "get_text"}]),
    # CDP variant — requires VS Code launched fresh with the debug port.
    "vscode_cdp": dict(app="Code", goal="read document.title over CDP", electron=True,
                       debugging_port=9222,
                       subgoals=[{"action": "execute_javascript", "javascript": "document.title"}]),
    # WRITE a file via VS Code — NO hardcoded steps/path. The skill's goal-
    # decomposition turns this goal into the input-synthesis sequence (with a
    # focus wait + Ctrl+N safe new tab) and verifies the file on disk. The path
    # is derived from the run dir (cwd). REQUIRES VS Code foreground during the
    # 6s focus wait. Change the goal text to write any file/content.
    "writefile": dict(app="Code",
                      goal="create hello_world.txt containing Hello World",
                      electron=True),
}


def main() -> int:
    enable_utf8_console()
    name = sys.argv[1] if len(sys.argv) > 1 else "list"
    if name not in TASKS:
        print("available tasks:")
        for k, v in TASKS.items():
            print(f"  {k:8s} {v['app']:12s} {v['goal']}")
        return 0 if name in ("list", "-h", "--help") else 2
    meta = dict(TASKS[name])
    app, goal = meta.pop("app"), meta.pop("goal")
    print(f"[tasks] running {name!r}: {app} — {goal}")
    result = asyncio.run(safe_run(app, goal, **meta))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
