"""Layer 4 — error recovery. One symptom (element_count == 0 / cache miss),
six causes, six guards (CUA_DRIVER_GUIDE §6, brief §6), tuned per OS.

The discriminator that makes this tractable: permissions/UAC failures are
*global* — probing a native app (Calculator) returns 0 too — whereas
Electron/canvas failures are *local* to one app. `handle_empty_tree` runs
the cheap fixes first (activation re-scan), then classifies what is left.

Nothing here auto-clicks anything destructive; the worst it does is launch
Calculator once as a permissions probe and bring a window to the front.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import driver, electron
from . import platform as plat
from .driver import PermissionsError, PreconditionError, WindowState

# Native app used as the global-permissions probe, per OS.
_PROBE_APP = "com.apple.calculator" if plat.OS_NAME == "macos" else "Calculator"


@dataclass
class EmptyTreeResolution:
    """What the cascade should do after an empty scan.

    route:
      "ax"       — resolved (e.g. activation worked); `state` is the fresh scan
      "electron" — relaunch with electron_debugging_port and drive via `page`
      "vision"   — no accessibility structure (canvas/game) → escalate to L3
    """
    resolved: bool
    route: str
    state: WindowState | None
    note: str


def is_stale_index_error(outcome: str) -> bool:
    """The cache-miss signature from dispatching a click against an index that
    a state-changing action invalidated (Invariant B). The fix is a re-scan,
    not a retry of the same index."""
    o = outcome.lower()
    return "not found in cache" in o or "call get_window_state first" in o


def probe_global_blackout() -> bool:
    """Discriminator: launch + scan a known-native app. If IT also returns an
    empty tree, the failure is global (TCC on macOS / UAC mismatch on Windows),
    not specific to the target app. Best-effort — any error returns False so a
    flaky probe never masks the real cause."""
    try:
        launched = driver.launch_app(_PROBE_APP)
        lp = launched.get("pid")
        lp = int(lp) if lp is not None else None
        found = driver.find_app_window(_PROBE_APP, launch_pid=lp, retries=6)
        if found is None and plat.OS_NAME == "macos" and lp is not None:
            plat.activate(pid=lp, app=_PROBE_APP)
            found = driver.find_app_window(_PROBE_APP, launch_pid=lp, retries=6)
        if found is None:
            return True  # native probe app launched but no window realised → global
        owner_pid, wid = found
        st = driver.get_window_state(owner_pid, wid, capture_mode="ax")
        return st.empty
    except Exception:  # noqa: BLE001 - a diagnostic must never raise
        return False


def handle_empty_tree(pid: int, window_id: int, state: WindowState, *,
                      app: str | None = None, activated: bool = False) -> EmptyTreeResolution:
    """The six-trap dispatcher. Tries the cheap fix (activation), then routes
    what remains. Raises PermissionsError for the one unrecoverable bucket."""
    # Guard 1 (cheap): window backgrounded → activate and re-scan once.
    if not activated:
        try:
            plat.activate(pid=pid, app=app or "", window_id=window_id)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        rescan = driver.get_window_state(pid, window_id, capture_mode="ax")
        if not rescan.empty:
            return EmptyTreeResolution(True, "ax", rescan, "resolved via window activation")
        state = rescan

    # Guard 2: known Electron app → opaque WebArea, drive the DOM via `page`.
    if app and electron.is_electron(app):
        return EmptyTreeResolution(False, "electron", None,
                                   "known Electron app — relaunch with debugging port and use page")

    # Guard 3: global blackout → permissions. Probe a native app to confirm.
    if probe_global_blackout():
        raise PermissionsError(
            "Empty AX/UIA tree across the whole desktop — a global permissions "
            "issue. Suspects:\n  - " + "\n  - ".join(plat.empty_tree_suspects())
        )

    # Guard 4: app-specific, not Electron, not permissions → canvas/game.
    return EmptyTreeResolution(False, "vision", None,
                               "no accessibility structure (canvas/game) — escalate to vision")


def precondition_empty_tree() -> PreconditionError:
    """The single highest-value guard (brief §6): a fully-spelled-out empty-tree
    error listing every OS-relevant suspect, raised when the cascade declines
    to auto-recover."""
    return PreconditionError(
        "cua-driver returned an empty tree (element_count == 0). Check:\n  - "
        + "\n  - ".join(plat.empty_tree_suspects())
    )


def on_verify_fail(pid: int, window_id: int) -> WindowState:
    """Post-condition not met. The UI may have reflowed; re-scan so the next
    turn re-resolves indices against a fresh map (Invariant B). Carrying state
    across the failure is intentionally minimal — a fresh scan is the cheapest
    reliable recovery."""
    return driver.get_window_state(pid, window_id, capture_mode="ax")
