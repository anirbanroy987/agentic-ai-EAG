"""Session 10: the `computer` skill.

A cascade wrapper around a running `cua-driver` daemon — the desktop
counterpart of Session 9's `browser` skill. cua-driver supplies perception
(AX/UIA tree scans, screenshots) and action (click/type/hotkey); this
package supplies the five layers it does not:

    Layer 1   — extract a value straight from the AX tree (no LLM)
    Layer 2a  — deterministic key/click sequences (no LLM)
    Layer 2b  — perception filter + V9 cheap-LLM judge (text)
    Layer 3   — set-of-marks + V9 vision → click by (x, y)

plus goal decomposition, scan-act-verify sequencing, and six-trap recovery.

Mirrors `browser/` in shape: one catalogue entry (agent_config.yaml) and one
dispatch branch (skills.py) wire it into the orchestrator. Every model call
goes through the V9 gateway (reusing browser.client.V9Client); no new gateway,
no paid APIs.

The whole package is cross-platform: every OS-specific decision
(activation primitive, modifier key, launch identity, daemon lifecycle,
empty-tree causes) is isolated in `platform.py`.
"""
from __future__ import annotations

import sys as _sys


def enable_utf8_console() -> None:
    """Force UTF-8 on stdout/stderr. Windows consoles default to cp1252 and
    crash printing app text with non-cp1252 chars (e.g. VS Code's ￼ \\ufffc).
    Called by every CLI entry point (tasks/run/smoke)."""
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
