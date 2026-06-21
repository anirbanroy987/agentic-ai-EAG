"""The cross-platform shim — the ONLY module that branches on OS.

`cua-driver` ships the same 34-tool JSON surface and the same cache
invariants on macOS, Windows, and Linux. What differs is six things, and
they all live here so the rest of the package can stay OS-agnostic:

  1. binary path          — where cua-driver.exe / cua-driver lives
  2. daemon lifecycle     — `serve &` (mac/linux) vs `autostart` (windows)
  3. activation primitive — AppleScript `activate` vs Windows `bring_to_front`
  4. modifier key         — `cmd` vs `ctrl`
  5. launch identity      — `bundle_id` vs `name`
  6. empty-tree causes    — TCC (mac) vs UAC (win) vs Qt-env (linux)

The brief and CUA_DRIVER_GUIDE are macOS-centric; several of their
"non-negotiable rules" invert on Windows (most notably: `bring_to_front`
is *the* activation primitive on Windows but a no-op error on macOS). This
module encodes the correct per-OS behaviour for each.
"""
from __future__ import annotations

import os
import platform as _py_platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _detect_os() -> str:
    s = _py_platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


OS_NAME = _detect_os()


# ── binary resolution ─────────────────────────────────────────────────────────
def _windows_candidates() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    home = Path.home()
    cands = []
    if local:
        cands.append(Path(local) / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe")
    cands += [
        home / ".cua-driver" / "packages" / "current" / "cua-driver.exe",
        home / ".local" / "bin" / "cua-driver.exe",
    ]
    return cands


def _unix_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / ".local" / "bin" / "cua-driver",
        Path("/usr/local/bin/cua-driver"),
        Path("/opt/homebrew/bin/cua-driver"),
    ]


def resolve_binary() -> str | None:
    """Locate the cua-driver executable.

    Order: PATH first (so a user's explicit install wins), then the known
    per-OS install locations. Returns the absolute path, or None if the
    binary cannot be found — callers surface an install hint in that case
    rather than blowing up with a bare FileNotFoundError.
    """
    on_path = shutil.which("cua-driver")
    if on_path:
        return on_path
    cands = _windows_candidates() if OS_NAME == "windows" else _unix_candidates()
    for c in cands:
        if c.exists():
            return str(c)
    return None


def install_hint() -> str:
    """The exact command to install cua-driver for THIS OS, for error text."""
    if OS_NAME == "windows":
        return (
            "irm https://raw.githubusercontent.com/trycua/cua/main/"
            "libs/cua-driver/scripts/install.ps1 | iex"
        )
    return (
        '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/'
        'cua/main/libs/cua-driver/scripts/install.sh)"'
    )


# ── daemon lifecycle ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DaemonPlan:
    """How to bring the daemon up on this OS.

    `commands` are run in order. `background` flags the long-running `serve`
    process that must be Popen'd and left detached (macOS/Linux); the
    Windows `autostart` commands are short-lived and return on their own.
    """
    commands: list[tuple[list[str], bool]] = field(default_factory=list)  # (argv-tail, background)


def daemon_plan() -> DaemonPlan:
    if OS_NAME == "windows":
        # autostart registers an interactive-session Scheduled Task (Session 1+)
        # and `kick` starts it immediately. Both are short-lived.
        return DaemonPlan(commands=[
            (["autostart", "enable"], False),
            (["autostart", "kick"], False),
        ])
    # macOS / Linux: a single long-running serve process.
    return DaemonPlan(commands=[(["serve"], True)])


# ── action conventions ────────────────────────────────────────────────────────
MODIFIER = "cmd" if OS_NAME == "macos" else "ctrl"
LAUNCH_KEY = "bundle_id" if OS_NAME == "macos" else "name"


def hotkey(*keys: str) -> list[str]:
    """Normalise a chord, mapping the logical 'mod' token to this OS's
    primary modifier. `hotkey("mod", "z")` → ["cmd","z"] on macOS,
    ["ctrl","z"] elsewhere. Used for select-all, undo, copy, etc."""
    return [MODIFIER if k == "mod" else k for k in keys]


def launch_args(app: str, *, electron_port: int | None = None,
                additional_arguments: list[str] | None = None) -> dict:
    """Build the `launch_app` argument dict with the right identity key for
    this OS. On macOS `app` is treated as a bundle_id; elsewhere as a name.
    `additional_arguments` are forwarded to the process (honored on Windows) —
    e.g. a file path so `code <path>` opens a named editor (no Save dialog)."""
    args: dict = {LAUNCH_KEY: app}
    if electron_port is not None:
        args["electron_debugging_port"] = electron_port
    if additional_arguments:
        args["additional_arguments"] = additional_arguments
    return args


# ── activation (empty-tree trap #2) ───────────────────────────────────────────
def activate(*, pid: int, app: str, window_id: int | None = None) -> dict:
    """Bring a window to the foreground so its AX/UIA subtree realises. The
    single biggest OS divergence:

      - Windows: `bring_to_front` IS the primitive (SetForegroundWindow), and
        it requires a `window_id` — pid alone errors "no windows found".
        The brief's "do NOT use bring_to_front" rule is macOS-only.
      - macOS:   `bring_to_front` errors; use AppleScript `activate` (by app).
      - Linux:   best-effort `bring_to_front` (no-op on many WMs).

    driver is imported lazily to avoid a circular import (driver imports this
    module at top). Returns the tool's raw response dict.
    """
    if OS_NAME == "macos":
        import subprocess
        subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to activate'],
            capture_output=True, text=True,
        )
        return {"activated": True, "via": "osascript"}
    from . import driver
    args: dict = {"pid": pid}
    if window_id is not None:
        args["window_id"] = window_id
    return driver.call("bring_to_front", args)


def empty_tree_suspects() -> list[str]:
    """Ordered, OS-tuned list of why a scan returned element_count == 0.
    Surfaced verbatim in the empty-tree PreconditionError."""
    if OS_NAME == "windows":
        return [
            "target launched elevated (UAC) but the agent is not — relaunch one side to match",
            "window not in the foreground — call bring_to_front then re-scan",
            "Electron app (one opaque AXWebArea) — relaunch with electron_debugging_port and use the page tool",
            "game / canvas renderer (no UIA structure) — use vision (capture_mode=vision)",
        ]
    if OS_NAME == "macos":
        return [
            "TCC permissions not granted — run `cua-driver permissions grant`",
            "app launched in background (self_activation_suppressed) — osascript activate then re-scan",
            "Electron app (one opaque AXWebArea) — relaunch with electron_debugging_port and use the page tool",
            "game / canvas renderer (no AX) — use vision (capture_mode=vision)",
        ]
    return [
        "Qt app launched without QT_ACCESSIBILITY=1 — relaunch with that env var",
        "Wayland session without RemoteDesktop portal grant — use an X11 session",
        "Electron app (opaque WebArea) — relaunch with electron_debugging_port and use the page tool",
        "game / canvas renderer (no AT-SPI) — use vision (capture_mode=vision)",
    ]


__all__ = [
    "OS_NAME", "MODIFIER", "LAUNCH_KEY",
    "resolve_binary", "install_hint", "daemon_plan", "DaemonPlan",
    "hotkey", "launch_args", "activate", "empty_tree_suspects",
]
