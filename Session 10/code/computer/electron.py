"""Layer 2, special case — the Electron / Chromium DOM path.

A large slice of modern desktop apps (VS Code, Slack, Discord, Notion,
Cursor, Obsidian, Linear, 1Password…) render their UI as HTML inside an
embedded Chromium. To the AX/UIA tree they are a single opaque WebArea, so
`get_window_state` returns the menu bar and nothing actionable inside.

cua-driver's answer (guide §7.2): drive the app's DOM via the `page` tool.
The page tool has two sub-modes on Windows, verified empirically:

  - READ  (get_text, query_dom): works via UIA on an ALREADY-RUNNING window,
    no debug port, no relaunch. `get_text` returns the rendered window text.
  - CDP   (execute_javascript, click_element): needs the app launched with
    --remote-debugging-port; an already-running single-instance app must be
    closed first so the relaunch can open the CDP server.

Page actions (cua-driver verbs): execute_javascript | get_text | query_dom |
click_element. On Windows `page` also requires `window_id` (from list_windows),
not just pid.
"""
from __future__ import annotations

from . import driver

# Apps known to be Electron/Chromium. Matched case-insensitively as a
# substring of the launch identity (name or bundle_id) so "Visual Studio
# Code", "com.microsoft.VSCode", and "code" all resolve.
KNOWN_ELECTRON_APPS: set[str] = {
    "code", "visual studio code", "vscode", "com.microsoft.vscode",
    "cursor", "com.todesktop",                 # Cursor
    "slack", "com.tinyspeck.slackmacgap",
    "discord", "com.hnc.discord",
    "notion", "notion.id",
    "obsidian", "md.obsidian",
    "linear", "com.linear",
    "1password", "com.1password",
    "spotify", "figma",                        # Figma shell is Electron (canvas inside is still vision)
    "claude",                                  # Claude desktop app
}

DEFAULT_DEBUG_PORT = 9222


def is_electron(app: str) -> bool:
    a = (app or "").lower()
    return any(marker in a for marker in KNOWN_ELECTRON_APPS)


def relaunch_with_cdp(app: str, *, port: int = DEFAULT_DEBUG_PORT) -> dict:
    """Launch (or relaunch) an Electron app with the debugging port enabled so
    the `page` tool can attach. Returns the launch_app response (carries pid).
    The caller is responsible for killing a prior instance if one is already
    running without the port — a plain relaunch will not add the flag to an
    existing process."""
    return driver.launch_app(app, electron_port=port)


def page_action(pid: int, action: str, *, selector: str | None = None,
                window_id: int | None = None, **extra) -> dict:
    """One CDP DOM action via the `page` tool (click/fill/eval/wait/…).
    Addresses by CSS selector, never element_index — the AX cache does not
    apply on this path, so the scan-act-verify invariants are replaced by
    selector waits inside `page`. window_id is required on Windows."""
    return driver.page(pid, action, selector=selector, window_id=window_id, **extra)
