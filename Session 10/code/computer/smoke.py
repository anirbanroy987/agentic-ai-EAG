"""Live smoke diagnostics — require the cua-driver binary + a running daemon.

Stage 1 of the staged build: launch an app, activate it, scan it, and print
element_count + the parsed/raw tree. Exercises the real driver/daemon/platform
modules end-to-end with NO LLM and NO vision.

    uv run python -m computer.smoke --app Calculator
    uv run python -m computer.smoke --app Calculator --query button
"""
from __future__ import annotations

import argparse
import sys
import time

from . import daemon, driver, enable_utf8_console, perception
from . import platform as plat


def scan_app(app: str, *, query: str | None = None, mode: str = "ax",
             show: int = 60) -> int:
    print(f"[smoke] OS={plat.OS_NAME}  binary={plat.resolve_binary()}")
    try:
        daemon.ensure_daemon()
    except driver.CuaError as e:
        print(f"[smoke] daemon error: {e}")
        return 1
    print("[smoke] daemon up")

    try:
        pid, wid = driver.launch_and_focus(app)
    except driver.CuaError as e:
        print(f"[smoke] launch/focus failed: {e}")
        return 1
    print(f"[smoke] launched + focused: pid={pid} window_id={wid}")

    st = driver.get_window_state(pid, int(wid), capture_mode=mode, query=query)
    print(f"\n[smoke] ===== element_count={st.element_count} (empty={st.empty}) =====")
    rows = perception.parse_rows(st.tree_markdown)
    print(f"[smoke] parsed addressable rows={len(rows)}; first {min(show, len(rows))}:")
    for r in rows[:show]:
        print("   ", r.render())
    print("\n[smoke] ----- raw tree_markdown (first 1800 chars) -----")
    print(st.tree_markdown[:1800])
    return 0 if not st.empty else 2


def main() -> int:
    enable_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--query")
    ap.add_argument("--mode", default="ax", choices=["ax", "som", "vision"])
    ap.add_argument("--show", type=int, default=60)
    a = ap.parse_args()
    return scan_app(a.app, query=a.query, mode=a.mode, show=a.show)


if __name__ == "__main__":
    sys.exit(main())
